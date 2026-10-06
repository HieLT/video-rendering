"""V2 foundation tests: temporary SQLite only; no accounts, server, or generation."""
from contextlib import closing
from pathlib import Path
import sqlite3
import tempfile
import unittest
from unittest.mock import patch

from asset_storage import asset_relative_path, asset_root, resolve_asset_path
from reference_aliases import resolve_reference_aliases
from store import TaskStore
from production_store import SCHEMA_VERSION


class MigrationTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'tasks.db'

    def tearDown(self):
        self.tmp.cleanup()

    def legacy(self):
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript("""
                CREATE TABLE tasks (id TEXT PRIMARY KEY, name TEXT, model TEXT, prompt TEXT,
                ratio TEXT, duration INTEGER, status TEXT, video_url TEXT, error TEXT,
                created_at REAL, updated_at REAL, deleted_at REAL, edit_selected INTEGER,
                tag_filename TEXT, account TEXT, account_uuid TEXT, batch_id TEXT,
                batch_index INTEGER, batch_count INTEGER, start_end INTEGER);
                INSERT INTO tasks VALUES ('old','scene 26','seedance-2.5','@Image1 walks',
                '16:9',30,'completed','http://localhost/videos/old.mp4',NULL,
                123,456,NULL,1,'old-tag.mp4','account','uuid','batch',1,2,1);
                INSERT INTO tasks SELECT 'hidden',name,model,prompt,ratio,duration,status,
                video_url,error,created_at,updated_at,789,edit_selected,tag_filename,
                account,account_uuid,batch_id,batch_index,batch_count,start_end FROM tasks;
                CREATE TABLE api_keys (key TEXT PRIMARY KEY, name TEXT, enabled INTEGER,
                created_at REAL, last_used_at REAL);
                INSERT INTO api_keys VALUES ('test-key','client',1,1,2);
                CREATE TABLE generated_reset_tasks (task_id TEXT PRIMARY KEY);
                INSERT INTO generated_reset_tasks VALUES ('old');
            """)
            rows = conn.execute('SELECT * FROM tasks ORDER BY id').fetchall()
            columns = [r[1] for r in conn.execute('PRAGMA table_info(tasks)')]
            rootpage = conn.execute("SELECT rootpage FROM sqlite_master WHERE name='tasks'").fetchone()[0]
        return columns, rows, rootpage

    def test_fresh_database_schema_and_foreign_keys(self):
        store = TaskStore(str(self.path))
        try:
            self.assertEqual(store._conn.execute('PRAGMA user_version').fetchone()[0], SCHEMA_VERSION)
            self.assertEqual(store._conn.execute('PRAGMA foreign_keys').fetchone()[0], 1)
            tables = {r[0] for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='table'")}
            self.assertTrue({'tasks','projects','scenes','assets','scene_assets','api_keys','generated_reset_tasks'} <= tables)
            fk = store._conn.execute('PRAGMA foreign_key_list(tasks)').fetchall()
            self.assertTrue(any(r[2] == 'scenes' and r[3] == 'scene_id' for r in fk))
            self.assertEqual(store._conn.execute('PRAGMA foreign_key_check').fetchall(), [])
            self.assertFalse(store._conn.in_transaction)
            indexes = {r[0] for r in store._conn.execute("SELECT name FROM sqlite_master WHERE type='index'")}
            self.assertTrue({'tasks_scene_created','assets_project','scene_assets_asset','scenes_selected_task'} <= indexes)
        finally:
            store._conn.close()
        self.assertTrue(Path(str(self.path) + '.before_v2.bak').is_file())

    def test_history_backup_and_idempotence_without_rebuilding_tasks(self):
        columns, before, rootpage = self.legacy()
        backup = Path(str(self.path) + '.before_v2.bak')
        for _ in range(2):
            store = TaskStore(str(self.path))
            try:
                after = [tuple(store.get(row[0])[key] for key in columns) for row in before]
                self.assertEqual(after, before)
                self.assertIsNone(store.get('old')['scene_id'])
                self.assertIsNone(store.get('hidden')['scene_id'])
                self.assertEqual(store.list_projects(), [])
                self.assertEqual(store._conn.execute("SELECT rootpage FROM sqlite_master WHERE name='tasks'").fetchone()[0], rootpage)
                self.assertEqual(store._conn.execute('SELECT * FROM generated_reset_tasks').fetchone()[0], 'old')
                self.assertEqual(store.get_key('test-key')['name'], 'client')
            finally:
                store._conn.close()
        with closing(sqlite3.connect(backup)) as conn:
            self.assertEqual(conn.execute('SELECT * FROM tasks ORDER BY id').fetchall(), before)
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertNotIn('scene_id', {r[1] for r in conn.execute('PRAGMA table_info(tasks)')})

    def test_migration_failure_rolls_back_and_releases_lock(self):
        columns, before, _ = self.legacy()
        original = TaskStore._migrate_v2
        def fail_after_schema(store):
            original(store)
            raise sqlite3.OperationalError('injected migration failure')
        with patch.object(TaskStore, '_migrate_v2', fail_after_schema):
            with self.assertRaisesRegex(sqlite3.OperationalError, 'injected'):
                TaskStore(str(self.path))
        backup_bytes = Path(str(self.path) + '.before_v2.bak').read_bytes()
        with closing(sqlite3.connect(self.path, timeout=0)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertEqual([r[1] for r in conn.execute('PRAGMA table_info(tasks)')], columns)
            self.assertEqual(conn.execute('SELECT * FROM tasks ORDER BY id').fetchall(), before)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='projects'").fetchone())
            conn.execute('BEGIN IMMEDIATE')
            conn.rollback()
        store = TaskStore(str(self.path))
        store._conn.close()
        self.assertEqual(Path(str(self.path) + '.before_v2.bak').read_bytes(), backup_bytes)

    def test_foreign_key_check_failure_rolls_back(self):
        self.legacy()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.executescript("CREATE TABLE broken (task_id TEXT REFERENCES tasks(id)); INSERT INTO broken VALUES ('missing');")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'foreign_key_check'):
            TaskStore(str(self.path))
        with closing(sqlite3.connect(self.path, timeout=0)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertNotIn('scene_id', {r[1] for r in conn.execute('PRAGMA table_info(tasks)')})
            conn.execute('BEGIN IMMEDIATE')
            conn.rollback()

    def test_backup_failure_never_publishes_incomplete_backup(self):
        columns, before, _ = self.legacy()
        with patch('production_store.os.link', side_effect=OSError('injected backup failure')):
            with self.assertRaisesRegex(OSError, 'backup failure'):
                TaskStore(str(self.path))
        self.assertFalse(Path(str(self.path) + '.before_v2.bak').exists())
        self.assertEqual(list(self.path.parent.glob('*.before_v2.bak.*')), [])
        with closing(sqlite3.connect(self.path)) as conn:
            self.assertEqual(conn.execute('SELECT * FROM tasks ORDER BY id').fetchall(), before)
            self.assertEqual([r[1] for r in conn.execute('PRAGMA table_info(tasks)')], columns)
        store = TaskStore(str(self.path))
        store._conn.close()

    def test_existing_scene_id_without_foreign_key_is_rejected_safely(self):
        self.legacy()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute('ALTER TABLE tasks ADD COLUMN scene_id TEXT')
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'missing the required'):
            TaskStore(str(self.path))
        with closing(sqlite3.connect(self.path, timeout=0)) as conn:
            self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0], 0)
            self.assertIsNone(conn.execute("SELECT name FROM sqlite_master WHERE name='projects'").fetchone())
            conn.execute('BEGIN IMMEDIATE')
            conn.rollback()

    def test_newer_schema_is_rejected_without_changes(self):
        self.legacy()
        with closing(sqlite3.connect(self.path)) as conn:
            conn.execute(f'PRAGMA user_version={SCHEMA_VERSION + 1}')
        with self.assertRaisesRegex(RuntimeError, 'Unsupported'):
            TaskStore(str(self.path))
        self.assertFalse(Path(str(self.path) + '.before_v2.bak').exists())

    def test_reopen_preserves_production_data_and_selection(self):
        store = TaskStore(str(self.path))
        project = store.create_project('Film')
        scene = store.create_scene(project['id'], 26)
        store.create_asset(project['id'], 'Riven', 'character', f"{project['id']}/riven.webp")
        store.attach_asset_to_scene(scene['id'], 'riven', 1, 'Riven')
        store.create('attempt', 'seedance-2.5', '@Image1 walks', '16:9', 30, scene_id=scene['id'])
        store.update('attempt', status='completed', video_url='http://localhost/videos/a.mp4')
        store.select_scene_task(scene['id'], 'attempt')
        store._conn.close()
        store = TaskStore(str(self.path))
        try:
            self.assertEqual(store.get_scene(scene['id'])['selected_task_id'], 'attempt')
            self.assertEqual(store.list_scene_assets(scene['id'])[0]['asset_id'], 'riven')
            self.assertEqual(store.get('attempt')['scene_id'], scene['id'])
        finally:
            store._conn.close()


class ProductionTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name) / 'tasks.db'
        self.store = TaskStore(str(self.path))
        self.project = self.store.create_project('Film', project_id='film')
        self.scene = self.store.create_scene('film', 26, 'The First Reaction', '@Riven walks',
                                             'seedance-2.5', '16:9', 30)
        self.scene_id = self.scene['id']

    def tearDown(self):
        self.store._conn.close()
        self.tmp.cleanup()

    def asset(self, identifier='riven', extension='.png', project_id='film'):
        return self.store.create_asset(project_id, identifier, 'character',
                                       f'{project_id}/{identifier}{extension}', asset_id=identifier)

    def task(self, identifier='attempt', scene_id=None, status='completed', url='http://localhost/videos/a.mp4'):
        self.store.create(identifier, 'seedance-2.5', '@Image1 walks', '16:9', 30,
                           scene_id=self.scene_id if scene_id is None else scene_id)
        self.store.update(identifier, status=status, video_url=url)
        return identifier

    def unlocked(self):
        self.assertFalse(self.store._conn.in_transaction)
        with closing(sqlite3.connect(self.path, timeout=0)) as conn:
            conn.execute('BEGIN IMMEDIATE')
            conn.rollback()

    def test_project_and_scene_crud(self):
        self.assertEqual(self.store.get_project('film'), self.project)
        self.assertEqual(self.store.list_projects(), [self.project])
        self.store.update_project('film', name='New film')
        self.assertEqual(self.store.get_project('film')['name'], 'New film')
        earlier = self.store.create_scene('film', 2)
        self.assertEqual([s['scene_number'] for s in self.store.list_project_scenes('film')], [2,26])
        changed = self.store.update_scene(earlier['id'], scene_name='Opening', prompt='new', start_end=True)
        self.assertEqual(changed['start_end'], 1)
        self.assertEqual(changed['scene_name'], 'Opening')
        self.assertIsNone(self.store.get_scene('missing'))
        self.assertIsNone(self.store.get_project('missing'))

    def test_scene_project_and_unique_number_validation(self):
        with self.assertRaises(ValueError):
            self.store.create_scene('missing', 1)
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_scene('film', 26)
        for fields in ({'duration':11}, {'scene_number':0}, {'scene_number':True}, {'start_end':2},
                       {'project_id':'other'}, {'selected_task_id':'attempt'}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.store.update_scene(self.scene_id, **fields)
        self.unlocked()

    def test_asset_metadata_all_extensions_and_immutable_identity(self):
        for index, ext in enumerate(('.jpg','.jpeg','.png','.webp')):
            asset = self.asset(f'asset{index}', ext)
            self.assertEqual(asset['file_path'], f'film/asset{index}{ext}')
            self.assertEqual(self.store.get_asset(asset['id']), asset)
            self.assertFalse(Path(asset['file_path']).is_absolute())
        self.assertEqual(len(self.store.list_project_assets('film')), 4)
        with self.assertRaises(sqlite3.IntegrityError):
            self.asset('asset0', '.webp')
        self.assertEqual(self.store.get_asset('asset0')['file_path'], 'film/asset0.jpg')
        self.assertIsNone(self.store.get_asset('missing'))

    def test_asset_project_type_and_path_validation(self):
        for project, kind, path in (
            ('missing','character','missing/a.png'),
            ('film','video','film/a.png'),
            ('film','character','../a.png'),
            ('film','character','C:/a.png'),
            ('film','character','film/a.gif'),
            ('film','character','other/a.png'),
            ('film','character','film/nested/a.png'),
            ('film','character','film/a.PNG'),
        ):
            with self.subTest(path=path, kind=kind), self.assertRaises(ValueError):
                self.store.create_asset(project, 'A', kind, path, asset_id='a')
        self.unlocked()

    def test_cannot_attach_cross_project_asset(self):
        self.store.create_project('Other', project_id='other')
        self.asset('otherasset', project_id='other')
        with self.assertRaisesRegex(ValueError, 'same project'):
            self.store.attach_asset_to_scene(self.scene_id, 'otherasset', 1, 'Hero')
        self.assertEqual(self.store.list_scene_assets(self.scene_id), [])
        self.unlocked()

    def test_ordering_and_alias_mapping_follow_position(self):
        self.asset('riven')
        self.asset('cabin')
        self.asset('sword')
        self.store.attach_asset_to_scene(self.scene_id, 'sword', 3, ' @Sword ')
        self.store.attach_asset_to_scene(self.scene_id, 'riven', 1, 'Riven')
        self.store.attach_asset_to_scene(self.scene_id, 'cabin', 2, 'Cabin')
        rows = self.store.list_scene_assets(self.scene_id)
        self.assertEqual([r['asset_id'] for r in rows], ['riven','cabin','sword'])
        self.assertEqual([r['reference_alias'] for r in rows], ['Riven','Cabin','Sword'])
        rows = self.store.reorder_scene_assets(self.scene_id, ['cabin','sword','riven'])
        self.assertEqual([r['position'] for r in rows], [1,2,3])
        self.assertEqual(resolve_reference_aliases('@Riven in @Cabin with @Sword',
                         [r['reference_alias'] for r in rows], 3), '@Image3 in @Image1 with @Image2')
        self.assertEqual(self.store.get_scene(self.scene_id)['prompt'], '@Riven walks')

    def test_duplicate_position_rejected_without_partial_changes(self):
        self.asset('a')
        self.asset('b')
        self.store.attach_asset_to_scene(self.scene_id, 'a', 1, 'A')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.attach_asset_to_scene(self.scene_id, 'b', 1, 'B')
        self.assertEqual(len(self.store.list_scene_assets(self.scene_id)), 1)
        self.unlocked()

    def test_duplicate_alias_case_insensitive_including_unicode(self):
        self.asset('a')
        self.asset('b')
        self.store.attach_asset_to_scene(self.scene_id, 'a', 1, 'HÉRO')
        with self.assertRaisesRegex(ValueError, 'Duplicate reference'):
            self.store.attach_asset_to_scene(self.scene_id, 'b', 2, ' @héro ')
        self.unlocked()

    def test_duplicate_attachment_and_invalid_alias(self):
        self.asset('a')
        self.store.attach_asset_to_scene(self.scene_id, 'a', 1, 'A')
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.attach_asset_to_scene(self.scene_id, 'a', 2, 'B')
        for alias in ('', '@', '@@A', 'A@B'):
            with self.subTest(alias=alias), self.assertRaises(ValueError):
                self.store.attach_asset_to_scene(self.scene_id, 'a', 2, alias)
        self.unlocked()

    def test_reorder_rejects_incomplete_duplicate_or_unknown_list(self):
        self.asset('a')
        self.asset('b')
        self.store.attach_asset_to_scene(self.scene_id, 'a', 5, 'A')
        self.store.attach_asset_to_scene(self.scene_id, 'b', 10, 'B')
        before = self.store.list_scene_assets(self.scene_id)
        for order in ([], ['a'], ['a','a'], ['a','missing']):
            with self.subTest(order=order), self.assertRaises(ValueError):
                self.store.reorder_scene_assets(self.scene_id, order)
            self.assertEqual(self.store.list_scene_assets(self.scene_id), before)
            self.unlocked()
        self.assertTrue(self.store.detach_asset_from_scene(self.scene_id, 'a'))
        self.assertFalse(self.store.detach_asset_from_scene(self.scene_id, 'a'))
        self.assertEqual(self.store.list_scene_assets(self.scene_id)[0]['position'], 10)
        self.assertEqual(self.store.reorder_scene_assets(self.scene_id, ['b'])[0]['position'], 1)

    def test_reorder_failure_rolls_back_all_positions(self):
        self.asset('a')
        self.asset('b')
        self.store.attach_asset_to_scene(self.scene_id, 'a', 1, 'A')
        self.store.attach_asset_to_scene(self.scene_id, 'b', 2, 'B')
        before = self.store.list_scene_assets(self.scene_id)
        before_scene = self.store.get_scene(self.scene_id)
        self.store._conn.execute("""CREATE TRIGGER fail_reorder BEFORE UPDATE OF position ON scene_assets
            WHEN NEW.position=2 BEGIN SELECT RAISE(ABORT, 'injected reorder failure'); END""")
        with self.assertRaisesRegex(sqlite3.IntegrityError, 'reorder failure'):
            self.store.reorder_scene_assets(self.scene_id, ['b','a'])
        self.assertEqual(self.store.list_scene_assets(self.scene_id), before)
        self.assertEqual(self.store.get_scene(self.scene_id), before_scene)
        self.unlocked()

    def test_selection_failure_preserves_previous_selection(self):
        self.task('a')
        self.task('b', status='failed')
        self.store.select_scene_task(self.scene_id, 'a')
        before = self.store.get_scene(self.scene_id)
        with self.assertRaises(ValueError):
            self.store.select_scene_task(self.scene_id, 'b')
        self.assertEqual(self.store.get_scene(self.scene_id), before)
        self.unlocked()

    def test_metadata_does_not_touch_persistent_file(self):
        with patch('config.ASSET_DIR', self.tmp.name):
            folder = Path(self.tmp.name) / 'film'
            folder.mkdir()
            original = folder / 'a.webp'
            original.write_bytes(b'persistent file')
            self.asset('a', '.webp')
            with self.assertRaises(sqlite3.IntegrityError):
                self.asset('a', '.webp')
            self.store.attach_asset_to_scene(self.scene_id, 'a', 1, 'Hero')
            self.store.detach_asset_from_scene(self.scene_id, 'a')
            self.assertEqual(original.read_bytes(), b'persistent file')

    def test_old_create_batch_without_scene(self):
        # Retains the entire original positional signature.
        self.store.create_batch(['a','b'], 'model', 'prompt', '16:9', 10,
                                None, None, None, None, 0, 0, 0, False, 'batch', 'old name')
        self.assertIsNone(self.store.get('a')['scene_id'])
        self.assertEqual(self.store.get('a')['name'], 'old name (1/2)')
        self.assertEqual(self.store.get('b')['batch_count'], 2)

    def test_scene_tasks_snapshot_and_no_automatic_selection(self):
        self.task('a')
        self.store.select_scene_task(self.scene_id, 'a')
        self.task('b')
        self.store.update_scene(self.scene_id, prompt='Changed', model='seedance-2.0', duration=10, scene_name='Renamed')
        self.assertEqual(self.store.get_scene(self.scene_id)['selected_task_id'], 'a')
        self.assertEqual(self.store.get('a')['prompt'], '@Image1 walks')
        self.assertEqual(self.store.get('a')['duration'], 30)
        self.assertEqual(self.store.get('b')['scene_id'], self.scene_id)
        self.store.select_scene_task(self.scene_id, 'b')
        self.assertEqual(self.store.get_scene(self.scene_id)['selected_task_id'], 'b')
        self.assertFalse(self.store.get('b')['edit_selected'])

    def test_invalid_scene_rejected_before_task_insertion(self):
        with self.assertRaisesRegex(ValueError, 'scenes record not found'):
            self.store.create_batch(['a','b'], 'model','prompt','16:9',10, scene_id='missing')
        self.assertIsNone(self.store.get('a'))
        self.assertIsNone(self.store.get('b'))
        self.unlocked()

    def test_generic_update_cannot_change_scene_assignment(self):
        self.task('a')
        other = self.store.create_scene('film', 27)
        for scene in (other['id'], None, self.scene_id):
            with self.subTest(scene=scene), self.assertRaisesRegex(ValueError, 'immutable'):
                self.store.update('a', scene_id=scene)
        self.assertEqual(self.store.get('a')['scene_id'], self.scene_id)

    def test_select_task_from_other_scene_or_legacy_task_rejected(self):
        other = self.store.create_scene('film', 27)
        self.task('other', scene_id=other['id'])
        self.store.create('legacy','model','prompt','16:9',10)
        self.store.update('legacy', status='completed', video_url='url')
        for identifier in ('other','legacy','missing'):
            with self.subTest(task=identifier), self.assertRaises(ValueError):
                self.store.select_scene_task(self.scene_id, identifier)
        self.assertIsNone(self.store.get_scene(self.scene_id)['selected_task_id'])
        self.unlocked()

    def test_select_invalid_status_deleted_or_missing_video(self):
        for index, status in enumerate(('failed','processing','queued','stopped','needs_recovery')):
            identifier = self.task(str(index), status=status)
            with self.subTest(status=status), self.assertRaises(ValueError):
                self.store.select_scene_task(self.scene_id, identifier)
        self.task('deleted')
        self.store.delete_task('deleted')
        self.task('empty', url=' ')
        self.task('null', url=None)
        for identifier in ('deleted','empty','null'):
            with self.subTest(task=identifier), self.assertRaises(ValueError):
                self.store.select_scene_task(self.scene_id, identifier)
        self.unlocked()

    def test_selected_task_soft_delete_guard_and_clear(self):
        self.task('selected')
        self.store.select_scene_task(self.scene_id, 'selected')
        with self.assertRaisesRegex(ValueError, 'Clear scene selection'):
            self.store.delete_task('selected')
        self.assertIsNone(self.store.get('selected')['deleted_at'])
        self.unlocked()
        self.store.clear_scene_selection(self.scene_id)
        self.assertTrue(self.store.delete_task('selected'))
        self.assertFalse(self.store.delete_task('selected'))
        self.assertIsNone(self.store.get_scene(self.scene_id)['selected_task_id'])

    def test_generic_update_cannot_bypass_selected_task_invariants(self):
        self.task('a')
        self.store.select_scene_task(self.scene_id, 'a')
        for fields in ({'deleted_at':1}, {'status':'processing'}, {'video_url':None}):
            with self.subTest(fields=fields), self.assertRaises(ValueError):
                self.store.update('a', **fields)
            self.unlocked()
        self.store.update('a', last_poll_at=123)
        self.assertEqual(self.store.get('a')['last_poll_at'], 123)

    def test_foreign_keys_protect_direct_writes(self):
        with self.assertRaises(sqlite3.IntegrityError), self.store._conn:
            self.store._conn.execute("UPDATE scenes SET selected_task_id='missing'")
        self.task('a')
        with self.assertRaises(sqlite3.IntegrityError), self.store._conn:
            self.store._conn.execute("UPDATE tasks SET scene_id='missing' WHERE id='a'")
        with self.assertRaises(sqlite3.IntegrityError), self.store._conn:
            self.store._conn.execute("DELETE FROM projects WHERE id='film'")
        self.unlocked()

    def test_task_update_failure_does_not_leave_transaction(self):
        self.task('a')
        self.task('b')
        with self.assertRaises(ValueError):
            self.store.update('a', id='b')
        with self.assertRaises(ValueError):
            self.store.update('a', **{'bad-column':'value'})
        with self.assertRaises(sqlite3.IntegrityError):
            self.store.create_batch(['new','a'], 'model','prompt','16:9',10, scene_id=self.scene_id)
        self.assertIsNone(self.store.get('new'))
        self.unlocked()


class AssetPathTests(unittest.TestCase):
    def test_root_is_independent_of_cwd_and_extensions_are_normalized(self):
        with patch('config.ASSET_DIR', 'assets'):
            self.assertEqual(asset_root(), Path(__file__).resolve().parent / 'assets')
        self.assertEqual(asset_relative_path('film','hero','.JPEG'), 'film/hero.jpeg')
        with tempfile.TemporaryDirectory() as folder, patch('config.ASSET_DIR', folder):
            self.assertEqual(resolve_asset_path('film/hero.webp'), Path(folder).resolve() / 'film/hero.webp')
            for path in ('../escape.png', 'film/../../escape.png', '/absolute.png', 'C:/absolute.png', 'film\\hero.png'):
                with self.subTest(path=path), self.assertRaises(ValueError):
                    resolve_asset_path(path)


if __name__ == '__main__':
    unittest.main()
