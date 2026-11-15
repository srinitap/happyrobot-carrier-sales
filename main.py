import os
import secrets
import time
from collections import Counter
from datetime import datetime, timezone
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

from dotenv import load_dotenv
load_dotenv()

import requests
from fastapi import Depends, FastAPI, HTTPException, Security
from fastapi.security import APIKeyHeader, HTTPAuthorizationCredentials, HTTPBearer

import zipcodes

import negotiation
import tms_client

FMCSA_WEB_KEY = os.environ.get("FMCSA_WEB_KEY")
API_KEY = os.environ.get("API_KEY", "")

api_key_header = APIKeyHeader(name="X-API-Key", auto_error=False)
bearer_scheme = HTTPBearer(auto_error=False)


def require_api_key(key: str = Security(api_key_header),
                    bearer: HTTPAuthorizationCredentials = Security(bearer_scheme)):
    """Refuse any request without the right key. If the server has no API_KEY
    set, refuse everything (fail safe) instead of running unprotected."""
    if not API_KEY:
        raise HTTPException(status_code=503, detail="Server is missing API_KEY")
    key = (bearer.credentials if bearer else None) or key
    if not key or not secrets.compare_digest(key.encode(), API_KEY.encode()):
        raise HTTPException(status_code=401, detail="Invalid or missing API key")


app = FastAPI(dependencies=[Depends(require_api_key)])

# a code expires after 5 minutes
OTP_TTL_SECONDS = 300  
# wrong guesses allowed before the code is dismissed   
OTP_MAX_ATTEMPTS = 3  
# forget old calls after 2 hours, to free memory     
CLEANUP_SECONDS = 7200    

NEXT_STEP = {
    "start": "verify_carrier",
    "verified": "send_otp",
    "code_sent": "verify_otp",
    "otp_passed": "search_loads",
    "loads_found": "get_offer",
    "negotiating": "respond_to_offer",
    "deal_agreed": "transfer_to_rep",
    "transferred": "end_call",
}


calls = {}


def new_call_record():
    return {
        "started": time.time(),
        "stage": "start",
        "mc": None,            
        "carrier_name": None,
        "otp": None,          
        "load_prices": {},    
        "negotiations": {}, 
        "deal": None,        
        "transferred_at": None,
    }


def get_call(call_id):
    """This call's record, created on first use. Also drops calls older
    than CLEANUP_SECONDS so memory doesn't grow forever."""
    now = time.time()
    for old_id in [c for c, d in calls.items() if now - d["started"] > CLEANUP_SECONDS]:
        calls.pop(old_id, None)
    call_id = (call_id or "").strip()
    if not call_id:
        # no call ID - a throwaway record
        return new_call_record()
    if call_id not in calls:
        calls[call_id] = new_call_record()
    return calls[call_id]


STAGES = list(NEXT_STEP)   


def at_least(call, stage):
    return STAGES.index(call["stage"]) >= STAGES.index(stage)


def reply(call, result, next_step=None, **extra):
    out = {"result": result, **extra}
    out["next_step"] = next_step or NEXT_STEP[call["stage"]]
    return out


def clean_mc(mc_number: str) -> str:
    return "".join(ch for ch in (mc_number or "") if ch.isdigit())


def not_verified(call, mc):
    if call["mc"] == mc and at_least(call, "otp_passed"):
        return None
    step = NEXT_STEP[call["stage"]] if call["mc"] == mc else "verify_carrier"
    return reply(call, "not_verified", next_step=step)


@app.get("/verify-carrier")
def verify_carrier(call_id: str, mc_number: str):
    call = get_call(call_id)
    mc = clean_mc(mc_number)

    if at_least(call, "deal_agreed"):
        return reply(call, "out_of_order")         
    if call["mc"] == mc and at_least(call, "otp_passed"):
        return reply(call, "already_verified")

    # 8 or more is invalid
    if not mc or len(mc) >= 8:
        return reply(call, "invalid_mc", next_step="verify_carrier")

    url = f"https://mobile.fmcsa.dot.gov/qc/services/carriers/docket-number/{mc}"
    try:
        resp = requests.get(url, params={"webKey": FMCSA_WEB_KEY}, timeout=8)
        resp.raise_for_status()
        data = resp.json()
    except Exception as e:
        # FMCSA is down or slow: fail safe, never let them through
        print("FMCSA call failed:", type(e).__name__)
        return reply(call, "unavailable", next_step="end_call")

    # no content means no carrier with this MC number
    content = data.get("content") or []
    if isinstance(content, dict):
        content = [content]
    if not content:
        return reply(call, "invalid_mc", next_step="verify_carrier")

    carrier = content[0].get("carrier", {})
    if carrier.get("allowedToOperate") == "Y":
        # start this call fresh for this carrier
        call.update(mc=mc, carrier_name=carrier.get("legalName"), stage="verified",
                    otp=None, load_prices={}, negotiations={})
        return reply(call, "verified", carrier_name=carrier.get("legalName"))

    return reply(call, "not_verified", next_step="end_call")


@app.get("/send-otp")
def send_otp(call_id: str, mc_number: str):
    call = get_call(call_id)
    mc = clean_mc(mc_number)

    if call["mc"] == mc and at_least(call, "otp_passed"):
        return reply(call, "already_verified")
    if call["mc"] != mc or call["stage"] not in ("verified", "code_sent"):
        return reply(call, "out_of_order", next_step="verify_carrier")

    code = f"{secrets.randbelow(1_000_000):06d}"
    call["otp"] = {"code": code, "expires": time.time() + OTP_TTL_SECONDS,
                   "attempts": 0}
    call["stage"] = "code_sent"

    # MOCK: print the code instead of texting it. 
    print(f"\n>>> OTP for MC {mc}: {code}\n")

    return reply(call, "sent")


@app.get("/verify-otp")
def verify_otp(call_id: str, mc_number: str, code: str):
    call = get_call(call_id)
    mc = clean_mc(mc_number)

    if call["mc"] == mc and at_least(call, "otp_passed"):
        return reply(call, "already_verified")
    if call["mc"] != mc or call["stage"] != "code_sent":
        step = NEXT_STEP[call["stage"]] if call["mc"] == mc else "verify_carrier"
        return reply(call, "out_of_order", next_step=step)

    entry = call["otp"]
    if not entry or time.time() > entry["expires"]:
        call.update(otp=None, stage="verified")      
        return reply(call, "expired")

    entry["attempts"] += 1
    spoken = "".join(ch for ch in (code or "") if ch.isdigit())

    if secrets.compare_digest(spoken, entry["code"]):
        call.update(otp=None, stage="otp_passed")   
        return reply(call, "verified")

    if entry["attempts"] >= OTP_MAX_ATTEMPTS:
        call.update(otp=None, stage="verified")     
        return reply(call, "locked", next_step="end_call")

    return reply(call, "wrong_code", attempts_left=OTP_MAX_ATTEMPTS - entry["attempts"])

# Search Loads

# Fields the AI is allowed to see. max_rate is not here.
PUBLIC_FIELDS = [
    "load_id", "origin", "destination", "pickup_datetime", "delivery_datetime",
    "equipment_type", "loadboard_rate", "weight", "commodity_type",
    "num_of_pieces", "miles", "dimensions", "notes",
]

VALID_EQUIPMENT = {"DRY_VAN", "REEFER", "FLATBED", "STEP_DECK", "POWER_ONLY"}

US_STATES = {
    "AL", "AK", "AZ", "AR", "CA", "CO", "CT", "DE", "FL", "GA", "HI", "ID",
    "IL", "IN", "IA", "KS", "KY", "LA", "ME", "MD", "MA", "MI", "MN", "MS",
    "MO", "MT", "NE", "NV", "NH", "NJ", "NM", "NY", "NC", "ND", "OH", "OK",
    "OR", "PA", "RI", "SC", "SD", "TN", "TX", "UT", "VT", "VA", "WA", "WV",
    "WI", "WY", "DC",
}


def split_place(text):
    """Turn what the AI sends into (city, state) for the TMS.
    'Chicago, IL' -> ('Chicago', 'IL');  'Chicago' -> ('Chicago', '')
    'CO' -> ('', 'CO')   a bare state code is a state, not a city.
    The TMS matches cities loosely but states exactly."""
    parts = [p.strip() for p in (text or "").split(",")]
    first = parts[0] if parts else ""
    if len(parts) == 1 and first.upper() in US_STATES:
        return "", first.upper()
    state = parts[1].upper() if len(parts) > 1 else ""
    if state not in US_STATES:
        state = ""
    return first, state


def parse_date(text):
    """'2027-07-07' -> '20270707' (the TMS's PICKUP_DATE format).
    Blank -> '' (no date filter). Not a real date -> None."""
    text = (text or "").strip()
    if not text:
        return ""
    for fmt in ("%Y-%m-%d", "%Y%m%d"):
        try:
            return datetime.strptime(text, fmt).strftime("%Y%m%d")
        except ValueError:
            pass
    return None


ZIP_TZ, CITY_TZ, STATE_TZ = {}, {}, {}


def build_tz_tables():
    per_state = {}
    for z in zipcodes.list_all():
        tz, state = z.get("timezone"), (z.get("state") or "").upper()
        if not tz:
            continue
        ZIP_TZ[z.get("zip_code")] = tz
        CITY_TZ.setdefault(((z.get("city") or "").upper(), state), tz)
        per_state.setdefault(state, Counter())[tz] += 1
    for state, counts in per_state.items():
        STATE_TZ[state] = counts.most_common(1)[0][0]


build_tz_tables()


def place_tz(zip_code, place):
    city, state = split_place(place)
    zip5 = (zip_code or "").strip()[:5]
    name = ZIP_TZ.get(zip5) or CITY_TZ.get((city.upper(), state)) or STATE_TZ.get(state)
    try:
        return ZoneInfo(name) if name else None
    except (ZoneInfoNotFoundError, ValueError):
        return None


def local_time(utc_text, zip_code, place):
    tz = place_tz(zip_code, place)
    try:
        moment = datetime.strptime(utc_text or "", "%Y-%m-%d %H:%M UTC")
    except ValueError:
        return utc_text
    if tz is None:
        return utc_text
    local = moment.replace(tzinfo=timezone.utc).astimezone(tz)
    hour = local.strftime("%I").lstrip("0")
    return f"{local:%Y-%m-%d} {hour}:{local:%M %p} {local.tzname()}"


def for_the_ai(load):
    public = {k: load.get(k) for k in PUBLIC_FIELDS}
    public["pickup_datetime"] = local_time(load.get("pickup_datetime"),
                                           load.get("origin_zip"), load.get("origin"))
    public["delivery_datetime"] = local_time(load.get("delivery_datetime"),
                                             load.get("destination_zip"), load.get("destination"))
    return public


@app.get("/search-loads")
def search_loads(call_id: str, mc_number: str, origin: str, equipment_type: str = "",
                 destination: str = "", pickup_date: str = ""):
    call = get_call(call_id)
    refused = not_verified(call, clean_mc(mc_number))
    if refused:
        return refused
    if at_least(call, "deal_agreed"):
        return reply(call, "out_of_order")

    # optional
    equipment = (equipment_type or "").strip().upper().replace(" ", "_")
    if equipment and equipment not in VALID_EQUIPMENT:
        return reply(call, "invalid_equipment", next_step="search_loads")

    # optional
    day = parse_date(pickup_date)
    if day is None:
        return reply(call, "invalid_date", next_step="search_loads")

    orig_city, orig_state = split_place(origin)
    dest_city, dest_state = split_place(destination)
    if not (orig_city or orig_state):
        return reply(call, "need_origin", next_step="search_loads")
    filters = {"ORIG_CITY": orig_city, "ORIG_STATE": orig_state,
               "DEST_CITY": dest_city, "DEST_STATE": dest_state,
               "EQTYPE": equipment, "PICKUP_DATE": day}
    try:
        results = tms_client.query_loads(filters, max_results=5)
    except (tms_client.TMSError, tms_client.TMSFault, ValueError) as e:
        print("TMS search failed:", getattr(e, "code", type(e).__name__))
        return reply(call, "unavailable", next_step="end_call")

    open_loads = [l for l in results if l["status"] in ("", "OPEN")]
    if not open_loads:
        return reply(call, "no_loads", next_step="search_loads")
    open_loads.sort(key=lambda l: l["pickup_datetime"])   # soonest first

    loads = []
    for load in open_loads[:3]:
        try:
            full = tms_client.get_load(load["load_id"])
        except (tms_client.TMSError, tms_client.TMSFault):
            full = load
        if full.get("loadboard_rate") is not None:
            call["load_prices"][full["load_id"]] = {
                "board_rate": full["loadboard_rate"],
                "max_rate": full.get("max_rate")}
        loads.append(for_the_ai(full))

    if call["stage"] == "otp_passed":
        call["stage"] = "loads_found"
    return reply(call, "found", count=len(loads), loads=loads)

# Negotiation

def parse_price(text):
    digits = "".join(ch for ch in (text or "") if ch.isdigit() or ch == ".")
    try:
        return int(float(digits)) if digits else None
    except ValueError:
        return None


def negotiation_reply(call, state, result, rate):
    extra = {}
    if rate is not None:
        extra["rate"] = rate
    if result in ("counter", "final_offer", "hold"):
        extra["rounds_left"] = max(negotiation.MAX_ROUNDS - state["round"], 0)
    return reply(call, result, **extra)


@app.get("/get-offer")
def get_offer(call_id: str, mc_number: str, load_id: str):
    """Our current offer for a load from this call's search. Never returns max_rate."""
    call = get_call(call_id)
    refused = not_verified(call, clean_mc(mc_number))
    if refused:
        return refused
    if not at_least(call, "loads_found"):
        return reply(call, "out_of_order")          
    if at_least(call, "deal_agreed"):
        return reply(call, "out_of_order")         

    load_id = (load_id or "").strip().upper()
    prices = call["load_prices"].get(load_id)
    if prices is None:
        return reply(call, "unknown_load", next_step="search_loads")

    state = call["negotiations"].get(load_id)
    if state is None:
        max_rate = prices["max_rate"]
        if max_rate is None:
            max_rate = prices["board_rate"]   
        state = negotiation.new_state(prices["board_rate"], max_rate)
        call["negotiations"][load_id] = state
    call["stage"] = "negotiating"

    result, rate = negotiation.decide(state, None)
    return negotiation_reply(call, state, result, rate)


@app.get("/respond-to-offer")
def respond_to_offer(call_id: str, mc_number: str, load_id: str,
                     carrier_accept: bool, carrier_ask: str = ""):
    """carrier_accept (required): true if they said yes to our offer, false if no.
    carrier_ask (optional): a price they named, digits only, e.g. "3000".
    Returns accept / counter / final_offer / hold / end / confirm_rate.
    "accept" only RECORDS the deal; it's booked after the call (see /end-call)."""
    call = get_call(call_id)
    mc = clean_mc(mc_number)
    refused = not_verified(call, mc)
    if refused:
        return refused
    if at_least(call, "deal_agreed"):
        return reply(call, "out_of_order")

    load_id = (load_id or "").strip().upper()
    state = call["negotiations"].get(load_id)
    if state is None:
        return reply(call, "out_of_order", next_step="get_offer")

    if carrier_accept:
        response = negotiation.ACCEPTED        
    else:
        price = parse_price(carrier_ask)
        response = price if price is not None else negotiation.REJECTED

    result, rate = negotiation.decide(state, response)

    if result == "accept":
        prices = call["load_prices"].get(load_id, {})
        call["deal"] = {"load_id": load_id, "rate": rate,
                        "board_rate": prices.get("board_rate"),
                        "max_rate": prices.get("max_rate")}
        call["stage"] = "deal_agreed"
        print(f">>> DEAL AGREED on call {call_id}: {load_id} for MC {mc} at ${rate}"
              " (books when the call ends)")
    elif result == "end":
        call["stage"] = "loads_found"
        return negotiation_reply(call, state, result, rate) | {"next_step": "end_call"}
    return negotiation_reply(call, state, result, rate)


# Transfer

@app.get("/transfer")
def transfer(call_id: str):
    """HappyRobot calls this from transfer_to_rep, right before the Transfer step.
    Only a call at "deal_agreed" may transfer, and only once.
    result "allowed" lets the transfer happen; anything else blocks it."""
    call = calls.get((call_id or "").strip())
    if call is None:
        return {"result": "no_deal", "next_step": "verify_carrier"}
    if call["stage"] == "transferred":
        return reply(call, "already_transferred")
    if call["stage"] != "deal_agreed":
        return reply(call, "no_deal")

    call["stage"] = "transferred"
    call["transferred_at"] = time.time()
    deal = call["deal"]
    print(f">>> TRANSFER to senior rep on call {call_id}: MC {call['mc']} "
          f"({call['carrier_name']}), {deal['load_id']} at ${deal['rate']}")
    return reply(call, "allowed", **rep_handoff(call))


def money(value):
    return f"${value:,}" if isinstance(value, int) else "unknown"


def rep_handoff(call):
    deal = call["deal"]
    summary = (f"Agreed deal: {call['carrier_name'] or 'Unknown carrier'} "
               f"(MC {call['mc']}), load {deal['load_id']} at {money(deal['rate'])}. "
               f"Board rate {money(deal['board_rate'])}, max rate {money(deal['max_rate'])}. "
               "The carrier is verified (FMCSA and code). It books in the TMS when the "
               "call ends; please confirm the booking.")
    return {"rep_summary": summary, "carrier_name": call["carrier_name"],
            "mc_number": call["mc"], "load_id": deal["load_id"],
            "agreed_rate": deal["rate"], "board_rate": deal["board_rate"],
            "max_rate": deal["max_rate"]}



finished_calls = {}


NEEDS_REVIEW = {"booking_unconfirmed", "booking_failed", "load_taken"}


def book(mc, load_id, rate):
    """Book one load in the TMS at the agreed rate. Returns one of:
    booked_pending      - success: the TMS accepted it (PENDING until a rep confirms)
    booking_unconfirmed - a reply broke and we can't tell if it went through
    load_taken          - the TMS says it was already booked
    booking_failed      - the TMS refused it or couldn't be reached"""
    try:
        outcome = tms_client.book_load(load_id, mc, rate)
    except tms_client.TMSError as e:
        print("TMS booking refused:", e.code)
        tms_says = f"{e.code}: {e.msg}" if e.msg else e.code
        if e.code == "ALREADY_BOOKED":
            return {"result": "load_taken",
                    "reason": f"Load was already booked before our first try (TMS: {tms_says})"}
        if e.code == "INVALID_RATE":
            return {"result": "booking_failed",
                    "reason": f"TMS rejected the agreed rate ${rate} (TMS: {tms_says})"}
        return {"result": "booking_failed", "reason": f"TMS refused the booking (TMS: {tms_says})"}
    except (tms_client.TMSFault, ValueError) as e:
        print("TMS booking failed:", type(e).__name__)
        return {"result": "booking_failed",
                "reason": f"Booking did not go through ({e})"}

    if outcome["status"] == "accepted":
        print(f">>> BOOKED (pending rep confirmation) {load_id} for MC {mc} at ${rate} "
              f"(ref {outcome['booking_ref']}, TMS status {outcome['tms_status']})")
        return {"result": "booked_pending", "booking_ref": outcome["booking_ref"],
                "booked_at": outcome["timestamp"]}
    print(f">>> BOOKING UNCONFIRMED {load_id} for MC {mc} at ${rate}: check the TMS")
    return {"result": "booking_unconfirmed",
            "reason": "TMS reply broke, so we can't tell if the booking went through. "
                      "Check the load's status in the TMS."}


@app.get("/end-call")
def end_call(call_id: str):
    """Run once when the call ends. Books the deal agreed on this call (if any),
    then forgets the call, so its verification expires too.
    Returns one flat summary of the call, matching the Twin call_logs columns.
    Safe to call twice: the second time just returns the same summary."""
    call_id = (call_id or "").strip()
    if not call_id:
        return {"result": "missing_call_id"}
    if call_id in finished_calls:
        return finished_calls[call_id]

    call = calls.pop(call_id, None)  
    stage = call["stage"] if call else "start"
    deal = call["deal"] if call else None

    review_reason = ""               
    if deal:
        booking = book(call["mc"], deal["load_id"], deal["rate"])
        outcome = booking["result"] 
        if outcome in NEEDS_REVIEW:
            review_reason = booking.get("reason", "")
    elif not at_least({"stage": stage}, "otp_passed"):
        outcome = "not_verified"     
    else:
        outcome = "no_deal"       
        
    summary = {
        "result": "done",
        "call_id": call_id,
        "mc_number": (call["mc"] if call else None) or "unknown",
        "carrier_name": (call["carrier_name"] if call else None) or "unknown",
        "load_id": deal["load_id"] if deal else None,
        "agreed_rate": deal["rate"] if deal else None,
        "board_rate": deal["board_rate"] if deal else None,
        "max_rate": deal["max_rate"] if deal else None,
        "outcome": outcome,
        "needs_review": outcome in NEEDS_REVIEW,
        "review_reason": review_reason,
        "transferred": bool(call and call["transferred_at"]),
        "last_step": stage,
    }
    finished_calls[call_id] = summary
    return summary