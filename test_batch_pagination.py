import unittest

from store import TaskStore


class BatchPaginationTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(':memory:')
        for batch in range(12):
            ids = [f't{batch}_{i}' for i in range(30 if batch == 11 else 2)]
            for start in range(0, len(ids), 5):
                self.store.create_batch(ids[start:start + 5], 'model', 'prompt', '16:9', 10,
                                        batch_id=f'b{batch}', append_batch=start > 0)
            for task_id in ids:
                self.store.update(task_id, created_at=batch)

    def tearDown(self):
        self.store._conn.close()

    def test_whole_batches_and_disjoint_pages(self):
        first = self.store.recent_tasks(page=1, group_by_batch=True)
        second = self.store.recent_tasks(page=2, group_by_batch=True)
        self.assertEqual(first['pages'], 2)
        self.assertEqual(first['total_groups'], 12)
        self.assertEqual(len({t['batch_id'] for t in first['tasks']}), 10)
        self.assertEqual(len([t for t in first['tasks'] if t['batch_id'] == 'b11']), 30)
        self.assertFalse({t['id'] for t in first['tasks']} & {t['id'] for t in second['tasks']})
        self.assertEqual(len(first['tasks']) + len(second['tasks']), first['total'])
        self.assertEqual(self.store.recent_tasks(page=99, group_by_batch=True)['page'], 2)

    def test_filters_and_standalone_groups(self):
        self.store.update('t11_0', status='completed', batch_id=None)
        self.store.update('t11_1', status='completed', batch_id='')
        result = self.store.recent_tasks(page=5, status='completed', group_by_batch=True)
        self.assertEqual(result['total_groups'], 2)
        self.assertEqual(result['page'], 1)
        self.assertEqual(len(result['tasks']), 2)
        empty = self.store.recent_tasks(page=2, status='failed', group_by_batch=True)
        self.assertEqual((empty['pages'], empty['page'], empty['tasks']), (1, 1, []))

    def test_task_pagination_unchanged(self):
        result = self.store.recent_tasks(limit=10, page=1)
        self.assertEqual(len(result['tasks']), 10)
        self.assertEqual(result['pages'], 6)


if __name__ == '__main__':
    unittest.main()
