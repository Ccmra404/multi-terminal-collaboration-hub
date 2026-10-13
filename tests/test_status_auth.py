import unittest

from pathlib import Path
import sys

TOOLS = str(Path(__file__).resolve().parents[1] / "tools")
if TOOLS not in sys.path:
    sys.path.insert(0, TOOLS)

import status_auth


class StatusAuthTests(unittest.TestCase):
    def test_real_ip_is_trusted_only_from_loopback_proxy(self):
        self.assertEqual(status_auth.client_source("127.0.0.1", "203.0.113.8"), "203.0.113.8")
        self.assertEqual(status_auth.client_source("::1", "invalid"), "::1")
        self.assertEqual(status_auth.client_source("203.0.113.9", "198.51.100.4"), "203.0.113.9")

    def test_real_ip_is_trusted_only_from_loopback_proxy(self):
        self.assertEqual(status_auth.client_source("127.0.0.1", "203.0.113.8"), "203.0.113.8")
        self.assertEqual(status_auth.client_source("::1", "invalid"), "::1")
        self.assertEqual(status_auth.client_source("203.0.113.9", "198.51.100.4"), "203.0.113.9")

    def setUp(self):
        self.now = [1_800_000_000]
        self.config = status_auth.create_config(
            [("admin", "sample-test-password", "maintainer")],
            iterations=100_000,
        )
        self.auth = status_auth.AuthManager(self.config, clock=lambda: self.now[0])

    def test_password_hash_and_login_are_case_sensitive(self):
        self.assertIsNotNone(self.auth.authenticate("admin", "sample-test-password"))
        self.assertIsNone(self.auth.authenticate("admin", "Sample-test-password"))
        self.assertIsNone(self.auth.authenticate("unknown", "sample-test-password"))

    def test_signed_session_is_bound_to_user_role_and_csrf(self):
        session = self.auth.create_session("admin")
        self.assertEqual(self.auth.read_session(session["token"])["username"], "admin")
        self.assertTrue(self.auth.verify_csrf(self.auth.read_session(session["token"]), session["csrf_token"]))
        self.assertFalse(self.auth.verify_csrf(self.auth.read_session(session["token"]), "wrong"))
        tampered = session["token"][:-1] + ("a" if session["token"][-1] != "a" else "b")
        self.assertIsNone(self.auth.read_session(tampered))

    def test_session_expires_and_password_change_invalidates_old_session(self):
        session = self.auth.create_session("admin")
        self.now[0] += status_auth.SESSION_TTL_SECONDS + 1
        reloaded = status_auth.AuthManager(self.config, clock=lambda: self.now[0])
        self.assertIsNone(reloaded.read_session(session["token"]))

        self.now[0] -= status_auth.SESSION_TTL_SECONDS + 2
        session = self.auth.create_session("admin")
        self.config["users"][0] = status_auth.make_user("admin", "replacement-password", "maintainer", 100_000)
        reloaded = status_auth.AuthManager(self.config, clock=lambda: self.now[0])
        self.assertIsNone(reloaded.read_session(session["token"]))

    def test_login_throttles_repeated_failures(self):
        for _ in range(status_auth.MAX_LOGIN_FAILURES):
            self.assertIsNone(self.auth.authenticate("admin", "wrong-password", source="client-a"))
        with self.assertRaises(status_auth.AuthError):
            self.auth.authenticate("admin", "sample-test-password", source="client-a")
        self.assertIsNotNone(self.auth.authenticate("admin", "sample-test-password", source="client-b"))

    def test_invalid_config_fails_closed(self):
        invalid = dict(self.config)
        invalid["session_secret"] = "too-short"
        with self.assertRaises(status_auth.AuthError):
            status_auth.AuthManager(invalid)


if __name__ == "__main__":
    unittest.main()
