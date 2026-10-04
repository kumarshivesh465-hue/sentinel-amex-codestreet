# Monitoring & Alerts

## Uptime checks
| Check | Endpoint | Interval | Healthy when |
| --- | --- | --- | --- |
| Liveness | `/api/health/live` | 30s | `200` and `live: true` |
| Readiness | `/api/health` | 60s | `200` and `status: "ok"` (DB + audit chain OK) |
| Login path | `POST /api/auth/login` (bad creds) | 5m | `401` (not 5xx/429 for a real user) |
| Core flow | send a message after login | 5m | `200` with a `type` |

`/api/health` returns component-level status (`database`, `audit_chain`) and
returns `503` with `status: "degraded"` if the audit chain is broken.

## Key metrics
**Technical:** error rate (5xx / total), p95 latency per path, DB file size and
lock timeouts, rate-limit rejections (429 count). All of these are derivable
from the structured JSON logs (`logging_setup.py`), which include `request_id`,
`method`, `path`, `status`, `duration_ms`, `user_id`, `role`.

**Business:** sessions created, autonomous resolutions, escalations, average
quality score, average predicted CSAT, feedback count (all on the dashboard).

## Alert rules (only what needs a human)
| Alert | Threshold | Severity | Notify |
| --- | --- | --- | --- |
| Readiness failing | 2 consecutive checks | Critical | on-call |
| Error rate | > 2% of requests over 10m | High | on-call |
| p95 latency | > 1s over 15m | Medium | team channel |
| Audit chain broken | any `audit_chain.status=error` | Critical | on-call + compliance |
| Auth limit saturation | 429s from one IP > 50/15m | Medium | security |

Each alert links to a runbook in `docs/runbooks/`.

## Logging
Structured JSON to stdout, one object per line, with a `request_id` echoed in
the `X-Request-ID` response header. Sensitive fields (password, token, secret,
authorization, email) are redacted before writing. `LOG_LEVEL=debug` must not be
used in production.

## What is not wired yet
An external error tracker (Sentry/Rollbar) and a public status page. Until then,
watch the logs and the health endpoint. Recommended next step: ship stdout logs
to a hosted log drain (Axiom/Better Stack) and add uptime pings from an external
service so an outage is noticed off-host.
