"""Migration and shared queue regression checks; only temporary databases."""
import sqlite3
import tempfile
import unittest
from contextlib import closing
from pathlib import Path
from store import TaskStore


class QueueMergeTests(unittest.TestCase):
    def test_project_v6_backup_before_queue_columns(self):
        with tempfile.TemporaryDirectory() as root:
            path = Path(root) / 'tasks.db'
            db = TaskStore(str(path))
            project = db.create_project('Fixture')
            db.create('video_fixture', 'seedance-2.5', 'prompt', '16:9', 30, name='scene 2')
            db._conn.execute('ALTER TABLE tasks DROP COLUMN phase')
            db._conn.commit()
            db._conn.close()
            db = TaskStore(str(path))
            backup = Path(str(path) + '.before_queue.bak')
            self.assertTrue(backup.is_file())
            with closing(sqlite3.connect(backup)) as saved:
                self.assertNotIn('phase', {r[1] for r in saved.execute('PRAGMA table_info(tasks)')})
                self.assertEqual(saved.execute('SELECT name FROM tasks').fetchone()[0], 'scene 2')
            self.assertEqual(db.get('video_fixture')['phase'], 'ready')
            self.assertEqual(db.get_project(project['id'])['name'], 'Fixture')
            db._conn.close()
            contents = backup.read_bytes()
            db = TaskStore(str(path))
            db._conn.close()
            self.assertEqual(backup.read_bytes(), contents)

    def test_all_and_filters_keep_edit_selection(self):
        db = TaskStore(':memory:')
        try:
            for i in range(205):
                db.create(f'video_{i}', 'seedance-2.5', 'prompt', '16:9', 30)
            db.update('video_0', status='completed', video_url='/videos/scene_video_0.mp4', edit_selected=1)
            self.assertEqual(db.recent_tasks(limit=-1, page=1)['total'], 205)
            self.assertEqual(len(db.recent_tasks(limit=-1, page=1)['tasks']), 205)
            result = db.recent_tasks(limit=-1, page=1, edit_selected=True, video_filter='available')
            self.assertEqual([r['id'] for r in result['tasks']], ['video_0'])
        finally:
            db._conn.close()


if __name__ == '__main__':
    unittest.main()
