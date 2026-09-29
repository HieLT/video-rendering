"""Isolated migration tests without launching browsers or touching live databases."""
from contextlib import closing
import json
from datetime import date, datetime
from unittest.mock import patch
import ast
import sqlite3
import tempfile
import time
import unittest
import uuid
from pathlib import Path


def migration_class():
    tree = ast.parse(Path("browser_pool.py").read_text(encoding="utf-8"))
    cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BrowserPool")
    methods = {"_init_account_uuids", "_uuid_for_name", "_ensure_meta", "_meta", "resolve_account", "account_uuid", "public_account", "_rolling_usage", "_usage_key", "used_today", "_claim", "_mark_daily_limit", "_mark_quota_blocked", "_next_limit_reset"}
    cls.body = [n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name in methods]
    scope = dict(sqlite3=sqlite3, uuid=uuid, time=time, closing=closing, json=json, date=date, datetime=datetime, DAILY_LIMIT=2)
    exec(compile(ast.Module(body=[cls], type_ignores=[]), "browser_pool.py", "exec"), scope)
    return scope["BrowserPool"]


class AccountUuidTest(unittest.TestCase):
    def test_preservation_and_idempotence(self):
        with tempfile.TemporaryDirectory() as directory:
            path = Path(directory) / "pool.db"
            pool = migration_class()()
            pool._conn = sqlite3.connect(path)
            pool._conn.row_factory = sqlite3.Row
            pool._conn.execute("CREATE TABLE accounts_meta(name TEXT PRIMARY KEY, created_at REAL, email TEXT, note TEXT)")
            pool._conn.execute("INSERT INTO accounts_meta VALUES ('acc1', 123, 'user@example.com', 'keep')")
            pool._conn.commit()
            pool._init_account_uuids()
            first = dict(pool._meta("acc1"))
            self.assertEqual(first["email"], "user@example.com")
            self.assertEqual(first["created_at"], 123)
            self.assertEqual(first["note"], "keep")
            uuid.UUID(first["uuid"])
            self.assertEqual(pool.resolve_account(first["uuid"]), "acc1")
            self.assertEqual(pool.resolve_account("acc1"), "acc1")
            pool._init_account_uuids()
            self.assertEqual(dict(pool._meta("acc1")), first)
            new = str(uuid.uuid4())
            pool._ensure_meta(new)
            self.assertEqual(pool.account_uuid(new), new)
            self.assertEqual(pool.public_account({"name": "acc1", "uuid": first["uuid"]})["name"], first["uuid"])
            with closing(sqlite3.connect(str(path) + ".before_uuid.bak")) as backup:
                self.assertEqual(backup.execute("SELECT * FROM accounts_meta").fetchall(), [("acc1", 123, "user@example.com", "keep")])
            pool._conn.close()


class RollingUsageTest(unittest.TestCase):
    def setUp(self):
        self.pool = migration_class()()
        self.pool._conn = sqlite3.connect(":memory:")
        self.pool._conn.row_factory = sqlite3.Row
        self.pool._conn.executescript("""
            CREATE TABLE accounts_meta(name TEXT PRIMARY KEY, uuid TEXT, email TEXT, account_type TEXT, last_used_at REAL, rate_limited_until REAL DEFAULT 0, quota_blocked_until REAL DEFAULT 0, limit_reason TEXT, quota_reason TEXT);
            CREATE TABLE usage(account TEXT, day TEXT, used INTEGER);
            CREATE TABLE identity_usage(account TEXT, day TEXT, used INTEGER, PRIMARY KEY(account,day));
            CREATE TABLE account_usage(uuid TEXT PRIMARY KEY, used INTEGER, last_used_at REAL);
        """)
        self.last = datetime(2026, 9, 25, 23, 50).timestamp()
        self.pool._conn.execute("INSERT INTO accounts_meta(name,uuid,email,account_type,last_used_at) VALUES ('acc1','uuid-1','','unknown',?)", (self.last,))
        self.pool._conn.execute("INSERT INTO identity_usage VALUES (?,?,1)", (json.dumps(["profile", "acc1"]), "2026-09-25"))
        self.pool._conn.commit()

    def tearDown(self):
        self.pool._conn.close()

    def test_midnight_does_not_reset_and_exact_deadline_does(self):
        with patch("time.time", return_value=self.last+1200):
            self.assertEqual(self.pool.used_today("acc1"), 1)
        with patch("time.time", return_value=self.last+86400-1):
            self.assertEqual(self.pool.used_today("acc1"), 1)
        with patch("time.time", return_value=self.last+86400):
            self.assertEqual(self.pool.used_today("acc1"), 0)
            self.pool._claim("acc1")
            self.assertEqual(self.pool.used_today("acc1"), 1)
        self.assertEqual(self.pool._conn.execute("SELECT used FROM identity_usage").fetchone()[0], 1)

    def test_last_use_extends_reset_and_migration_is_idempotent(self):
        with patch("time.time", return_value=self.last+3600):
            self.pool._claim("acc1")
            self.assertEqual(self.pool.used_today("acc1"), 2)
            self.assertEqual(self.pool._next_limit_reset("acc1"), self.last+3600+86400)
        with patch("time.time", return_value=self.last+86400):
            self.assertEqual(self.pool.used_today("acc1"), 2)
        with patch("time.time", return_value=self.last+3600+86400):
            self.assertEqual(self.pool.used_today("acc1"), 0)

    def test_quota_block_does_not_revive_expired_usage(self):
        with patch("time.time", return_value=self.last+86400):
            self.pool._mark_quota_blocked("acc1")
            self.assertEqual(self.pool.used_today("acc1"), 0)
            self.assertEqual(self.pool._meta("acc1")["quota_blocked_until"], self.last+172800)

    def test_daily_limit_sets_24_hour_deadline(self):
        with patch("time.time", return_value=self.last+100):
            self.pool._mark_daily_limit("acc1")
            self.assertEqual(self.pool.used_today("acc1"), 2)
            self.assertEqual(self.pool._meta("acc1")["rate_limited_until"], self.last+86500)


class GeneratedTotalsTest(unittest.TestCase):
    def test_totals_pinned_to_uuid_and_keep_deleted_records(self):
        from store import TaskStore
        store = TaskStore(":memory:")
        try:
            store._conn.executemany("INSERT INTO tasks(id,account,status,created_at,deleted_at) VALUES (?,?,?,?,?)", [
                ("old", "acc1", "completed", time.time(), None),
                ("hidden", "acc1", "completed", time.time(), time.time()),
                ("failed", "acc1", "failed", time.time(), None),
                ("reset", "acc1", "completed", time.time(), None),
                ("uuid-task", "uuid-1", "completed", time.time(), None),
            ])
            store._conn.execute("INSERT INTO generated_reset_tasks VALUES ('reset')")
            store._conn.commit()
            store.bind_account_uuids([{"name":"acc1", "uuid":"uuid-1"}])
            self.assertEqual(store.stats()["per_account_total"], {"uuid-1":3})
            store.bind_account_uuids([{"name":"acc1", "uuid":"uuid-2"}])
            self.assertEqual(store.stats()["per_account_total"], {"uuid-1":3})
        finally:
            store._conn.close()


if __name__ == "__main__":
    unittest.main()
