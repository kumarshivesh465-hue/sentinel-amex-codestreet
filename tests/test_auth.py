"""Unit tests for the authentication primitives.

These exercise ``backend/auth`` directly - no server, no database - so they run
fast and pin down the security-relevant behaviour: hashes are salted, wrong
passwords fail, and tokens cannot be forged or replayed after expiry.
"""

import os
import sys
import time
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from backend import auth

SECRET = "test-secret-do-not-use-in-production"


class PasswordTests(unittest.TestCase):
    def test_hash_and_verify(self):
        salt, digest = auth.hash_password("passw0rd1")
        self.assertTrue(auth.verify_password("passw0rd1", salt, digest))

    def test_wrong_password_fails(self):
        salt, digest = auth.hash_password("passw0rd1")
        self.assertFalse(auth.verify_password("passw0rd2", salt, digest))

    def test_same_password_gets_different_salt(self):
        salt_a, hash_a = auth.hash_password("passw0rd1")
        salt_b, hash_b = auth.hash_password("passw0rd1")
        self.assertNotEqual(salt_a, salt_b)
        self.assertNotEqual(hash_a, hash_b)

    def test_verification_uses_stored_salt(self):
        salt, digest = auth.hash_password("passw0rd1")
        # Re-hashing with the stored salt must reproduce the stored hash.
        _, again = auth.hash_password("passw0rd1", salt)
        self.assertEqual(again, digest)

    def test_missing_inputs_fail_closed(self):
        self.assertFalse(auth.verify_password("", "aa", "bb"))
        self.assertFalse(auth.verify_password("x", "", "bb"))
        self.assertFalse(auth.verify_password("x", "aa", ""))

    def test_password_rules(self):
        self.assertIsNotNone(auth.password_problem("short"))
        self.assertIsNotNone(auth.password_problem("allletters"))
        self.assertIsNotNone(auth.password_problem("12345678"))
        self.assertIsNone(auth.password_problem("passw0rd"))

    def test_email_rules(self):
        self.assertIsNotNone(auth.email_problem("not-an-email"))
        self.assertIsNotNone(auth.email_problem(""))
        self.assertIsNone(auth.email_problem("a@b.com"))


class TokenTests(unittest.TestCase):
    def _user(self):
        return {"id": "USR-1", "email": "a@b.com", "name": "Alice", "role": "supervisor"}

    def test_issue_and_verify(self):
        token = auth.issue_token(self._user(), SECRET, 3600)
        payload = auth.verify_token(token, SECRET)
        self.assertIsNotNone(payload)
        self.assertEqual(payload["sub"], "USR-1")
        self.assertEqual(payload["role"], "supervisor")

    def test_wrong_secret_rejected(self):
        token = auth.issue_token(self._user(), SECRET, 3600)
        self.assertIsNone(auth.verify_token(token, "a-different-secret"))

    def test_tampered_payload_rejected(self):
        token = auth.issue_token(self._user(), SECRET, 3600)
        body, signature = token.split(".")
        head = ("A" if body[0] != "A" else "B") + body[1:]
        forged = head + "." + signature
        self.assertIsNone(auth.verify_token(forged, SECRET))

    def test_expired_token_rejected(self):
        token = auth.issue_token(self._user(), SECRET, ttl_seconds=-1)
        self.assertIsNone(auth.verify_token(token, SECRET))

    def test_malformed_tokens_rejected(self):
        for bad in ("", "no-dot", "a.b.c", "!!!.???", None):
            self.assertIsNone(auth.verify_token(bad, SECRET))

    def test_tokens_differ_for_different_users(self):
        a = auth.issue_token({"id": "USR-1", "role": "cardmember"}, SECRET, 3600)
        b = auth.issue_token({"id": "USR-2", "role": "cardmember"}, SECRET, 3600)
        self.assertNotEqual(a, b)

    def test_token_is_valid_immediately(self):
        token = auth.issue_token(self._user(), SECRET, 60)
        self.assertIsNotNone(auth.verify_token(token, SECRET))
        self.assertLess(abs(int(time.time()) - auth.verify_token(token, SECRET)["exp"]), 120)


if __name__ == "__main__":
    unittest.main(verbosity=2)
