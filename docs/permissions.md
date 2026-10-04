# Permissions

Authorization is enforced on the **server** and is the source of truth. The
frontend hides actions a role cannot take, but that is cosmetic only.

## Roles
- **anonymous** — no valid token.
- **cardmember** — authenticated customer; sees only their own cases.
- **supervisor** — authenticated operations user; sees all cases and audit.

## Matrix (action × role)
| Action | anonymous | cardmember | supervisor |
| --- | --- | --- | --- |
| Register / login / health / config | ✅ | ✅ | ✅ |
| Create session / send message | ❌ | ✅ | ✅ |
| Read **own** session, timeline, audit, feedback | ❌ | ✅ | ✅ |
| Read **another user's** session | ❌ | ❌ (404) | ✅ |
| Confirm / decline own proposed action | ❌ | ✅ | ✅ |
| Submit feedback on own session | ❌ | ✅ | ✅ |
| Export own data / delete own account | ❌ | ✅ | ✅ |
| Dashboard (scoped to own sessions) | ❌ | ✅ | ✅ (all) |
| Audit explorer (all records) / chain verify | ❌ | ❌ | ✅ |
| Tamper demo (demo mode only) | ❌ | ❌ | ✅ |

## Rules every handler follows
1. **Authenticate** — missing/invalid token → `401`.
2. **Authorize the role** — e.g. chain verification → `403` for non-supervisors.
3. **Authorize the object** — a session's `user_id` must match the caller (or the
   caller is a supervisor). A mismatch returns **`404`**, never `403`, so the
   existence of another user's resource is not disclosed (broken object-level
   authorization / IDOR).
4. **Scope queries** — list/dashboard/audit queries filter by `user_id` unless
   the caller is a supervisor.

## Test coverage
`tests/test_api.py` (`AuthApiTests`) asserts: unauthenticated → 401, wrong role →
403, cross-user → 404, and that a cardmember's dashboard is scoped to their own
sessions.
