
import os
import socket
import time

TMS_HOST = os.environ.get("TMS_HOST", "")
TMS_PORT = int(os.environ.get("TMS_PORT", "0") or 0)
TMS_TOKEN = os.environ.get("TMS_TOKEN", "")

TIMEOUT_SECONDS = 6        
MAX_RESPONSE_BYTES = 65536 
READ_RETRIES = 3          
BOOK_ATTEMPTS = 3         

QUERY_REQUIRED = {"LOAD_ID", "ORIG_CITY", "ORIG_STATE", "DEST_CITY",
                  "DEST_STATE", "PICKUP_DT", "EQTYPE", "RATE", "STATUS"}
GET_REQUIRED = QUERY_REQUIRED | {"DELIVERY_DT", "WEIGHT", "COMMODITY",
                                 "MILES", "NOTES"}
BOOK_REQUIRED = {"LOAD_ID", "BOOKING_REF", "STATUS"}


BOOK_STATUSES = {"PENDING", "BOOKED"}

NUMBER_FIELDS = {"RATE", "MAX_BUY"}


class TMSError(Exception):

    def __init__(self, code, msg=""):
        super().__init__(f"{code}: {msg}")
        self.code = code  
        self.msg = msg


class TMSFault(Exception):
    """The reply was broken (timeout, cut off, garbled). Worth retrying."""



def _build_request(cmd, fields):
    parts = [f"CMD:{cmd}", f"AUTH:{TMS_TOKEN}"]
    for key, value in fields.items():
        value = str(value)
        if "|" in value or "\r" in value or "\n" in value:
            raise ValueError(f"illegal character in {key}")
        parts.append(f"{key.upper()}:{value}")
    return "|".join(parts) + "\r\n"


def _read_reply(sock):

    data = b""
    while True:
        chunk = sock.recv(4096)
        if not chunk:
            return data.decode("ascii", errors="replace")
        data += chunk
        if len(data) > MAX_RESPONSE_BYTES:
            raise TMSFault("response too large")
        text = data.decode("ascii", errors="replace")
        complete = text.split("\r\n")[:-1]   # only lines that ended in \r\n
        if complete and complete[0].startswith("ERR|"):
            return text
        if "END" in complete:
            return text


def _parse_record(line):
    record = {}
    for pair in line.split("|"):
        if ":" not in pair:
            raise TMSFault(f"malformed field {pair!r}")
        key, value = pair.split(":", 1)   # first colon only (times contain :)
        if not key or key != key.upper() or key in record:
            raise TMSFault(f"malformed key {key!r}")
        value = value.rstrip()            # remove the padding spaces
        if key in NUMBER_FIELDS and not value.isdigit():
            raise TMSFault(f"{key} is not a number: {value!r}")
        record[key] = value
    return record


def _parse_reply(text):
    if not text:
        raise TMSFault("empty response")
    lines = text.split("\r\n")

    if lines[0].startswith("ERR|"):
        if len(lines) < 2:
            raise TMSFault("unterminated error line")
        info = {}
        for pair in lines[0][len("ERR|"):].split("|"):
            if ":" in pair:
                k, v = pair.split(":", 1)
                info[k] = v
        raise TMSError(info.get("CODE", "UNKNOWN"), info.get("MSG", ""))

    if "END" not in lines[:-1]:
        raise TMSFault("partial response: no END")
    body = lines[:lines.index("END")]
    if any(line == "" for line in body):
        raise TMSFault("blank line in response")
    return [_parse_record(line) for line in body]


def send(cmd, fields=None, retries=1, required=None):
    """Send one command and return a list of record dicts.
    Raises TMSError for proper ERR replies, TMSFault if every attempt broke."""
    request = _build_request(cmd, fields or {}).encode("ascii")
    last_fault = None
    for attempt in range(retries):
        try:
            with socket.create_connection((TMS_HOST, TMS_PORT),
                                          timeout=TIMEOUT_SECONDS) as sock:
                sock.sendall(request)
                records = _parse_reply(_read_reply(sock))
            for record in records:
                missing = (required or set()) - record.keys()
                if missing:
                    raise TMSFault(f"record missing {sorted(missing)}")
            return records
        except TMSError:
            raise                       
        except (TMSFault, OSError) as e: 
            last_fault = e
            print(f"TMS {cmd} attempt {attempt + 1} failed: {type(e).__name__}")
            time.sleep(0.3 * (attempt + 1))
    raise TMSFault(f"{cmd} failed after {retries} attempts: {last_fault}")



def _to_int(value):
    """'00002150' -> 2150. Blank or junk -> None instead of crashing."""
    try:
        return int(value)
    except (TypeError, ValueError):
        return None


def _to_datetime(value):
    if not value or len(value) != 14 or not value.isdigit():
        return value or ""
    return f"{value[0:4]}-{value[4:6]}-{value[6:8]} {value[8:10]}:{value[10:12]}"


def _to_utc(value):
    text = _to_datetime(value)
    return f"{text} UTC" if text and text != value else text


def clean_load(r):
    origin = ", ".join(p for p in [r.get("ORIG_CITY"), r.get("ORIG_STATE")] if p)
    dest = ", ".join(p for p in [r.get("DEST_CITY"), r.get("DEST_STATE")] if p)
    return {
        "load_id": r.get("LOAD_ID", ""),
        "origin": origin,
        "destination": dest,
        "origin_zip": (r.get("ORIG_ZIP") or "").strip(),
        "destination_zip": (r.get("DEST_ZIP") or "").strip(),
        "pickup_datetime": _to_utc(r.get("PICKUP_DT")),
        "delivery_datetime": _to_utc(r.get("DELIVERY_DT")),
        "equipment_type": (r.get("EQTYPE") or "").replace("_", " ").lower(),
        "loadboard_rate": _to_int(r.get("RATE")),
        "weight": _to_int(r.get("WEIGHT")),
        "commodity_type": r.get("COMMODITY", ""),
        "num_of_pieces": _to_int(r.get("PIECES")),
        "miles": _to_int(r.get("MILES")),
        "dimensions": r.get("DIMS", ""),
        "notes": r.get("NOTES", ""),
        "status": (r.get("STATUS") or "").strip(),
        "max_rate": _to_int(r.get("MAX_BUY")),
    }



def debug_echo(msg="HELLO"):
    request = _build_request("DEBUG_ECHO", {"MSG": msg}).encode("ascii")
    with socket.create_connection((TMS_HOST, TMS_PORT), timeout=TIMEOUT_SECONDS) as sock:
        sock.sendall(request)
        text = _read_reply(sock)
    if text.startswith("ERR|"):
        _parse_reply(text)         
    return text.split("\r\n")[0]    


def query_loads(filters, max_results=5):
    filters = {k: v for k, v in filters.items() if v}
    if not filters:
        raise ValueError("at least one filter is required")
    filters["MAX_RESULTS"] = max_results
    records = send("LOAD_QUERY", filters, retries=READ_RETRIES,
                   required=QUERY_REQUIRED)
    return [clean_load(r) for r in records]


def get_load(load_id):
    records = send("LOAD_GET", {"LOAD_ID": load_id}, retries=READ_RETRIES,
                   required=GET_REQUIRED)
    if len(records) != 1:
        raise TMSFault(f"expected 1 record, got {len(records)}")
    return clean_load(records[0])


def book_load(load_id, mc_number, rate):
   
    fields = {"LOAD_ID": load_id, "MC_NUM": mc_number, "AGREED_RATE": int(rate)}
    had_fault = False
    for attempt in range(BOOK_ATTEMPTS):
        try:
            records = send("LOAD_BOOK", fields, retries=1, required=BOOK_REQUIRED)
            if (len(records) != 1 or records[0]["STATUS"] not in BOOK_STATUSES
                    or records[0]["LOAD_ID"] != load_id):
                print(f"TMS LOAD_BOOK attempt {attempt + 1}: odd reply {records}")
                raise TMSFault("unexpected booking reply")
        except TMSError as e:
            print(f"TMS LOAD_BOOK attempt {attempt + 1}: {e.code}")
            if e.code == "ALREADY_BOOKED" and had_fault:
                return {"status": "unconfirmed"}   # probably our own earlier try
            raise
        except TMSFault:
            had_fault = True
            continue
        r = records[0]
        return {"status": "accepted", "booking_ref": r["BOOKING_REF"],
                "timestamp": _to_utc(r.get("TIMESTAMP")),
                "tms_status": r["STATUS"]}

    try:
        load = get_load(load_id)
    except (TMSError, TMSFault):
        print("TMS LOAD_BOOK: every attempt broke and LOAD_GET failed too")
        return {"status": "unconfirmed"}
    print(f"TMS LOAD_BOOK: every attempt broke; LOAD_GET says {load['status']!r}")
    if load["status"] in ("", "OPEN"):
        raise TMSFault("booking failed: load is still open")
    return {"status": "unconfirmed"}