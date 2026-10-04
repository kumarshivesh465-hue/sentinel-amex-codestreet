"""Unit tests for backend.security: redaction, rate limiting, headers."""

import os
import sys
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import security


class RedactionTests(unittest.TestCase):
    def test_sensitive_keys_are_redacted(self):
        result = security.redact(
            {
                "email": "a@b.com",
                "password": "hunter2",
                "token": "abc.def",
                "authorization": "Bearer x",
                "secret": "s",
                "api_key": "k",
                "nested": {"pw_hash": "deadbeef", "user_id": "USR-1"},
            }
        )
        self.assertEqual(result["email"], security.REDACTED)
        self.assertEqual(result["password"], security.REDACTED)
        self.assertEqual(result["token"], security.REDACTED)
        self.assertEqual(result["secret"], security.REDACTED)
        self.assertEqual(result["nested"]["pw_hash"], security.REDACTED)
        # Non-sensitive context is preserved for debugging.
        self.assertEqual(result["nested"]["user_id"], "USR-1")

    def test_lists_are_redacted_recursively(self):
        result = security.redact([{"password": "x"}, {"ok": 1}])
        self.assertEqual(result[0]["password"], security.REDACTED)
        self.assertEqual(result[1]["ok"], 1)

    def test_non_dict_values_pass_through(self):
        self.assertEqual(security.redact("plain"), "plain")
        self.assertEqual(security.redact(5), 5)


class RateLimiterTests(unittest.TestCase):
    def test_allows_up_to_limit_then_blocks(self):
        limiter = security.RateLimiter()
        for _ in range(3):
            allowed, _ = limiter.check("k", 3, 60)
            self.assertTrue(allowed)
        allowed, retry = limiter.check("k", 3, 60)
        self.assertFalse(allowed)
        self.assertGreaterEqual(retry, 1)

    def test_keys_are_independent(self):
        limiter = security.RateLimiter()
        limiter.check("a", 1, 60)
        self.assertFalse(limiter.check("a", 1, 60)[0])
        self.assertTrue(limiter.check("b", 1, 60)[0])

    def test_window_expiry(self):
        from unittest import mock

        limiter = security.RateLimiter()
        # Deterministic clock: avoids depending on real sub-tick resolution.
        with mock.patch.object(security.time, "time", return_value=1000.0):
            self.assertTrue(limiter.check("k", 1, 60)[0])
            self.assertFalse(limiter.check("k", 1, 60)[0])
        with mock.patch.object(security.time, "time", return_value=1061.0):
            self.assertTrue(limiter.check("k", 1, 60)[0])

    def test_zero_limit_disables(self):
        limiter = security.RateLimiter()
        self.assertTrue(limiter.check("k", 0, 60)[0])


class HeaderTests(unittest.TestCase):
    def test_core_headers_present(self):
        headers = security.security_headers()
        self.assertEqual(headers["X-Content-Type-Options"], "nosniff")
        self.assertEqual(headers["X-Frame-Options"], "DENY")
        self.assertIn("Strict-Transport-Security", headers)

    def test_html_gets_a_csp(self):
        headers = security.security_headers(is_html=True)
        self.assertIn("Content-Security-Policy", headers)
        self.assertIn("frame-ancestors 'none'", headers["Content-Security-Policy"])

    def test_json_has_no_csp(self):
        self.assertNotIn("Content-Security-Policy", security.security_headers())

    def test_microphone_allowed_for_voice(self):
        self.assertIn("microphone=(self)", security.security_headers()["Permissions-Policy"])

    def test_request_ids_are_unique(self):
        self.assertNotEqual(security.new_request_id(), security.new_request_id())


if __name__ == "__main__":
    unittest.main(verbosity=2)
