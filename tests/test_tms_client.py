"""TMS client (tms_client.py): reading replies, spotting broken ones, booking safely."""

import unittest

from helpers import BackendTest, tms_client


class TestParsing(unittest.TestCase):
    """The line protocol, without any server."""

    def test_parses_records_and_strips_padding(self):
        recs = tms_client._parse_reply("LOAD_ID:LD1|RATE:00002150   |STATUS:OPEN    \r\nEND\r\n")
        self.assertEqual(recs, [{"LOAD_ID": "LD1", "RATE": "00002150", "STATUS": "OPEN"}])

    def test_empty_result_is_valid(self):
        self.assertEqual(tms_client._parse_reply("END\r\n"), [])

    def test_err_line_raises_tms_error_with_code_and_message(self):
        with self.assertRaises(tms_client.TMSError) as ctx:
            tms_client._parse_reply("ERR|CODE:UNKNOWN_LOAD|MSG:load not found\r\n")
        self.assertEqual(ctx.exception.code, "UNKNOWN_LOAD")
        self.assertEqual(ctx.exception.msg, "load not found")

    def test_missing_end_is_a_broken_reply(self):
        with self.assertRaises(tms_client.TMSFault):
            tms_client._parse_reply("LOAD_ID:LD1|RATE:00002150\r\n")

    def test_garbled_rate_is_a_broken_reply(self):
        with self.assertRaises(tms_client.TMSFault):
            tms_client._parse_reply("LOAD_ID:LD1|RATE:00X02150\r\nEND\r\n")

    def test_garbled_key_is_a_broken_reply(self):
        with self.assertRaises(tms_client.TMSFault):
            tms_client._parse_reply("LOAD_ID:LD1|rate:00002150\r\nEND\r\n")

    def test_pipe_in_a_value_is_refused_before_sending(self):
        with self.assertRaises(ValueError):
            tms_client._build_request("LOAD_QUERY", {"ORIG_CITY": "Dallas|X"})

    def test_clean_load_translates_fields_and_hides_nothing_extra(self):
        load = tms_client.clean_load({"LOAD_ID": "LD1", "ORIG_CITY": "Richmond",
                                      "ORIG_STATE": "VA", "EQTYPE": "STEP_DECK",
                                      "RATE": "00002150", "MAX_BUY": "00002600",
                                      "PICKUP_DT": "20261112080000", "STATUS": "OPEN"})
        self.assertEqual(load["origin"], "Richmond, VA")
        self.assertEqual(load["equipment_type"], "step deck")
        self.assertEqual(load["loadboard_rate"], 2150)
        self.assertEqual(load["max_rate"], 2600)
        self.assertEqual(load["pickup_datetime"], "2026-11-12 08:00 UTC")

    def test_booking_timestamp_is_labelled_utc(self):
        self.assertEqual(tms_client._to_utc("20261111025444"), "2026-11-11 02:54 UTC")


class TestTalkingToTheTMS(BackendTest):
    """Against the fake TMS server."""

    def test_query_retries_after_a_broken_reply(self):
        self.tms.add_load("LD1")
        self.tms.faults.append("drop")                 # first reply breaks
        loads = self.quiet(tms_client.query_loads, {"ORIG_STATE": "VA"})
        self.assertEqual([l["load_id"] for l in loads], ["LD1"])
        self.assertEqual(len(self.tms.queries), 2)     # it asked twice

    def test_query_needs_at_least_one_filter(self):
        with self.assertRaises(ValueError):
            tms_client.query_loads({"ORIG_STATE": "", "EQTYPE": ""})

    def test_book_pending_reply_is_accepted(self):
        self.tms.add_load("LD1")
        r = tms_client.book_load("LD1", "1234", 2000)
        self.assertEqual(r["status"], "accepted")
        self.assertEqual(r["tms_status"], "PENDING")
        self.assertEqual(r["timestamp"], "2026-11-11 02:54 UTC")

    def test_book_booked_reply_is_accepted_too(self):
        self.tms.add_load("LD1")
        self.tms.faults.append("booked_status")
        self.assertEqual(tms_client.book_load("LD1", "1234", 2000)["status"], "accepted")

    def test_book_garbled_reply_then_already_booked_is_unconfirmed(self):
        self.tms.add_load("LD1")
        self.tms.faults.append("garbled_status")
        r = self.quiet(tms_client.book_load, "LD1", "1234", 2000)
        self.assertEqual(r, {"status": "unconfirmed"})
        self.assertEqual(len(self.tms.books), 2)       # tried, broke, retried once

    def test_book_cut_off_reply_then_already_booked_is_unconfirmed(self):
        self.tms.add_load("LD1")
        self.tms.faults.append("drop")
        self.assertEqual(self.quiet(tms_client.book_load, "LD1", "1234", 2000),
                         {"status": "unconfirmed"})

    def test_book_already_booked_on_first_try_is_raised(self):
        self.tms.add_load("LD1")
        self.tms.booked.add("LD1")
        with self.assertRaises(tms_client.TMSError) as ctx:
            self.quiet(tms_client.book_load, "LD1", "1234", 2000)
        self.assertEqual(ctx.exception.code, "ALREADY_BOOKED")

    def test_book_invalid_rate_is_raised_without_retrying(self):
        self.tms.add_load("LD1")
        self.tms.faults.append("invalid_rate")
        with self.assertRaises(tms_client.TMSError) as ctx:
            self.quiet(tms_client.book_load, "LD1", "1234", 2000)
        self.assertEqual(ctx.exception.code, "INVALID_RATE")
        self.assertEqual(len(self.tms.books), 1)


if __name__ == "__main__":
    unittest.main()
