import unittest
from store import TaskStore, TaskSubmissionConflict

class BatchManagementTests(unittest.TestCase):
    def setUp(self):
        self.store = TaskStore(':memory:')
        self.add(['a', 'b'])
    def tearDown(self):
        self.store._conn.close()
    def add(self, ids, **kwargs):
        self.store.create_batch(ids, 'seedance-2.0', 'prompt', '16:9', 10, batch_id='batch_test', api_key_hash=kwargs.pop('api_key_hash', 'owner'), **kwargs)
    def test_rename_and_append(self):
        self.store.rename_batch('batch_test', 'My batch')
        self.add(['c'], append_batch=True)
        rows = [self.store.get(key) for key in ['a','b','c']]
        self.assertEqual([row['batch_index'] for row in rows], [1,2,3])
        self.assertTrue(all(row['batch_count']==3 and row['batch_name']=='My batch' for row in rows))
        self.assertEqual(self.store.list_batches()[0]['task_count'], 3)
    def test_foreign_owner_rolls_back(self):
        with self.assertRaises(TaskSubmissionConflict):
            self.add(['c'], append_batch=True, api_key_hash='other')
        self.assertIsNone(self.store.get('c'))
        self.assertEqual(self.store.get('a')['batch_count'], 2)
    def test_unknown_batch(self):
        with self.assertRaises(KeyError):
            self.store.rename_batch('missing', 'Name')
        with self.assertRaises(TaskSubmissionConflict):
            self.store.create_batch(['c'], 'model', 'prompt', '16:9', 10, batch_id='missing', append_batch=True)

if __name__ == '__main__':
    unittest.main()
