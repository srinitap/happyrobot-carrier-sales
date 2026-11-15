"""API key check (main.require_api_key)."""

import unittest
from types import SimpleNamespace
from unittest import mock

from helpers import main

KEY = "test-key-123"


class TestApiKey(unittest.TestCase):

    def check(self, sent, server_key=KEY, bearer=None):
        with mock.patch.object(main, "API_KEY", server_key):
            try:
                main.require_api_key(sent, bearer)
                return 200
            except main.HTTPException as e:
                return e.status_code

    def test_right_key_is_let_in(self):
        self.assertEqual(self.check(KEY), 200)

    def test_wrong_key_is_refused(self):
        self.assertEqual(self.check("wrong"), 401)

    def test_missing_key_is_refused(self):
        self.assertEqual(self.check(None), 401)
        self.assertEqual(self.check(""), 401)

    def test_right_bearer_token_is_let_in(self):
        token = SimpleNamespace(scheme="Bearer", credentials=KEY)
        self.assertEqual(self.check(None, bearer=token), 200)

    def test_wrong_bearer_token_is_refused(self):
        token = SimpleNamespace(scheme="Bearer", credentials="wrong")
        self.assertEqual(self.check(None, bearer=token), 401)

    def test_server_without_a_key_refuses_everything(self):
        self.assertEqual(self.check(KEY, server_key=""), 503)

    def test_every_endpoint_is_protected(self):
        # the check is attached to the whole app, so no endpoint can skip it
        deps = [d.dependency for d in main.app.router.dependencies]
        self.assertIn(main.require_api_key, deps)


if __name__ == "__main__":
    unittest.main()
