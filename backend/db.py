
import sqlite3
import hashlib
import json
import time
import uuid
import os

DB_PATH = os.path.join(os.path.dirname(__file__), "sentinel.db")
GENESIS_HASH = "0" * 64


def get_conn():
    conn = sqlite3.connect(DB_PATH)
    conn.row_factory = sqlite3.Row
    return conn


def init_db(reset=False):
    if reset and os.path.exists(DB_PATH):
        os.remove(DB_PATH)
    conn = get_conn()
    cur = conn.cursor()
    cur.executescript(
        """
        CREATE TABLE IF NOT EXISTS sessions (
            id TEXT PRIMARY KEY,
            flow TEXT,
            status TEXT DEFAULT 'Active',
            confidence REAL,
            created_at REAL,
            updated_at REAL
        );

        CREATE TABLE IF NOT EXISTS messages (
            id INTEGER PRIMARY KEY AUTOINCREMENT,
            session_id TEXT,
            role TEXT,
            content TEXT,
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
        """
    )
    conn.commit()
    conn.close()


def _hash_record(record: dict) -> str:
    encoded = json.dumps(record, sort_keys=True).encode("utf-8")
    return hashlib.sha256(encoded).hexdigest()


def new_session_id():
    return "SES-" + uuid.uuid4().hex[:8].upper()


def create_session(session_id: str):
    conn = get_conn()
    now = time.time()
    conn.execute(
        "INSERT INTO sessions (id, status, created_at, updated_at) VALUES (?,?,?,?)",
        (session_id, "Active", now, now),
    )
    conn.commit()
    conn.close()


def update_session(session_id: str, **fields):
    if not fields:
        return
    conn = get_conn()
    fields["updated_at"] = time.time()
    cols = ", ".join(f"{k}=?" for k in fields)
    values = list(fields.values()) + [session_id]
    conn.execute(f"UPDATE sessions SET {cols} WHERE id=?", values)
    conn.commit()
    conn.close()


def get_session(session_id: str):
    conn = get_conn()
    row = conn.execute("SELECT * FROM sessions WHERE id=?", (session_id,)).fetchone()
    conn.close()
    return dict(row) if row else None


def list_sessions():
    conn = get_conn()
    rows = conn.execute("SELECT * FROM sessions ORDER BY created_at DESC").fetchall()
    conn.close()
    return [dict(r) for r in rows]


def add_message(session_id: str, role: str, content: str):
    conn = get_conn()
    conn.execute(
        "INSERT INTO messages (session_id, role, content, created_at) VALUES (?,?,?,?)",
        (session_id, role, content, time.time()),
    )
    conn.commit()
    conn.close()


def get_messages(session_id: str):
    conn = get_conn()
    rows = conn.execute(
        "SELECT * FROM messages WHERE session_id=? ORDER BY created_at ASC", (session_id,)
    ).fetchall()
    conn.close()
    return [dict(r) for r in rows]


def write_audit(session_id: str, action: str, payload: dict, confidence: float,
                 policy: str = "", rationale: str = ""):
    """
    Append-only, hash-chained audit write.
    Each record's hash is a function of its own content + the previous
    record's hash, so any retroactive edit breaks the chain — this is
    the mechanism behind PRD section 23 (Audit Trail) and the
    /api/audit/verify endpoint.
    """
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
        (audit_id, session_id, action, json.dumps(payload), confidence, policy,
         rationale, prev_hash, record_hash, created_at),
    )
    conn.commit()
    conn.close()
    return {
        "id": audit_id, "session_id": session_id, "action": action, "payload": payload,
        "confidence": confidence, "policy": policy, "rationale": rationale,
        "prev_hash": prev_hash, "hash": record_hash, "created_at": created_at,
    }


def list_audit(session_id: str = None):
    conn = get_conn()
    if session_id:
        rows = conn.execute(
            "SELECT * FROM audit_log WHERE session_id=? ORDER BY created_at ASC, rowid ASC",
            (session_id,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT * FROM audit_log ORDER BY created_at ASC, rowid ASC"
        ).fetchall()
    conn.close()
    out = []
    for r in rows:
        d = dict(r)
        d["payload"] = json.loads(d["payload"])
        out.append(d)
    return out


def verify_chain():
    """
    Recomputes every hash in the chain from stored content and compares
    it against what's stored — the same check a compliance auditor's
    tool would run. Returns (ok: bool, broken_at: str|None).
    """
    entries = list_audit()
    prev = GENESIS_HASH
    for e in entries:
        record_for_hash = {
            "session_id": e["session_id"],
            "action": e["action"],
            "payload": e["payload"],
            "confidence": e["confidence"],
            "prev_hash": prev,
            "created_at": e["created_at"],
        }
        recomputed = _hash_record(record_for_hash)
        if recomputed != e["hash"] or e["prev_hash"] != prev:
            return False, e["id"]
        prev = e["hash"]
    return True, None


def tamper_demo(audit_id: str):
    """Intentionally corrupts one record's stored hash — for the live
    'Simulate Tampering' demo only. Never exposed outside this demo build."""
    conn = get_conn()
    conn.execute("UPDATE audit_log SET hash = 'TAMPERED' || hash WHERE id=?", (audit_id,))
    conn.commit()
    conn.close()
