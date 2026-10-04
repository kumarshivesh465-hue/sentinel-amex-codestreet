# 0003 — Hash-chained append-only audit log

**Status:** Accepted

## Context
Every automated decision must be explainable and tamper-evident for compliance.

## Decision
Each audit record stores a SHA-256 hash of its canonical content (session id,
action, payload, confidence, previous hash, timestamp) plus the previous
record's hash. Verification recomputes the whole chain and reports the first
broken record. Appends are serialised behind a lock.

## Alternatives considered
- **Plain log table** — cannot detect retroactive edits.
- **External append-only store / blockchain** — over-engineered here.

## Consequences
- A single edited record breaks verification from that point.
- Account deletion retains audit records (the chain must stay intact); only the
  user's sessions/messages/feedback are removed.
- The tamper endpoint is exposed only in demo mode and only to supervisors.
