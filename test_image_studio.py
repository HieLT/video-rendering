import asyncio
import json
import tempfile
import unittest
import io
from unittest.mock import patch, AsyncMock, Mock
from aiohttp import web
from contextlib import asynccontextmanager
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from image_library import ImageLibrary
from image_studio import ImageRequest, ImageRetry, ImageStudio, register_image_studio
from image_worker import parse_image_result, submitted_image_count, submission_structure, download_originals, select_image_ratio, upload_image_references, ImageQuotaExceeded, IMAGE_DAILY_LIMIT_TEXT
from patchright.async_api import TimeoutError as PlaywrightTimeoutError


class RatioDiagnosticsTests(unittest.IsolatedAsyncioTestCase):
    async def test_missing_button_reports_structure_without_url_query(self):
        button = Mock()
        button.wait_for = AsyncMock(side_effect=PlaywrightTimeoutError('missing'))
        page = Mock()
        page.url = 'https://www.dola.com/chat?private=secret'
        page.get_by_test_id.return_value = button
        page.evaluate = AsyncMock(return_value={'ratio_nodes': 0, 'editors': 1})
        with self.assertRaises(RuntimeError) as raised:
            await select_image_ratio(page, '16:9')
        self.assertIn('"ratio_nodes": 0', str(raised.exception))
        self.assertNotIn('secret', str(raised.exception))


class UploadReadinessTests(unittest.IsolatedAsyncioTestCase):
    async def test_preview_does_not_finish_before_storage_upload(self):
        page = Mock()
        listener = {}
        page.on.side_effect = lambda event, callback: listener.update({event: callback})
        page.wait_for_function = AsyncMock()
        page.wait_for_timeout = AsyncMock()
        file_input = page.locator.return_value.filter.return_value.last.locator.return_value.first
        file_input.set_input_files = AsyncMock()
        async def upload_response():
            await asyncio.sleep(.02)
            listener['response'](Mock(url='https://upload.example/upload/v1/file', status=200))
        pending = asyncio.create_task(upload_image_references(page, ['reference.png']))
        await upload_response()
        await pending
        page.wait_for_timeout.assert_awaited_once_with(800)
        page.remove_listener.assert_called_once_with('response', listener['response'])


class DownloadTests(unittest.IsolatedAsyncioTestCase):
    async def test_incomplete_download_is_retried_from_start(self):
        image = io.BytesIO()
        Image.new('RGB', (12, 8), 'green').save(image, format='PNG')
        body = image.getvalue()
        calls = []
        async def serve(request):
            calls.append(1)
            if len(calls) == 1:
                response = web.StreamResponse(headers={'Content-Length': str(len(body))})
                await response.prepare(request)
                await response.write(body[:10])
                request.transport.close()
                return response
            return web.Response(body=body, content_type='image/png')
        app = web.Application()
        app.router.add_get('/image', serve)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as tmp, patch('image_worker.http_proxy', return_value={}):
                outputs = await download_originals([{'url': f'http://127.0.0.1:{port}/image',
                                                     'creation_id': 'retry'}], 'test', tmp)
                self.assertEqual(len(calls), 2)
                self.assertEqual((Path(tmp)/outputs[0]['filename']).read_bytes(), body)
        finally:
            await runner.cleanup()

    async def test_signed_url_preserves_escaping(self):
        image = io.BytesIO()
        Image.new('RGB', (12, 8), 'green').save(image, format='PNG')
        expected = '/original.png?signature=a%2Fb%3D&key=x%2By'
        async def serve(request):
            if request.raw_path != expected:
                return web.Response(status=403)
            return web.Response(body=image.getvalue(), content_type='image/png')
        app = web.Application()
        app.router.add_get('/original.png', serve)
        runner = web.AppRunner(app)
        await runner.setup()
        site = web.TCPSite(runner, '127.0.0.1', 0)
        await site.start()
        port = site._server.sockets[0].getsockname()[1]
        try:
            with tempfile.TemporaryDirectory() as tmp, patch('image_worker.http_proxy', return_value={}):
                outputs = await download_originals([{'url': f'http://127.0.0.1:{port}'+expected,
                                                     'creation_id': 'signed'}], 'test', tmp)
                self.assertEqual(outputs[0]['width'], 12)
                self.assertEqual((Path(tmp)/outputs[0]['filename']).read_bytes(), image.getvalue())
        finally:
            await runner.cleanup()


def message(index, creations, **extra):
    return {'index_in_conv': index, 'user_type': 2, 'content_status': 0,
            'ext': {'is_finish': 'true'}, 'content_block': [{'block_type': 2074,
            'content': {'creation_block': {'creations': creations}}}], **extra}


def creation(identity='one', raw=True, status=2):
    return {'id': identity, 'type': 1, 'image': {'status': status,
            'image_ori': {'url': 'https://cdn.test/watermarked.png'},
            'image_ori_raw': {'url': 'https://cdn.test/' + identity + '.png'} if raw else None}}


class ParserTests(unittest.TestCase):
    def test_error_codes_are_taken_only_from_current_assistant(self):
        old = message(1, [])
        old['ext']['ai_creation_res_code'] = '123'
        user = {'index_in_conv': 2, 'user_type': 1}
        current = message(3, [])
        current['ext'].update(ai_creation_res_code='710082003',
                              ai_creation_tool_list=json.dumps([{'fail_code': '456'}, {'fail_code': 'secret'}]))
        result = parse_image_result([old, user, current])
        self.assertEqual(result['error_codes'], ['456', '710082003'])

    def test_request_requires_uploaded_image_resource(self):
        self.assertEqual(submitted_image_count({'messages':[{'content_block':[{'content':{'text_block':{'text':'Use uploaded master image'}}}]}]}),0)
        image={'type':1,'image':{'uri':'tos/reference.png'}}
        payload={'messages':[{'content_block':[{'content':{'attachment_block':{'attachments':[image,image]}}}]}]}
        self.assertEqual(submitted_image_count(payload),1)
        image['image']['uri']=''
        self.assertEqual(submitted_image_count(payload),0)

    def test_submission_diagnostics_do_not_include_payload_values(self):
        payload = {'messages': [{'content_block': [{'block_type': 2, 'content': {
            'text_block': {'text': 'private prompt'}, 'attachment_block': {'attachments': [
                {'type': 1, 'image': {'uri': 'private-resource'}}]}}}]}]}
        report = json.dumps(submission_structure(payload))
        self.assertIn('attachment_block', report)
        self.assertIn('uri', report)
        self.assertNotIn('private', report)
        self.assertEqual(submitted_image_count(payload), 1)
    def test_latest_assistant_only_and_dedup(self):
        user = {'user_type': 1, 'index_in_conv': 3, 'content_block': message(3,[creation('input')])['content_block']}
        result = parse_image_result([message(2,[creation('old')]), user,
                                    message(4,[creation(),creation(),creation('two')])])
        self.assertEqual([i['creation_id'] for i in result['images']], ['one','two'])
        self.assertTrue(result['finished'])

    def test_no_watermarked_fallback_and_pending_sibling(self):
        result = parse_image_result([message(1,[creation(),creation('pending',False,1)])])
        self.assertTrue(result['incomplete'])
        self.assertEqual(len(result['images']),1)
        missing = parse_image_result([message(1,[creation(raw=False)])])
        self.assertEqual(missing['images'],[])

    def test_content_string_and_text_rejection(self):
        row = message(1,[creation()])
        row['content'] = json.dumps(row.pop('content_block'))
        self.assertEqual(len(parse_image_result([row])['images']),1)
        row['content'] = json.dumps([{'block_type':10000,'content':{'text_block':{'text':'No credits'}}}])
        self.assertEqual(parse_image_result([row])['text'],'No credits')


class Pool:
    def __init__(self):
        self._locks={}
        self.reservations={}
        self.semaphore=asyncio.Semaphore(2)

    def list_accounts(self):
        return [{'name':'test-account','auth_state':'active','scheduling':True,
                 'used_today':2,'login_checked_at':1}]

    @asynccontextmanager
    async def account_activity(self, account, activity):
        async with self._locks.setdefault(account,asyncio.Lock()):
            yield


class JobTests(unittest.IsolatedAsyncioTestCase):
    async def asyncSetUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.pool=Pool()
        self.calls=[]
        async def worker(*args, **kwargs):
            self.calls.append(kwargs)
            self.assertTrue(self.pool._locks['test-account'].locked())
            args[6](phase='submitting')
            args[6](conversation_id='123',phase='generating')
            return [{'filename':'0.png','source':'image_ori_raw','width':32,'height':32}]
        self.studio=ImageStudio(self.tmp.name,self.pool,ImageLibrary(Path(self.tmp.name)/'library'),worker=worker)

    async def asyncTearDown(self):
        await self.studio.stop()
        self.studio.db.close()
        self.tmp.cleanup()

    def request(self, **kwargs):
        return ImageRequest(prompt='test image',request_id='test-request',**kwargs)

    async def test_four_free_accounts_run_despite_pinned_waiters(self):
        self.pool.semaphore = asyncio.Semaphore(10)
        self.pool.list_accounts = lambda: [dict(name=f'account-{i}',auth_state='active',scheduling=True) for i in range(5)]
        self.pool.reservations['account-0'] = 'video-task'
        started = set()
        all_started = asyncio.Event()
        release = asyncio.Event()
        async def worker(account, *args, **kwargs):
            started.add(account)
            if len(started) == 4:
                all_started.set()
            await release.wait()
            return []
        self.studio.worker = worker
        for i in range(4):
            self.studio.create(ImageRequest(prompt='pinned',account='account-0',request_id=f'pinned-request-{i}'))
        for i in range(4):
            self.studio.create(ImageRequest(prompt='auto',request_id=f'free-request-{i}'))
        await asyncio.wait_for(all_started.wait(), 3)
        self.assertEqual(started, {f'account-{i}' for i in range(1,5)})
        release.set()

    async def test_idempotent_and_video_quota_independent(self):
        first=self.studio.create(self.request())
        second=self.studio.create(self.request())
        self.assertEqual(first['id'],second['id'])
        await self.studio.runners[first['id']]
        self.assertEqual(self.studio.get(first['id'])['status'],'completed')
        self.assertEqual(len(self.calls),1)
        with self.assertRaises(HTTPException):
            self.studio.create(ImageRequest(prompt='different',request_id='test-request'))

    async def test_explicit_image_quota_persists_and_reopens_after_24_hours(self):
        async def limited(account, *args, **kwargs):
            args[5](conversation_id='123', phase='generating')
            raise ImageQuotaExceeded(IMAGE_DAILY_LIMIT_TEXT)
        self.studio.worker = limited
        with patch('image_studio.time.time', return_value=1000):
            job = self.studio.create(self.request())
            await self.studio.runners[job['id']]
            self.assertEqual(self.studio.get(job['id'])['status'], 'failed')
            self.assertEqual(self.studio.accounts(), [])
            self.assertEqual(self.studio.accounts(include_image_limited=True)[0]['name'], 'test-account')
            self.assertEqual(self.studio.limited_accounts()[0]['retry_at'], 87400)
        self.studio.db.close()
        with patch('image_studio.time.time', return_value=87399):
            self.studio = ImageStudio(self.tmp.name, self.pool, ImageLibrary(Path(self.tmp.name)/'library'))
            self.assertEqual(self.studio.accounts(), [])
        with patch('image_studio.time.time', return_value=87400):
            self.assertEqual(self.studio.accounts()[0]['name'], 'test-account')
            self.assertEqual(self.studio.limited_accounts(), [])

    async def test_network_access_denied_disables_account_and_falls_through(self):
        accounts = [dict(name='blocked', auth_state='active', scheduling=True),
                    dict(name='healthy', auth_state='active', scheduling=True)]
        self.pool.list_accounts = lambda: accounts
        disabled = []
        self.pool.set_scheduling = lambda name, enabled, manual=False: disabled.append((name, enabled, manual))
        calls = []
        async def worker(account, *args, **kwargs):
            calls.append(account)
            if account == 'blocked':
                raise RuntimeError('Page.goto: net::ERR_NETWORK_ACCESS_DENIED at https://www.dola.com/chat')
            return []
        self.studio.worker = worker
        job = self.studio.create(self.request())
        await self.studio.runners[job['id']]
        self.assertEqual(calls, ['blocked', 'healthy'])
        self.assertEqual(disabled, [('blocked', False, True)])
        self.assertEqual(self.studio.get(job['id'])['status'], 'completed')

    async def test_generic_failure_does_not_mark_image_quota(self):
        async def failed(account, *args, **kwargs):
            raise RuntimeError('network failed')
        self.studio.worker = failed
        job = self.studio.create(self.request())
        await self.studio.runners[job['id']]
        self.assertEqual(self.studio.limited_accounts(), [])

    async def test_queued_pinned_job_fails_promptly_without_extending_limit(self):
        job = self.studio.create(self.request(account='test-account'))
        self.studio.mark_image_limit('test-account', detected_at=1000)
        with patch('image_studio.time.time', return_value=2000):
            await self.studio.runners[job['id']]
            self.assertEqual(self.studio.get(job['id'])['status'], 'failed')
            self.assertEqual(self.studio.limited_accounts()[0]['retry_at'], 87400)
            self.assertEqual(self.calls, [])

    async def test_import_existing_quota_error_uses_original_timestamp(self):
        job = self.studio.create(self.request())
        await self.studio.stop()
        self.studio.update(job['id'], account='test-account', status='failed', error=IMAGE_DAILY_LIMIT_TEXT)
        self.studio.db.execute('UPDATE image_jobs SET updated_at=1000 WHERE id=?', (job['id'],))
        self.studio.db.commit()
        self.studio.db.close()
        with patch('image_studio.time.time', return_value=5000):
            self.studio = ImageStudio(self.tmp.name, self.pool, ImageLibrary(Path(self.tmp.name)/'library'))
            self.assertEqual(self.studio.limited_accounts()[0]['retry_at'], 87400)

    async def test_busy_video_reservation_not_launched(self):
        self.pool.reservations['test-account']='video-task'
        job=self.studio.create(self.request())
        await asyncio.sleep(.03)
        self.assertEqual(self.calls,[])
        self.assertEqual(self.studio.get(job['id'])['status'],'queued')

    async def test_reference_snapshot_survives_library_deletion(self):
        import io
        data=io.BytesIO();Image.new('RGB',(16,16),'red').save(data,format='PNG')
        saved=self.studio.library.save(data.getvalue(),'ref.png',10000)
        job=self.studio.create(self.request(references=[saved['id']]))
        self.studio.library.path(saved['id']).unlink()
        await self.studio.runners[job['id']]
        paths=self.calls[0]['reference_image_paths']
        self.assertEqual(Path(paths[0]).read_bytes(),data.getvalue())
        self.assertEqual(self.studio.get(job['id'])['references'],[saved['id']])

    async def test_unknown_reference_rejected(self):
        with self.assertRaises(HTTPException) as error:
            self.studio.create(self.request(references=['../secret']))
        self.assertEqual(error.exception.status_code,422)

    async def test_retry_preserves_snapshot_and_uses_new_conversation(self):
        import io
        data=io.BytesIO();Image.new('RGB',(8,8),'blue').save(data,format='PNG')
        ref=self.studio.library.save(data.getvalue(),'input.png',10000)
        first=self.studio.create(self.request(references=[ref['id']]))
        await self.studio.runners[first['id']]
        self.studio.library.path(ref['id']).unlink()
        original=self.pool.list_accounts
        self.pool.list_accounts=lambda: original()+[dict(original()[0],name='second')]
        self.studio.start=lambda identity:None
        body=ImageRetry(account='second',request_id='retry-test-key')
        retried=self.studio.retry(first['id'],body)
        self.assertEqual(retried['requested_account'],'second')
        self.assertIsNone(retried['conversation_id'])
        self.assertEqual(retried['prompt'],first['prompt'])
        self.assertEqual((self.studio.root/retried['id']/'inputs'/'0.png').read_bytes(),data.getvalue())
        self.assertEqual(self.studio.retry(first['id'],body)['id'],retried['id'])
        self.assertEqual(retried['id'],first['id'])
        self.assertEqual(self.studio.get(first['id'])['status'],'queued')
        self.assertEqual(retried['outputs'],[])
        self.assertIsNone(retried['account'])
        self.assertEqual(self.studio.db.execute('SELECT count(*) FROM image_jobs').fetchone()[0],1)
        with self.assertRaises(HTTPException):
            self.studio.retry(retried['id'],ImageRetry(request_id='running-test-key'))

    async def test_recovery_never_resubmits_uncertain_job(self):
        job=self.studio.create(self.request())
        await self.studio.stop()
        await asyncio.sleep(0)
        self.studio.update(job['id'],status='running',phase='submitting')
        self.studio.recover()
        self.assertEqual(self.studio.get(job['id'])['status'],'interrupted')
        self.assertEqual(self.calls,[])

    async def test_resume_passes_existing_conversation(self):
        job=self.studio.create(self.request())
        await self.studio.stop()
        await asyncio.sleep(0)
        self.studio.update(job['id'],status='running',phase='generating',account='test-account',conversation_id='456')
        self.studio.recover()
        await self.studio.runners[job['id']]
        self.assertEqual(self.calls,[{'conversation_id':'456'}])


class RouteTests(unittest.TestCase):
    def test_batch_idempotency_and_capacity(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio = ImageStudio(tmp, Pool(), ImageLibrary(Path(tmp)/'library'))
            studio.start = lambda identity: None
            app = FastAPI()
            def auth(key):
                if key != 'test-key':
                    raise HTTPException(401, 'denied')
            register_image_studio(app, studio, auth)
            headers = {'X-Admin-Key': 'test-key'}
            body = {'prompt': 'batch test', 'request_id': 'batch-request', 'count': 3}
            with TestClient(app) as client:
                url = '/api/admin/image-studio/batches'
                self.assertEqual(client.post(url, json=body).status_code, 401)
                first = client.post(url, headers=headers, json=body)
                self.assertEqual(first.status_code, 202)
                ids = [j['id'] for j in first.json()['jobs']]
                self.assertEqual(len(set(ids)), 3)
                again = client.post(url, headers=headers, json=body)
                self.assertEqual([j['id'] for j in again.json()['jobs']], ids)
                for count in (0, 11):
                    self.assertEqual(client.post(url, headers=headers, json={**body, 'count': count}).status_code, 422)
                for i in range(26):
                    studio.create(ImageRequest(prompt='filler', request_id=f'filler-request-{i}'))
                rejected = client.post(url, headers=headers, json={**body, 'request_id': 'overflow-request'})
                self.assertEqual(rejected.status_code, 429)
                self.assertEqual(studio.db.execute('SELECT count(*) FROM image_jobs').fetchone()[0], 29)
                self.assertEqual(client.post(url, headers=headers, json=body).status_code, 202)
            studio.db.close()

    def test_auth_and_file_access_and_library(self):
        with tempfile.TemporaryDirectory() as tmp:
            studio=ImageStudio(tmp,Pool(),ImageLibrary(Path(tmp)/'library'))
            app=FastAPI()
            def auth(key):
                if key!='test-key':raise HTTPException(401,'denied')
            register_image_studio(app,studio,auth)
            studio.start=lambda identity:None
            with TestClient(app) as client:
                self.assertEqual(client.get('/api/admin/image-studio/jobs').status_code,401)
                headers={'X-Admin-Key':'test-key'}
                bad=client.post('/api/admin/image-studio/jobs',headers=headers,json={'prompt':'x','request_id':'abcdefgh','model':'invalid'})
                self.assertEqual(bad.status_code,422)
                job=client.post('/api/admin/image-studio/jobs',headers=headers,json={'prompt':'x','request_id':'abcdefgh'}).json()
                path=Path(tmp)/job['id'];path.mkdir()
                Image.new('RGB',(10,12),'green').save(path/'0.png')
                studio.update(job['id'],status='completed',outputs=[{'filename':'0.png','width':10,'height':12}])
                url=f"/api/admin/image-studio/jobs/{job['id']}/files/0"
                self.assertEqual(client.get(url).status_code,401)
                self.assertEqual(client.get(url,headers=headers).status_code,200)
                self.assertEqual(client.get(url[:-1]+'-1',headers=headers).status_code,404)
                saved=client.post(f"/api/admin/image-studio/jobs/{job['id']}/library/0",headers=headers)
                self.assertEqual(saved.status_code,200)
                self.assertTrue(studio.library.path(saved.json()['id']).exists())
                job_url = f"/api/admin/image-studio/jobs/{job['id']}"
                self.assertEqual(client.delete(job_url).status_code,401)
                self.assertEqual(client.delete(job_url,headers=headers).status_code,409)
                Image.new('RGB',(20,24),'blue').save(path/'1.png')
                studio.update(job['id'],outputs=[{'filename':'0.png','width':10,'height':12},
                                                {'filename':'1.png','width':20,'height':24}])
                self.assertEqual(client.get('/api/admin/image-studio/gallery').status_code,401)
                self.assertEqual(client.get('/api/admin/image-studio/gallery',headers=headers).json()['total'],2)
                self.assertEqual(client.delete(url).status_code,401)
                self.assertTrue((path/'0.png').exists())
                self.assertEqual(client.delete(url,headers=headers).status_code,200)
                self.assertFalse((path/'0.png').exists())
                self.assertEqual(client.get(url,headers=headers).status_code,404)
                self.assertEqual(client.get(url[:-1]+'1',headers=headers).status_code,200)
                remaining=client.get('/api/admin/image-studio/gallery',headers=headers).json()
                self.assertEqual(remaining['total'],1)
                self.assertEqual(remaining['images'][0]['index'],1)
                self.assertEqual(client.delete(url,headers=headers).status_code,200)
                self.assertTrue(studio.library.path(saved.json()['id']).exists())
                self.assertEqual(client.delete(url[:-1]+'1',headers=headers).status_code,200)
                studio.update(job['id'],status='running')
                self.assertEqual(client.delete(job_url,headers=headers).status_code,409)
                studio.update(job['id'],status='failed')
                self.assertEqual(client.delete(job_url,headers=headers).status_code,200)
                self.assertEqual(client.get(job_url,headers=headers).status_code,404)
                self.assertFalse(path.exists())
                self.assertTrue(studio.library.path(saved.json()['id']).exists())
                queued=client.post('/api/admin/image-studio/jobs',headers=headers,json={'prompt':'queued','request_id':'queued-delete'}).json()
                self.assertEqual(client.delete(f"/api/admin/image-studio/jobs/{queued['id']}",headers=headers).status_code,200)
            studio.db.close()


if __name__=='__main__':
    unittest.main()
