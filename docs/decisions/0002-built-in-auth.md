# 0002 — Built-in authentication (PBKDF2 + signed tokens)

**Status:** Accepted; revisit before handling real cardmember PII

## Context
The app needs real login with roles and per-user data isolation, but the
zero-dependency constraint rules out a managed provider (Clerk/Auth0) and the
usual Python libraries.

## Decision
Implement auth with vetted primitives from the standard library:
- PBKDF2-HMAC-SHA256, 200k iterations, per-user random salt, for passwords.
- Stateless HMAC-SHA256 signed bearer tokens with an expiry.
- Roles (`cardmember`, `supervisor`) enforced on the server.
- Strict rate limiting on login/register; identical error for unknown email and
  wrong password to prevent account enumeration.

## Alternatives considered
- **Managed provider** — best practice, but a network dependency and an account;
  out of scope for a self-contained demo.
- **Server-side session table** — more revocable, but more state and no real
  benefit here; tokens are short-lived and `AUTH_SECRET` makes them durable.

## Consequences
- No third-party auth; the security review in `docs/security/threat-model.md`
  covers the residual risks (no email verification, no server-side revocation).
- A production deployment handling real PII should migrate to a managed provider
  or add email verification + token revocation.
