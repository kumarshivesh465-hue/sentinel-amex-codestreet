"""Sentinel HTTP server: routing, orchestration, and static frontend serving.

Standard library only - no web framework. Each request is handled on its own
thread by ``ThreadingHTTPServer``, so every handler is written to be safe under
concurrency: database access is per-thread (see ``db.py``), and the audit chain
is appended under a lock.

Request lifecycle for a servicing message:

    classify_intent -> risk_assessment -> resolver -> policy_gate
      -> (propose | escalate) -> audit on execution

Nothing is written to the audit trail until a case is actually resolved or
escalated; proposals are confirm-gated by the customer in the UI.
"""

from __future__ import annotations

import json
import os
import signal
import sqlite3
import sys
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, unquote, urlparse

# Importable both as ``backend.server`` (tests) and as ``python backend/server.py``
# (the documented run command). The fallback keeps the original behaviour.
try:
    from . import agents, auth, config, db, security
    from . import intent as llm_intent
    from . import logging_setup
except ImportError:  # executed as a top-level script
    sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
    import agents
    import auth
    import config
    import db
    import security
    import intent as llm_intent
    import logging_setup

log = logging_setup.get_logger()

# Maps an HTTP status to a stable machine-readable error code.
_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "forbidden",
    404: "not_found",
    409: "conflict",
    413: "payload_too_large",
    429: "rate_limited",
    500: "internal_error",
}

FRONTEND_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "..", "frontend")

_CONTENT_TYPES = {
    ".html": "text/html; charset=utf-8",
    ".css": "text/css; charset=utf-8",
    ".js": "application/javascript; charset=utf-8",
    ".json": "application/json; charset=utf-8",
    ".svg": "image/svg+xml",
    ".png": "image/png",
    ".ico": "image/x-icon",
}


def json_body(handler) -> dict:
    length = int(handler.headers.get("Content-Length", 0) or 0)
    if length <= 0:
        return {}
    raw = handler.rfile.read(length)
    try:
        parsed = json.loads(raw)
        return parsed if isinstance(parsed, dict) else {}
    except (json.JSONDecodeError, UnicodeDecodeError):
        return {}


class Handler(BaseHTTPRequestHandler):
    server_version = "Sentinel/1.0"

    def log_message(self, fmt, *args):
        print(f"[sentinel] {self.address_string()} - {fmt % args}")

    # ---------- helpers ----------
    def _rid(self):
        rid = getattr(self, "_request_id", None)
        if not rid:
            rid = security.new_request_id()
            self._request_id = rid
        return rid

    def send_json(self, status, data, extra_headers=None):
        # One error shape everywhere: {"error": <message>, "code": <stable code>}.
        if isinstance(data, dict) and data.get("error") and "code" not in data:
            data = dict(data)
            data["code"] = _STATUS_CODES.get(status, "error")
        body = json.dumps(data, default=str).encode("utf-8")
        self._status = status
        self.send_response(status)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Content-Length", str(len(body)))
        # Authenticated API responses must never be cached by a shared layer.
        self.send_header("Cache-Control", "no-store")
        self.send_header(security.REQUEST_ID_HEADER, self._rid())
        for key, value in security.security_headers().items():
            self.send_header(key, value)
        for key, value in (extra_headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def send_file(self, path, content_type):
        try:
            with open(path, "rb") as handle:
                body = handle.read()
        except FileNotFoundError:
            return self.send_json(404, {"error": "not found"})
        is_html = content_type.startswith("text/html")
        self._status = 200
        self.send_response(200)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        # HTML revalidates every load so a deploy is picked up; other assets cache.
        self.send_header("Cache-Control", "no-cache" if is_html else "public, max-age=3600")
        self.send_header(security.REQUEST_ID_HEADER, self._rid())
        for key, value in security.security_headers(is_html=is_html).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    # ---------- rate limiting + request logging ----------
    def _allow(self, key, limit, window):
        allowed, retry_after = security.limiter.check(key, limit, window)
        if not allowed:
            self.send_json(
                429,
                {"error": "too many requests; please slow down"},
                extra_headers={"Retry-After": str(retry_after)},
            )
            return False
        return True

    def _log_request(self, method, path, started, user=None):
        logging_setup.log_event(
            "request",
            request_id=self._rid(),
            method=method,
            path=path,
            status=getattr(self, "_status", 500),
            duration_ms=round((time.time() - started) * 1000, 1),
            client=self.address_string(),
            user_id=(user or getattr(self, "_user", None) or {}).get("id"),
            role=(user or getattr(self, "_user", None) or {}).get("role"),
        )

    def resolve_static(self, path: str):
        """Map a URL path to a file inside FRONTEND_DIR, blocking traversal."""
        relative = "index.html" if path in ("/", "/index.html") else path.lstrip("/")
        candidate = os.path.normpath(os.path.join(FRONTEND_DIR, relative))
        root = os.path.normpath(FRONTEND_DIR)
        if not candidate.startswith(root):
            return None, None
        if not os.path.isfile(candidate):
            return None, None
        ext = os.path.splitext(candidate)[1].lower()
        return candidate, _CONTENT_TYPES.get(ext, "application/octet-stream")

    # ---------- authentication helpers ----------
    def _bearer_token(self):
        header = self.headers.get("Authorization", "") or ""
        if not header.lower().startswith("bearer "):
            return None
        return header[7:].strip() or None

    def _require_user(self):
        """Return the authenticated user, or ``None`` after sending a 401."""
        if not config.settings.auth_enabled:
            self._user = {"id": None, "email": None, "name": "Demo", "role": "supervisor"}
            return self._user
        token = self._bearer_token()
        payload = auth.verify_token(token, config.settings.auth_secret) if token else None
        if not payload:
            self.send_json(401, {"error": "authentication required"})
            return None
        user = db.get_user(payload["sub"])
        if not user:
            self.send_json(401, {"error": "unknown user"})
            return None
        self._user = user
        return user

    def _health(self, path):
        """Component-level health, plus a lightweight liveness probe."""
        if path == "/api/health/live":
            return self.send_json(200, {"status": "ok", "live": True})

        components = {}
        try:
            db.get_conn().execute("SELECT 1").fetchone()
            components["database"] = {"status": "ok"}
        except Exception as exc:  # pragma: no cover - only on a broken DB
            components["database"] = {"status": "error", "detail": type(exc).__name__}

        try:
            ok, broken_at, count = db.verify_chain()
            components["audit_chain"] = {
                "status": "ok" if ok else "error",
                "records": count,
                "broken_at": broken_at,
            }
        except Exception as exc:  # pragma: no cover
            components["audit_chain"] = {"status": "error", "detail": type(exc).__name__}

        healthy = all(c["status"] == "ok" for c in components.values())
        return self.send_json(
            200 if healthy else 503,
            {
                "status": "ok" if healthy else "degraded",
                "demo_mode": config.settings.demo_mode,
                "auth_enabled": config.settings.auth_enabled,
                "llm": llm_intent.status(),
                "components": components,
                "audit": {
                    "ok": components["audit_chain"].get("status") == "ok",
                    "broken_at": components["audit_chain"].get("broken_at"),
                    "count": components["audit_chain"].get("records", 0),
                },
            },
        )

    def _pagination(self):
        query = parse_qs(urlparse(self.path).query)
        try:
            limit = int(query.get("limit", ["100"])[0])
        except (ValueError, TypeError):
            limit = 100
        try:
            offset = int(query.get("offset", ["0"])[0])
        except (ValueError, TypeError):
            offset = 0
        return max(1, min(limit, 500)), max(0, offset)

    @staticmethod
    def _public_user(user):
        return {
            "id": user.get("id"),
            "email": user.get("email"),
            "name": user.get("name"),
            "role": user.get("role", "cardmember"),
        }

    @staticmethod
    def _is_supervisor(user):
        if not config.settings.auth_enabled:
            return True
        return (user or {}).get("role") in ("supervisor", "admin")

    @staticmethod
    def _may_access_session(user, session):
        if not config.settings.auth_enabled:
            return True
        if (user or {}).get("role") in ("supervisor", "admin"):
            return True
        owner = session.get("user_id")
        return owner is None or owner == (user or {}).get("id")

    def _decorate_session(self, session):
        if not session:
            return session
        enriched = dict(session)
        enriched["csat"] = agents.predict_csat(
            session.get("confidence") or 0.0,
            sentiment={"score": session.get("sentiment") or 0.0},
            escalated=bool(session.get("escalated")),
            resolved=bool(session.get("resolved")),
            quality={"score": session.get("quality_score")},
        )
        return enriched

    def _decorate_sessions(self, sessions):
        return [self._decorate_session(s) for s in sessions]

    # ---------- auth routes ----------
    def handle_register(self, body):
        if not config.settings.auth_enabled:
            return self.send_json(403, {"error": "authentication is disabled"})
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        name = (body.get("name") or "").strip() or email.split("@")[0]
        role = (body.get("role") or "cardmember").strip().lower()
        if role not in auth.VALID_ROLES:
            return self.send_json(400, {"error": "role must be cardmember or supervisor"})
        problem = auth.email_problem(email) or auth.password_problem(password)
        if problem:
            return self.send_json(400, {"error": problem})
        if db.get_user_by_email(email):
            return self.send_json(409, {"error": "an account with that email already exists"})
        salt, pw_hash = auth.hash_password(password)
        try:
            user = db.create_user(email, name, role, salt, pw_hash)
        except sqlite3.IntegrityError:
            return self.send_json(409, {"error": "an account with that email already exists"})
        token = auth.issue_token(
            user, config.settings.auth_secret, config.settings.token_ttl_seconds
        )
        return self.send_json(200, {"token": token, "user": self._public_user(user)})

    def handle_login(self, body):
        if not config.settings.auth_enabled:
            return self.send_json(403, {"error": "authentication is disabled"})
        email = (body.get("email") or "").strip()
        password = body.get("password") or ""
        user = db.get_user_by_email(email)
        if not user or not auth.verify_password(
            password, user.get("pw_salt"), user.get("pw_hash")
        ):
            # Deliberately one message for both cases so we never reveal which
            # email addresses have accounts.
            return self.send_json(401, {"error": "invalid email or password"})
        token = auth.issue_token(
            user, config.settings.auth_secret, config.settings.token_ttl_seconds
        )
        return self.send_json(200, {"token": token, "user": self._public_user(user)})

    def do_OPTIONS(self):
        self.send_response(204)
        self.send_header("Access-Control-Allow-Origin", "*")
        self.send_header("Access-Control-Allow-Methods", "GET, POST, OPTIONS")
        self.send_header("Access-Control-Allow-Headers", "Content-Type")
        self.end_headers()

    # ---------- GET ----------
    def do_GET(self):
        started = time.time()
        self._request_id = security.new_request_id()
        self._status = 500
        path = unquote(urlparse(self.path).path)
        try:
            # Health endpoints are public and exempt from rate limiting so a
            # monitoring probe is never throttled.
            if path in ("/api/health", "/api/health/live"):
                return self._health(path)

            if path == "/api/config":
                return self.send_json(
                    200,
                    {
                        "demo_mode": config.settings.demo_mode,
                        "max_message_chars": config.settings.max_message_chars,
                        "auth_enabled": config.settings.auth_enabled,
                        "llm_enabled": llm_intent.status()["usable"],
                        "policy": {
                            "autonomy_threshold": agents.AUTONOMY_CONFIDENCE_THRESHOLD,
                            "fee_waiver_limit": agents.FEE_WAIVER_LIMIT,
                            "dispute_limit": agents.DISPUTE_AUTONOMOUS_LIMIT,
                        },
                    },
                )

            if path == "/api/auth/me":
                user = self._require_user()
                if user is None:
                    return
                return self.send_json(200, {"user": self._public_user(user)})

            # Everything else under /api/ requires a valid bearer token.
            if path.startswith("/api/"):
                ip = security.client_ip(self)
                if not self._allow(
                    "api:" + ip,
                    config.settings.rate_limit_api,
                    config.settings.rate_limit_api_window,
                ):
                    return
                user = self._require_user()
                if user is None:
                    return
                return self._api_get(path, user)

            # Static frontend (index.html, css/, js/, assets/) is public.
            file_path, content_type = self.resolve_static(path)
            if file_path:
                return self.send_file(file_path, content_type)

            return self.send_json(404, {"error": "not found"})

        except Exception:
            # Full detail goes to the log; the client only sees a clean message.
            logging_setup.get_logger().error(
                "unhandled error in GET",
                exc_info=True,
                extra={"request_id": self._rid(), "path": path, "client": self.address_string()},
            )
            self.send_json(500, {"error": "internal server error"})
        finally:
            self._log_request("GET", path, started)

    def _api_get(self, path, user):
        supervisor = self._is_supervisor(user)

        if path == "/api/account/export":
            return self.send_json(
                200,
                {"user": self._public_user(user), "data": db.export_user_data(user["id"])},
            )

        if path == "/api/dashboard/sessions":
            limit, offset = self._pagination()
            scope = None if supervisor else user["id"]
            sessions = db.list_sessions(user_id=scope, limit=limit, offset=offset)
            return self.send_json(
                200,
                {
                    "sessions": self._decorate_sessions(sessions),
                    "stats": db.session_stats(user_id=scope),
                    "page": {"limit": limit, "offset": offset, "count": len(sessions)},
                },
            )

        if path == "/api/audit/verify":
            if not supervisor:
                return self.send_json(403, {"error": "supervisor role required"})
            ok, broken_at, count = db.verify_chain()
            return self.send_json(200, {"ok": ok, "broken_at": broken_at, "count": count})

        if path == "/api/audit":
            limit, offset = self._pagination()
            records = db.list_audit(
                user_id=None if supervisor else user["id"], limit=limit, offset=offset
            )
            return self.send_json(
                200,
                {
                    "records": records,
                    "page": {"limit": limit, "offset": offset, "count": len(records)},
                },
            )

        if path.startswith("/api/audit/"):
            session_id = path.split("/api/audit/", 1)[1]
            session = db.get_session(session_id)
            if not session:
                return self.send_json(404, {"error": "unknown session"})
            if not self._may_access_session(user, session):
                return self.send_json(404, {"error": "unknown session"})
            limit, offset = self._pagination()
            records = db.list_audit(session_id, limit=limit, offset=offset)
            return self.send_json(200, {"records": records})

        if path.startswith("/api/sessions/") and path.endswith("/timeline"):
            session_id = path.split("/api/sessions/", 1)[1].rsplit("/timeline", 1)[0]
            return self._timeline(session_id, user)

        if path.startswith("/api/sessions/"):
            session_id = path.split("/api/sessions/", 1)[1]
            session = db.get_session(session_id)
            if not session:
                return self.send_json(404, {"error": "unknown session"})
            if not self._may_access_session(user, session):
                return self.send_json(404, {"error": "unknown session"})
            return self.send_json(
                200,
                {
                    "session": self._decorate_session(session),
                    "messages": db.get_messages(session_id),
                    "audit": db.list_audit(session_id),
                },
            )

        return self.send_json(404, {"error": "not found"})

    def _audit_summary(self):
        try:
            ok, broken_at, count = db.verify_chain()
            return {"ok": ok, "broken_at": broken_at, "count": count}
        except Exception:
            return {"ok": None, "broken_at": None, "count": 0}

    # ---------- POST ----------
    def do_POST(self):
        started = time.time()
        self._request_id = security.new_request_id()
        self._status = 500
        path = unquote(urlparse(self.path).path)
        try:
            body = json_body(self)

            # Strictest limits on credential endpoints, keyed by both IP and
            # email so neither a single host nor a single account can be
            # brute-forced.
            if path in ("/api/auth/register", "/api/auth/login"):
                ip = security.client_ip(self)
                if not self._allow(
                    "auth-ip:" + ip,
                    config.settings.rate_limit_auth,
                    config.settings.rate_limit_auth_window,
                ):
                    return
                email = (body.get("email") or "").strip().lower()
                if email and not self._allow(
                    "auth-email:" + email,
                    config.settings.rate_limit_auth,
                    config.settings.rate_limit_auth_window,
                ):
                    return

            if path == "/api/auth/register":
                return self.handle_register(body)

            if path == "/api/auth/login":
                return self.handle_login(body)

            if path == "/api/auth/logout":
                # Tokens are stateless, so logout is a client-side token discard.
                if config.settings.auth_enabled and self._require_user() is None:
                    return
                return self.send_json(200, {"logged_out": True})

            if path.startswith("/api/"):
                ip = security.client_ip(self)
                if not self._allow(
                    "api:" + ip,
                    config.settings.rate_limit_api,
                    config.settings.rate_limit_api_window,
                ):
                    return
                user = self._require_user()
                if user is None:
                    return
                return self._api_post(path, body, user)

            return self.send_json(404, {"error": "not found"})

        except Exception:
            logging_setup.get_logger().error(
                "unhandled error in POST",
                exc_info=True,
                extra={"request_id": self._rid(), "path": path, "client": self.address_string()},
            )
            self.send_json(500, {"error": "internal server error"})
        finally:
            self._log_request("POST", path, started)

    def _api_post(self, path, body, user):
        if path == "/api/sessions":
            session_id = db.new_session_id()
            db.create_session(session_id, user_id=user["id"])
            return self.send_json(200, {"session_id": session_id})

        if path.startswith("/api/sessions/") and path.endswith("/messages"):
            session_id = path.split("/api/sessions/", 1)[1].rsplit("/messages", 1)[0]
            return self.handle_message(session_id, body, user)

        if path == "/api/account/delete":
            result = db.delete_user_data(user["id"])
            logging_setup.log_event(
                "account deleted", request_id=self._rid(), user_id=user["id"]
            )
            return self.send_json(200, {"deleted": True, "removed": result})

        if path == "/api/actions/confirm":
            return self.handle_confirm(body, user)

        if path == "/api/actions/decline":
            return self.handle_decline(body, user)

        if path == "/api/feedback":
            return self.handle_feedback(body, user)

        if path == "/api/audit/tamper":
            if not self._is_supervisor(user):
                return self.send_json(403, {"error": "supervisor role required"})
            if not config.settings.demo_mode:
                return self.send_json(
                    403, {"error": "tamper simulation is disabled outside demo mode"}
                )
            audit_id = body.get("audit_id")
            if not audit_id:
                return self.send_json(400, {"error": "audit_id is required"})
            changed = db.tamper_demo(audit_id)
            if not changed:
                return self.send_json(404, {"error": "unknown audit record"})
            return self.send_json(200, {"tampered": audit_id})

        return self.send_json(404, {"error": "not found"})

    # ---------- orchestration ----------
    def handle_message(self, session_id, body, user):
        text = (body.get("text") or "").strip()
        if not text:
            return self.send_json(400, {"error": "text is required"})
        if len(text) > config.settings.max_message_chars:
            return self.send_json(
                413,
                {"error": f"message exceeds {config.settings.max_message_chars} characters"},
            )

        session = db.get_session(session_id)
        if session:
            if not self._may_access_session(user, session):
                return self.send_json(404, {"error": "unknown session"})
        else:
            db.create_session(session_id, user_id=user["id"])
        db.add_message(session_id, "user", text)

        classification = agents.classify_intent(text)
        llm_used = False

        refinement = llm_intent.classify(text)
        if refinement:
            # The LLM may override the label, but the rule-based pass stays the
            # baseline. Only accept a refinement when it disagrees cleanly.
            if refinement["intent"] != classification["intent"]:
                classification = {
                    "intent": refinement["intent"],
                    "label": agents.INTENT_LABELS.get(refinement["intent"], refinement["label"]),
                    "confidence": refinement["confidence"],
                    "secondary_intents": classification.get("secondary_intents", []),
                    "ambiguous": classification.get("ambiguous", False),
                }
            else:
                classification["confidence"] = max(
                    classification["confidence"], refinement["confidence"]
                )
            llm_used = True

        intent_name = classification["intent"]
        sentiment = agents.detect_sentiment(text)
        risk = agents.risk_assessment(
            intent_name, sentiment, classification.get("ambiguous", False)
        )

        db.update_session(
            session_id,
            flow=classification["label"],
            sentiment=sentiment["score"],
        )

        if intent_name == "HUMAN_REQUEST":
            return self.escalate(
                session_id,
                text,
                1.0,
                "Explicit human request",
                agents.explain("ESCALATION_HUMAN_REQUEST"),
                sentiment,
                risk,
            )

        # Frustration outranks an unclassified message: a furious customer gets
        # a human whether or not the intent was understood. Handing an angry
        # person to a bot is exactly the failure mode this guards against.
        if sentiment["score"] >= agents.FRUSTRATION_ESCALATION_THRESHOLD:
            return self.escalate(
                session_id,
                text,
                classification["confidence"],
                "High customer frustration detected",
                agents.explain(
                    "ESCALATION_FRUSTRATION",
                    signals=", ".join(sentiment["signals"]) or "negative language",
                ),
                sentiment,
                risk,
            )

        if intent_name == "UNKNOWN":
            return self.escalate(
                session_id,
                text,
                classification["confidence"],
                "Low-confidence intent classification",
                agents.explain(
                    "ESCALATION_LOW_CONFIDENCE",
                    threshold=agents.AUTONOMY_CONFIDENCE_THRESHOLD,
                ),
                sentiment,
                risk,
            )

        resolution, rationale = self.resolve(intent_name, text, session_id)
        if resolution is None:
            return self.escalate(
                session_id,
                text,
                classification["confidence"],
                "No resolver produced a decision",
                agents.explain("ESCALATION_LOW_CONFIDENCE",
                               threshold=agents.AUTONOMY_CONFIDENCE_THRESHOLD),
                sentiment,
                risk,
            )

        if not resolution["autonomous"]:
            return self.escalate(
                session_id,
                text,
                resolution["confidence"],
                f"{resolution['action']} exceeds the autonomous threshold",
                rationale,
                sentiment,
                risk,
                resolution,
            )

        return self.propose(
            session_id, intent_name, resolution, rationale, risk, sentiment, llm_used
        )

    def resolve(self, intent_name, text, session_id):
        """Run the matching domain resolver. Returns ``(resolution, rationale)``."""
        if intent_name == "LOST_STOLEN":
            resolution = agents.resolve_lost_stolen()
            rationale = agents.explain("CARD_REISSUE", policy=resolution["policy"])
            return resolution, rationale

        if intent_name == "FEE_REVERSAL":
            amount = agents.extract_amount(text)
            resolution = agents.resolve_fee_reversal(amount)
            payload = resolution["payload"]
            if not resolution["autonomous"]:
                key = "FEE_REVERSAL_DENIED"
            elif payload.get("amount_assumed"):
                key = "FEE_REVERSAL_ASSUMED"
            else:
                key = "FEE_REVERSAL"
            rationale = agents.explain(
                key,
                amount=payload["amount"],
                limit=agents.FEE_WAIVER_LIMIT,
                policy=resolution["policy"],
            )
            return resolution, rationale

        if intent_name == "CREDIT_LIMIT":
            resolution = agents.resolve_credit_limit(seed=session_id)
            rationale = agents.explain("CREDIT_LIMIT_PREVIEW", **resolution["payload"])
            return resolution, rationale

        if intent_name == "DISPUTE":
            amount = agents.extract_amount(text)
            resolution = agents.resolve_dispute(amount)
            payload = resolution["payload"]
            if not resolution["autonomous"]:
                key = "DISPUTE_DENIED"
            elif payload.get("amount_assumed"):
                key = "DISPUTE_ASSUMED"
            else:
                key = "DISPUTE_PROVISIONAL_CREDIT"
            rationale = agents.explain(
                key,
                amount=payload["amount"],
                limit=agents.DISPUTE_AUTONOMOUS_LIMIT,
                policy=resolution["policy"],
            )
            return resolution, rationale

        return None, ""

    def propose(self, session_id, intent_name, resolution, rationale, risk, sentiment, llm_used):
        gate = agents.policy_gate(resolution["action"], resolution["payload"])
        if not gate["passed"]:
            return self.escalate(
                session_id,
                "",
                resolution["confidence"],
                "Policy Gateway rejected proposed action: " + gate["reason"],
                rationale,
                sentiment,
                risk,
                resolution,
            )

        assumed = bool(resolution["payload"].get("amount_assumed"))
        quality = agents.quality_score(
            resolution["confidence"],
            policy_passed=True,
            escalated=False,
            sentiment=sentiment,
            ambiguous=False,
            assumed_amount=assumed,
        )
        csat = agents.predict_csat(
            resolution["confidence"],
            sentiment=sentiment,
            resolved=False,
            escalated=False,
            quality=quality,
            assumed_amount=assumed,
        )

        db.update_session(
            session_id,
            status="Awaiting Confirmation",
            confidence=resolution["confidence"],
            quality_score=quality["score"],
        )
        db.add_message(session_id, "agent", resolution["summary"], meta={
            "action": resolution["action"],
            "confidence": resolution["confidence"],
        })

        return self.send_json(
            200,
            {
                "type": "proposal",
                "session_id": session_id,
                "intent": intent_name,
                "action": resolution["action"],
                "summary": resolution["summary"],
                "confidence": resolution["confidence"],
                "policy": resolution["policy"],
                "rationale": rationale,
                "payload": resolution["payload"],
                "step_up_required": risk["step_up_required"],
                "autonomous": resolution["autonomous"],
                "sentiment": sentiment,
                "quality": quality,
                "csat": csat,
                "llm_used": llm_used,
            },
        )

    def escalate(self, session_id, text, confidence, reason, rationale,
                 sentiment=None, risk=None, resolution=None):
        sentiment = sentiment or {"score": 0.0, "label": "neutral", "signals": []}

        assumed = bool(resolution and resolution.get("payload", {}).get("amount_assumed"))
        quality = agents.quality_score(
            confidence,
            policy_passed=True,
            escalated=True,
            sentiment=sentiment,
            assumed_amount=assumed,
        )
        csat = agents.predict_csat(
            confidence,
            sentiment=sentiment,
            escalated=True,
            quality=quality,
            assumed_amount=assumed,
        )

        context = agents.build_escalation_context(
            session_id, text, confidence, reason, sentiment, quality
        )
        entry = db.write_audit(
            session_id,
            "ESCALATION",
            context,
            confidence,
            policy="ESCALATION-POLICY",
            rationale=rationale,
        )
        db.update_session(
            session_id,
            status="Escalated",
            confidence=confidence,
            escalated=1,
            escalation_reason=reason,
            quality_score=quality["score"],
        )

        return self.send_json(
            200,
            {
                "type": "escalation",
                "session_id": session_id,
                "reason": reason,
                "rationale": rationale,
                "context": context,
                "quality": quality,
                "csat": csat,
                "action": (resolution or {}).get("action"),
                "payload": (resolution or {}).get("payload", {}),
                "confidence": confidence,
                "audit": entry,
            },
        )

    def handle_confirm(self, body, user):
        session_id = body.get("session_id")
        action = body.get("action")
        payload = body.get("payload", {}) or {}
        confidence = body.get("confidence", 0.0)
        policy = body.get("policy", "")
        rationale = body.get("rationale", "")

        if not session_id or not action:
            return self.send_json(400, {"error": "session_id and action are required"})
        session = db.get_session(session_id)
        if not session:
            return self.send_json(404, {"error": "unknown session"})
        if not self._may_access_session(user, session):
            return self.send_json(404, {"error": "unknown session"})

        # Re-check policy at execution time. The proposal may have been crafted
        # by a caller, so client-supplied numbers are never trusted.
        gate = agents.policy_gate(action, payload)
        if not gate["passed"]:
            return self.send_json(
                403, {"error": "Policy Gateway rejected execution: " + gate["reason"]}
            )

        entry = db.write_audit(
            session_id, action, payload, confidence, policy=policy, rationale=rationale
        )
        db.update_session(session_id, status="Resolved", resolved=1, ended_at=time.time())
        db.add_message(
            session_id, "system", f"Action {action} executed and logged as {entry['id']}"
        )

        return self.send_json(200, {"executed": True, "audit": entry})

    def handle_decline(self, body, user):
        session_id = body.get("session_id")
        reason = body.get("reason", "Customer declined the proposed action")
        session = db.get_session(session_id) if session_id else None
        if session and not self._may_access_session(user, session):
            return self.send_json(404, {"error": "unknown session"})
        if session:
            db.write_audit(
                session_id,
                "DECLINED",
                {"reason": reason},
                0.0,
                policy="CUSTOMER-DECLINED",
                rationale="Customer declined the proposed action; nothing was executed.",
            )
            db.update_session(session_id, status="Declined", ended_at=time.time())
            db.add_message(session_id, "system", "Customer declined. No action taken.")
        return self.send_json(200, {"declined": True})

    def handle_feedback(self, body, user):
        session_id = body.get("session_id")
        rating = body.get("rating")
        comment = body.get("comment", "")

        session = db.get_session(session_id) if session_id else None
        if not session:
            return self.send_json(404, {"error": "unknown session"})
        if not self._may_access_session(user, session):
            return self.send_json(404, {"error": "unknown session"})
        try:
            rating = int(rating)
        except (TypeError, ValueError):
            return self.send_json(400, {"error": "rating must be an integer 1-5"})
        if not 1 <= rating <= 5:
            return self.send_json(400, {"error": "rating must be between 1 and 5"})

        entry = db.add_feedback(session_id, rating, str(comment)[:500])
        return self.send_json(200, {"recorded": True, "feedback": entry})

    def _timeline(self, session_id, user):
        session = db.get_session(session_id)
        if not session:
            return self.send_json(404, {"error": "unknown session"})
        if not self._may_access_session(user, session):
            return self.send_json(404, {"error": "unknown session"})

        events = []
        for message in db.get_messages(session_id):
            events.append(
                {
                    "at": message["created_at"],
                    "kind": "message",
                    "role": message["role"],
                    "text": message["content"],
                }
            )
        for record in db.list_audit(session_id):
            events.append(
                {
                    "at": record["created_at"],
                    "kind": "audit",
                    "role": record["action"],
                    "text": record.get("rationale") or record["action"],
                    "audit_id": record["id"],
                    "confidence": record["confidence"],
                    "policy": record["policy"],
                }
            )
        events.sort(key=lambda e: e["at"])

        return self.send_json(
            200, {"session": session, "timeline": events, "count": len(events)}
        )


def main() -> int:
    logging_setup.configure(config.settings.log_level)

    # Fail loudly on an invalid configuration instead of running unsafely.
    problems = config.settings.validate()
    if problems:
        for problem in problems:
            print(f"[sentinel] CONFIG ERROR: {problem}", file=sys.stderr)
        print("[sentinel] refusing to start with an invalid configuration.", file=sys.stderr)
        return 2

    if config.settings.reset_db_on_start:
        db.init_db(reset=True)
    else:
        db.init_db()

    removed = db.reconcile_orphan_sessions()
    llm = llm_intent.status()

    logging_setup.log_event(
        "startup",
        port=config.settings.port,
        demo_mode=config.settings.demo_mode,
        auth_enabled=config.settings.auth_enabled,
        audit_secret_configured=config.settings.auth_secret_configured,
        llm_usable=llm["usable"],
        llm_provider=llm["provider"],
        orphans_cleared=removed,
        database=db.DB_PATH,
        frontend=os.path.abspath(FRONTEND_DIR),
    )

    server = ThreadingHTTPServer((config.settings.host, config.settings.port), Handler)
    server.daemon_threads = True
    server.trust_proxy_headers = config.settings.trust_proxy_headers

    # Graceful shutdown: SIGTERM (container stop) and Ctrl+C drain in-flight
    # requests instead of cutting them off mid-response.
    def _shutdown(signum=None, frame=None):
        logging_setup.log_event("shutdown requested", signal=signum)
        threading.Thread(target=server.shutdown, daemon=True).start()

    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            signal.signal(sig, _shutdown)
        except (ValueError, AttributeError, OSError):  # pragma: no cover
            pass

    print(f"Sentinel backend running at http://localhost:{config.settings.port}")
    print(f"Serving frontend from: {os.path.abspath(FRONTEND_DIR)}")
    print(f"Database: {db.DB_PATH}")
    print(f"Demo mode: {'ON' if config.settings.demo_mode else 'OFF'}")
    if config.settings.auth_enabled:
        note = "" if config.settings.auth_secret_configured else " (ephemeral secret; set AUTH_SECRET to persist tokens)"
        print(f"Authentication: ON{note}")
    else:
        print("Authentication: OFF (open demo)")
    try:
        server.serve_forever()
    finally:
        db.close_all_conns()
        logging_setup.log_event("shutdown complete")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
