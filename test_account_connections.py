"""Offline account routing/API tests; no real accounts or proxy traffic."""
import ast
import asyncio
import json
import time
import uuid
from pathlib import Path
import tempfile
import unittest
from unittest.mock import AsyncMock, Mock, patch

from fastapi import FastAPI, Header, HTTPException
from fastapi.testclient import TestClient
from pydantic import BaseModel, Field

import account_connections as connections
import browser
from browser_pool import BrowserPool


class ConnectionTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name)
        self.path = self.root / '.connections.local.json'
        self.path.write_text(json.dumps({'connections': {'fixture': {
            'label': 'Fixture proxy', 'server': 'http://127.0.0.1:9999',
            'username': 'fixture-user', 'password': 'fixture-secret'}}, 'accounts': {}}), encoding='utf-8')
        settings = patch.object(connections, 'SETTINGS_PATH', self.path)
        settings.start()
        self.addCleanup(settings.stop)
        self.addCleanup(self.tmp.cleanup)
        for name in ('one', 'two'):
            (self.root / 'accounts' / name).mkdir(parents=True)
        self.pool = BrowserPool(str(self.root / 'accounts'), str(self.root / 'pool.db'))
        for name in ('one', 'two'):
            self.pool._ensure_meta(name)
        self.addCleanup(self.pool._conn.close)
        app = FastAPI()
        self.scheduler = Mock()
        self.scheduler.reservation_ids.return_value = []
        self.scheduler.eligible.return_value = False
        self.scheduler.dual_requests = False
        self.jobs = {}
        self.web = {}
        def auth(key):
            if key != 'fixture-admin':
                raise HTTPException(401, 'Unauthorized')
        self.ns = dict(app=app, pool=self.pool, scheduler=self.scheduler, DOMAIN_WEB_QUEUE=None,
                       account_connections=connections, JOBS=self.jobs, WEB_SESSIONS=self.web,
                       _admin_auth=auth, Header=Header, HTTPException=HTTPException,
                       BaseModel=BaseModel, Field=Field)
        self.ns.update(uuid=uuid, time=time, asyncio=asyncio,
                       find_duplicate_account=Mock(return_value=None),
                       find_duplicate_login=Mock(return_value=None),
                       cookie_identity=Mock(return_value=''),
                       _run_add_job=AsyncMock(), ACCOUNT_IMPORTS={}, ACCOUNT_IMPORT_TASKS=set())
        from bulk_accounts import parse_accounts
        self.ns['parse_accounts'] = parse_accounts
        tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
        names = {'AccountConnectionAssignment', 'assign_account_connections', 'admin_accounts',
                 'AccountAdd', 'GoogleBulkAdd', 'admin_account_add', 'google_bulk_add', 'retry_account',
                 'ProxyImport', 'admin_proxies', 'admin_proxy_import', 'admin_proxy_delete',
                 'distribute_account_connections'}
        nodes = [n for n in tree.body if getattr(n, 'name', None) in names]
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'server.py', 'exec'), self.ns)
        self.http = TestClient(app)
        self.addCleanup(self.http.close)

    def assign(self, names, identifier='fixture', auth=True):
        return self.http.patch('/api/admin/account-connections',
            json={'accounts': names, 'connection_id': identifier},
            headers={'X-Admin-Key': 'fixture-admin'} if auth else {})

    def test_batch_persists_and_lists_without_credentials(self):
        ids = [self.pool.account_uuid(name) for name in ('one', 'two')]
        self.assertEqual(self.assign(ids).status_code, 200)
        self.assertEqual(connections.account_connection('one'), 'fixture')
        self.assertEqual(connections.account_connection('two'), 'fixture')
        data = self.http.get('/api/admin/accounts', headers={'X-Admin-Key': 'fixture-admin'}).json()
        self.assertEqual({a['connection_id'] for a in data['accounts']}, {'fixture'})
        self.assertNotIn('fixture-secret', json.dumps(data))
        self.assertNotIn('fixture-user', json.dumps(data))
        self.assertEqual(self.assign(ids, 'direct').status_code, 200)
        self.assertIsNone(connections.browser_proxy('one'))

    async def test_busy_or_reserved_batch_changes_nothing(self):
        lock = self.pool._locks.setdefault('two', asyncio.Lock())
        await lock.acquire()
        try:
            self.assertEqual(self.assign(['one', 'two']).status_code, 409)
            self.assertEqual(connections.account_connection('one'), 'direct')
        finally:
            lock.release()
        self.scheduler.reservation_ids.side_effect = lambda name: ['video'] if name == 'two' else []
        self.assertEqual(self.assign(['one', 'two']).status_code, 409)
        self.assertEqual(connections.account_connection('one'), 'direct')

    def test_invalid_unauthenticated_and_unknown_accounts(self):
        self.assertEqual(self.assign(['one'], auth=False).status_code, 401)
        self.assertEqual(self.assign(['one'], 'missing').status_code, 422)
        self.assertEqual(self.assign(['one', 'missing']).status_code, 404)
        self.assertEqual(connections.account_connection('one'), 'direct')

    def test_missing_or_corrupt_proxy_never_falls_back(self):
        connections.assign(['one'], 'fixture')
        data = json.loads(self.path.read_text())
        data['connections'].clear()
        self.path.write_text(json.dumps(data))
        with self.assertRaises(RuntimeError):
            connections.browser_proxy('one')
        self.path.write_text('invalid json')
        with self.assertRaises(RuntimeError):
            connections.browser_proxy('one')

    async def test_browser_uses_each_account_route(self):
        connections.assign(['one'], 'fixture')
        driver = Mock()
        context = Mock()
        driver.chromium.launch_persistent_context = AsyncMock(return_value=context)
        with patch('browser.Path', side_effect=lambda path: self.root / path), patch('browser.account_launch_args', return_value=[]), patch('proxy_bridge.prepare_browser_proxy', new=AsyncMock(side_effect=lambda proxy: (proxy, None))), patch.dict(browser.ACTIVE_CONTEXTS, {}, clear=True):
            await browser.launch_account_context(driver, 'one')
            kwargs = driver.chromium.launch_persistent_context.call_args.kwargs
            self.assertEqual(kwargs['proxy']['server'], 'http://127.0.0.1:9999')
            self.assertEqual(kwargs['proxy']['password'], 'fixture-secret')
            await browser.launch_account_context(driver, 'two')
            self.assertNotIn('proxy', driver.chromium.launch_persistent_context.call_args.kwargs)

    def test_http_download_route_matches_browser(self):
        connections.assign(['one'], 'fixture')
        options = connections.http_proxy('one')
        self.assertEqual(options['proxy'], connections.browser_proxy('one')['server'])
        self.assertEqual(options['proxy_auth'].login, 'fixture-user')
        self.assertEqual(connections.http_proxy('two'), {'proxy': None})

    async def test_new_and_retry_google_assign_before_login(self):
        async def login(name, *args):
            self.assertEqual(connections.account_connection(name), 'fixture')
        self.ns['_run_add_job'].side_effect = login
        body = self.ns['AccountAdd'](email='fixture@example.com', password='fixture', connection_id='fixture')
        result = await self.ns['admin_account_add'](body, 'fixture-admin')
        await asyncio.sleep(0)
        self.ns['_run_add_job'].assert_awaited_once()
        self.assertEqual(connections.account_connection(result['uuid']), 'fixture')
        await self.ns['retry_account']('one', body, 'fixture-admin')
        await asyncio.sleep(0)
        self.assertEqual(connections.account_connection('one'), 'fixture')
        with self.assertRaises(HTTPException) as error:
            await self.ns['admin_account_add'](self.ns['AccountAdd'](connection_id='missing'), 'fixture-admin')
        self.assertEqual(error.exception.status_code, 422)

    async def test_bulk_import_passes_selected_route_to_each_account(self):
        async def runner(accounts, concurrency, progress, submit, status):
            for email, password in accounts:
                name = await submit(email, password)
                self.assertEqual(connections.account_connection(name), 'fixture')
        self.ns['run_import'] = runner
        request = self.ns['GoogleBulkAdd'](text='first@example.com|fixture\nsecond@example.com|fixture', connection_id='fixture')
        await self.ns['google_bulk_add'](request, 'fixture-admin')
        tasks = list(self.ns['ACCOUNT_IMPORT_TASKS'])
        await asyncio.gather(*tasks)
        await asyncio.sleep(0)
        self.assertEqual(self.ns['_run_add_job'].await_count, 2)

    async def test_google_login_launcher_uses_assigned_proxy(self):
        import add_account
        connections.assign(['one'], 'fixture')
        driver = Mock()
        driver.chromium.launch_persistent_context = AsyncMock(side_effect=RuntimeError('fixture stop before login'))
        manager = AsyncMock()
        manager.__aenter__.return_value = driver
        with patch('add_account.async_playwright', return_value=manager), patch('add_account.Path', side_effect=lambda path: self.root / path), patch('add_account.account_launch_args', return_value=[]), patch('add_account.log'):
            with self.assertRaisesRegex(RuntimeError, 'fixture stop'):
                await add_account.add_account_flow('one', 'fixture@example.com', 'fixture', '')
        self.assertEqual(driver.chromium.launch_persistent_context.call_args.kwargs['proxy'], connections.browser_proxy('one'))

    async def test_actual_download_and_captcha_use_account_connection(self):
        import video_worker
        import video_worker_ui
        connections.assign(['one'], 'fixture')
        async def chunks(size):
            yield b'fixture-video'
        response = Mock()
        response.content.iter_chunked = chunks
        response_manager = AsyncMock()
        response_manager.__aenter__.return_value = response
        session = Mock()
        session.get.return_value = response_manager
        session_manager = AsyncMock()
        session_manager.__aenter__.return_value = session
        with patch('video_worker.aiohttp.ClientSession', return_value=session_manager), patch.object(video_worker.config, 'DOWNLOAD_DIR', str(self.root / 'downloads')):
            output = await video_worker._download('https://fixture.test/video', 'one')
        self.assertEqual(output.read_bytes(), b'fixture-video')
        self.assertEqual(session.get.call_args.kwargs['proxy'], connections.browser_proxy('one')['server'])
        context = Mock()
        context.request.get = AsyncMock(return_value=Mock(ok=True, body=AsyncMock(return_value=b'fixture-image')))
        self.assertEqual(await video_worker_ui._fetch_bytes('https://fixture.test/image', context), b'fixture-image')
        context.request.get.assert_awaited_once()


if __name__ == '__main__':
    unittest.main()
