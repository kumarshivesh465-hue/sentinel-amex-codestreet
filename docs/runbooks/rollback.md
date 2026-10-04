# Runbook — Rollback

## When
A deploy causes errors, failing health checks, or a regression in a core flow.

## Steps
1. **Stop the bleeding:** redeploy the previous known-good revision (platform
   "rollback" button, or `git revert <sha>` + redeploy).
2. **Restore the database if a migration ran.** The schema changes in
   `backend/db.py` are additive (`ALTER TABLE ADD COLUMN`), so older code keeps
   working against the newer schema in almost all cases — you usually do **not**
   need to restore. Only restore if data was corrupted:
   follow `docs/runbooks/backups.md` to restore a snapshot to a new file and
   point `DB_PATH` at it.
3. **Verify:** `/api/health` returns `ok`, `/api/audit/verify` returns
   `ok: true`, and a manual login + message round-trip succeeds.
4. **Post-incident:** write a short review (see `docs/runbooks/incident.md`).

## Practise
Roll back a staging deploy on purpose once per quarter so the steps stay fresh.
