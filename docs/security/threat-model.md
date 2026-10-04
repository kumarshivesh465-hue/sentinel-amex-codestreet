# Threat Model — Sentinel

## Assets
- User credentials (password hashes, salts) and bearer tokens.
- Cardmember case data: messages, decisions, proposed financial actions.
- The audit trail (integrity is the product's core promise).

## Entry points
Every `/api/*` endpoint, the static file handler, and (in demo mode only) the
audit-tamper endpoint.

## Threats (OWASP-oriented) and mitigations
| # | Threat | Mitigation in this codebase | Residual |
| --- | --- | --- | --- |
| 1 | Broken access control / IDOR | Server-side auth on every data route; ownership check on every session object; cross-user → **404**; `user_id` scoping on all list/audit queries | — |
| 2 | Injection (SQL) | All queries use parameterised statements; no string-built SQL from user input | — |
| 3 | Injection (path traversal) | `resolve_static` normalises and rejects paths outside the frontend root | — |
| 4 | XSS | Frontend renders user text through `escapeHtml`; CSP restricts sources | CSP still allows `'unsafe-inline'` because the SPA uses inline handlers — see below |
| 5 | CSRF | Not cookie-based: auth uses `Authorization: Bearer`, so a cross-site form can't attach the token | — |
| 6 | Brute force / enumeration | Login/register rate limited per IP **and** per email; identical error for unknown email vs wrong password | No account lockout yet |
| 7 | Credential theft at rest | PBKDF2-HMAC-SHA256, 200k iterations, per-user salt; tokens are signed, not stored | DB file not encrypted at rest |
| 8 | Weak/leaked secrets | `AUTH_SECRET` required outside demo; `.env` git-ignored; gitleaks in pre-commit + CI | — |
| 9 | Verbose errors / info leak | Clients get a generic 500; details go to the log only; log redaction removes passwords/tokens/emails | — |
| 10 | Missing security headers | CSP, HSTS, nosniff, DENY frame, referrer, permissions policy applied centrally | — |
| 11 | SSRF / open redirect | No endpoint fetches a user-supplied URL; no redirects from input | — |
| 12 | Resource abuse / bills | Per-IP API limit; strict auth limits; request body cap | In-process limiter (single instance) |
| 13 | Stale/leaked cached data | API responses `no-store`; scoped queries | — |
| 14 | Denial via slow external call | LLM call is optional, timeout-bound, fails safe to rule-based | — |

## Known residual risks (tracked)
1. **CSP allows `'unsafe-inline'`** for scripts/styles because the SPA uses inline
   event handlers. Removing it requires moving handlers to `addEventListener` and
   the script to an external file. Documented follow-up.
2. **No email verification or password reset** — requires an email provider
   (out of scope for the self-contained demo).
3. **No server-side token revocation** — stateless tokens expire (TTL) but can't
   be revoked individually; deleted users are rejected because the user lookup fails.
4. **In-process rate-limit counters** — reset on restart and don't share across
   instances; move to a shared store when scaling out.
5. **SQLite file not encrypted at rest** — rely on host/disk encryption.

## Verify (run before every release)
- Paste `<script>alert(1)</script>` into every field — nothing executes.
- Copy an API request, swap in another user's session id — you get 404.
- Poke a Viewer/cardmember at a supervisor endpoint — you get 403.
- `grep` the logs for `password` / an email — zero raw hits.
- Deliberately tamper a record — chain verification fails.
