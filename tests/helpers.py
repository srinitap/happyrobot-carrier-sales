"""
Shared setup for the tests: point the backend at the fake TMS, fake FMCSA,
and give each test a fresh, verified call.

Nothing here talks to the real TMS or FMCSA, or reads your real secrets.
"""

import contextlib
import io
import os
import re
import sys
import unittest
from unittest import mock

# make the project folder importable (tests/ lives inside it)
ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
if ROOT not in sys.path:
    sys.path.insert(0, ROOT)

import main            # noqa: E402  (after the path fix on purpose)
import tms_client      # noqa: E402

from fake_tms import FakeTMS   # noqa: E402

MC = "1234"


class FakeFMCSA:
    """Stands in for requests.get to the FMCSA API."""

    def __init__(self, allowed="Y", name="ACME TRUCKING LLC", found=True):
        self.allowed, self.name, self.found = allowed, name, found

    def __call__(self, *args, **kwargs):
        return self

    def raise_for_status(self):
        pass

    def json(self):
        if not self.found:
            return {"content": []}
        return {"content": [{"carrier": {"allowedToOperate": self.allowed,
                                         "legalName": self.name}}]}


class BackendTest(unittest.TestCase):
    """Base class: a fresh fake TMS, fake FMCSA and empty call memory per test."""

    def setUp(self):
        self.tms = FakeTMS()
        # point the TMS client at the fake (module settings are read per call)
        self._patches = [
            mock.patch.object(tms_client, "TMS_HOST", "127.0.0.1"),
            mock.patch.object(tms_client, "TMS_PORT", self.tms.port),
            mock.patch.object(tms_client, "TMS_TOKEN", "test-token"),
            mock.patch.object(tms_client.time, "sleep", lambda s: None),  # no retry waits
            mock.patch.object(main.requests, "get", FakeFMCSA()),
        ]
        for p in self._patches:
            p.start()
        main.calls.clear()
        main.finished_calls.clear()

    def tearDown(self):
        for p in self._patches:
            p.stop()
        self.tms.stop()

    # ------------------------------------------------------------ helpers

    def quiet(self, fn, *args, **kwargs):
        """Run fn without its print() output cluttering the test results."""
        with contextlib.redirect_stdout(io.StringIO()):
            return fn(*args, **kwargs)

    def send_code(self, call_id, mc=MC):
        """Call /send-otp and read the code it prints (the mock 'text message')."""
        buf = io.StringIO()
        with contextlib.redirect_stdout(buf):
            result = main.send_otp(call_id, mc)
        found = re.search(r"OTP for MC \d+: (\d{6})", buf.getvalue())
        return result, found.group(1) if found else None

    def verified_call(self, call_id="call-1", mc=MC):
        """A call that has passed FMCSA and the one-time code."""
        self.assertEqual(main.verify_carrier(call_id, mc)["result"], "verified")
        _, code = self.send_code(call_id, mc)
        self.assertEqual(main.verify_otp(call_id, mc, code)["result"], "verified")
        return call_id

    def search(self, call_id, **kwargs):
        kwargs.setdefault("origin", "VA")
        return self.quiet(main.search_loads, call_id, MC, **kwargs)

    def deal(self, call_id="call-1", load_id="LD1", **load):
        """A verified call that has agreed a price on load_id (board rate)."""
        self.tms.add_load(load_id, **load)
        self.verified_call(call_id)
        self.search(call_id, origin=load.get("state", "VA"))
        main.get_offer(call_id, MC, load_id)
        r = self.quiet(main.respond_to_offer, call_id, MC, load_id, True, "")
        self.assertEqual(r["result"], "accept")
        return call_id
