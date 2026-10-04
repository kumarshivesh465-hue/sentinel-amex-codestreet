# Performance & Scaling

## Measured (local, `scripts/load_test.py`)
Run: 300 requests, 20 concurrent workers, one process, SQLite, standard library.

```
300 requests across 20 workers in 2.54s (118 req/s)

endpoint                          p50      p95      p99    err%
GET /api/health                 40.0ms  537.4ms  589.5ms   0.0%
GET /api/dashboard/sessions     36.1ms  595.6ms 1528.6ms   0.0%
POST /api/auth/login           219.6ms  704.3ms  729.8ms   0.0%
POST /api/sessions/<id>/messages 40.9ms 553.7ms 1525.7ms   0.0%
```

## Reading the numbers
- **No errors under load** — the audit chain stays intact (see the concurrency test).
- **p50 is fast (~40ms)** for reads and the message pipeline.
- **p95/p99 are high** and the login p50 far exceeds the others. The bottleneck is
  **PBKDF2 password hashing (200k iterations)**: it is CPU-bound, so concurrent
  logins queue on the GIL instead of overlapping. This is the intended security
  cost, not a bug — hashing is *supposed* to be expensive.

## What breaks first at 10×/100×
1. **CPU-bound logins** — run multiple processes/instances behind a load balancer.
   The app is otherwise stateless (no in-memory sessions), so this is safe.
2. **In-process rate limiter** — counters don't share across instances; move to a
   shared store (Redis/Upstash) before horizontal scaling.
3. **SQLite single writer** — fine for this load; migrate to Postgres when write
   volume or multiple instances require it.
4. **Audit append lock** — serialises writes by design; keep the chain but move
   heavy work off the request path if it grows.

## Optimisation options (simplest first)
1. Lower PBKDF2 iterations only with a documented risk decision (do not).
2. Raise the process count / add instances (cheapest real win).
3. Move the login hash to a worker pool or use an async-friendly store.
4. Move rate limits and sessions to a shared store, then scale horizontally.

## Rule
Every optimisation must come with before/after numbers from `scripts/load_test.py`.
