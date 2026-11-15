# Inbound Carrier Sales Agent: Backend

Backend for a HappyRobot voice agent that answers calls from freight carriers, verifies them, finds them a load, negotiates the rate, and books it.

The voice agent runs in HappyRobot. Every decision that matters (who is verified, which loads are offered, what price is acceptable, what gets booked) is made here, in the backend, so the AI can't skip a step or reveal something it shouldn't.

---

## How a call works

1. **Verify the carrier.** The caller gives their MC number. The backend checks it with the FMCSA API and only continues if the carrier is allowed to operate.
2. **One-time code.** A 6-digit code is "texted" to the carrier (mocked: printed in the server log). The caller reads it back. Three wrong tries end the call.
3. **Search loads.** By pickup city/state, and optionally destination, equipment type and pickup date. Up to three open loads are offered, soonest pickup first. Pickup and delivery times are given in each stop's local time.
4. **Negotiate.** Up to 3 rounds. The opening offer is the posted (board) rate; counter-offers move toward a maximum rate that is never sent to the AI.
5. **Transfer to a rep.** Once a price is agreed, the call is handed to a sales rep with a summary of the deal (mocked for web calls).
6. **Book and log.** When the call ends, the load is booked in the TMS at the agreed rate, and one row per call is written to the HappyRobot Twin table `call_logs`.

---

## Endpoints

All endpoints are `GET` and require the API key (see **Security**).

| Endpoint | What it does |
|---|---|
| `/verify-carrier` | Checks the MC number with FMCSA |
| `/send-otp` | Creates the one-time code (printed in the log) |
| `/verify-otp` | Checks the code the caller reads back |
| `/search-loads` | Finds open loads in the TMS |
| `/get-offer` | Our current offer for a load |
| `/respond-to-offer` | The carrier's answer: yes, no, or a counter price |
| `/transfer` | Allows the transfer once a deal is agreed, and returns the rep's summary |
| `/end-call` | Books the deal and returns one summary row for Twin |

Every reply includes `next_step`, so the agent always knows what to do next and can't do things out of order.

---

## Security

- **API key on every endpoint.** HappyRobot sends it as a Bearer token (`Authorization: Bearer <key>`); an `X-API-Key` header also works. The check is attached to the whole app, so no endpoint can skip it. If the server has no `API_KEY` set, it refuses everything.
- **The maximum rate never reaches the AI.** It stays in the backend and is only shared with the human rep at transfer.
- **Verification can't be skipped.** Searching, negotiating and booking all require a verified carrier on the same call. Verification ends when the call ends.
- **Fails safe.** If FMCSA is down, the carrier is not let through. If a booking reply is unclear, the call is flagged for review instead of guessing.

---

## Project layout

```
main.py            API endpoints, call flow, load search, time zones, booking, call summary
negotiation.py     Negotiation rules (rounds, counter-offers, accept/decline)
tms_client.py      Client for the legacy TMS (line protocol over TCP, with retries)
tests/             Automated tests (fake TMS and fake FMCSA; no real services used)
QA_RESULTS.md      What the tests cover and the latest results
Dockerfile         Container image for the backend
requirements.txt   Python packages
.env.example       The environment variables to set (no values)
```

---

## Environment variables

Copy `.env.example` to `.env` and fill in the values. Never commit `.env`.

| Variable | What it is |
|---|---|
| `API_KEY` | The key HappyRobot must send on every request |
| `FMCSA_WEB_KEY` | FMCSA QCMobile API key |
| `TMS_HOST` | Legacy TMS host |
| `TMS_PORT` | Legacy TMS port |
| `TMS_TOKEN` | Legacy TMS auth token |

---

## Run it locally

Requires Python 3.12.

```bash
python3 -m venv venv
source venv/bin/activate
pip install -r requirements.txt
cp .env.example .env        
uvicorn main:app --reload --port 8000
```

The API is then at `http://localhost:8000`. To reach it from HappyRobot while running locally, expose it with a tunnel.

## Run with Docker

```bash
docker build -t carrier-sales-backend .
docker run --env-file .env -p 8000:8000 carrier-sales-backend
```

## Run the tests

```bash
python -m unittest discover -s tests -v
```

The tests use a fake TMS server and a fake FMCSA, so they never touch real services or need `.env`. See `QA_RESULTS.md` for what is covered.

---

## Call log (Twin `call_logs`)

`/end-call` returns one flat row per call:

| Column | Notes |
|---|---|
| `call_id`, `mc_number`, `carrier_name` | `unknown` if the caller was never verified |
| `load_id`, `agreed_rate`, `board_rate`, `max_rate` | Empty when there was no deal |
| `outcome` | `booked_pending`, `no_deal`, `not_verified`, `load_taken`, `booking_failed` or `booking_unconfirmed` |
| `needs_review`, `review_reason` | Set when a booking needs a human to check it |
| `transferred`, `last_step` | Whether the call reached a rep, and how far it got |

---

## Known limits

- **Call state is kept in memory.** Run a single instance; a restart forgets calls in progress.
- **The TMS returns at most 5 loads per search.** Narrowing by city, date or equipment helps in busy areas.
- **The pickup date filter uses the TMS's UTC day,** so a late-evening pickup on the West Coast can fall on the next UTC day.
- **The one-time code and the rep transfer are mocked** for web calls.