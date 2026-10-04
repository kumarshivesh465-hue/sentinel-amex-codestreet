# Caching Strategy

## Rules (non-negotiable)
1. **Authenticated responses are never cached at a shared layer.** All `/api/*`
   JSON is sent `Cache-Control: no-store`.
2. **Any cache of user data includes the `user_id` in its key**, and a user never
   receives another user's cached entry.
3. **Invalidate on write** — this app reads fresh from SQLite on every request,
   so there is effectively no application cache to invalidate today.

## Where things are cached today
| Content | Where | TTL / header | Invalidation |
| --- | --- | --- | --- |
| `index.html` | Browser | `Cache-Control: no-cache` (revalidate) | Deploy changes the file |
| Other static assets | Browser | `public, max-age=3600` | Rename or add a hash to the filename |
| API JSON | Not cached | `no-store` | n/a |
| Health | Not cached | `no-store` | n/a |

## Recommended next steps (when traffic justifies it)
- Serve static assets from a CDN with **content-hashed filenames** and
  `max-age=31536000, immutable`.
- If a cache is introduced for expensive reads, use **cache-aside with keys that
  include `user_id`**, and invalidate on every write that affects the data. Never
  cache the audit log.
- Optimise the bundled fonts (`docs`/frontend) and remove the Google Fonts
  request for a fully self-hosted, offline-capable page.

## Verify
- Two users in different browsers never see each other's data, even after refresh.
- Update a record; the new value appears immediately (no stale cache).
