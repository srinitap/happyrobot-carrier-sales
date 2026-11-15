"""
The backend endpoints (main.py), call by call, against the fake TMS and fake FMCSA.

Each test class covers one step of the call. The endpoint functions are called
directly (no web server needed), exactly as FastAPI would call them.
"""

import json
import unittest
from unittest import mock

from helpers import MC, BackendTest, FakeFMCSA, main

TWIN_COLUMNS = {"call_id", "mc_number", "carrier_name", "load_id", "agreed_rate",
                "board_rate", "max_rate", "outcome", "needs_review", "review_reason",
                "transferred", "last_step"}
# Columns marked required (not nullable) in Twin. created_at is filled by Twin itself.
REQUIRED_COLUMNS = {"call_id", "mc_number", "carrier_name", "outcome", "last_step",
                    "transferred", "needs_review"}


class TestVerifyCarrier(BackendTest):

    def test_valid_carrier_is_verified(self):
        r = main.verify_carrier("c1", MC)
        self.assertEqual(r["result"], "verified")
        self.assertEqual(r["carrier_name"], "ACME TRUCKING LLC")
        self.assertEqual(r["next_step"], "send_otp")

    def test_mc_with_symbols_is_cleaned(self):
        self.assertEqual(main.verify_carrier("c1", "MC-12 34")["result"], "verified")

    def test_mc_with_8_or_more_digits_is_invalid_without_asking_fmcsa(self):
        with mock.patch.object(main.requests, "get") as fmcsa:
            r = main.verify_carrier("c1", "12345678")
        self.assertEqual(r["result"], "invalid_mc")
        fmcsa.assert_not_called()

    def test_unknown_mc_is_invalid(self):
        with mock.patch.object(main.requests, "get", FakeFMCSA(found=False)):
            self.assertEqual(main.verify_carrier("c1", MC)["result"], "invalid_mc")

    def test_carrier_not_allowed_to_operate_is_refused_and_call_ends(self):
        with mock.patch.object(main.requests, "get", FakeFMCSA(allowed="N")):
            r = main.verify_carrier("c1", MC)
        self.assertEqual((r["result"], r["next_step"]), ("not_verified", "end_call"))

    def test_fmcsa_down_fails_safe(self):
        with mock.patch.object(main.requests, "get", side_effect=OSError("down")):
            r = self.quiet(main.verify_carrier, "c1", MC)
        self.assertEqual((r["result"], r["next_step"]), ("unavailable", "end_call"))


class TestOneTimeCode(BackendTest):

    def test_code_is_never_returned_to_the_ai(self):
        main.verify_carrier("c1", MC)
        result, code = self.send_code("c1")
        self.assertEqual(result["result"], "sent")
        self.assertNotIn(code, json.dumps(result))

    def test_send_code_before_verifying_is_out_of_order(self):
        result, code = self.send_code("c1")
        self.assertEqual(result["result"], "out_of_order")
        self.assertIsNone(code)

    def test_right_code_passes(self):
        main.verify_carrier("c1", MC)
        _, code = self.send_code("c1")
        r = main.verify_otp("c1", MC, code)
        self.assertEqual((r["result"], r["next_step"]), ("verified", "search_loads"))

    def test_spoken_code_with_spaces_and_dashes_passes(self):
        main.verify_carrier("c1", MC)
        _, code = self.send_code("c1")
        spoken = " - ".join(code)
        self.assertEqual(main.verify_otp("c1", MC, spoken)["result"], "verified")

    def test_three_wrong_codes_lock_the_call(self):
        main.verify_carrier("c1", MC)
        self.send_code("c1")
        r1 = main.verify_otp("c1", MC, "000000")
        self.assertEqual((r1["result"], r1["attempts_left"]), ("wrong_code", 2))
        main.verify_otp("c1", MC, "000000")
        r3 = main.verify_otp("c1", MC, "000000")
        self.assertEqual((r3["result"], r3["next_step"]), ("locked", "end_call"))

    def test_expired_code_needs_a_new_one(self):
        main.verify_carrier("c1", MC)
        _, code = self.send_code("c1")
        main.calls["c1"]["otp"]["expires"] = 0          # pretend 5 minutes passed
        r = main.verify_otp("c1", MC, code)
        self.assertEqual((r["result"], r["next_step"]), ("expired", "send_otp"))


class TestCallOrder(BackendTest):
    """The backend, not the AI, decides the order of the call."""

    def test_search_before_verifying_is_refused(self):
        self.tms.add_load("LD1")
        self.assertEqual(self.search("c1")["result"], "not_verified")
        self.assertEqual(self.tms.queries, [])          # never reached the TMS

    def test_search_after_fmcsa_but_before_code_is_refused(self):
        main.verify_carrier("c1", MC)
        r = self.search("c1")
        self.assertEqual((r["result"], r["next_step"]), ("not_verified", "send_otp"))

    def test_verification_does_not_carry_over_to_another_call(self):
        self.verified_call("c1")
        self.assertEqual(self.search("c2")["result"], "not_verified")

    def test_verification_ends_when_the_call_ends(self):
        self.verified_call("c1")
        self.quiet(main.end_call, "c1")
        self.assertEqual(self.search("c1")["result"], "not_verified")

    def test_offer_before_search_is_out_of_order(self):
        self.verified_call("c1")
        self.assertEqual(main.get_offer("c1", MC, "LD1")["result"], "out_of_order")

    def test_every_reply_says_what_to_do_next(self):
        self.tms.add_load("LD1")
        self.verified_call("c1")
        replies = [self.search("c1"), main.get_offer("c1", MC, "LD1"),
                   self.quiet(main.respond_to_offer, "c1", MC, "LD1", True, ""),
                   self.quiet(main.transfer, "c1")]
        self.assertEqual([r["next_step"] for r in replies],
                         ["get_offer", "respond_to_offer", "transfer_to_rep", "end_call"])


class TestSearchLoads(BackendTest):

    def setUp(self):
        super().setUp()
        self.verified_call("c1")

    def test_finds_loads_and_never_shows_max_rate(self):
        self.tms.add_load("LD1", max_buy=2600)
        r = self.search("c1")
        self.assertEqual((r["result"], r["count"]), ("found", 1))
        self.assertNotIn("max_rate", r["loads"][0])
        self.assertNotIn("2600", json.dumps(r))

    def test_times_are_shown_in_each_citys_local_time(self):
        # fake load: pickup 2026-11-13 08:00 UTC in Richmond, VA;
        # delivery 2026-11-16 17:00 UTC in Dallas, TX
        self.tms.add_load("LD1")
        load = self.search("c1")["loads"][0]
        self.assertEqual(load["pickup_datetime"], "2026-11-13 3:00 AM EST")
        self.assertEqual(load["delivery_datetime"], "2026-11-16 11:00 AM CST")

    def test_soonest_pickup_first_even_across_time_zones(self):
        # Texas has two time zones. El Paso 8:00 AM MST is 15:00 UTC;
        # Dallas 8:30 AM CST is 14:30 UTC, so Dallas really picks up first,
        # even though "8:00" looks earlier than "8:30".
        self.tms.add_load("ELP", city="El Paso", state="TX", orig_zip="79901",
                          pickup="20261113150000")
        self.tms.add_load("DAL", city="Dallas", state="TX", orig_zip="75201",
                          pickup="20261113143000")
        loads = self.search("c1", origin="TX")["loads"]
        self.assertEqual([l["load_id"] for l in loads], ["DAL", "ELP"])
        self.assertEqual(loads[0]["pickup_datetime"], "2026-11-13 8:30 AM CST")
        self.assertEqual(loads[1]["pickup_datetime"], "2026-11-13 8:00 AM MST")

    def test_state_only_searches_by_state(self):
        self.tms.add_load("LD1")
        self.search("c1", origin="VA")
        self.assertEqual(self.tms.queries[0].get("ORIG_STATE"), "VA")
        self.assertNotIn("ORIG_CITY", self.tms.queries[0])

    def test_city_and_state_search_both(self):
        self.tms.add_load("LD1")
        self.search("c1", origin="Richmond, VA")
        self.assertEqual((self.tms.queries[0]["ORIG_CITY"], self.tms.queries[0]["ORIG_STATE"]),
                         ("Richmond", "VA"))

    def test_equipment_is_optional(self):
        self.tms.add_load("LD1", equipment="REEFER")
        r = self.search("c1", equipment_type="")
        self.assertEqual(r["result"], "found")
        self.assertNotIn("EQTYPE", self.tms.queries[0])

    def test_equipment_words_are_translated(self):
        self.tms.add_load("LD1", equipment="STEP_DECK")
        self.assertEqual(self.search("c1", equipment_type="step deck")["result"], "found")
        self.assertEqual(self.tms.queries[0]["EQTYPE"], "STEP_DECK")

    def test_unknown_equipment_is_refused_before_the_tms(self):
        r = self.search("c1", equipment_type="boat")
        self.assertEqual(r["result"], "invalid_equipment")
        self.assertEqual(self.tms.queries, [])

    def test_no_location_asks_for_one(self):
        self.assertEqual(self.search("c1", origin="")["result"], "need_origin")

    def test_nothing_found(self):
        r = self.search("c1", origin="OH")
        self.assertEqual((r["result"], r["next_step"]), ("no_loads", "search_loads"))

    def test_only_open_loads_are_pitched(self):
        self.tms.add_load("LD1", status="COVERED")
        self.tms.add_load("LD2")
        self.assertEqual([l["load_id"] for l in self.search("c1")["loads"]], ["LD2"])

    def test_at_most_three_loads_soonest_pickup_first(self):
        for i, day in enumerate(["15", "12", "14", "13"]):
            self.tms.add_load(f"LD{i}", pickup=f"202611{day}080000")
        ids = [l["load_id"] for l in self.search("c1")["loads"]]
        self.assertEqual(ids, ["LD1", "LD3", "LD2"])

    def test_pickup_date_filters_to_that_day(self):
        self.tms.add_load("LD1", pickup="20270707080000")
        self.tms.add_load("LD2", pickup="20270708080000")
        r = self.search("c1", pickup_date="2027-07-07")
        self.assertEqual([l["load_id"] for l in r["loads"]], ["LD1"])
        self.assertEqual(self.tms.queries[0]["PICKUP_DATE"], "20270707")

    def test_pickup_date_is_optional(self):
        self.tms.add_load("LD1")
        self.assertEqual(self.search("c1")["result"], "found")
        self.assertNotIn("PICKUP_DATE", self.tms.queries[0])

    def test_bad_pickup_date_is_refused_before_the_tms(self):
        for bad in ["July 7th", "2027-02-30", "07/07/2027"]:
            r = self.search("c1", pickup_date=bad)
            self.assertEqual((r["result"], r["next_step"]), ("invalid_date", "search_loads"))
        self.assertEqual(self.tms.queries, [])

    def test_tms_down_fails_safe(self):
        self.tms.fail_states.add("VA")
        r = self.search("c1")
        self.assertEqual((r["result"], r["next_step"]), ("unavailable", "end_call"))


class TestNegotiation(BackendTest):

    def setUp(self):
        super().setUp()
        self.tms.add_load("LD1", rate=2000, max_buy=2600)
        self.verified_call("c1")
        self.search("c1")

    def respond(self, accept, ask=""):
        return self.quiet(main.respond_to_offer, "c1", MC, "LD1", accept, ask)

    def test_opening_offer_is_board_rate(self):
        r = main.get_offer("c1", MC, "LD1")
        self.assertEqual((r["result"], r["rate"]), ("offer", 2000))

    def test_only_pitched_loads_can_be_negotiated(self):
        r = main.get_offer("c1", MC, "LD999")
        self.assertEqual((r["result"], r["next_step"]), ("unknown_load", "search_loads"))

    def test_counter_offers_never_reveal_or_exceed_max(self):
        main.get_offer("c1", MC, "LD1")
        replies = [self.respond(False, "9000") for _ in range(3)]
        self.assertEqual([r["result"] for r in replies], ["counter", "counter", "final_offer"])
        self.assertTrue(all(r["rate"] < 2600 for r in replies))
        self.assertNotIn("max", json.dumps(replies).lower())

    def test_too_high_after_final_offer_ends_the_call(self):
        main.get_offer("c1", MC, "LD1")
        for _ in range(3):
            self.respond(False, "9000")
        r = self.respond(False, "9000")
        self.assertEqual((r["result"], r["next_step"]), ("end", "end_call"))

    def test_accept_records_the_deal_but_does_not_book_yet(self):
        main.get_offer("c1", MC, "LD1")
        r = self.respond(True)
        self.assertEqual((r["result"], r["rate"]), ("accept", 2000))
        self.assertNotIn("max_rate", r)                 # the AI still never sees it
        self.assertEqual(main.calls["c1"]["deal"], {"load_id": "LD1", "rate": 2000,
                                                    "board_rate": 2000, "max_rate": 2600})
        self.assertEqual(self.tms.books, [])            # booking waits for the call to end

    def test_price_spoken_with_dollar_sign_and_commas(self):
        main.get_offer("c1", MC, "LD1")
        self.assertEqual(self.respond(False, "$2,100")["result"], "accept")

    def test_nothing_changes_after_a_deal(self):
        main.get_offer("c1", MC, "LD1")
        self.respond(True)
        self.assertEqual(self.respond(False, "3000")["result"], "out_of_order")
        self.assertEqual(self.search("c1")["result"], "out_of_order")


class TestTransfer(BackendTest):

    def test_no_transfer_without_a_deal(self):
        self.tms.add_load("LD1")
        self.verified_call("c1")
        self.assertEqual(self.quiet(main.transfer, "c1")["result"], "no_deal")

    def test_unknown_call_cannot_transfer(self):
        self.assertEqual(self.quiet(main.transfer, "nope")["result"], "no_deal")

    def test_transfer_after_deal_only_once(self):
        self.deal("c1")
        r = self.quiet(main.transfer, "c1")
        self.assertEqual((r["result"], r["next_step"]), ("allowed", "end_call"))
        self.assertEqual(self.quiet(main.transfer, "c1")["result"], "already_transferred")

    def test_transfer_gives_the_rep_the_deal(self):
        self.deal("c1", rate=2000, max_buy=2600)
        r = self.quiet(main.transfer, "c1")
        self.assertEqual((r["load_id"], r["agreed_rate"], r["board_rate"], r["max_rate"]),
                         ("LD1", 2000, 2000, 2600))
        self.assertIn("load LD1 at $2,000", r["rep_summary"])
        self.assertIn("max rate $2,600", r["rep_summary"])

    def test_no_deal_transfer_reveals_nothing(self):
        self.tms.add_load("LD1", max_buy=2600)
        self.verified_call("c1")
        self.search("c1")
        r = self.quiet(main.transfer, "c1")
        self.assertEqual(r["result"], "no_deal")
        self.assertNotIn("2600", json.dumps(r))


class TestEndCall(BackendTest):

    def test_deal_is_booked_when_the_call_ends(self):
        self.deal("c1")
        self.quiet(main.transfer, "c1")
        s = self.quiet(main.end_call, "c1")
        self.assertEqual(set(s) - {"result"}, TWIN_COLUMNS)   # matches the Twin table
        self.assertEqual(s["outcome"], "booked_pending")
        self.assertFalse(s["needs_review"])
        self.assertEqual(s["review_reason"], "")
        self.assertTrue(s["transferred"])
        self.assertEqual((s["load_id"], s["agreed_rate"], s["mc_number"]), ("LD1", 2000, MC))
        self.assertEqual((s["board_rate"], s["max_rate"]), (2000, 2600))
        self.assertEqual(self.tms.books[0]["AGREED_RATE"], "2000")

    def test_ending_twice_does_not_book_twice(self):
        self.deal("c1")
        first = self.quiet(main.end_call, "c1")
        second = self.quiet(main.end_call, "c1")
        self.assertEqual(first, second)
        self.assertEqual(len(self.tms.books), 1)

    def test_load_already_taken_needs_review_with_reason(self):
        self.deal("c1")
        self.tms.booked.add("LD1")
        s = self.quiet(main.end_call, "c1")
        self.assertEqual(s["outcome"], "load_taken")
        self.assertTrue(s["needs_review"])
        self.assertIn("ALREADY_BOOKED", s["review_reason"])

    def test_rejected_rate_needs_review_with_reason(self):
        self.deal("c1")
        self.tms.faults.append("invalid_rate")
        s = self.quiet(main.end_call, "c1")
        self.assertEqual(s["outcome"], "booking_failed")
        self.assertTrue(s["needs_review"])
        self.assertIn("INVALID_RATE", s["review_reason"])

    def test_broken_booking_reply_needs_review(self):
        self.deal("c1")
        self.tms.faults.append("garbled_status")
        s = self.quiet(main.end_call, "c1")
        self.assertEqual(s["outcome"], "booking_unconfirmed")
        self.assertTrue(s["needs_review"])
        self.assertIn("check", s["review_reason"].lower())

    def test_verified_but_no_deal(self):
        self.verified_call("c1")
        s = self.quiet(main.end_call, "c1")
        self.assertEqual((s["outcome"], s["needs_review"]), ("no_deal", False))
        self.assertEqual((s["board_rate"], s["max_rate"]), (None, None))
        self.assertEqual(self.tms.books, [])

    def test_never_verified(self):
        main.verify_carrier("c1", MC)
        self.assertEqual(self.quiet(main.end_call, "c1")["outcome"], "not_verified")

    def test_required_twin_columns_are_never_null(self):
        # A call the backend never heard of (hung up before giving an MC number).
        s = self.quiet(main.end_call, "ghost")
        for column in REQUIRED_COLUMNS:
            self.assertIsNotNone(s[column], column)
        self.assertEqual((s["mc_number"], s["carrier_name"]), ("unknown", "unknown"))
        self.assertEqual((s["outcome"], s["last_step"]), ("not_verified", "start"))
        self.assertEqual((s["needs_review"], s["transferred"]), (False, False))

    def test_required_columns_filled_on_a_real_deal(self):
        self.deal("c1")
        s = self.quiet(main.end_call, "c1")
        for column in REQUIRED_COLUMNS:
            self.assertIsNotNone(s[column], column)
        self.assertEqual(s["carrier_name"], "ACME TRUCKING LLC")

    def test_missing_call_id(self):
        self.assertEqual(main.end_call("  ")["result"], "missing_call_id")


if __name__ == "__main__":
    unittest.main()


class TestLocalTimes(unittest.TestCase):
    """main.local_time: UTC from the TMS -> the ZIP's local time.
    Uses the real zipcodes package (pip install zipcodes)."""

    def test_zip_gives_the_time_zone(self):
        self.assertEqual(main.local_time("2026-11-13 08:00 UTC", "78205", "San Antonio, TX"),
                         "2026-11-13 2:00 AM CST")
        self.assertEqual(main.local_time("2026-11-13 08:00 UTC", "19103", "Philadelphia, PA"),
                         "2026-11-13 3:00 AM EST")

    def test_daylight_saving_is_handled(self):
        self.assertEqual(main.local_time("2026-07-07 12:00 UTC", "60601", "Chicago, IL"),
                         "2026-07-07 7:00 AM CDT")

    def test_arizona_has_no_daylight_saving(self):
        self.assertEqual(main.local_time("2026-07-07 12:00 UTC", "85004", "Phoenix, AZ"),
                         "2026-07-07 5:00 AM MST")

    def test_split_state_zips_use_their_own_zone(self):
        self.assertEqual(main.local_time("2026-11-13 15:00 UTC", "32501", "Pensacola, FL"),
                         "2026-11-13 9:00 AM CST")
        self.assertEqual(main.local_time("2026-11-13 15:00 UTC", "33101", "Miami, FL"),
                         "2026-11-13 10:00 AM EST")
        self.assertEqual(main.local_time("2026-11-13 15:00 UTC", "79901", "El Paso, TX"),
                         "2026-11-13 8:00 AM MST")

    def test_missing_or_broken_zip_falls_back_to_the_city(self):
        self.assertEqual(main.local_time("2026-11-13 15:00 UTC", "", "Pensacola, FL"),
                         "2026-11-13 9:00 AM CST")
        self.assertEqual(main.local_time("2026-11-13 15:00 UTC", "3#5", "El Paso, TX"),
                         "2026-11-13 8:00 AM MST")

    def test_unknown_city_falls_back_to_the_state(self):
        self.assertEqual(main.local_time("2026-11-13 15:00 UTC", "", "Tinytown, TX"),
                         "2026-11-13 9:00 AM CST")

    def test_local_day_can_differ_from_utc_day(self):
        self.assertEqual(main.local_time("2026-11-14 02:00 UTC", "90012", "Los Angeles, CA"),
                         "2026-11-13 6:00 PM PST")

    def test_unknown_place_or_time_keeps_utc(self):
        self.assertEqual(main.local_time("2026-11-13 08:00 UTC", "", "Somewhere"),
                         "2026-11-13 08:00 UTC")
        self.assertEqual(main.local_time("", "30303", "Atlanta, GA"), "")
        self.assertEqual(main.local_time("garbled", "30303", "Atlanta, GA"), "garbled")
