"""Offline checks for custom task metadata alongside upstream start/end."""
import asyncio
import importlib
import unittest
from unittest.mock import MagicMock, patch

from store import TaskStore


class MergeCompatibilityTests(unittest.TestCase):
    def test_start_end_and_scene_name_reach_storage_and_worker(self):
        database = TaskStore(':memory:')
        pool = MagicMock()
        pool.accounts = ['acc1']
        pool.available = True
        with patch('store.TaskStore', return_value=database), \
             patch('browser_pool.BrowserPool', return_value=pool):
            server = importlib.import_module('server')
        server.UPLOADED_REFERENCES['uploaded://test'] = (None, ['start.png', 'end.png'])
        queued = []

        def capture(coroutine):
            queued.append(coroutine.cr_frame.f_locals.copy())
            coroutine.close()

        try:
            with patch.object(server, 'store', database), \
                 patch.object(server, 'pool', pool), \
                 patch.object(server, '_auth', return_value=server._anonymous_client()), \
                 patch.object(server.asyncio, 'create_task', side_effect=capture):
                result = asyncio.run(server.create_video(server.VideoGenRequest(
                    name=' scene 2 ', prompt='A moving subject', start_end=True,
                    reference_images=['uploaded://test'])))
                row = database.get(result.id)
                self.assertEqual(row['name'], 'scene 2')
                self.assertEqual(row['start_end'], 1)
                self.assertIn('opening frame', result.prompt)
                self.assertEqual(row['prompt'], result.prompt)
                self.assertEqual(queued[0]['prompt'], result.prompt)
                for i in range(205):
                    database.create(str(i), 'model', 'prompt', '16:9', 10)
                with patch.object(server, '_admin_auth'):
                    self.assertEqual(len(asyncio.run(server.admin_tasks())['tasks']), 206)
        finally:
            server.UPLOADED_REFERENCES.pop('uploaded://test', None)
            database._conn.close()


if __name__ == '__main__':
    unittest.main()
