"""Persistence layer: sessions, messages, and the hash-chained audit log.

Concurrency notes
-----------------
``ThreadingHTTPServer`` serves each request on its own thread, and the previous
implementation opened a fresh SQLite connection per call with no timeout. Two
simultaneous writes could raise ``database is locked`` because SQLite's default
busy timeout is zero. This module now:

  * uses one connection per thread (``threading.local``) instead of per call,
  * enables WAL journaling so readers never block the writer,
  * sets a busy timeout so contention waits instead of failing,
  * serialises audit appends behind a lock, because appending to a hash chain
    is inherently a read-modify-write and must not interleave.
"""

from __future__ import annotations

import hashlib
import json
import os
import sqlite3
import threading
import time
import uuid

# Overridable so a deployment (or a test) can point at a different file, e.g. a
# mounted volume or a temporary database.
DB_PATH = os.environ.get("DB_PATH") or os.path.join(
    os.path.dirname(os.path.abspath(__file__)), "sentinel.db"
)
GENESIS_HASH = "0" * 64

_local = threading.local()
_audit_lock = threading.Lock()

# Every connection ever opened in this process. ThreadingHTTPServer hands each
# request to a new thread, so per-thread storage alone is not enough: a thread
# that exits without closing would leak an open file handle and keep the
# database locked (which on Windows blocks deleting or replacing the file).
_all_conns = set()
_conns_lock = threading.Lock()

SCHEMA = """
CREATE TABLE IF NOT EXISTS users (
    id TEXT PRIMARY KEY,
    email TEXT UNIQUE,
    name TEXT,
    role TEXT DEFAULT 'cardmember',
    pw_salt TEXT,
    pw_hash TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS sessions (
    id TEXT PRIMARY KEY,
    user_id TEXT,
    flow TEXT,
    status TEXT DEFAULT 'Active',
    confidence REAL,
    created_at REAL,
    updated_at REAL,
    escalated INTEGER DEFAULT 0,
    resolved INTEGER DEFAULT 0,
    escalation_reason TEXT,
    sentiment REAL,
    quality_score REAL,
    ended_at REAL
);

CREATE TABLE IF NOT EXISTS messages (
    id INTEGER PRIMARY KEY AUTOINCREMENT,
    session_id TEXT,
    role TEXT,
    content TEXT,
    meta TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS audit_log (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    action TEXT,
    payload TEXT,
    confidence REAL,
    policy TEXT,
    rationale TEXT,
    prev_hash TEXT,
    hash TEXT,
    created_at REAL
);

CREATE TABLE IF NOT EXISTS feedback (
    id TEXT PRIMARY KEY,
    session_id TEXT,
    rating INTEGER,
    comment TEXT,
    created_at REAL
);

CREATE INDEX IF NOT EXISTS idx_messages_session ON messages(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_audit_session ON audit_log(session_id, created_at);
CREATE INDEX IF NOT EXISTS idx_sessions_created ON sessions(created_at DESC);
CREATE UNIQUE INDEX IF NOT EXISTS idx_users_email ON users(email);
"""

# Columns added after the first release. Applied to pre-existing databases so
# an existing sentinel.db keeps working instead of erroring on the new code.
_MIGRATIONS = {
    "messages": {
        "meta": "TEXT",
    },
    "sessions": {
        "user_id": "TEXT",
        "escalated": "INTEGER DEFAULT 0",
        "resolved": "INTEGER DEFAULT 0",
        "escalation_reason": "TEXT",
        "sentiment": "REAL",
        "quality_score": "REAL",
        "ended_at": "REAL",
    },
}


def get_conn():
    """Per-thread connection with WAL enabled and sane locking behaviour."""
    conn = getattr(_local, "conn", None)
    if conn is None:
        conn = sqlite3.connect(DB_PATH, timeout=15.0)
        conn.row_factory = sqlite3.Row
        conn.execute("PRAGMA journal_mode=WAL")
        conn.execute("PRAGMA synchronous=NORMAL")
        conn.execute("PRAGMA busy_timeout=15000")
        conn.execute("PRAGMA foreign_keys=ON")
        _local.conn = conn
        with _conns_lock:
            _all_conns.add(conn)
    return conn


def close_all_conns() -> None:
    """Close every connection in this process. Used on shutdown and in tests."""
    with _conns_lock:
        conns = list(_all_conns)
        _all_conns.clear()
    for conn in conns:
        try:
            conn.close()
        except sqlite3.Error:
            pass
    _local.conn = None


def close_conn() -> None:
    """Close this thread's connection.

    Without this, a long-lived process accumulates one open handle per thread,
    and on Windows the database file stays locked so it cannot be deleted or
    replaced (which breaks cleanup in tests and any reset-the-database flow).
    """
    conn = getattr(_local, "conn", None)
    if conn is not None:
        try:
            conn.close()
        except sqlite3.Error:
            pass
        _local.conn = None


def _table_columns(conn, table: str) -> set:
    rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
    return {r["name"] for r in rows}


def init_db(reset: bool = False) -> None:
    if reset:
        for suffix in ("", "-wal", "-shm"):
            path = DB_PATH + suffix
            if os.path.exists(path):
                os.remove(path)

    conn = get_conn()
    conn.executescript(SCHEMA)

    for table, columns in _MIGRATIONS.items():
        existing = _table_columns(conn, table)
        for name, ddl in columns.items():
            if name not in existing:
                conn.execute(f"ALTER TABLE {table} ADD COLUMN {name} {ddl}")

    # Created after migrations, so it can rely on the user_id column existing on
    # databases that predate it.
    conn.execute(
        "CREATE INDEX IF NOT EXISTS idx_sessions_user ON sessions(user_id, created_at DESC)"
    )

    conn.commit()


# ---------------------------------------------------------------------------
# Sessions
# ---------------------------------------------------------------------------

def new_session_id() -> str:
    return "SES-" + uuid.uuid4().hex[:8].upper()


def create_session(session_id: str, flow: str = None, user_id: str = None) -> None:
    conn = get_conn()
    now = time.time()
    conn.execute(
        "INSERT OR IGNORE INTO sessions (id, user_id, flow, status, created_at, updated_at) VALUES (?,?,?,?,?,?)",
        (session_id, user_id, flow, "Active", now, now),
    )
    conn.commit()


def update_session(session_id: str, **fields) -> None:
    if not fields:
        return
    conn = get_conn()
    fields["updated_at"] = time.time()
    columns = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [session_id]
    conn.execute(f"UPDATE sessions SET {columns} WHERE id=?", values)
    conn.commit()


def get_session(session_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    return dict(row) if row else None


def list_sessions(
    include_empty: bool = False, user_id: str = None, limit: int = None, offset: int = 0
):
    """Sessions newest-first.

    Empty sessions (created by a page load but never used) are hidden by
    default so the supervisor dashboard is not flooded with junk rows. When
    ``user_id`` is given, only that owner's sessions are returned. ``limit``
    enables cursor-free pagination on the list endpoint.
    """
    conn = get_conn()
    where = []
    params = []
    if not include_empty:
        where.append("EXISTS (SELECT 1 FROM messages m WHERE m.session_id = s.id)")
    if user_id is not None:
        where.append("s.user_id = ?")
        params.append(user_id)
    clause = ("WHERE " + " AND ".join(where)) if where else ""
    pagination = ""
    if limit is not None:
        pagination = " LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset or 0)])
    rows = conn.execute(
        f"SELECT s.* FROM sessions s {clause} ORDER BY s.created_at DESC{pagination}",
        params,
    ).fetchall()
    return [dict(r) for r in rows]


def delete_session(session_id: str) -> None:
    conn = get_conn()
    conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
    conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    conn.commit()


def reconcile_orphan_sessions() -> int:
    """Delete sessions that were created but never used.

    Called at start-up: the old frontend created a session on every page load,
    which left the dashboard full of blank rows. Existing junk is cleaned up
    here, and the frontend no longer creates them eagerly.
    """
    conn = get_conn()
    cur = conn.execute(
        """DELETE FROM sessions
           WHERE NOT EXISTS (SELECT 1 FROM messages m WHERE m.session_id = sessions.id)"""
    )
    conn.commit()
    return cur.rowcount or 0


def count_users() -> int:
    conn = get_conn()
    return conn.execute("SELECT COUNT(*) AS c FROM users").fetchone()["c"]


def _feedback_for_session(session_id: str):
    conn = get_conn()
    rows = conn.execute(
        "SELECT id, rating, comment, created_at FROM feedback WHERE session_id=?",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


def export_user_data(user_id: str) -> dict:
    """Everything the product holds about one user, for a data-export request."""
    sessions = list_sessions(include_empty=True, user_id=user_id)
    return {
        "sessions": [
            {
                "session": session,
                "messages": get_messages(session["id"]),
                "audit": list_audit(session["id"]),
                "feedback": _feedback_for_session(session["id"]),
            }
            for session in sessions
        ]
    }


def delete_user_data(user_id: str) -> dict:
    """Delete a user and their sessions, messages and feedback.

    Audit records are deliberately retained: they are the immutable compliance
    trail and removing them would break the hash chain. They hold a session id
    and the decision that was made, not the user's credentials.
    """
    conn = get_conn()
    session_ids = [
        row["id"]
        for row in conn.execute("SELECT id FROM sessions WHERE user_id=?", (user_id,)).fetchall()
    ]
    removed = {"sessions": len(session_ids), "messages": 0, "feedback": 0, "user": 0}
    for session_id in session_ids:
        cur = conn.execute("DELETE FROM messages WHERE session_id=?", (session_id,))
        removed["messages"] += cur.rowcount or 0
        cur = conn.execute("DELETE FROM feedback WHERE session_id=?", (session_id,))
        removed["feedback"] += cur.rowcount or 0
        conn.execute("DELETE FROM sessions WHERE id=?", (session_id,))
    cur = conn.execute("DELETE FROM users WHERE id=?", (user_id,))
    removed["user"] = cur.rowcount or 0
    conn.commit()
    return removed


def create_user(email: str, name: str, role: str, pw_salt: str, pw_hash: str) -> dict:
    """Insert a user. Raises ``sqlite3.IntegrityError`` if the email exists."""
    conn = get_conn()
    user_id = "USR-" + uuid.uuid4().hex[:8].upper()
    email = (email or "").strip().lower()
    conn.execute(
        """INSERT INTO users (id, email, name, role, pw_salt, pw_hash, created_at)
           VALUES (?,?,?,?,?,?,?)""",
        (user_id, email, name, role, pw_salt, pw_hash, time.time()),
    )
    conn.commit()
    return {"id": user_id, "email": email, "name": name, "role": role}


def get_user_by_email(email: str):
    conn = get_conn()
    row = conn.execute(
        "SELECT * FROM users WHERE email = ?", ((email or "").strip().lower(),)
    ).fetchone()
    return dict(row) if row else None


def get_user(user_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM users WHERE id = ?", (user_id,)).fetchone()
    return dict(row) if row else None


def session_stats(user_id: str = None) -> dict:
    conn = get_conn()
    scope = ""
    params = []
    if user_id is not None:
        scope = "WHERE user_id = ?"
        params = [user_id]
    def _count(extra: str = "") -> int:
        clause, p = scope, list(params)
        if extra:
            clause = (clause + " AND " if clause else "WHERE ") + extra
        return conn.execute(f"SELECT COUNT(*) AS c FROM sessions {clause}", p).fetchone()["c"]

    total = _count()
    resolved = _count("resolved=1")
    escalated = _count("escalated=1")

    avg_clause = (scope + " AND " if scope else "WHERE ") + "confidence IS NOT NULL"
    row = conn.execute(
        f"SELECT AVG(confidence) AS a, AVG(quality_score) AS q FROM sessions {avg_clause}",
        params,
    ).fetchone()
    confidence = row["a"]
    quality = row["q"]

    if scope:
        rating_row = conn.execute(
            """SELECT AVG(f.rating) AS r, COUNT(f.id) AS c FROM feedback f
               JOIN sessions s ON s.id = f.session_id WHERE s.user_id = ?""",
            params,
        ).fetchone()
        avg_rating = rating_row["r"]
        feedback_count = rating_row["c"]
    else:
        avg_rating = conn.execute("SELECT AVG(rating) AS r FROM feedback").fetchone()["r"]
        feedback_count = conn.execute("SELECT COUNT(*) AS c FROM feedback").fetchone()["c"]

    resolution_rate = round(100.0 * resolved / total, 1) if total else 0.0

    return {
        "total_sessions": total,
        "resolved": resolved,
        "escalated": escalated,
        "resolution_rate": resolution_rate,
        "avg_confidence": round(confidence, 4) if confidence is not None else None,
        "avg_quality": round(quality, 1) if quality is not None else None,
        "avg_rating": round(avg_rating, 2) if avg_rating is not None else None,
        "feedback_count": feedback_count,
    }


# ---------------------------------------------------------------------------
# Messages
# ---------------------------------------------------------------------------

def add_message(session_id: str, role: str, content: str, meta: dict = None) -> None:
    conn = get_conn()
    conn.execute(
        "INSERT INTO messages (session_id, role, content, meta, created_at) VALUES (?,?,?,?,?)",
        (session_id, role, content, json.dumps(meta) if meta else None, time.time()),
    )
    conn.commit()


def get_messages(session_id: str):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM messages WHERE session_id=? ORDER BY created_at ASC, id ASC",
        (session_id,),
    ).fetchall()
    return [dict(r) for r in rows]


# ---------------------------------------------------------------------------
# Immutable audit chain
# ---------------------------------------------------------------------------

def _hash_record(record: dict) -> str:
    encoded = json.dumps(record, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def write_audit(
    session_id: str,
    action: str,
    payload: dict,
    confidence: float,
    policy: str = "",
    rationale: str = "",
):
    """Atomically append one record to the hash chain.

    The lock is required, not cosmetic. Reading the previous hash and writing
    the new record is a read-modify-write: two concurrent callers could read
    the same predecessor and each fork the chain, silently breaking integrity
    while every individual hash still looked valid.
    """
    with _audit_lock:
        conn = get_conn()
        last = conn.execute(
            "SELECT hash FROM audit_log ORDER BY created_at ASC, rowid ASC"
        ).fetchall()
        prev_hash = last[-1]["hash"] if last else GENESIS_HASH

        audit_id = "AUD-" + uuid.uuid4().hex[:8].upper()
        created_at = time.time()

        record_for_hash = {
            "session_id": session_id,
            "action": action,
            "payload": payload,
            "confidence": confidence,
            "prev_hash": prev_hash,
            "created_at": created_at,
        }
        record_hash = _hash_record(record_for_hash)

        conn.execute(
            """INSERT INTO audit_log
               (id, session_id, action, payload, confidence, policy, rationale, prev_hash, hash, created_at)
               VALUES (?,?,?,?,?,?,?,?,?,?)""",
            (
                audit_id,
                session_id,
                action,
                json.dumps(payload),
                confidence,
                policy,
                rationale,
                prev_hash,
                record_hash,
                created_at,
            ),
        )
        conn.commit()

        return {
            "id": audit_id,
            "session_id": session_id,
            "action": action,
            "payload": payload,
            "confidence": confidence,
            "policy": policy,
            "rationale": rationale,
            "prev_hash": prev_hash,
            "hash": record_hash,
            "created_at": created_at,
        }


def list_audit(session_id: str = None, user_id: str = None, limit: int = None, offset: int = 0):
    conn = get_conn()
    if session_id:
        sql = "SELECT * FROM audit_log WHERE session_id=? ORDER BY created_at ASC, rowid ASC"
        params = [session_id]
    elif user_id is not None:
        sql = (
            "SELECT a.* FROM audit_log a JOIN sessions s ON s.id = a.session_id "
            "WHERE s.user_id = ? ORDER BY a.created_at ASC, a.rowid ASC"
        )
        params = [user_id]
    else:
        sql = "SELECT * FROM audit_log ORDER BY created_at ASC, rowid ASC"
        params = []
    if limit is not None:
        sql += " LIMIT ? OFFSET ?"
        params.extend([int(limit), int(offset or 0)])
    rows = conn.execute(sql, params).fetchall()

    out = []
    for row in rows:
        record = dict(row)
        try:
            record["payload"] = json.loads(record["payload"])
        except (TypeError, json.JSONDecodeError):
            record["payload"] = {}
        out.append(record)
    return out


def verify_chain():
    """Recompute every hash and confirm each link points at its predecessor.

    Returns ``(ok, broken_at, count)``. This is the same check an external
    auditor would run against the stored records.
    """
    entries = list_audit()
    prev = GENESIS_HASH
    for entry in entries:
        record_for_hash = {
            "session_id": entry["session_id"],
            "action": entry["action"],
            "payload": entry["payload"],
            "confidence": entry["confidence"],
            "prev_hash": prev,
            "created_at": entry["created_at"],
        }
        if _hash_record(record_for_hash) != entry["hash"] or entry["prev_hash"] != prev:
            return False, entry["id"], len(entries)
        prev = entry["hash"]
    return True, None, len(entries)


def find_audit(audit_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM audit_log WHERE id=?", (audit_id,)).fetchone()
    if not row:
        return None
    record = dict(row)
    try:
        record["payload"] = json.loads(record["payload"])
    except (TypeError, json.JSONDecodeError):
        record["payload"] = {}
    return record


def get_audit_by_action(action: str):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM audit_log WHERE action=? ORDER BY created_at DESC", (action,)
    ).fetchall()
    return [dict(r) for r in rows]


def tamper_demo(audit_id: str) -> bool:
    """Corrupt one stored hash, for the live tamper demo only.

    Note this rewrites the *stored* hash but not the referenced payload, so
    verification fails exactly as it would for a real retroactive edit. This is
    reachable only through an endpoint that is disabled when ``DEMO_MODE=false``.
    """
    conn = get_conn()
    cur = conn.execute(
        "UPDATE audit_log SET hash = 'TAMPERED' || hash WHERE id=?", (audit_id,)
    )
    conn.commit()
    return bool(cur.rowcount)


# ---------------------------------------------------------------------------
# Feedback (CSAT)
# ---------------------------------------------------------------------------

def add_feedback(session_id: str, rating: int, comment: str = "") -> dict:
    conn = get_conn()
    feedback_id = "FBK-" + uuid.uuid4().hex[:8].upper()
    conn.execute(
        "INSERT INTO feedback (id, session_id, rating, comment, created_at) VALUES (?,?,?,?,?)",
        (feedback_id, session_id, rating, comment, time.time()),
    )
    conn.commit()
    return {"id": feedback_id, "session_id": session_id, "rating": rating}
