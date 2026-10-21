import asyncio
import json
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

import session_tracking as tracking


class TrackingTests(unittest.IsolatedAsyncioTestCase):
    async def test_session_loss_is_explicitly_marked(self):
        class Context:
            def __init__(self):
                self.handlers = {}
                self.auth = True
            def on(self, event, callback): self.handlers[event] = callback
            async def cookies(self, url):
                return ([dict(name='sessionid', domain='.dola.com', value='x')]
                        if self.auth else [])
            async def close(self): self.handlers['close']()
        with tempfile.TemporaryDirectory() as tmp:
            logfile = Path(tmp) / 'trace.jsonl'
            with patch.object(tracking, 'LOG_PATH', logfile):
                context = Context(); run = tracking.trace_disk('fixture', tmp, 'before_launch')
                await tracking.track_session(context, 'fixture', tmp, run)
                context.auth = False
                await asyncio.sleep(2.1)
                await context.close()
            events = [json.loads(line) for line in logfile.read_text().splitlines()]
            self.assertTrue(any(e['event'] == 'session_lost' for e in events))
    async def test_lifecycle_and_secret_redaction(self):
        class Context:
            def __init__(self):
                self.handlers = {}

            def on(self, event, callback):
                self.handlers[event] = callback

            async def cookies(self, url):
                return [dict(name='sessionid', domain='.dola.com', value='SECRET_COOKIE',
                             expires=2000000000, secure=True, httpOnly=True)]

            async def close(self):
                self.handlers['close']()

        class Response:
            url = 'https://www.dola.com/auth/callback?token=SECRET_QUERY#SECRET_FRAGMENT'
            status = 200

            async def header_values(self, name):
                return ['sessionid=SECRET_HEADER; Max-Age=0; HttpOnly',
                        'other=SECRET_OTHER']

        with tempfile.TemporaryDirectory() as tmp:
            logfile = Path(tmp) / 'trace.jsonl'
            with patch.object(tracking, 'LOG_PATH', logfile):
                context = Context()
                run = tracking.trace_disk('fixture', tmp, 'before_launch')
                await tracking.track_session(context, 'fixture', tmp, run)
                context.handlers['response'](Response())
                await asyncio.sleep(.01)
                await context.close()
            text = logfile.read_text()
            self.assertNotIn('SECRET', text)
            events = [json.loads(line) for line in text.splitlines()]
            self.assertEqual(events[0]['event'], 'before_launch')
            self.assertEqual(events[-1]['event'], 'after_close_return')
            self.assertTrue(any(e['event'] == 'before_close' for e in events))
            response = next(e for e in events if e['event'] == 'auth_response')
            self.assertEqual(response['cookies'][0]['max-age'], '0')
            self.assertEqual({e['run'] for e in events}, {run})

    def test_readonly_disk_metadata(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / 'Default/Network/Cookies'
            path.parent.mkdir(parents=True)
            with sqlite3.connect(path) as db:
                db.execute('CREATE TABLE cookies(host_key,name,is_persistent,expires_utc,encrypted_value)')
                db.execute('INSERT INTO cookies VALUES(?,?,?,?,?)',
                           ('.dola.com', 'sessionid', 1, 12345, b'SECRET'))
            db.close()
            state = tracking.disk_snapshot(tmp)
            self.assertEqual(state[0]['auth'][0]['encrypted_bytes'], 6)
            self.assertNotIn('SECRET', json.dumps(state))
            self.assertEqual(state[0]['total'], 1)

    def test_unwritable_log_does_not_raise(self):
        with tempfile.TemporaryDirectory() as tmp:
            with patch.object(tracking, 'LOG_PATH', Path(tmp)):
                tracking.emit('fixture', 'run', 'test')


if __name__ == '__main__':
    unittest.main()
