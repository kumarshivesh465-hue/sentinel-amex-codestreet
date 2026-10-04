#!/usr/bin/env python3
"""Live smoke test: boot the server exactly as the README says, then hit it.

This is deliberately separate from the unit tests. It runs
``python backend/server.py`` as a real subprocess on a real port, waits for the
health check, exercises the main API paths over HTTP, and shuts down. If this
passes, the documented run command works.

    python scripts/smoke_test.py
"""

import json
import os
import subprocess
import sys
import time
import urllib.error
import urllib.request

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("SMOKE_PORT", "8123"))
BASE = f"http://127.0.0.1:{PORT}"

PASSED = []
FAILED = []
TOKEN = None  # bearer token obtained after registering a smoke-test supervisor


def check(name, condition, detail=""):
    if condition:
        PASSED.append(name)
        print(f"  PASS  {name}")
    else:
        FAILED.append(name)
        print(f"  FAIL  {name}  {detail}")


def request(method, path, body=None, raw=False):
    url = BASE + path
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if TOKEN:
        headers["Authorization"] = "Bearer " + TOKEN
    req = urllib.request.Request(url, data=data, method=method, headers=headers)
    try:
        with urllib.request.urlopen(req, timeout=15) as response:
            payload = response.read().decode("utf-8", errors="replace")
            return response.status, payload if raw else json.loads(payload)
    except urllib.error.HTTPError as exc:
        payload = exc.read().decode("utf-8", errors="replace")
        try:
            return exc.code, json.loads(payload)
        except json.JSONDecodeError:
            return exc.code, payload


def wait_for_server(process, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        try:
            status, _ = request("GET", "/api/health")
            if status == 200:
                return True
        except Exception:
            pass
        time.sleep(0.4)
    return False


def main() -> int:
    env = dict(os.environ)
    env["PORT"] = str(PORT)
    env["DEMO_MODE"] = "true"

    # Run against a throwaway database. The smoke test deliberately tampers with
    # the audit chain, and it must never touch the developer's real sentinel.db.
    import tempfile

    tmpdir = tempfile.mkdtemp(prefix="sentinel-smoke-")
    env["DB_PATH"] = os.path.join(tmpdir, "smoke.db")

    print(f"Starting server on port {PORT} ...")
    process = subprocess.Popen(
        [sys.executable, os.path.join("backend", "server.py")],
        cwd=ROOT, env=env,
        stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True,
    )

    try:
        if not wait_for_server(process):
            print("Server did not become healthy.")
            output = process.stdout.read() if process.stdout else ""
            print(output[-2000:])
            return 1

        print("Server healthy. Running checks:\n")

        status, health = request("GET", "/api/health")
        check("health returns ok", status == 200 and health.get("status") == "ok")

        status, body = request("GET", "/", raw=True)
        check("frontend page served", status == 200 and "<html" in body.lower())

        status, config = request("GET", "/api/config")
        check("policy limits exposed", status == 200 and "fee_waiver_limit" in config.get("policy", {}))
        check("auth is advertised as enabled", config.get("auth_enabled") is True)

        # Real authentication is on by default: protected calls must 401 first.
        status, _ = request("GET", "/api/dashboard/sessions")
        check("unauthenticated request rejected", status == 401)

        import uuid

        status, reg = request("POST", "/api/auth/register", {
            "email": f"smoke-{uuid.uuid4().hex[:8]}@sentinel.test",
            "password": "passw0rd1", "name": "Smoke Supervisor", "role": "supervisor",
        })
        check("register issues a token", status == 200 and bool(reg.get("token")))

        global TOKEN
        TOKEN = reg.get("token")

        status, me = request("GET", "/api/auth/me")
        check("authenticated /me returns the user", status == 200 and me.get("user", {}).get("role") == "supervisor")

        status, login = request("POST", "/api/auth/login", {
            "email": reg.get("user", {}).get("email"), "password": "passw0rd1",
        })
        check("login returns a token", status == 200 and bool(login.get("token")))

        # Security headers + component health, checked while the audit chain is
        # still intact (the tamper test below deliberately degrades it).
        with urllib.request.urlopen(BASE + "/api/health", timeout=10) as resp:
            headers = {k.lower(): v for k, v in resp.getheaders()}
        check(
            "security headers present",
            headers.get("x-content-type-options") == "nosniff"
            and headers.get("x-frame-options") == "DENY"
            and headers.get("content-security-policy") is None,
        )
        check("request id echoed", headers.get("x-request-id", "").startswith("REQ-"))

        status, health_body = request("GET", "/api/health")
        check(
            "health reports components",
            status == 200 and "database" in health_body.get("components", {}),
        )

        status, live = request("GET", "/api/health/live")
        check("liveness probe", status == 200 and live.get("live") is True)

        status, session = request("POST", "/api/sessions")
        session_id = session.get("session_id")
        check("session created", status == 200 and bool(session_id))

        status, proposal = request("POST", f"/api/sessions/{session_id}/messages", {"text": "I lost my card"})
        check("lost card proposes reissue", proposal.get("action") == "CARD_REISSUE")

        status, escalated = request("POST", f"/api/sessions/{session_id}/messages", {"text": "waive my $5000 fee"})
        check("over-limit fee escalates", escalated.get("type") == "escalation")

        status, confirmed = request("POST", "/api/actions/confirm", {
            "session_id": session_id,
            "action": proposal.get("action"),
            "payload": proposal.get("payload", {}),
            "confidence": proposal.get("confidence", 0.9),
            "policy": proposal.get("policy", ""),
            "rationale": proposal.get("rationale", ""),
        })
        check("confirm executes and audits", status == 200 and confirmed.get("executed") is True)

        status, verified = request("GET", "/api/audit/verify")
        check("audit chain verifies", verified.get("ok") is True)

        status, tampered = request("POST", "/api/audit/tamper", {"audit_id": confirmed.get("audit", {}).get("id")})
        check("tamper accepted in demo mode", status == 200)

        status, verified = request("GET", "/api/audit/verify")
        check("tampering detected", verified.get("ok") is False)

        status, dashboard = request("GET", "/api/dashboard/sessions")
        check("dashboard has stats", status == 200 and "stats" in dashboard)

        status, timeline = request("GET", f"/api/sessions/{session_id}/timeline")
        check("timeline returns events", status == 200 and timeline.get("count", 0) > 0)

        status, feedback = request("POST", "/api/feedback", {"session_id": session_id, "rating": 5})
        check("feedback recorded", status == 200 and feedback.get("recorded") is True)

        status, proposal2 = request("POST", f"/api/sessions/{session_id}/messages", {"text": "I lost my card"})
        check("CSAT prediction is included", status == 200 and "csat" in proposal2 and "predicted" in proposal2.get("csat", {}))

        status, export = request("GET", "/api/account/export")
        check("data export works", status == 200 and "data" in export)

        status, paged = request("GET", "/api/dashboard/sessions?limit=1")
        check("pagination metadata present", status == 200 and paged.get("page", {}).get("limit") == 1)

    finally:
        process.terminate()
        import shutil

        shutil.rmtree(tmpdir, ignore_errors=True)
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()

    print(f"\n{len(PASSED)} passed, {len(FAILED)} failed")
    if FAILED:
        print("Failed: " + ", ".join(FAILED))
        return 1
    print("SMOKE TEST PASSED")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
