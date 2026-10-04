# Runbook — Backups & Restore

## Policy
- **What:** the SQLite database file (including `-wal` and `-shm` while running).
- **How often:** daily automated snapshot; before every deploy.
- **Retention:** 30 days.
- **Where:** platform volume snapshots or a copy to object storage — never the
  same volume only.

## Take a consistent backup (hot, no downtime)
SQLite's `.backup` produces a consistent copy while the app is running:
```bash
python - <<'PY'
import sqlite3, time
src = sqlite3.connect("backend/sentinel.db")
dst = sqlite3.connect(f"backup-sentinel-{int(time.time())}.db")
with dst:
    src.backup(dst)
print("backup written")
PY
```

## Restore (test this, don't assume it)
1. Stop the app (or point it at the new file).
2. Copy the backup to a **new** path — do not overwrite the live DB until verified:
   `cp backup-sentinel-<ts>.db restored.db`
3. Verify the restored file opens and its chain is intact:
   ```bash
   DB_PATH=restored.db python - <<'PY'
   from backend import db
   db.init_db()
   print(db.verify_chain())
   print(db.session_stats())
   PY
   ```
   Expect `(True, None, <n>)`.
4. Point `DB_PATH` at `restored.db` and restart, or swap the file into place.

## Verify
You have **actually restored once** and seen `verify_chain()` return `ok` — a
backup you have never restored is a hope, not a backup.
