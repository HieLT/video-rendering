"""Offline two-request scheduling, quota, recovery and authenticated toggle tests."""
import ast
import asyncio
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock, patch

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field
from browser_pool import BrowserPool
from store import TaskStore
from video_schedule import VideoScheduler
from video_worker_ui import AccountLimitedError


class RequestModeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        root = Path(self.tmp.name)
        (root / 'accounts' / 'fixture').mkdir(parents=True)
        self.pool = BrowserPool(str(root / 'accounts'), str(root / 'pool.db'))
        self.pool._ensure_meta('fixture')
        with self.pool._conn:
            self.pool._conn.execute("UPDATE accounts_meta SET login_ok=1,auth_state='active',scheduling=1 WHERE name='fixture'")
        self.store = TaskStore(str(root / 'tasks.db'))
        self.scheduler = VideoScheduler(self.store, self.pool)
        for task in ('first', 'second', 'third'):
            self.store.create(task, 'seedance-2.5', 'fixture prompt', '16:9', 30)

    def tearDown(self):
        self.store._conn.close()
        self.pool._conn.close()
        self.tmp.cleanup()

    async def assign(self, task):
        return await self.scheduler.account(self.store.get(task))

    async def test_default_keeps_one_request(self):
        self.assertFalse(self.scheduler.dual_requests)
        self.assertTrue(await self.assign('first'))
        self.scheduler.claim_once(self.store.get('first'))
        self.assertFalse(await self.assign('second'))

    async def test_two_unfinished_requests_but_no_third(self):
        self.scheduler.set_dual_requests(True)
        self.assertTrue(await self.assign('first'))
        self.scheduler.claim_once(self.store.get('first'))
        self.store.update('first', status='processing', phase='waiting', conversation_id='chat-1')
        self.assertTrue(await self.assign('second'))
        self.scheduler.claim_once(self.store.get('second'))
        self.store.update('second', status='processing', phase='waiting', conversation_id='chat-2')
        self.assertFalse(await self.assign('third'))
        self.assertEqual(self.pool.used_today('fixture'), 2)
        self.assertEqual(self.scheduler.reservation_ids('fixture'), {'first', 'second'})
        self.assertTrue(await self.assign('first'))
        self.assertTrue(await self.assign('second'))

    async def test_second_dispatches_while_first_waits_for_dola(self):
        self.scheduler.set_dual_requests(True)
        submitted = []
        two_submitted = asyncio.Event()
        async def submit(row, paths):
            self.scheduler.claim_once(row)
            submitted.append(row['id'])
            self.store.update(row['id'], conversation_id='chat-' + row['id'])
            if len(submitted) == 2:
                two_submitted.set()
            return {'kind': 'accepted', 'poll': {'accepted': True, 'texts': []}}
        with patch.object(self.scheduler, 'submit', side_effect=submit), \
                patch.object(self.scheduler, 'references', AsyncMock(return_value=[])):
            tasks = [asyncio.create_task(self.scheduler.run(task, {})) for task in ('first', 'second', 'third')]
            try:
                await asyncio.wait_for(two_submitted.wait(), 3)
                await asyncio.sleep(.05)
                self.assertEqual(submitted, ['first', 'second'])
                self.assertEqual(self.store.get('first')['phase'], 'waiting')
                self.assertEqual(self.store.get('second')['phase'], 'waiting')
                self.assertEqual(self.store.get('third')['status'], 'queued')
                self.assertNotEqual(self.store.get('first')['conversation_id'], self.store.get('second')['conversation_id'])
            finally:
                for task in tasks:
                    task.cancel()
                await asyncio.gather(*tasks, return_exceptions=True)

    async def test_unclaimed_assignments_hold_slots_and_quota(self):
        self.scheduler.set_dual_requests(True)
        self.assertTrue(await self.assign('first'))
        self.assertTrue(await self.assign('second'))
        self.assertFalse(await self.assign('third'))
        self.scheduler.claim_once(self.store.get('first'))
        self.scheduler.claim_once(self.store.get('second'))
        self.scheduler.claim_once(self.store.get('second'))
        self.assertEqual(self.pool.used_today('fixture'), 2)
        self.store.update('third', account='fixture')
        with self.assertRaises(AccountLimitedError):
            self.scheduler.claim_once(self.store.get('third'))
        self.assertEqual(self.pool.used_today('fixture'), 2)

    async def test_existing_usage_leaves_only_one_quota_slot(self):
        self.assertTrue(await self.assign('first'))
        self.scheduler.claim_once(self.store.get('first'))
        self.store.update('first', status='completed')
        self.scheduler.release(self.store.get('first'))
        self.scheduler.set_dual_requests(True)
        self.assertTrue(await self.assign('second'))
        self.assertFalse(await self.assign('third'))

    async def test_release_either_task_keeps_other_reservation(self):
        self.scheduler.set_dual_requests(True)
        await self.assign('first')
        await self.assign('second')
        self.store.update('second', status='stopped')
        self.scheduler.release(self.store.get('second'))
        self.assertEqual(self.scheduler.reserved, {'fixture': 'first'})
        self.assertEqual(self.scheduler.reservation_ids('fixture'), {'first'})
        await self.assign('third')
        self.store.update('first', status='completed')
        self.scheduler.release(self.store.get('first'))
        self.assertEqual(self.scheduler.reserved, {'fixture': 'third'})

    async def test_restart_restores_both_and_toggle_off_keeps_running_tasks(self):
        self.scheduler.set_dual_requests(True)
        await self.assign('first')
        await self.assign('second')
        self.store.update('second', status='needs_recovery', phase='review')
        restarted = VideoScheduler(self.store, self.pool)
        self.assertTrue(restarted.dual_requests)
        restarted.recover_reservations()
        self.assertEqual(restarted.reservation_ids('fixture'), {'first', 'second'})
        self.assertTrue(restarted.can_resume('fixture', 'second'))
        restarted.set_dual_requests(False)
        self.assertTrue(await restarted.account(self.store.get('first')))
        self.assertTrue(restarted.can_resume('fixture', 'second'))
        self.assertFalse(await restarted.account(self.store.get('third')))
        self.assertFalse(VideoScheduler(self.store, self.pool).dual_requests)

    async def test_browser_profile_lock_still_exclusive(self):
        self.scheduler.set_dual_requests(True)
        async with self.pool.account_activity('fixture', 'generating'):
            self.assertFalse(await self.assign('first'))
        self.assertTrue(await self.assign('first'))

    def api_client(self):
        tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
        names = {'AccountRequestMode', 'admin_account_request_mode', 'admin_accounts'}
        selected = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.AsyncFunctionDef)) and node.name in names]
        def auth(key):
            if key != 'fixture-admin':
                raise HTTPException(401, 'Unauthorized')
        namespace = dict(app=FastAPI(), BaseModel=BaseModel, Field=Field, Header=Header,
                         _admin_auth=auth, scheduler=self.scheduler, pool=self.pool, JOBS={})
        exec(compile(ast.Module(body=selected, type_ignores=[]), 'server.py', 'exec'), namespace)
        return TestClient(namespace['app'])

    def test_authenticated_toggle_and_dashboard_capacity(self):
        client = self.api_client()
        path = '/api/admin/account-request-mode'
        self.assertEqual(client.patch(path, json={'enabled': True}).status_code, 401)
        headers = {'X-Admin-Key': 'fixture-admin'}
        self.assertEqual(client.patch(path, json={'enabled': 'yes'}, headers=headers).status_code, 422)
        self.assertEqual(client.patch(path, json={'enabled': True}, headers=headers).json(), {'dual_requests': True})
        self.store.update('first', account='fixture', status='processing', phase='waiting')
        self.scheduler.reserved['fixture'] = 'first'
        self.scheduler.claim_once(self.store.get('first'))
        result = client.get('/api/admin/accounts', headers=headers).json()
        self.assertTrue(result['dual_requests'])
        self.assertTrue(result['accounts'][0]['ready_to_generate'])
        self.assertTrue(result['accounts'][0]['busy'])
        self.assertEqual(result['accounts'][0]['active_requests'], 1)


if __name__ == '__main__':
    unittest.main()
