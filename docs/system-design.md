# System Design — Sentinel

## Problem statement
Cardmembers raise repetitive servicing requests (lost card, fee reversal,
credit-limit increase, billing dispute) that are slow and expensive to handle
one-by-one. Sentinel resolves the high-confidence, policy-compliant majority
autonomously and routes everything else to a human with full context — while
producing an audit trail a compliance team can verify.

## Users and roles
| Role | Who | Can do |
| --- | --- | --- |
| Cardmember | The customer | Chat with Sentinel, confirm/decline actions, rate the outcome, see their own cases and timeline |
| Supervisor | Servicing operations | Everything a cardmember can, plus all cases, the audit explorer, chain verification and the tamper demo |

Roles are enforced server-side. See `docs/permissions.md`.

## Core user flows
1. **Resolve a request** — cardmember sends a message; intent → risk → resolver →
   policy gate; a proposal is shown and the cardmember confirms or declines.
2. **Escalate** — low confidence, explicit human request, or high frustration
   routes the case to a human with transcript, reasoning and confidence.
3. **Review** — a supervisor watches live sessions, confidence, quality and CSAT
   on the dashboard.
4. **Audit** — anyone with supervisor rights verifies the hash chain; the demo
   tamper action proves detection works.
5. **Replay** — the timeline merges messages and audit events for one session.

## Functional requirements
- Classify servicing intent for the four supported categories.
- Deterministic, explainable decisions with confidence and policy citations.
- Append-only, tamper-evident audit records.
- Per-user data isolation; authenticated access only.
- CSAT prediction surfaced for each case.

## Non-functional requirements
- **Security**: server-side authn/authz, input validation, rate limiting,
  security headers, no secrets in logs.
- **Availability**: `/api/health` (component) and `/api/health/live` (liveness).
- **Performance**: sub-100ms typical API responses at demo scale (SQLite, indexed).
- **Operability**: structured JSON logs with request IDs; graceful shutdown.
- **Portability**: standard-library only; runs anywhere Python 3.11+ runs.

## Out of scope (deliberately)
- Payments/billing, real email delivery, SSO, multi-organisation tenancy.
- File uploads and object storage.
- Horizontal scaling and a shared cache/rate-limit store (see `docs/hosting.md`).

## Open questions
- Replace built-in auth with a managed provider before handling real PII?
- Add organisation-level tenancy if this becomes multi-team?
- Which queue/store for rate limits once the app runs on more than one instance?
