"""Tests for the persistence layer, including audit-chain integrity.

Uses a temporary database so the real sentinel.db is never touched.
"""

import importlib
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))


class DatabaseTestCase(unittest.TestCase):
    def setUp(self):
        self.tmpdir = tempfile.TemporaryDirectory()
        self.db_path = os.path.join(self.tmpdir.name, "test.db")

        from backend import db as db_module

        self.db = importlib.reload(db_module)
        self.db.DB_PATH = self.db_path
        # Force a fresh per-thread connection bound to the temp path.
        self.db._local = type(self.db._local)()
        self.db.init_db(reset=True)

    def tearDown(self):
        self.db.close_all_conns()
        self.tmpdir.cleanup()


class SessionTests(DatabaseTestCase):
    def test_create_and_fetch(self):
        session_id = self.db.new_session_id()
        self.db.create_session(session_id)
        self.assertIsNotNone(self.db.get_session(session_id))

    def test_unknown_session_returns_none(self):
        self.assertIsNone(self.db.get_session("SES-NOPE"))

    def test_create_is_idempotent(self):
        session_id = self.db.new_session_id()
        self.db.create_session(session_id)
        self.db.create_session(session_id)
        self.assertEqual(len(self.db.list_sessions(include_empty=True)), 1)

    def test_empty_sessions_hidden_by_default(self):
        """Regression: page loads created junk dashboard rows."""
        self.db.create_session(self.db.new_session_id())
        self.assertEqual(len(self.db.list_sessions()), 0)

    def test_session_with_message_is_listed(self):
        session_id = self.db.new_session_id()
        self.db.create_session(session_id)
        self.db.add_message(session_id, "user", "hello")
        self.assertEqual(len(self.db.list_sessions()), 1)

    def test_orphan_reconciliation(self):
        used = self.db.new_session_id()
        self.db.create_session(used)
        self.db.add_message(used, "user", "hello")
        for _ in range(5):
            self.db.create_session(self.db.new_session_id())

        removed = self.db.reconcile_orphan_sessions()
        self.assertEqual(removed, 5)
        self.assertEqual(len(self.db.list_sessions(include_empty=True)), 1)

    def test_update_session_fields(self):
        session_id = self.db.new_session_id()
        self.db.create_session(session_id)
        self.db.update_session(session_id, status="Resolved", resolved=1)
        session = self.db.get_session(session_id)
        self.assertEqual(session["status"], "Resolved")
        self.assertEqual(session["resolved"], 1)

    def test_stats(self):
        resolved = self.db.new_session_id()
        self.db.create_session(resolved)
        self.db.update_session(resolved, resolved=1, confidence=0.9, quality_score=88.0)

        escalated = self.db.new_session_id()
        self.db.create_session(escalated)
        self.db.update_session(escalated, escalated=1, confidence=0.4, quality_score=50.0)

        stats = self.db.session_stats()
        self.assertEqual(stats["total_sessions"], 2)
        self.assertEqual(stats["resolved"], 1)
        self.assertEqual(stats["escalated"], 1)
        self.assertEqual(stats["resolution_rate"], 50.0)


class MessageTests(DatabaseTestCase):
    def test_ordering_is_stable(self):
        session_id = self.db.new_session_id()
        self.db.create_session(session_id)
        for i in range(5):
            self.db.add_message(session_id, "user", f"message {i}")

        contents = [m["content"] for m in self.db.get_messages(session_id)]
        self.assertEqual(contents, [f"message {i}" for i in range(5)])

    def test_meta_roundtrip(self):
        session_id = self.db.new_session_id()
        self.db.create_session(session_id)
        self.db.add_message(session_id, "agent", "hi", meta={"action": "CARD_REISSUE"})
        self.assertIsNotNone(self.db.get_messages(session_id))


class AuditChainTests(DatabaseTestCase):
    def test_first_record_links_to_genesis(self):
        entry = self.db.write_audit("SES-1", "CARD_REISSUE", {"a": 1}, 0.97)
        self.assertEqual(entry["prev_hash"], self.db.GENESIS_HASH)

    def test_chain_links_sequentially(self):
        first = self.db.write_audit("SES-1", "CARD_REISSUE", {"a": 1}, 0.97)
        second = self.db.write_audit("SES-1", "FEE_REVERSAL", {"amount": 50}, 0.91)
        self.assertEqual(second["prev_hash"], first["hash"])

    def test_verify_clean_chain(self):
        for i in range(10):
            self.db.write_audit(f"SES-{i}", "EVENT", {"i": i}, 0.9)
        ok, broken_at, count = self.db.verify_chain()
        self.assertTrue(ok)
        self.assertIsNone(broken_at)
        self.assertEqual(count, 10)

    def test_verify_empty_chain(self):
        ok, broken_at, count = self.db.verify_chain()
        self.assertTrue(ok)
        self.assertEqual(count, 0)

    def test_tampering_is_detected(self):
        entries = [self.db.write_audit("SES-1", "EVENT", {"i": i}, 0.9) for i in range(5)]
        self.db.tamper_demo(entries[2]["id"])

        ok, broken_at, _ = self.db.verify_chain()
        self.assertFalse(ok)
        self.assertEqual(broken_at, entries[2]["id"])

    def test_payload_edit_is_detected(self):
        """The chain must catch an edit to the stored data, not just the hash."""
        entry = self.db.write_audit("SES-1", "FEE_REVERSAL", {"amount": 50}, 0.9)
        conn = self.db.get_conn()
        conn.execute(
            "UPDATE audit_log SET payload=? WHERE id=?",
            ('{"amount": 5000}', entry["id"]),
        )
        conn.commit()

        ok, broken_at, _ = self.db.verify_chain()
        self.assertFalse(ok)
        self.assertEqual(broken_at, entry["id"])

    def test_tamper_unknown_id(self):
        self.assertFalse(self.db.tamper_demo("AUD-NOPE"))

    def test_hash_is_deterministic(self):
        record = {"a": 1, "b": [1, 2, 3]}
        self.assertEqual(self.db._hash_record(record), self.db._hash_record(record))

    def test_key_order_does_not_affect_hash(self):
        self.assertEqual(
            self.db._hash_record({"a": 1, "b": 2}),
            self.db._hash_record({"b": 2, "a": 1}),
        )


class MigrationTests(DatabaseTestCase):
    def test_legacy_database_gains_new_columns(self):
        """An existing sentinel.db from the prototype must keep working."""
        import sqlite3

        path = os.path.join(self.tmpdir.name, "legacy.db")
        conn = sqlite3.connect(path)
        conn.executescript(
            """
            CREATE TABLE sessions (
                id TEXT PRIMARY KEY, flow TEXT, status TEXT,
                confidence REAL, created_at REAL, updated_at REAL
            );
            CREATE TABLE messages (
                id INTEGER PRIMARY KEY AUTOINCREMENT, session_id TEXT,
                role TEXT, content TEXT, created_at REAL
            );
            CREATE TABLE audit_log (
                id TEXT PRIMARY KEY, session_id TEXT, action TEXT, payload TEXT,
                confidence REAL, policy TEXT, rationale TEXT, prev_hash TEXT,
                hash TEXT, created_at REAL
            );
            """
        )
        conn.execute(
            "INSERT INTO sessions (id, status, created_at, updated_at) VALUES ('SES-OLD','Active',1,1)"
        )
        conn.commit()
        conn.close()

        # Exercise the migration path on the module under test. Reloading the
        # module would create a second instance whose connection registry is
        # not the one the test helpers use.
        self.db.DB_PATH = path
        self.db.close_all_conns()
        self.db.init_db()

        session = self.db.get_session("SES-OLD")
        self.assertIsNotNone(session)
        self.assertIn("escalated", session)
        self.assertIn("quality_score", session)

        # And the upgraded database is fully usable.
        entry = self.db.write_audit("SES-OLD", "MIGRATED", {"ok": True}, 1.0)
        self.assertTrue(entry["id"].startswith("AUD-"))
        ok, _, _ = self.db.verify_chain()
        self.assertTrue(ok)

class UserTests(DatabaseTestCase):
    def test_create_and_fetch_user(self):
        user = self.db.create_user("a@b.com", "Alice", "supervisor", "salt", "hash")
        self.assertTrue(user["id"].startswith("USR-"))
        # Emails are matched case-insensitively.
        self.assertEqual(self.db.get_user_by_email("A@B.COM")["id"], user["id"])
        self.assertEqual(self.db.get_user(user["id"])["role"], "supervisor")

    def test_unknown_user(self):
        self.assertIsNone(self.db.get_user_by_email("nobody@example.com"))
        self.assertIsNone(self.db.get_user("USR-NOPE"))

    def test_duplicate_email_rejected(self):
        import sqlite3

        self.db.create_user("a@b.com", "Alice", "cardmember", "s", "h")
        with self.assertRaises(sqlite3.IntegrityError):
            self.db.create_user("a@b.com", "Bob", "cardmember", "s", "h")

    def test_count_users(self):
        self.assertEqual(self.db.count_users(), 0)
        self.db.create_user("a@b.com", "Alice", "cardmember", "s", "h")
        self.assertEqual(self.db.count_users(), 1)

    def test_sessions_are_scoped_to_owner(self):
        u1 = self.db.create_user("a@b.com", "A", "cardmember", "s", "h")["id"]
        u2 = self.db.create_user("c@d.com", "B", "cardmember", "s", "h")["id"]
        s1 = self.db.new_session_id()
        self.db.create_session(s1, user_id=u1)
        self.db.add_message(s1, "user", "hi")
        s2 = self.db.new_session_id()
        self.db.create_session(s2, user_id=u2)
        self.db.add_message(s2, "user", "yo")

        self.assertEqual([s["id"] for s in self.db.list_sessions(user_id=u1)], [s1])
        self.assertEqual(len(self.db.list_sessions()), 2)

    def test_stats_are_scoped_to_owner(self):
        u1 = self.db.create_user("a@b.com", "A", "cardmember", "s", "h")["id"]
        u2 = self.db.create_user("c@d.com", "B", "cardmember", "s", "h")["id"]
        s1 = self.db.new_session_id()
        self.db.create_session(s1, user_id=u1)
        self.db.update_session(s1, resolved=1, confidence=0.9)
        s2 = self.db.new_session_id()
        self.db.create_session(s2, user_id=u2)
        self.db.update_session(s2, resolved=1, confidence=0.5)

        self.assertEqual(self.db.session_stats(user_id=u1)["total_sessions"], 1)
        self.assertEqual(self.db.session_stats()["total_sessions"], 2)

    def test_export_user_data(self):
        user = self.db.create_user("a@b.com", "A", "cardmember", "s", "h")["id"]
        session_id = self.db.new_session_id()
        self.db.create_session(session_id, user_id=user)
        self.db.add_message(session_id, "user", "hi")
        exported = self.db.export_user_data(user)
        self.assertEqual(len(exported["sessions"]), 1)
        self.assertEqual(exported["sessions"][0]["session"]["id"], session_id)

    def test_delete_user_data(self):
        user = self.db.create_user("a@b.com", "A", "cardmember", "s", "h")["id"]
        session_id = self.db.new_session_id()
        self.db.create_session(session_id, user_id=user)
        self.db.add_message(session_id, "user", "hi")
        self.db.add_feedback(session_id, 5)

        removed = self.db.delete_user_data(user)
        self.assertEqual(removed["sessions"], 1)
        self.assertEqual(removed["messages"], 1)
        self.assertEqual(removed["user"], 1)
        self.assertIsNone(self.db.get_user(user))
        self.assertEqual(self.db.get_messages(session_id), [])

    def test_list_sessions_pagination(self):
        user = self.db.create_user("a@b.com", "A", "cardmember", "s", "h")["id"]
        for _ in range(5):
            session_id = self.db.new_session_id()
            self.db.create_session(session_id, user_id=user)
            self.db.add_message(session_id, "user", "hi")
        self.assertEqual(len(self.db.list_sessions(user_id=user, limit=2)), 2)
        self.assertEqual(len(self.db.list_sessions(user_id=user, limit=2, offset=4)), 1)


class FeedbackTests(DatabaseTestCase):
    def test_add_feedback(self):
        session_id = self.db.new_session_id()
        self.db.create_session(session_id)
        result = self.db.add_feedback(session_id, 5, "great")
        self.assertTrue(result["id"].startswith("FBK-"))

    def test_stats_include_rating(self):
        session_id = self.db.new_session_id()
        self.db.create_session(session_id)
        self.db.add_feedback(session_id, 4)
        self.assertEqual(self.db.session_stats()["avg_rating"], 4.0)


if __name__ == "__main__":
    unittest.main(verbosity=2)
