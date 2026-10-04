"""End-to-end HTTP tests.

These boot the real server on an ephemeral port against a temporary database,
so they exercise routing, JSON handling, and the full orchestration path -
not just individual functions.
"""

import importlib
import json
import os
import sys
import tempfile
import threading
import unittest
import urllib.error
import urllib.request
import uuid
from http.server import ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


def _http(port, method, path, body=None, token=None, raw=False):
    """One HTTP call. ``token`` adds a bearer header; ``raw`` skips JSON parsing."""
    url = f"http://127.0.0.1:{port}{path}"
    data = json.dumps(body).encode("utf-8") if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(url, data=data, headers=headers, method=method)
    try:
        with urllib.request.urlopen(req, timeout=10) as response:
            payload = response.read().decode("utf-8", errors="replace")
            return response.status, (payload if raw else json.loads(payload))
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        if raw:
            return exc.code, payload
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, {"raw": payload}


class ServerTestCase(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.tmpdir = tempfile.TemporaryDirectory()
        db_path = os.path.join(cls.tmpdir.name, "e2e.db")

        from backend import config, db as db_module

        cls.db = importlib.reload(db_module)
        cls.db.DB_PATH = db_path
        cls.db._local = type(cls.db._local)()
        cls.db.init_db(reset=True)

        cls.config = config
        cls.config.settings.port = 0  # let the OS choose a free port
        cls.config.settings.demo_mode = True
        # The suite deliberately makes many calls from one IP; keep rate limits
        # out of the way here. Their own behaviour is tested in RateLimitTests.
        cls.config.settings.rate_limit_auth = 100000
        cls.config.settings.rate_limit_api = 100000
        from backend import security as security_module

        security_module.limiter.reset()

        from backend import server as server_module

        cls.server_module = importlib.reload(server_module)
        cls.httpd = ThreadingHTTPServer(("127.0.0.1", 0), cls.server_module.Handler)
        cls.httpd.daemon_threads = True
        cls.port = cls.httpd.server_address[1]

        cls.thread = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.thread.start()

        # Real authentication is enforced by default, so the suite registers a
        # supervisor (full access) and a cardmember (scoped access) once.
        cls.emails = {}
        cls.token = cls._bootstrap_user("supervisor")
        cls.cardmember_token = cls._bootstrap_user("cardmember")

    @classmethod
    def _bootstrap_user(cls, role):
        email = f"{role}-{uuid.uuid4().hex[:8]}@sentinel.test"
        status, data = _http(
            cls.port, "POST", "/api/auth/register",
            {"email": email, "password": "passw0rd1", "name": role.title(), "role": role},
        )
        assert status == 200 and data.get("token"), (status, data)
        cls.emails[role] = email
        return data["token"]

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()
        cls.thread.join(timeout=5)
        # Handler threads are daemons. Give any in-flight request a moment to
        # finish, then close every connection this process opened, so Windows
        # will release the temp directory.
        import time as _time

        _time.sleep(0.2)
        cls.db.close_all_conns()
        try:
            cls.tmpdir.cleanup()
        except PermissionError:
            pass  # a lingering handle must not fail the run

    # ---------- helpers ----------
    def request(self, method, path, body=None, token="__default__"):
        """Authenticated by default; pass ``token=None`` for anonymous calls."""
        tok = self.token if token == "__default__" else token
        return _http(self.port, method, path, body, tok)

    def request_text(self, method, path):
        """For endpoints that do not return JSON (the HTML page)."""
        return _http(self.port, method, path, raw=True)

    def new_session(self):
        status, data = self.request("POST", "/api/sessions")
        self.assertEqual(status, 200)
        return data["session_id"]

    def send(self, session_id, text):
        return self.request("POST", f"/api/sessions/{session_id}/messages", {"text": text})


class HealthTests(ServerTestCase):
    def test_health(self):
        status, data = self.request("GET", "/api/health")
        self.assertEqual(status, 200)
        self.assertEqual(data["status"], "ok")

    def test_config_exposes_policy(self):
        status, data = self.request("GET", "/api/config")
        self.assertEqual(status, 200)
        self.assertIn("autonomy_threshold", data["policy"])

    def test_frontend_is_served(self):
        status, body = self.request_text("GET", "/")
        self.assertEqual(status, 200)
        self.assertIn("Sentinel", body)

    def test_path_traversal_blocked(self):
        status, _ = self.request_text("GET", "/../backend/db.py")
        self.assertNotEqual(status, 200)

    def test_unknown_route(self):
        status, _ = self.request("GET", "/api/nope")
        self.assertEqual(status, 404)


class ConversationFlowTests(ServerTestCase):
    def test_lost_stolen_proposes(self):
        session_id = self.new_session()
        status, data = self.send(session_id, "I lost my card")
        self.assertEqual(status, 200)
        self.assertEqual(data["type"], "proposal")
        self.assertEqual(data["action"], "CARD_REISSUE")
        self.assertTrue(data["step_up_required"])

    def test_small_fee_proposes(self):
        session_id = self.new_session()
        status, data = self.send(session_id, "please reverse my $50 annual fee")
        self.assertEqual(status, 200)
        self.assertEqual(data["type"], "proposal")
        self.assertEqual(data["payload"]["amount"], 50.0)

    def test_large_fee_escalates(self):
        session_id = self.new_session()
        status, data = self.send(session_id, "waive the $900 fee")
        self.assertEqual(status, 200)
        self.assertEqual(data["type"], "escalation")

    def test_human_request_escalates(self):
        session_id = self.new_session()
        status, data = self.send(session_id, "let me speak to a human")
        self.assertEqual(data["type"], "escalation")

    def test_frustration_escalates(self):
        session_id = self.new_session()
        status, data = self.send(
            session_id, "This is absolutely unacceptable and I am furious about my card"
        )
        self.assertEqual(data["type"], "escalation")
        self.assertIn("frustration", data["reason"].lower())

    def test_unknown_escalates(self):
        session_id = self.new_session()
        status, data = self.send(session_id, "hello")
        self.assertEqual(data["type"], "escalation")

    def test_credit_limit_preview_is_stable(self):
        session_id = self.new_session()
        _, first = self.send(session_id, "I want a credit limit increase")
        _, second = self.send(session_id, "credit limit increase please")
        self.assertEqual(first["payload"]["utilization"], second["payload"]["utilization"])

    def test_empty_text_rejected(self):
        session_id = self.new_session()
        status, _ = self.send(session_id, "   ")
        self.assertEqual(status, 400)

    def test_oversized_text_rejected(self):
        session_id = self.new_session()
        status, _ = self.send(session_id, "x" * 5000)
        self.assertEqual(status, 413)

    def test_case_number_not_treated_as_amount(self):
        """Regression: 'card ending 1234' used to parse as a $1234 fee."""
        session_id = self.new_session()
        _, data = self.send(session_id, "I lost my card ending 1234")
        self.assertEqual(data["type"], "proposal")


class ConfirmationTests(ServerTestCase):
    def test_confirm_executes_and_audits(self):
        session_id = self.new_session()
        _, proposal = self.send(session_id, "I lost my card")

        status, result = self.request(
            "POST",
            "/api/actions/confirm",
            {
                "session_id": session_id,
                "action": proposal["action"],
                "payload": proposal["payload"],
                "confidence": proposal["confidence"],
                "policy": proposal["policy"],
                "rationale": proposal["rationale"],
            },
        )
        self.assertEqual(status, 200)
        self.assertTrue(result["executed"])
        self.assertTrue(result["audit"]["id"].startswith("AUD-"))

        _, session = self.request("GET", f"/api/sessions/{session_id}")
        self.assertEqual(session["session"]["status"], "Resolved")

    def test_policy_gate_blocks_over_limit_at_execution(self):
        """Server must re-check policy; client payloads are untrusted."""
        session_id = self.new_session()
        status, result = self.request(
            "POST",
            "/api/actions/confirm",
            {
                "session_id": session_id,
                "action": "FEE_REVERSAL",
                "payload": {"amount": 99999},
                "confidence": 0.99,
                "policy": "FEE-WAIVER-02",
                "rationale": "attacker supplied",
            },
        )
        self.assertEqual(status, 403)

    def test_confirm_unknown_session(self):
        status, _ = self.request(
            "POST",
            "/api/actions/confirm",
            {"session_id": "SES-NOPE", "action": "CARD_REISSUE", "payload": {}},
        )
        self.assertEqual(status, 404)

    def test_decline_writes_audit(self):
        session_id = self.new_session()
        self.send(session_id, "I lost my card")
        status, _ = self.request("POST", "/api/actions/decline", {"session_id": session_id})
        self.assertEqual(status, 200)

        _, audit = self.request("GET", f"/api/audit/{session_id}")
        self.assertTrue(any(r["action"] == "DECLINED" for r in audit["records"]))


class AuditApiTests(ServerTestCase):
    def test_verify_endpoint(self):
        session_id = self.new_session()
        self.send(session_id, "I lost my card")
        status, data = self.request("GET", "/api/audit/verify")
        self.assertEqual(status, 200)
        self.assertIn("ok", data)

    def test_tamper_then_verify_fails(self):
        session_id = self.new_session()
        _, escalation = self.send(session_id, "hello")
        audit_id = escalation["audit"]["id"]

        self.request("POST", "/api/audit/tamper", {"audit_id": audit_id})

        _, data = self.request("GET", "/api/audit/verify")
        self.assertFalse(data["ok"])
        self.assertEqual(data["broken_at"], audit_id)

    def test_tamper_requires_id(self):
        status, _ = self.request("POST", "/api/audit/tamper", {})
        self.assertEqual(status, 400)


class TimelineTests(ServerTestCase):
    def test_timeline_merges_messages_and_audit(self):
        session_id = self.new_session()
        self.send(session_id, "I lost my card")
        status, data = self.request("GET", f"/api/sessions/{session_id}/timeline")
        self.assertEqual(status, 200)
        self.assertGreaterEqual(data["count"], 1)

    def test_timeline_unknown_session(self):
        status, _ = self.request("GET", "/api/sessions/SES-NOPE/timeline")
        self.assertEqual(status, 404)


class DashboardTests(ServerTestCase):
    def test_dashboard_shape(self):
        session_id = self.new_session()
        self.send(session_id, "I lost my card")
        status, data = self.request("GET", "/api/dashboard/sessions")
        self.assertEqual(status, 200)
        self.assertIn("sessions", data)
        self.assertIn("stats", data)


class FeedbackTests(ServerTestCase):
    def test_record_feedback(self):
        session_id = self.new_session()
        status, data = self.request(
            "POST", "/api/feedback", {"session_id": session_id, "rating": 5, "comment": "good"}
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["recorded"])

    def test_reject_bad_rating(self):
        session_id = self.new_session()
        status, _ = self.request(
            "POST", "/api/feedback", {"session_id": session_id, "rating": 9}
        )
        self.assertEqual(status, 400)

    def test_reject_non_numeric_rating(self):
        session_id = self.new_session()
        status, _ = self.request(
            "POST", "/api/feedback", {"session_id": session_id, "rating": "five"}
        )
        self.assertEqual(status, 400)


class AuthApiTests(ServerTestCase):
    def test_health_and_config_are_public(self):
        self.assertEqual(self.request("GET", "/api/health", token=None)[0], 200)
        self.assertEqual(self.request("GET", "/api/config", token=None)[0], 200)

    def test_register_login_and_me(self):
        email = f"user-{uuid.uuid4().hex[:8]}@sentinel.test"
        status, data = self.request(
            "POST", "/api/auth/register",
            {"email": email, "password": "passw0rd1", "name": "New", "role": "cardmember"},
            token=None,
        )
        self.assertEqual(status, 200)
        self.assertTrue(data["token"])

        status, login = self.request(
            "POST", "/api/auth/login", {"email": email, "password": "passw0rd1"}, token=None
        )
        self.assertEqual(status, 200)

        status, me = self.request("GET", "/api/auth/me", token=login["token"])
        self.assertEqual(status, 200)
        self.assertEqual(me["user"]["email"], email)

    def test_data_endpoints_require_auth(self):
        for method, path in (
            ("GET", "/api/dashboard/sessions"),
            ("GET", "/api/audit"),
            ("POST", "/api/sessions"),
        ):
            status, _ = self.request(method, path, token=None)
            self.assertEqual(status, 401, path)

    def test_invalid_token_rejected(self):
        status, _ = self.request("GET", "/api/dashboard/sessions", token="not.a.token")
        self.assertEqual(status, 401)

    def test_wrong_password_rejected(self):
        status, data = self.request(
            "POST", "/api/auth/login",
            {"email": self.emails["supervisor"], "password": "wrong-pass-9"}, token=None,
        )
        self.assertEqual(status, 401)
        self.assertNotIn("token", data)

    def test_duplicate_email_rejected(self):
        status, _ = self.request(
            "POST", "/api/auth/register",
            {"email": self.emails["supervisor"], "password": "passw0rd1"}, token=None,
        )
        self.assertEqual(status, 409)

    def test_weak_password_rejected(self):
        status, _ = self.request(
            "POST", "/api/auth/register",
            {"email": f"w-{uuid.uuid4().hex[:6]}@sentinel.test", "password": "short"}, token=None,
        )
        self.assertEqual(status, 400)

    def test_cross_user_session_forbidden(self):
        _, session = self.request("POST", "/api/sessions")
        session_id = session["session_id"]
        self.request("POST", f"/api/sessions/{session_id}/messages", {"text": "I lost my card"})

        # Cross-user access returns 404 (not 403) so the existence of another
        # user's resource is never disclosed.
        status, data = self.request("GET", f"/api/sessions/{session_id}", token=self.cardmember_token)
        self.assertEqual(status, 404)
        self.assertEqual(data["code"], "not_found")

    def test_cross_user_cannot_read_timeline(self):
        _, session = self.request("POST", "/api/sessions")
        session_id = session["session_id"]
        self.request("POST", f"/api/sessions/{session_id}/messages", {"text": "I lost my card"})
        status, _ = self.request(
            "GET", f"/api/sessions/{session_id}/timeline", token=self.cardmember_token
        )
        self.assertEqual(status, 404)

    def test_cardmember_cannot_verify_chain(self):
        status, _ = self.request("GET", "/api/audit/verify", token=self.cardmember_token)
        self.assertEqual(status, 403)

    def test_cardmember_cannot_tamper(self):
        status, _ = self.request(
            "POST", "/api/audit/tamper", {"audit_id": "AUD-ANY"}, token=self.cardmember_token
        )
        self.assertEqual(status, 403)

    def test_cardmember_dashboard_is_scoped(self):
        status, data = self.request("GET", "/api/dashboard/sessions", token=self.cardmember_token)
        self.assertEqual(status, 200)
        self.assertEqual(data["sessions"], [])


class CsatApiTests(ServerTestCase):
    def test_proposal_includes_csat(self):
        session_id = self.new_session()
        _, data = self.send(session_id, "I lost my card")
        self.assertIn("csat", data)
        self.assertGreaterEqual(data["csat"]["predicted"], 1.0)
        self.assertLessEqual(data["csat"]["predicted"], 5.0)

    def test_escalation_includes_csat(self):
        session_id = self.new_session()
        _, data = self.send(session_id, "waive the $900 fee")
        self.assertIn("csat", data)

    def test_dashboard_sessions_include_csat(self):
        session_id = self.new_session()
        self.send(session_id, "I lost my card")
        _, data = self.request("GET", "/api/dashboard/sessions")
        self.assertTrue(all("csat" in s for s in data["sessions"]))


class ConcurrencyTests(ServerTestCase):
    def test_parallel_messages_keep_chain_intact(self):
        """The audit chain must survive concurrent writers."""
        session_ids = [self.new_session() for _ in range(8)]
        errors = []

        def worker(session_id):
            try:
                self.send(session_id, "hello")
            except Exception as exc:  # pragma: no cover
                errors.append(exc)

        threads = [threading.Thread(target=worker, args=(s,)) for s in session_ids]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(timeout=15)

        self.assertEqual(errors, [])
        _, data = self.request("GET", "/api/audit/verify")
        self.assertTrue(data["ok"], f"chain broken at {data.get('broken_at')}")


class SecurityHeaderTests(ServerTestCase):
    def _headers(self, path):
        url = f"http://127.0.0.1:{self.port}{path}"
        with urllib.request.urlopen(url, timeout=10) as response:
            return {k.lower(): v for k, v in response.getheaders()}

    def test_json_responses_have_security_headers(self):
        headers = self._headers("/api/health")
        self.assertEqual(headers.get("x-content-type-options"), "nosniff")
        self.assertEqual(headers.get("x-frame-options"), "DENY")
        self.assertEqual(headers.get("referrer-policy"), "no-referrer")
        self.assertTrue(headers.get("x-request-id", "").startswith("REQ-"))

    def test_html_has_content_security_policy(self):
        headers = self._headers("/")
        self.assertIn("content-security-policy", headers)
        self.assertIn("frame-ancestors 'none'", headers["content-security-policy"])

    def test_api_is_not_shared_cacheable(self):
        self.assertEqual(self._headers("/api/health").get("cache-control"), "no-store")


class ErrorFormatTests(ServerTestCase):
    def test_error_has_stable_code(self):
        status, data = self.request("GET", "/api/nope")
        self.assertEqual(status, 404)
        self.assertEqual(data["error"], "not found")
        self.assertEqual(data["code"], "not_found")

    def test_unauthorized_error_code(self):
        status, data = self.request("GET", "/api/dashboard/sessions", token=None)
        self.assertEqual(status, 401)
        self.assertEqual(data["code"], "unauthorized")

    def test_missing_field_is_clean_400(self):
        status, data = self.request("POST", "/api/sessions/SES-X/messages", {"nope": 1})
        self.assertEqual(status, 400)
        self.assertEqual(data["code"], "bad_request")


class PaginationTests(ServerTestCase):
    def test_limit_caps_results(self):
        for _ in range(3):
            self.send(self.new_session(), "hello")
        status, data = self.request("GET", "/api/dashboard/sessions?limit=1")
        self.assertEqual(status, 200)
        self.assertLessEqual(len(data["sessions"]), 1)
        self.assertEqual(data["page"]["limit"], 1)
        self.assertIn("offset", data["page"])

    def test_limit_is_capped(self):
        status, data = self.request("GET", "/api/audit?limit=99999")
        self.assertEqual(status, 200)
        self.assertEqual(data["page"]["limit"], 500)


class AccountTests(ServerTestCase):
    def test_export_returns_own_data(self):
        session_id = self.new_session()
        self.send(session_id, "I lost my card")
        status, data = self.request("GET", "/api/account/export")
        self.assertEqual(status, 200)
        self.assertTrue(any(s["session"]["id"] == session_id for s in data["data"]["sessions"]))

    def test_delete_removes_user_and_revokes_access(self):
        email = f"del-{uuid.uuid4().hex[:8]}@sentinel.test"
        _, reg = self.request(
            "POST", "/api/auth/register",
            {"email": email, "password": "passw0rd1", "role": "cardmember"}, token=None,
        )
        token = reg["token"]
        _, session = self.request("POST", "/api/sessions", token=token)
        self.request(
            "POST", f"/api/sessions/{session['session_id']}/messages",
            {"text": "I lost my card"}, token=token,
        )

        status, data = self.request("POST", "/api/account/delete", token=token)
        self.assertEqual(status, 200)
        self.assertTrue(data["deleted"])
        # The old token now points at a user that no longer exists.
        self.assertEqual(self.request("GET", "/api/dashboard/sessions", token=token)[0], 401)


class RateLimitTests(ServerTestCase):
    def test_login_is_rate_limited(self):
        from backend import security as security_module

        original = self.config.settings.rate_limit_auth
        self.config.settings.rate_limit_auth = 3
        security_module.limiter.reset()
        try:
            statuses = [
                self.request(
                    "POST", "/api/auth/login",
                    {"email": f"nobody-{uuid.uuid4().hex[:6]}@sentinel.test", "password": "wrong-pass-1"},
                    token=None,
                )[0]
                for _ in range(6)
            ]
            self.assertIn(429, statuses)
        finally:
            self.config.settings.rate_limit_auth = original
            security_module.limiter.reset()


if __name__ == "__main__":
    unittest.main(verbosity=2)
