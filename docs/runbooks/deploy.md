# Runbook — Deploy

## First deploy
1. Provision a small instance/container with a persistent volume for the SQLite
   file. Choose a region close to your users.
2. Set environment variables (from `.env.example`):
   - `AUTH_SECRET` — 32+ random bytes: `python -c "import secrets; print(secrets.token_hex(32))"`
   - `DEMO_MODE=false`
   - `AUTH_ENABLED=true`
   - `LOG_LEVEL=INFO`
   - `PORT` / `HOST` as required by the platform
3. Put a TLS-terminating reverse proxy (Caddy/nginx/platform router) in front.
   If the proxy overwrites `X-Forwarded-For`, set `TRUST_PROXY_HEADERS=true`.
4. Start: `python backend/server.py`.
5. Verify: `curl -f https://<host>/api/health/live` → `200`; `/api/health` → `ok`.
6. Register the first supervisor account, then create a cardmember account and
   confirm isolation (see `docs/runbooks/incident.md` checks).
7. Enable backups (`docs/runbooks/backups.md`) and billing alerts.

## Every deploy
1. CI is green (`python run_tests.py`, `python scripts/smoke_test.py`).
2. Take a DB snapshot (or confirm the platform's automatic backup).
3. Deploy the new version. The process installs no dependencies.
4. Hit `/api/health` — expect `ok`; investigate a `degraded` response.
5. Watch logs for `level=error` for a few minutes.

## Rollback
See `docs/runbooks/rollback.md`.
