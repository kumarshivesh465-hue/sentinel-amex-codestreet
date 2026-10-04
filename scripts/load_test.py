#!/usr/bin/env python3
"""Load test for the key endpoints, standard library only.

Boots the real server against a throwaway database, then drives concurrent
traffic and reports p50/p95/p99 latency and error rate per endpoint. This is the
"measure, don't guess" check from the scaling layer.

    python scripts/load_test.py
    LOAD_CONCURRENCY=50 LOAD_REQUESTS=1000 python scripts/load_test.py
"""

import json
import os
import statistics
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.request
import uuid

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
PORT = int(os.environ.get("LOAD_PORT", "8144"))
CONCURRENCY = int(os.environ.get("LOAD_CONCURRENCY", "20"))
REQUESTS = int(os.environ.get("LOAD_REQUESTS", "300"))
BASE = f"http://127.0.0.1:{PORT}"


def call(method, path, body=None, token=None):
    data = json.dumps(body).encode() if body is not None else None
    headers = {"Content-Type": "application/json"} if data else {}
    if token:
        headers["Authorization"] = "Bearer " + token
    req = urllib.request.Request(BASE + path, data=data, headers=headers, method=method)
    start = time.perf_counter()
    try:
        with urllib.request.urlopen(req, timeout=15) as resp:
            resp.read()
            code = resp.status
    except urllib.error.HTTPError as exc:
        exc.read()
        code = exc.code
    return (time.perf_counter() - start) * 1000.0, code


def wait_healthy(process, timeout=30):
    deadline = time.time() + timeout
    while time.time() < deadline:
        if process.poll() is not None:
            return False
        try:
            if call("GET", "/api/health")[1] == 200:
                return True
        except Exception:
            pass
        time.sleep(0.3)
    return False


def percentile(values, pct):
    if not values:
        return 0.0
    ordered = sorted(values)
    index = min(len(ordered) - 1, int(round((pct / 100.0) * len(ordered) + 0.5)) - 1)
    return ordered[max(0, index)]


def main() -> int:
    tmp = tempfile.mkdtemp(prefix="sentinel-load-")
    env = dict(os.environ, PORT=str(PORT), DEMO_MODE="true",
               DB_PATH=os.path.join(tmp, "load.db"),
               # Disable rate limits: this test measures throughput, not the limiter.
               RATE_LIMIT_API="1000000", RATE_LIMIT_AUTH="1000000")
    print(f"Starting server on {PORT} ...")
    process = subprocess.Popen(
        [sys.executable, os.path.join("backend", "server.py")], cwd=ROOT, env=env,
        stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL,
    )
    try:
        if not wait_healthy(process):
            print("server did not become healthy")
            return 1

        email = f"load-{uuid.uuid4().hex[:8]}@sentinel.test"
        _, code = call("POST", "/api/auth/register",
                       {"email": email, "password": "passw0rd1", "role": "supervisor"})
        if code != 200:
            print("registration failed", code)
            return 1
        # fetch a token
        body = json.dumps({"email": email, "password": "passw0rd1"}).encode()
        req = urllib.request.Request(BASE + "/api/auth/login", data=body,
                                     headers={"Content-Type": "application/json"}, method="POST")
        with urllib.request.urlopen(req, timeout=10) as resp:
            token = json.loads(resp.read().decode())["token"]
        req = urllib.request.Request(
            BASE + "/api/sessions",
            data=json.dumps({}).encode(),
            headers={"Content-Type": "application/json", "Authorization": "Bearer " + token},
            method="POST",
        )
        with urllib.request.urlopen(req, timeout=10) as resp:
            session_id = json.loads(resp.read().decode())["session_id"]
        call("POST", f"/api/sessions/{session_id}/messages", {"text": "I lost my card"}, token)

        plan = [
            ("GET /api/health", "GET", "/api/health", None, None),
            ("GET /api/dashboard/sessions", "GET", "/api/dashboard/sessions", None, token),
            ("POST /api/auth/login", "POST", "/api/auth/login",
             {"email": email, "password": "passw0rd1"}, None),
            ("POST /api/sessions/<id>/messages", "POST",
             f"/api/sessions/{session_id}/messages", {"text": "I lost my card"}, token),
        ]

        results = {name: {"lat": [], "errors": 0} for name, *_ in plan}
        lock = threading.Lock()
        counters = {"n": 0}

        def worker():
            while True:
                with lock:
                    if counters["n"] >= REQUESTS:
                        return
                    idx = counters["n"]
                    counters["n"] += 1
                name, method, path, body, tok = plan[idx % len(plan)]
                latency, code = call(method, path, body, tok)
                with lock:
                    results[name]["lat"].append(latency)
                    if code >= 400:
                        results[name]["errors"] += 1

        started = time.time()
        threads = [threading.Thread(target=worker) for _ in range(CONCURRENCY)]
        for t in threads:
            t.start()
        for t in threads:
            t.join()
        elapsed = time.time() - started

        print(f"\n{REQUESTS} requests across {CONCURRENCY} workers in {elapsed:.2f}s "
              f"({REQUESTS / elapsed:.0f} req/s)\n")
        print(f"{'endpoint':40} {'n':>5} {'p50':>8} {'p95':>8} {'p99':>8} {'err%':>6}")
        for name, data in results.items():
            lat = data["lat"]
            total = len(lat)
            err = 100.0 * data["errors"] / total if total else 0.0
            print(f"{name:40} {total:>5} {percentile(lat,50):>7.1f}ms "
                  f"{percentile(lat,95):>7.1f}ms {percentile(lat,99):>7.1f}ms {err:>5.1f}%")
        return 0
    finally:
        process.terminate()
        try:
            process.wait(timeout=10)
        except subprocess.TimeoutExpired:
            process.kill()
        import shutil

        shutil.rmtree(tmp, ignore_errors=True)


if __name__ == "__main__":
    raise SystemExit(main())
