"""Tag copies and persistence tests; only temporary files/databases."""
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from store import TaskStore
from video_tags import set_edit_selection
import ast
import asyncio
from fastapi import FastAPI, HTTPException, Header
from pydantic import BaseModel
from types import SimpleNamespace
from unittest.mock import Mock


class VideoTagTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.downloads = self.root / 'downloads'
        self.downloads.mkdir()
        self.tags = self.root / 'tag'
        self.store = TaskStore(str(self.root / 'tasks.db'))
        self.store.create('video_one', 'model', 'prompt', '16:9', 10, name='scene 2')
        self.store.update('video_one', status='completed', video_url='http://localhost/videos/original.mp4')
        self.original = self.downloads / 'original.mp4'
        self.original.write_bytes(b'original video fixture')

    def tearDown(self):
        self.store._conn.close()
        self.temp.cleanup()

    def select(self, selected=True, task='video_one'):
        return set_edit_selection(self.store, self.store.get(task), selected, self.downloads, self.tags)

    def test_copy_idempotence_restart_and_unselect(self):
        name = self.select()
        self.assertEqual((self.tags / name).read_bytes(), self.original.read_bytes())
        self.assertIn('scene 2', name)
        self.assertEqual(self.select(), name)
        self.assertEqual(len(list(self.tags.iterdir())), 1)
        self.store._conn.close()
        self.store = TaskStore(str(self.root / 'tasks.db'))
        self.assertEqual(self.store.get('video_one')['edit_selected'], 1)
        self.assertEqual(len(self.store.recent_tasks(-1, edit_selected=True)), 1)
        self.assertEqual(self.store.recent_tasks(-1, edit_selected=False), [])
        self.select(False)
        self.select(False)
        self.assertFalse((self.tags / name).exists())
        self.assertEqual(self.original.read_bytes(), b'original video fixture')
        self.assertEqual(self.store.get('video_one')['edit_selected'], 0)
        self.assertEqual(len(self.store.recent_tasks(-1, edit_selected=False)), 1)

    def test_same_scene_has_distinct_files(self):
        first = self.select()
        self.store.create('video_two', 'model', 'prompt', '16:9', 10, name='scene 2')
        self.store.update('video_two', status='completed', video_url='http://localhost/videos/original.mp4')
        second = self.select(task='video_two')
        self.assertNotEqual(first, second)
        self.assertEqual(len(list(self.tags.iterdir())), 2)

    def test_missing_original_and_incomplete_do_not_mark(self):
        self.original.unlink()
        with self.assertRaises(FileNotFoundError):
            self.select()
        self.assertFalse(self.store.get('video_one')['edit_selected'])
        self.store.update('video_one', status='processing')
        with self.assertRaises(ValueError):
            self.select()

    def test_copy_failure_and_database_failure_roll_back(self):
        with patch('video_tags.shutil.copy2', side_effect=OSError('disk full')):
            with self.assertRaises(OSError):
                self.select()
        self.assertEqual(list(self.tags.iterdir()), [])
        with patch.object(self.store, 'update', side_effect=RuntimeError('DB failed')):
            with self.assertRaises(RuntimeError):
                self.select()
        self.assertEqual(list(self.tags.iterdir()), [])
        filename = self.select()
        with patch.object(self.store, 'update', side_effect=RuntimeError('DB failed')):
            with self.assertRaises(RuntimeError):
                self.select(False)
        self.assertTrue((self.tags / filename).is_file())
        self.assertTrue(self.store.get('video_one')['edit_selected'])

    def test_paths_are_contained_and_existing_files_not_overwritten(self):
        self.store.update('video_one', video_url='http://localhost/videos/%2e%2e/secret.mp4')
        with self.assertRaises(ValueError):
            self.select()
        self.store.update('video_one', video_url='http://localhost/videos/original.mp4', tag_filename='../secret.mp4')
        with self.assertRaises(ValueError):
            self.select(False)
        self.store.update('video_one', tag_filename='existing.mp4')
        self.tags.mkdir(exist_ok=True)
        (self.tags / 'existing.mp4').write_bytes(b'keep')
        with self.assertRaises(ValueError):
            self.select()
        self.assertEqual((self.tags / 'existing.mp4').read_bytes(), b'keep')

    def test_endpoint_auth_filter_and_delete_protection(self):
        import logging
        tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
        names = {'EditSelectionRequest', 'admin_edit_selection', 'admin_task_delete', 'admin_tasks'}
        nodes = [n for n in tree.body if isinstance(n, (ast.ClassDef, ast.AsyncFunctionDef)) and n.name in names]
        auth = Mock()
        ns = dict(app=FastAPI(), BaseModel=BaseModel, Header=Header, HTTPException=HTTPException,
                  asyncio=asyncio, Path=Path, logging=logging, store=self.store,
                  _admin_auth=auth, config=SimpleNamespace(DOWNLOAD_DIR=str(self.downloads)),
                  TASK_ACTION_LOCKS={}, TASK_RUNNERS={}, pool=Mock(),
                  __file__=str(self.root / 'server.py'))
        exec(compile(ast.Module(body=nodes, type_ignores=[]), 'server.py', 'exec'), ns)
        async def exercise():
            body = ns['EditSelectionRequest'](selected=True)
            result = await ns['admin_edit_selection']('video_one', body, 'admin-test')
            self.assertTrue(result['edit_selected'])
            auth.assert_called_with('admin-test')
            rows = await ns['admin_tasks'](edit_selected=True)
            self.assertEqual(len(rows['tasks']), 1)
            with self.assertRaises(HTTPException) as raised:
                await ns['admin_task_delete']('video_one', 'admin-test')
            self.assertEqual(raised.exception.status_code, 409)
            await ns['admin_edit_selection']('video_one', ns['EditSelectionRequest'](selected=False), 'admin-test')
            await ns['admin_task_delete']('video_one', 'admin-test')
            self.assertTrue(self.original.exists())
        asyncio.run(exercise())


if __name__ == '__main__':
    unittest.main()
