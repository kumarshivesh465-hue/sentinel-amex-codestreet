# API Conventions

## Style
- REST-ish, JSON in/out, UTF-8.
- Paths are lowercase, plural nouns: `/api/sessions`, `/api/auth/login`.
- Sub-resources hang off the id: `/api/sessions/<id>/messages`.
- Methods: `GET` read, `POST` create/act, `OPTIONS` CORS preflight.

## Authentication
- Public: `GET /api/health`, `GET /api/health/live`, `GET /api/config`,
  `POST /api/auth/register`, `POST /api/auth/login`.
- Everything else requires `Authorization: Bearer <token>`.
- Missing/invalid/expired token → `401 {"error":"authentication required","code":"unauthorized"}`.

## One error shape
Every error response is:
```json
{ "error": "human readable message", "code": "stable_machine_code" }
```
Stable codes: `bad_request`, `unauthorized`, `forbidden`, `not_found`,
`conflict`, `payload_too_large`, `rate_limited`, `internal_error`.

Status codes used: `200` ok, `400` bad input, `401` unauthenticated,
`403` authenticated but not permitted, `404` not found / not yours,
`409` conflict (duplicate email), `413` message too large, `429` rate limited,
`500` server error (generic message; details go to logs only), `503` degraded health.

## Validation
- Validate at the boundary, before touching business logic or the database.
- Reject unknown/missing required fields with `400`.
- Enforce size limits (`MAX_MESSAGE_CHARS` → `413`).

## Pagination
List endpoints accept `?limit=<1..500>&offset=<n>` and return a `page` object:
```json
{ "records": [ ... ], "page": { "limit": 100, "offset": 0, "count": 3 } }
```
Applies to `/api/dashboard/sessions`, `/api/audit`, `/api/audit/<sid>`.

## Rate limiting
- `429` responses include a `Retry-After` header (seconds).
- Login/register are limited per IP **and** per email; the API per IP.

## Caching
- Authenticated JSON responses are `Cache-Control: no-store`.
- `index.html` is `no-cache`; other static assets are `public, max-age=3600`.

## Headers on every response
`X-Request-ID` (also in every log line), plus the security headers from
`backend/security.py` (CSP on HTML, nosniff, DENY frame, HSTS, referrer policy).
