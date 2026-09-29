"""Offline regression for custom scene metadata with upstream batching."""
import asyncio
import unittest
from unittest.mock import patch
from test_video_batch import isolated_api
from store import TaskStore

class MergeCompatibilityTests(unittest.TestCase):
    def test_named_start_end_batch(self):
        ns, _ = isolated_api()
        queued = []
        def capture(coroutine):
            queued.append(coroutine.cr_frame.f_locals.copy())
            coroutine.close()
        try:
            with patch.object(ns['asyncio'], 'create_task', side_effect=capture):
                req = ns['VideoGenRequest'](name=' scene 2 ', prompt='@Hero moves toward @Door', count=2,
                    reference_images=['https://example.com/a.png','https://example.com/b.png'],
                    reference_aliases=['Hero','Door'], start_end=True)
                result = asyncio.run(ns['create_video'](req, None))
            self.assertEqual(len(result.tasks), 2)
            for index, task in enumerate(result.tasks, 1):
                row = ns['store'].get(task.id)
                self.assertEqual(row['name'], f'scene 2 ({index}/2)')
                self.assertEqual(task.name, row['name'])
                self.assertEqual(row['start_end'], 1)
                self.assertEqual(row['batch_index'], index)
                self.assertIn('opening frame', row['prompt'])
                self.assertIn('@Image1 moves toward @Image2', row['prompt'])
                self.assertEqual(queued[index-1]['prompt'], row['prompt'])
        finally:
            ns['store']._conn.close()

    def test_unlimited_scene_search(self):
        store = TaskStore(':memory:')
        try:
            for i in range(205):
                store.create(str(i), 'model', 'prompt', '16:9', 10, name='scene')
            self.assertEqual(len(store.recent_tasks(-1, query='scene', search_in='name')), 205)
            store.create('plain', 'model', 'prompt', '16:9', 10)
            self.assertEqual(store.get('plain')['name'], '')
        finally:
            store._conn.close()

if __name__ == '__main__': unittest.main()
