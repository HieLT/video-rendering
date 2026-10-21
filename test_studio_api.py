import asyncio
import io
import json
import tempfile
import unittest
from pathlib import Path
from unittest.mock import AsyncMock

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from film_studio import register_film_studio, StudioSettings
from image_library import ImageLibrary
from studio_api import register_studio_workflow
from studio_workflow import Workflow
from test_video_batch import isolated_api


class StudioAPITests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(); self.addCleanup(self.temp.cleanup)
        self.root = Path(self.temp.name)
        (self.root/'Profile 7').mkdir()
        (self.root/'Local State').write_text(json.dumps({'profile': {'info_cache': {'Profile 7': {'name': 'Test'}}}}))
        app = FastAPI()
        def auth(key):
            if key != 'test': raise HTTPException(401)
        registry = register_film_studio(app, auth, self.root/'flow.db', self.root)
        registry.save(StudioSettings(profile_id='Profile 7', project_url='https://chatgpt.com/g/g-p-test/project', instructions_confirmed=True))
        self.images = ImageLibrary(self.root/'images')
        register_studio_workflow(app, auth, registry, self.images, AsyncMock(), lambda _: None)
        self.flow = Workflow(self.root/'flow.db')
        self.client = TestClient(app); self.headers = {'X-Admin-Key': 'test'}
        self.base = '/api/admin/film-studio'

    def post(self, path, value):
        return self.client.post(self.base+path, json=value, headers=self.headers)

    def test_browser_connection_requires_auth_and_rejects_remote_hosts(self):
        self.assertEqual(self.client.post(self.base+'/browser/connect',json={'endpoint':'http://127.0.0.1:9222'}).status_code,401)
        response=self.post('/browser/connect',{'endpoint':'http://example.com:9222'})
        self.assertEqual(response.status_code,409)
        self.assertIn('local HTTP',response.json()['detail'])
        self.assertEqual(self.client.get(self.base+'/status',headers=self.headers).json()['chatgpt_transport'],'not_connected')

    def test_new_run_auth_idempotency_and_schema(self):
        self.assertEqual(self.client.get(self.base+'/runs').status_code, 401)
        payload = dict(title='Film', screenplay='A enters.', request_id='one')
        one = self.post('/runs', payload); self.assertEqual(one.status_code, 201, one.text)
        two = self.post('/runs', payload); self.assertEqual(one.json()['id'], two.json()['id'])
        run = one.json(); tid = run['tasks'][0]['id']
        envelope = self.client.get(f"{self.base}/runs/{run['id']}/tasks/{tid}/input", headers=self.headers).json()
        self.assertIn('output_schema', envelope)
        self.assertIn('project_instructions', envelope)
        bad = self.post(f"/runs/{run['id']}/tasks/{tid}/result", {'output': {'invented': True}})
        self.assertEqual(bad.status_code, 422)
        self.assertEqual(self.flow.get(run['id'])['tasks'][0]['status'], 'pending')

    def test_image_receipt_cannot_be_fabricated(self):
        run = self.flow.create('Film', 'A enters.', 'one', '')
        with self.flow.edit(run['id']) as current:
            task = self.flow.task(current, 'image_create', [], ref='A')
            task_id = task['id']
        output = io.BytesIO(); Image.new('RGB', (8, 8), 'red').save(output, format='PNG')
        image = self.images.save(output.getvalue(), 'test.png', 10000)
        response = self.post(f"/runs/{run['id']}/tasks/{task_id}/result", {'output': {'image_id': image['id']}})
        self.assertEqual(response.status_code, 409)
        response = self.client.post(f"{self.base}/runs/{run['id']}/tasks/{task_id}/image", files={'file': ('test.png', output.getvalue(), 'image/png')}, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json()['image_id'], image['id'])
        outsider = self.client.get(f"{self.base}/runs/{run['id']}/images/{'b'*64}", headers=self.headers)
        self.assertEqual(outsider.status_code, 404)

    def test_skill_snapshot_survives_source_update(self):
        run = self.flow.create('Film', 'A enters.', 'one', '')
        with self.flow.edit(run['id']) as current:
            task = self.flow.task(current, 'prompt', [])
            tid = task['id']
        for skill in ('emily2040', 'sjinn-ai', 'sd25-pe'):
            self.client.put(self.base+'/skills/'+skill, json={'version': 'v1', 'source': 'original '+skill}, headers=self.headers)
        url = f"{self.base}/runs/{run['id']}/tasks/{tid}/input"
        one = self.client.get(url, headers=self.headers).json()['skills']
        self.client.put(self.base+'/skills/sd25-pe', json={'version': 'v2', 'source': 'changed'}, headers=self.headers)
        two = self.client.get(url, headers=self.headers).json()['skills']
        self.assertEqual(one, two)


class StudioAdmissionTests(unittest.IsolatedAsyncioTestCase):
    async def test_first_batch_then_append_and_recover_exact_task_id(self):
        ns, client = isolated_api()
        self.addCleanup(ns['store']._conn.close)
        ns['_run_task'] = AsyncMock()
        request = ns['VideoGenRequest']
        batch = 'studio_film_test_0001'
        req = request(batch_id=batch, name='Video 001 _ Chương 01 _ Test', prompt='A enters.', duration=30)
        with self.assertRaises(HTTPException) as blocked:
            await ns['_submit_video'](req, client)
        self.assertEqual(blocked.exception.status_code, 409)
        await ns['_submit_video'](req, client, studio_task_id='video_first')
        await asyncio.sleep(0)
        await ns['_submit_video'](req, client, studio_task_id='video_first')
        await ns['_submit_video'](req, client, studio_task_id='video_second')
        await asyncio.sleep(0)
        self.assertEqual(ns['_run_task'].await_count, 2)
        self.assertEqual(ns['store'].get('video_first')['batch_id'], batch)
        self.assertEqual(ns['store'].get('video_second')['batch_index'], 2)
        self.assertEqual(ns['store'].get('video_second')['batch_name'], req.name)

    async def test_batch_pagination_uses_video_order_after_retry(self):
        ns, _ = isolated_api(); store = ns['store']; self.addCleanup(store._conn.close)
        for n in range(1, 13):
            store.create_batch([f'video_{n}'], 'seedance-2.5', 'test', '16:9', 30, batch_id=f'studio_film_test_{n:04d}')
        store.create_batch(['retry_1'], 'seedance-2.5', 'test', '16:9', 30, batch_id='studio_film_test_0001', append_batch=True)
        rows = store.recent_tasks(page=1, group_by_batch=True)['tasks']
        order = list(dict.fromkeys(int(r['batch_id'][-4:]) for r in rows))
        self.assertEqual(order, list(range(12, 2, -1)))


if __name__ == '__main__': unittest.main()
