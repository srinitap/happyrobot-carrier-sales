# QA Results: Inbound Carrier Sales Agent

**Last run:** November 14, 2026  
**Result:** 99 automated tests, 99 passed, 0 failed (about 0.1 seconds)  
**Covers:** `main.py`, `tms_client.py`, `negotiation.py` (API key, call flow, TMS client, negotiation)

---

## How to run the tests

From the project folder, with the virtual environment turned on:

```bash
python -m unittest discover -s tests -v
```

The tests use Python's built-in `unittest`.

The tests never touch the real TMS, the real FMCSA API, or the secrets in `.env`:
- **TMS:** `tests/fake_tms.py` is a small fake TMS server that speaks the same line protocol on this computer. It can break replies on purpose, the way the real TMS does.
- **FMCSA:** replaced with a stand-in that answers "allowed to operate" (or "not allowed", "not found" or "down" when a test asks for it).


---

## Summary by area

| Area | File | Tests | Result |
|---|---|---|---|
| API key | `test_auth.py` | 7 | all pass |
| Carrier verification (FMCSA) | `test_api.py` | 6 | all pass |
| One-time code | `test_api.py` | 6 | all pass |
| Call order (backend controls the flow) | `test_api.py` | 6 | all pass |
| Load search | `test_api.py` | 16 | all pass |
| Negotiation through the API | `test_api.py` | 7 | all pass |
| Transfer to a rep | `test_api.py` | 5 | all pass |
| End of call and booking | `test_api.py` | 10 | all pass |
| Local pickup/delivery times | `test_api.py` | 8 | all pass |
| Negotiation rules | `test_negotiation.py` | 11 | all pass |
| TMS reply parsing | `test_tms_client.py` | 9 | all pass |
| Talking to the TMS (retries, booking) | `test_tms_client.py` | 8 | all pass |
| Total | | 99 | 99 / 99 |

---

## What is checked

### API key
- Every endpoint requires the API key; the check is attached to the whole app, so no endpoint can skip it.
- HappyRobot sends the key as a Bearer token (`Authorization: Bearer <key>`); an `X-API-Key` header is also accepted.
- The right key is let in; a wrong or missing key gets `401`.
- If the server has no `API_KEY` set, it refuses everything (`503`) instead of running unprotected.

### Carrier verification
- A valid MC number is verified, and the carrier's name is returned.
- MC numbers with symbols (`MC-12 34`) are cleaned to digits.
- 8 or more digits is refused without calling FMCSA.
- An MC number FMCSA doesn't know is refused.
- A carrier that isn't allowed to operate is refused, and the call ends.
- If FMCSA is down, the carrier is **not** let through (fails safe).

### One-time code
- The code is printed for the mock "text message" and is **never** sent back to the AI.
- A code can't be sent before the MC number is verified.
- The right code passes, including when spoken with spaces or dashes.
- Three wrong codes lock the call and end it.
- An expired code (after 5 minutes) has to be sent again.

### Call order
- Searching before verification is refused, and the TMS is never contacted.
- Searching after FMCSA but before the code is refused.
- Verification on one call doesn't carry over to another call.
- Verification ends when the call ends.
- Asking for an offer before searching is refused.
- Every reply tells the AI the next step, in the correct order.

### Load search
- Found loads never include the maximum price.
- A state alone searches by state, and a city with a state searches by both.
- Equipment is optional; caller words like "step deck" are translated to TMS codes.
- An unknown equipment type is refused before contacting the TMS.
- No location asks the caller for one; no matches gives `no_loads`.
- Only `OPEN` loads are pitched: at most three, soonest pickup first.
- Pickup and delivery times are shown in each stop's local time, found from its ZIP code.
- Pickup date is optional; `2027-07-07` is sent to the TMS as `PICKUP_DATE:20270707` and only that day's loads come back.
- A pickup date that isn't a real `YYYY-MM-DD` date ("July 7th", `2027-02-30`) is refused before contacting the TMS.
- If the TMS is down, the call ends safely.

### Negotiation through the API
- The opening offer is the posted rate.
- Only loads pitched on this call can be negotiated.
- Counter-offers never reach or reveal the maximum price.
- Asking too much after the final offer ends the call.
- "Yes" records the deal but does **not** book it yet; booking happens after the call.
- Prices spoken like `$2,100` are understood.
- Nothing can be changed after a deal is agreed.

### Negotiation rules
- Counter-offers climb a third of the way toward the max, then two-thirds, then a final offer $20 under the max.
- The agent never offers or accepts more than the max.
- An ask at or below the current offer is accepted at the carrier's number.
- A misheard low number (like "21") is confirmed, not accepted.
- When the posted rate is at or above the max, only the max is offered, at most 3 times.

### Transfer to a rep
- No transfer without a deal; unknown calls can't transfer.
- A transfer happens only once per call.
- An allowed transfer hands the rep the deal: carrier, MC, load, agreed rate, board rate and max rate, plus a ready-to-read summary. (These fields are hidden from the AI in HappyRobot.)
- A refused transfer reveals no prices.

### End of call and booking
- The summary has exactly the columns the Twin `call_logs` table expects.
- A deal is booked at the agreed rate when the call ends, and comes back as `booked_pending`.
- The call log records the deal's board rate and max rate (empty when there's no deal); accepting a deal still never shows the max to the AI.
- Ending a call twice never books twice.
- These cases are marked `needs_review` with a reason:
  - load already taken (`ALREADY_BOOKED`)
  - rate rejected (`INVALID_RATE`)
  - booking reply broken (`booking_unconfirmed`)
- A verified call with no deal is `no_deal`; an unverified call is `not_verified`. Neither books anything.
- The required Twin columns (`call_id`, `mc_number`, `carrier_name`, `outcome`, `last_step`, `needs_review`, `transferred`) are never empty. A caller who hangs up before giving an MC number is logged as `unknown`.

### Local pickup and delivery times
- TMS times are treated as UTC. Pickup is shown in the pickup ZIP's time zone, delivery in the delivery ZIP's (e.g. `2026-11-13 2:00 AM CST` for San Antonio `78205`).
- The ZIP-to-time-zone data comes from the `zipcodes` package (every US ZIP, offline).
- Daylight saving is handled (CDT in July, CST in November); Arizona stays on MST.
- ZIPs in split states get their own zone (El Paso is Mountain, Pensacola is Central).
- A missing or broken ZIP falls back to the city and state, then to the state's main zone.
- Loads are still ordered by the real (UTC) pickup time, so "soonest first" is right across zones.
- If nothing can be found, the UTC time is kept, labelled `UTC`.

### TMS client
- Parses records and strips padding; an empty result is valid.
- An `ERR` line becomes a clear error with its code and message.
- Broken replies are detected and retried: missing `END`, garbled price, garbled field name.
- Values containing `|` are refused before sending, so they can't corrupt the request.
- Booking:
  - `PENDING` and `BOOKED` both count as accepted.
  - A broken reply followed by `ALREADY_BOOKED` is reported as unconfirmed, not as taken.
  - `INVALID_RATE` is not retried.
- TMS times (booking, pickup, delivery) are labelled UTC.

---

## Checking that the tests catch real bugs

To make sure the tests aren't just passing no matter what, three bugs were added on purpose to throwaway copies of the code:

| Deliberate bug | Tests that failed |
|---|---|
| Show the maximum price to the AI in search results | 1 (`test_finds_loads_and_never_shows_max_rate`) |
| Skip the "is this call verified?" check | 4 (all the call-order tests) |
| Remove the protection against booking twice | 1 (`test_ending_twice_does_not_book_twice`) |

All three were caught.

---

## Not covered by these tests

- **The real TMS and FMCSA.** Both are faked. Live calls are still needed to catch differences like the `PENDING` status above.
- **The TMS's accepted rate range** (`INVALID_RATE`). The rule isn't documented. A rejected rate is caught and flagged for review, but the range hasn't been measured.
- **The TMS returns at most 5 loads per search,** even when asked for 50. A busy state can have loads the search never sees; narrowing by city, date or equipment helps.