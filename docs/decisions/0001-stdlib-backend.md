# 0001 — Standard-library-only backend (no web framework)

**Status:** Accepted

## Context
The service must be easy to run, audit and demo, and the team is small. A
framework (FastAPI/Flask) would provide routing, validation and middleware, but
also adds dependencies to install, pin and patch.

## Decision
Build on the Python standard library only (`http.server`, `sqlite3`, `hashlib`,
`hmac`, `json`). Implement routing, security headers, structured logging and
rate limiting ourselves in small, tested modules.

## Alternatives considered
- **FastAPI + uvicorn** — better ergonomics and automatic docs, but a runtime
  dependency tree and a rewrite of the existing tests.
- **Flask** — lighter, still a dependency; no compelling gain at this scale.

## Consequences
- Zero installs; `python backend/server.py` is the whole story.
- We own concerns a framework normally handles (CSP, rate limits, input
  handling); each is covered by tests and documented.
- Migrating to a framework later is a contained change because business logic
  lives in `agents.py`/`db.py`, not in the handlers.
