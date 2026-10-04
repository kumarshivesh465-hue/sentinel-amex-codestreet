# Hosting & Environments

## Recommended launch setup
One small container/VM running `python backend/server.py`, with the SQLite file
on a persistent volume, behind a TLS-terminating reverse proxy. Managed
platforms (Render, Railway, Fly.io) are the lowest-ops choice at this stage.

| Component | Launch | At 10× | Change needed at 10× |
| --- | --- | --- | --- |
| App process | 1 instance (256–512 MB) | 2–3 instances | Stateless already, but move rate limits + sessions to a shared store |
| Database | SQLite on a persistent volume | Postgres (Neon/Supabase/RDS) | Swap `db.py`'s SQLite calls for a Postgres driver behind the same interface |
| Static assets | Served by the app | CDN | Content-hash filenames + long cache lifetimes |
| TLS / DNS | Platform or Caddy | same | — |
| Rate limiting | In-process | Shared (Redis/Upstash) | Replace `security.RateLimiter` backend |
| Email (if added) | None yet | Resend/Postmark | Add before password reset |

Estimated cost at launch: **$0–10/month** on a small managed plan; ~$25–60 at
10× with a managed Postgres.

## Environments
- **local** — `python backend/server.py`, SQLite next to the code, `DEMO_MODE=true`.
- **staging** — separate instance and a **separate database**, `DEMO_MODE=false`.
- **production** — separate instance and database, `DEMO_MODE=false`,
  `AUTH_SECRET` set, billing alerts on.

Never point two environments at the same database.

## Environment configuration
- Every variable is documented in `.env.example`.
- Startup validation (`config.Settings.validate`) refuses to boot on a bad
  config, and specifically refuses `DEMO_MODE=false` with `AUTH_ENABLED=false`
  or with no `AUTH_SECRET`.
- No URLs, keys or ids are hard-coded; the frontend talks to the same origin.

## Production readiness checklist
- [ ] Custom domain + HTTPS (proxy or platform TLS).
- [ ] Security headers present (the app sends them; confirm the proxy doesn't strip them).
- [ ] `AUTH_SECRET` set from a secret manager, not committed.
- [ ] `DEMO_MODE=false` (tamper endpoint disabled).
- [ ] `TRUST_PROXY_HEADERS=true` **only** if the proxy overwrites `X-Forwarded-For`.
- [ ] Health checks wired: liveness `/api/health/live`, readiness `/api/health`.
- [ ] Database volume backed up per `docs/runbooks/backups.md`.
- [ ] Billing alerts configured on the provider.
