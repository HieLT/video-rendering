"""Persistent library HTTP + mocked worker tests. No live Dola calls or production DBs."""
import asyncio
from contextlib import closing
from io import BytesIO
import json
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch, AsyncMock
from types import SimpleNamespace
import os
from video_schedule import VideoScheduler

from fastapi import HTTPException
from fastapi.testclient import TestClient
from PIL import Image

from asset_api import register_asset_routes
from asset_storage import resolve_asset_path
from production_store import AssetInUseError, SCHEMA_VERSION
from store import TaskStore
from test_video_batch import isolated_api, backend_checks


def run_saved_task(case, args):
    """Exercise server runner + real scheduler/reference copies/download, mock only Dola."""
    scheduler = VideoScheduler(case.store, SimpleNamespace())
    case.ns['scheduler'] = scheduler
    output = {}
    async def account(row):
        case.store.update(row['id'], account='fixture')
        return True
    async def submit(row, paths):
        output.update(await case.ns['pool'].generate_video(row['prompt'], row['ratio'], row['duration'], row['model'], reference_image_paths=paths))
        return {'kind':'completed','poll':{'videos':['https://fixture.test/video.mp4']}}
    async def download(*args): return output['local_path']
    previous = os.getcwd()
    try:
        os.chdir(case.tmp.name)
        with patch.object(scheduler,'account',account), patch.object(scheduler,'submit',submit), patch('video_schedule._download',download):
            asyncio.run(case.runner(*args))
    finally:
        os.chdir(previous)


def image_bytes(fmt='PNG', color='red'):
    data = BytesIO()
    Image.new('RGB', (12, 12), color).save(data, format=fmt)
    return data.getvalue()


class LibraryApiTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.root = Path(self.tmp.name) / 'assets'
        self.root_patch = patch('config.ASSET_DIR', str(self.root))
        self.root_patch.start()
        self.ns, self.client_policy = isolated_api()
        self.store = self.ns['store']
        self.runner = self.ns['_run_task']
        self.queued = []
        async def capture(*args):
            self.queued.append(args)
        self.ns['_run_task'] = capture
        def auth(key):
            if key != 'admin-test':
                raise HTTPException(401, 'invalid admin key')
        self.library = register_asset_routes(self.ns['app'], self.store, auth, self.ns['_auth'],
                                             self.ns['VideoGenRequest'], self.ns['_submit_video'])
        self.http = TestClient(self.ns['app'], headers={'X-Admin-Key':'admin-test'})
        self.http.__enter__()
        self.project = self.http.post('/api/admin/projects', json={'name':'Riven'}).json()
        self.project_id = self.project['id']
        self.scene = self.create_scene(26, '@Riven walks into @Cabin holding @Sword')

    def tearDown(self):
        self.http.__exit__(None, None, None)
        self.store._conn.close()
        self.root_patch.stop()
        self.tmp.cleanup()

    def create_scene(self, number, prompt='', **fields):
        response = self.http.post(f'/api/admin/projects/{self.project_id}/scenes',
            json={'scene_number':number,'scene_name':f'Scene {number}','prompt':prompt,
                  'model':'seedance-2.5','duration':30, **fields})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def upload(self, name='Riven', kind='character', filename='hero.png', data=None, project_id=None):
        return self.http.post(f'/api/admin/projects/{project_id or self.project_id}/assets',
            data={'name':name,'type':kind}, files={'file':(filename, data if data is not None else image_bytes(), 'image/png')})

    def asset(self, name='Riven'):
        response = self.upload(name)
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def attach(self, asset, alias, position, scene=None):
        response = self.http.post(f"/api/admin/scenes/{(scene or self.scene)['id']}/assets",
            json={'asset_id':asset['id'],'reference_alias':alias,'position':position})
        self.assertEqual(response.status_code, 201, response.text)
        return response.json()

    def prepare_references(self):
        riven, cabin, sword = self.asset('Riven'), self.asset('Cabin'), self.asset('Sword')
        self.attach(sword, 'Sword', 3)
        self.attach(riven, 'Riven', 1)
        self.attach(cabin, 'Cabin', 2)
        return riven, cabin, sword

    def generate(self, scene=None, count=1):
        response = self.http.post(f"/api/admin/scenes/{(scene or self.scene)['id']}/generate", json={'count':count})
        self.assertEqual(response.status_code, 202, response.text)
        return response.json()

    def test_upload_persists_relative_metadata_and_list_get(self):
        response = self.upload()
        self.assertEqual(response.status_code, 201, response.text)
        asset = response.json()
        path = resolve_asset_path(asset['file_path'])
        self.assertEqual(path.read_bytes(), image_bytes())
        self.assertFalse(Path(asset['file_path']).is_absolute())
        self.assertEqual(self.http.get(f"/api/admin/assets/{asset['id']}").json(), asset)
        self.assertEqual(self.http.get(f'/api/admin/projects/{self.project_id}/assets').json()['assets'], [asset])
        self.assertEqual(self.http.get('/api/admin/assets/missing').status_code, 404)

    def test_upload_supported_formats_use_actual_image_extension(self):
        for filename, fmt, expected in (('hero.jpeg','JPEG','.jpg'),('hero.PNG','PNG','.png'),('hero.webp','WEBP','.webp')):
            with self.subTest(fmt=fmt):
                response = self.upload(name=fmt, filename=filename, data=image_bytes(fmt))
                self.assertEqual(response.status_code, 201, response.text)
                self.assertTrue(response.json()['file_path'].endswith(expected))
        response = self.upload(name='Mislabeled', filename='mislabeled.jpg', data=image_bytes('PNG'))
        self.assertEqual(response.status_code, 201)
        self.assertTrue(response.json()['file_path'].endswith('.png'))

    def test_unsupported_extension_corrupt_file_type_and_project_rejected(self):
        cases = ({'filename':'hero.gif'}, {'data':b'not an image'}, {'kind':'video'},
                 {'project_id':'missing'}, {'name':' '}, {'filename':'hero.svg'})
        for args in cases:
            with self.subTest(args=args):
                response = self.upload(**args)
                self.assertIn(response.status_code, (404,422), response.text)
        self.assertEqual(self.store.list_project_assets(self.project_id), [])
        self.assertFalse(self.root.exists())

    def test_upload_size_limit(self):
        with patch('config.REFERENCE_IMAGE_MAX_BYTES', 8):
            self.assertEqual(self.upload(data=image_bytes()).status_code, 422)
        self.assertEqual(self.store.list_project_assets(self.project_id), [])

    def test_db_insert_failure_cleans_owned_file(self):
        with self.assertLogs('uvicorn.error', level='ERROR'), patch.object(self.store, 'create_asset', side_effect=sqlite3.OperationalError('injected insert failure')):
            response = self.upload()
        self.assertEqual(response.status_code, 500)
        self.assertEqual([p for p in self.root.rglob('*') if p.is_file()], [])
        self.assertEqual(self.store.list_project_assets(self.project_id), [])

    def test_file_write_failure_does_not_create_row(self):
        original_open = Path.open
        def fail(path, *args, **kwargs):
            if args and args[0] == 'xb' and path.is_relative_to(self.root):
                raise OSError('injected write failure')
            return original_open(path, *args, **kwargs)
        with self.assertLogs('uvicorn.error', level='ERROR'), patch.object(Path, 'open', fail):
            self.assertEqual(self.upload().status_code, 500)
        self.assertEqual(self.store.list_project_assets(self.project_id), [])

    def test_partial_file_write_failure_cleans_file_without_row(self):
        original_open = Path.open
        class PartialWriter:
            def __init__(self, image):
                self.image = image
            def __enter__(self):
                return self
            def __exit__(self, *args):
                self.image.close()
            def write(self, data):
                self.image.write(data[:8])
                raise OSError('injected partial write failure')
        def partial_write(path, *args, **kwargs):
            result = original_open(path, *args, **kwargs)
            if args and args[0] == 'xb' and path.is_relative_to(self.root):
                return PartialWriter(result)
            return result
        with self.assertLogs('uvicorn.error', level='ERROR'), patch.object(Path, 'open', partial_write):
            self.assertEqual(self.upload().status_code, 500)
        self.assertEqual(self.store.list_project_assets(self.project_id), [])
        self.assertEqual([p for p in self.root.rglob('*') if p.is_file()], [])

    def test_attached_asset_delete_conflicts_then_detached_asset_deletes(self):
        asset = self.asset()
        path = resolve_asset_path(asset['file_path'])
        self.attach(asset, 'Riven', 1)
        self.assertEqual(self.http.delete(f"/api/admin/assets/{asset['id']}").status_code, 409)
        self.assertTrue(path.is_file())
        self.assertEqual(self.http.delete(f"/api/admin/scenes/{self.scene['id']}/assets/{asset['id']}").status_code, 200)
        self.assertEqual(self.http.delete(f"/api/admin/assets/{asset['id']}").status_code, 200)
        self.assertFalse(path.exists())
        self.assertIsNone(self.store.get_asset(asset['id']))
        self.assertEqual(self.http.delete(f"/api/admin/assets/{asset['id']}").status_code, 404)

    def test_delete_database_failure_restores_file_and_metadata(self):
        asset = self.asset()
        path = resolve_asset_path(asset['file_path'])
        self.store._conn.execute("CREATE TRIGGER reject_delete BEFORE DELETE ON assets BEGIN SELECT RAISE(ABORT, 'injected delete failure'); END")
        self.assertEqual(self.http.delete(f"/api/admin/assets/{asset['id']}").status_code, 409)
        self.assertEqual(self.store.get_asset(asset['id']), asset)
        self.assertEqual(path.read_bytes(), image_bytes())
        self.assertEqual(list(path.parent.glob('.*.deleting-*')), [])
        self.assertFalse(self.store._conn.in_transaction)

    def test_delete_file_failure_compensates_metadata(self):
        asset = self.asset()
        path = resolve_asset_path(asset['file_path'])
        original_unlink = Path.unlink
        def fail(staged, *args, **kwargs):
            if '.deleting-' in staged.name:
                raise OSError('injected unlink failure')
            return original_unlink(staged, *args, **kwargs)
        with self.assertLogs('uvicorn.error', level='ERROR'), patch.object(Path, 'unlink', fail):
            self.assertEqual(self.http.delete(f"/api/admin/assets/{asset['id']}").status_code, 500)
        self.assertEqual(self.store.get_asset(asset['id']), asset)
        self.assertEqual(path.read_bytes(), image_bytes())
        self.assertEqual(list(path.parent.glob('.*.deleting-*')), [])
        self.assertFalse(self.store._conn.in_transaction)

    def test_interrupted_delete_with_metadata_restores_source_on_startup(self):
        asset = self.asset()
        original = resolve_asset_path(asset['file_path'])
        staged = original.with_name('.' + original.name + '.deleting-' + 'a' * 32)
        original.rename(staged)
        self.assertEqual(self.library.recover_deletions(), 1)
        self.assertTrue(original.is_file())
        self.assertFalse(staged.exists())
        self.assertEqual(self.store.get_asset(asset['id']), asset)

    def test_interrupted_committed_delete_cleans_staged_file_on_startup(self):
        asset = self.asset()
        original = resolve_asset_path(asset['file_path'])
        staged = original.with_name('.' + original.name + '.deleting-' + 'b' * 32)
        original.rename(staged)
        with self.store._conn:
            self.store._conn.execute('DELETE FROM assets WHERE id=?', (asset['id'],))
        self.assertEqual(self.library.recover_deletions(), 1)
        self.assertFalse(staged.exists())
        self.assertFalse(original.exists())
        self.assertIsNone(self.store.get_asset(asset['id']))

    def test_large_pixel_image_rejected_before_file_write(self):
        with patch('PIL.Image.MAX_IMAGE_PIXELS', 100):
            self.assertEqual(self.upload(data=image_bytes()).status_code, 422)
        self.assertEqual(self.store.list_project_assets(self.project_id), [])

    def test_scene_generation_without_references_uses_empty_snapshot(self):
        scene = self.create_scene(27, 'A quiet forest')
        result = self.generate(scene=scene)
        row = self.store.get(result['id'])
        self.assertEqual(row['reference_snapshot'], '[]')
        self.assertEqual(row['reference_images'], '[]')
        self.assertEqual(row['prompt'], 'A quiet forest')

    def test_delete_missing_source_preserves_metadata(self):
        asset = self.asset()
        resolve_asset_path(asset['file_path']).unlink()
        self.assertEqual(self.http.delete(f"/api/admin/assets/{asset['id']}").status_code, 409)
        self.assertEqual(self.store.get_asset(asset['id']), asset)

    def test_cross_project_attach_rejected(self):
        asset = self.asset()
        other = self.store.create_project('Other')
        other_scene = self.store.create_scene(other['id'], 1)
        response = self.http.post(f"/api/admin/scenes/{other_scene['id']}/assets",
            json={'asset_id':asset['id'],'position':1,'reference_alias':'Hero'})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.store.list_scene_assets(other_scene['id']), [])

    def test_reference_order_alias_update_and_uniqueness(self):
        riven, cabin, sword = self.prepare_references()
        url = f"/api/admin/scenes/{self.scene['id']}/assets"
        self.assertEqual([r['asset_id'] for r in self.http.get(url).json()['assets']], [riven['id'],cabin['id'],sword['id']])
        response = self.http.put(url + '/order', json={'asset_ids':[sword['id'],riven['id'],cabin['id']]})
        self.assertEqual(response.status_code, 200)
        self.assertEqual([r['position'] for r in response.json()['assets']], [1,2,3])
        response = self.http.patch(url + '/' + riven['id'], json={'reference_alias':' @Hero '})
        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()['assets'][1]['reference_alias'], 'Hero')
        response = self.http.patch(url + '/' + cabin['id'], json={'reference_alias':'hero'})
        self.assertEqual(response.status_code, 422)
        self.assertEqual(self.http.patch(url + '/missing', json={'reference_alias':'A'}).status_code, 404)
        self.assertEqual(self.http.put(url + '/order', json={'asset_ids':[riven['id']]}).status_code, 422)

    def test_generate_captures_order_aliases_scene_id_and_prompt(self):
        assets = self.prepare_references()
        result = self.generate()
        task = self.store.get(result['id'])
        snapshot = json.loads(task['reference_snapshot'])
        self.assertEqual([r['asset_id'] for r in snapshot], [a['id'] for a in assets])
        self.assertEqual([r['reference_alias'] for r in snapshot], ['Riven','Cabin','Sword'])
        self.assertEqual([r['file_path'] for r in snapshot], [a['file_path'] for a in assets])
        self.assertEqual(task['scene_id'], self.scene['id'])
        self.assertEqual(task['prompt'], '@Image1 walks into @Image2 holding @Image3')
        self.assertEqual(self.store.get_scene(self.scene['id'])['prompt'], '@Riven walks into @Cabin holding @Sword')
        self.assertEqual(json.loads(task['reference_images']), ['asset://' + a['id'] for a in assets])
        self.assertFalse(self.ns['UPLOADED_REFERENCES'])

    def test_edits_after_submit_do_not_change_task_snapshot_or_worker_paths(self):
        assets = self.prepare_references()
        result = self.generate()
        before = self.store.get(result['id'])
        url = f"/api/admin/scenes/{self.scene['id']}/assets"
        self.http.put(url + '/order', json={'asset_ids':[a['id'] for a in reversed(assets)]})
        self.http.patch(url + '/' + assets[0]['id'], json={'reference_alias':'OlderHero'})
        self.http.patch(f"/api/admin/scenes/{self.scene['id']}", json={'prompt':'Edited prompt','duration':10})
        after = self.store.get(result['id'])
        self.assertEqual(after['reference_snapshot'], before['reference_snapshot'])
        self.assertEqual(after['prompt'], before['prompt'])
        self.assertEqual(after['duration'], 30)
        captured = []
        async def worker(prompt, ratio, duration, model, **kwargs):
            captured.extend(kwargs['reference_image_paths'])
            self.assertEqual(prompt, before['prompt'])
            output = Path(self.tmp.name) / 'output.mp4'
            output.write_bytes(b'mocked video')
            return {'local_path':str(output)}
        self.ns['pool'].generate_video = worker
        args = next(args for args in self.queued if args[0] == result['id'])
        run_saved_task(self, args)
        self.assertEqual([Path(p).read_bytes() for p in captured], [resolve_asset_path(a['file_path']).read_bytes() for a in assets])
        self.assertTrue(all('.job_media' in p for p in captured))
        self.assertEqual(self.store.get(result['id'])['status'], 'completed')
        self.assertTrue(all(resolve_asset_path(a['file_path']).is_file() for a in assets))

    def test_persistent_sources_survive_worker_failure(self):
        assets = self.prepare_references()
        result = self.generate()
        async def fail(*args, **kwargs):
            raise self.ns['AllAccountsLimitedError']('fixture quota failure')
        self.ns['pool'].generate_video = fail
        args = next(args for args in self.queued if args[0] == result['id'])
        run_saved_task(self, args)
        self.assertEqual(self.store.get(result['id'])['status'], 'needs_recovery')
        self.assertTrue(all(resolve_asset_path(a['file_path']).is_file() for a in assets))
        self.assertFalse(self.ns['UPLOADED_REFERENCES'])

    def test_restart_style_runner_uses_saved_snapshot(self):
        assets = self.prepare_references()
        result = self.generate()
        for asset in assets:
            self.store.detach_asset_from_scene(self.scene['id'], asset['id'])
        # The recovery call receives only row fields; it does not reread scene_assets.
        row = self.store.get(result['id'])
        async def worker(*args, **kwargs):
            self.assertEqual([Path(p).read_bytes() for p in kwargs['reference_image_paths']], [resolve_asset_path(a['file_path']).read_bytes() for a in assets])
            output = Path(self.tmp.name) / 'recovered.mp4'
            output.write_bytes(b'mocked')
            return {'local_path':str(output)}
        self.ns['pool'].generate_video = worker
        run_saved_task(self, (row['id'],row['model'],row['prompt'],row['ratio'],row['duration'],
                                json.loads(row['reference_images']),self.client_policy))
        self.assertEqual(self.store.get(result['id'])['status'], 'completed')
        self.assertTrue(all(resolve_asset_path(a['file_path']).is_file() for a in assets))

    def test_snapshot_retains_sources_even_after_detach(self):
        assets = self.prepare_references()
        self.generate()
        for asset in assets:
            self.store.detach_asset_from_scene(self.scene['id'], asset['id'])
            response = self.http.delete(f"/api/admin/assets/{asset['id']}")
            self.assertEqual(response.status_code, 409)
            self.assertIn('snapshot', response.json()['detail'])
            self.assertTrue(resolve_asset_path(asset['file_path']).exists())

    def test_snapshot_is_immutable_through_generic_update(self):
        self.prepare_references()
        result = self.generate()
        with self.assertRaisesRegex(ValueError, 'immutable'):
            self.store.update(result['id'], reference_snapshot='[]')

    def test_generate_empty_prompt_missing_file_unknown_alias_and_start_end_validation(self):
        empty = self.create_scene(27)
        self.assertEqual(self.http.post(f"/api/admin/scenes/{empty['id']}/generate").status_code, 422)
        self.assertEqual(self.http.post('/api/admin/scenes/missing/generate').status_code, 404)
        assets = self.prepare_references()
        self.store.update_scene(self.scene['id'], start_end=True)
        self.assertEqual(self.http.post(f"/api/admin/scenes/{self.scene['id']}/generate").status_code, 422)
        self.store.update_scene(self.scene['id'], start_end=False, prompt='@Unknown moves')
        self.assertEqual(self.http.post(f"/api/admin/scenes/{self.scene['id']}/generate").status_code, 422)
        self.store.update_scene(self.scene['id'], prompt='@Riven walks')
        resolve_asset_path(assets[0]['file_path']).unlink()
        self.assertEqual(self.http.post(f"/api/admin/scenes/{self.scene['id']}/generate").status_code, 422)
        self.assertEqual(self.store.pending_task_count(), 0)

    def test_scene_generation_reuses_batch_and_quota_admission(self):
        self.prepare_references()
        self.client_policy['daily_limit'] = 1
        self.assertEqual(self.http.post(f"/api/admin/scenes/{self.scene['id']}/generate", json={'count':2}).status_code, 429)
        self.assertEqual(self.store.pending_task_count(), 0)
        self.client_policy['daily_limit'] = 0
        self.ns['config'].MAX_PENDING_TASKS = 1
        self.assertEqual(self.http.post(f"/api/admin/scenes/{self.scene['id']}/generate", json={'count':2}).status_code, 429)
        self.ns['config'].MAX_PENDING_TASKS = 100
        result = self.generate(count=2)
        self.assertEqual(len(result['tasks']), 2)
        rows = [self.store.get(t['id']) for t in result['tasks']]
        self.assertEqual(rows[0]['reference_snapshot'], rows[1]['reference_snapshot'])
        self.assertTrue(all(r['scene_id'] == self.scene['id'] for r in rows))

    def test_no_automatic_selected_output_replacement(self):
        self.prepare_references()
        first = self.generate()
        self.store.update(first['id'], status='completed', video_url='http://test/videos/a.mp4')
        self.store.select_scene_task(self.scene['id'], first['id'])
        self.generate()
        self.assertEqual(self.store.get_scene(self.scene['id'])['selected_task_id'], first['id'])

    def test_assets_reusable_in_multiple_scenes(self):
        asset = self.asset()
        other_scene = self.create_scene(27, '@Riven moves')
        self.store.update_scene(self.scene['id'], prompt='@Riven stands')
        self.attach(asset, 'Riven', 1)
        self.attach(asset, 'Riven', 1, scene=other_scene)
        first = self.generate()
        second = self.generate(scene=other_scene)
        self.assertEqual(json.loads(self.store.get(first['id'])['reference_snapshot'])[0]['asset_id'], asset['id'])
        self.assertEqual(json.loads(self.store.get(second['id'])['reference_snapshot'])[0]['asset_id'], asset['id'])
        self.assertEqual(len(self.store.list_project_assets(self.project_id)), 1)

    def test_admin_auth_and_openapi(self):
        self.assertEqual(self.http.get('/api/admin/projects', headers={'X-Admin-Key':'bad'}).status_code, 401)
        self.assertEqual(self.http.post(f'/api/admin/projects/{self.project_id}/assets',
            headers={'X-Admin-Key':'bad'}, data={'name':'Hero','type':'character'},
            files={'file':('hero.png',image_bytes(),'image/png')}).status_code, 401)
        spec = self.http.get('/openapi.json').json()
        self.assertIn('/v1/videos/generations', spec['paths'])
        self.assertNotIn('scene_id', spec['components']['schemas']['VideoGenRequest']['properties'])
        self.assertNotIn('reference_snapshot', spec['components']['schemas']['TaskResponse']['properties'])


class SnapshotMigrationTests(unittest.TestCase):
    def test_existing_version_two_upgrade_preserves_data_backup_and_idempotence(self):
        with tempfile.TemporaryDirectory() as folder:
            path = Path(folder) / 'tasks.db'
            with patch.object(TaskStore, '_migrate_v3', lambda self: None):
                store = TaskStore(str(path))
            project = store.create_project('Legacy V2')
            scene = store.create_scene(project['id'], 1)
            with store._conn:
                store._conn.execute("INSERT INTO tasks(id,prompt,status,created_at,updated_at,scene_id) VALUES ('old','old prompt','failed',1,2,?)", (scene['id'],))
            store._conn.close()
            for _ in range(2):
                upgraded = TaskStore(str(path))
                self.assertEqual(upgraded._conn.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
                self.assertIsNone(upgraded.get('old')['reference_snapshot'])
                self.assertEqual(upgraded.get('old')['scene_id'], scene['id'])
                self.assertEqual(upgraded.get('old')['prompt'], 'old prompt')
                self.assertEqual(upgraded.get_project(project['id']), project)
                upgraded._conn.close()
            with closing(sqlite3.connect(str(path) + '.before_v3.bak')) as backup:
                self.assertEqual(backup.execute('PRAGMA user_version').fetchone()[0], 2)
                self.assertNotIn('reference_snapshot', {r[1] for r in backup.execute('PRAGMA table_info(tasks)')})
                self.assertEqual(backup.execute('SELECT count(*) FROM tasks').fetchone()[0], 1)

    def test_snapshot_input_is_validated_and_failed_batch_rolls_back(self):
        store = TaskStore(':memory:')
        try:
            project = store.create_project('Film')
            scene = store.create_scene(project['id'], 1)
            for snapshot in ({}, [{'asset_id':'missing','reference_alias':'Hero','position':1,'file_path':'missing/hero.png'}]):
                with self.assertRaises(ValueError):
                    store.create('a','model','prompt','16:9',10,scene_id=scene['id'],reference_snapshot=snapshot)
                self.assertIsNone(store.get('a'))
                self.assertFalse(store._conn.in_transaction)
        finally:
            store._conn.close()


class LegacyReferenceTests(unittest.TestCase):
    def test_existing_temporary_reference_batch_worker_cleanup(self):
        asyncio.run(backend_checks())


if __name__ == '__main__':
    unittest.main()
