# Runbook — Incident Response

## Declare an incident when
Health checks fail for 2+ checks, error rate spikes, the audit chain reports
broken, or a security event (suspected credential stuffing, data exposure).

## Triage
1. Check `/api/health` (component status) and `/api/health/live`.
2. Pull recent logs and filter by `level=error` or `status>=500`; use the
   `request_id` from a failing response's `X-Request-ID` header to find every
   line for that request.
3. If `audit_chain.status=error`: do **not** delete records. Capture the
   `broken_at` id, snapshot the database, and treat it as a compliance event.
4. If it is a deploy regression, roll back (`docs/runbooks/rollback.md`).

## Communicate
- Open a single channel; post impact, start time, and updates every 30 minutes.
- Customers: post a status note (status page if available). Keep it factual.

## Post-incident review (template)
```
### Summary
### Impact (who, what, how long)
### Timeline (detection → mitigation → resolution)
### Root cause
### What went well / what didn't
### Action items (owner, due date)
```

## Security incident specifics
- Rotate `AUTH_SECRET` (invalidates all tokens) if tokens may have leaked.
- Preserve logs and the database snapshot for forensics; do not overwrite.
- If credentials are implicated, force a password reset for affected accounts
  (manual today — a password-reset flow is a tracked gap).
