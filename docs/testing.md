# Testing

## Levels
| Level | What | Where |
| --- | --- | --- |
| Unit | Pure functions: agents, auth, money, security | `tests/test_agents.py`, `test_auth.py`, `test_security.py` |
| Integration (API + real DB) | Real threaded server on an ephemeral port against a temp SQLite DB | `tests/test_api.py`, `test_db.py` |
| End-to-end (live HTTP) | Boots `python backend/server.py` as a subprocess and exercises it | `scripts/smoke_test.py` |

## What is never mocked
The database, the auth checks, and the HTTP stack — integration tests use the
real thing. Only external services would be mocked, and there are none on the
hot path (the optional LLM call is disabled by default).

## Critical paths that must stay covered
Signup/login, per-user data isolation (cross-user → 404), the audit chain
(clean verify, tamper detection, concurrent writers), policy limits, rate
limiting, and the money boundary ($150.00 passes, $150.01 does not).

## Running
```bash
python run_tests.py            # all unit + integration
python run_tests.py -v         # verbose
python scripts/smoke_test.py   # live end-to-end
```
CI runs all three on every push and pull request (`.github/workflows/ci.yml`).

## Rules
- A bug fix starts with a failing test that reproduces it.
- Never weaken or delete an assertion to make a build pass; fix the code.
- No skipped tests without a written reason.
- We do not assert on mocks; we assert on real behaviour and real data.

## Coverage target
~70–80% on business logic (`agents.py`, `auth.py`, `db.py`, `security.py`).
The handlers are covered by the API integration tests.

## Manual checks (have been run)
- Browser end-to-end via Chrome DevTools Protocol: auth gate → register →
  proposal → confirm → audit → dashboard → timeline → role gating, 0 console errors.
- Deliberately tamper the chain → `/api/audit/verify` fails with the broken id.
