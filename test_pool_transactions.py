import ast
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import time
import unittest

class PoolTransactionTests(unittest.TestCase):
    def test_cleanup_releases_write_lock(self):
        tree = ast.parse(Path('browser_pool.py').read_text(encoding='utf-8'))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == 'BrowserPool')
        method = next(n for n in cls.body if isinstance(n, ast.FunctionDef) and n.name == '_clear_expired_rate_limits')
        scope = {'time': time}
        exec(compile(ast.Module(body=[method], type_ignores=[]), 'browser_pool.py', 'exec'), scope)
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'pool.db'
            with closing(sqlite3.connect(path)) as first, closing(sqlite3.connect(path, timeout=0)) as second:
                first.execute('CREATE TABLE accounts_meta (rate_limited_until REAL, limit_reason TEXT, quota_blocked_until REAL, quota_reason TEXT)')
                first.execute("INSERT INTO accounts_meta VALUES (0, '', 0, '')")
                first.commit()
                pool = type('Pool', (), {'_conn': first})()
                for expired in (False, True):
                    with self.subTest(expired=expired):
                        if expired:
                            first.execute("UPDATE accounts_meta SET rate_limited_until=1, limit_reason='expired'")
                            first.commit()
                        scope['_clear_expired_rate_limits'](pool)
                        self.assertFalse(first.in_transaction)
                        with second:
                            second.execute("UPDATE accounts_meta SET quota_reason='other connection'")
                        self.assertEqual(second.execute('SELECT rate_limited_until, limit_reason FROM accounts_meta').fetchone(), (0, ''))
