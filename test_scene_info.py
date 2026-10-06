"""Offline summary metadata invariants and safe migration."""
import unittest
import sqlite3
import tempfile
from pathlib import Path
from contextlib import closing
from unittest.mock import patch
import test_asset_library as base
from store import TaskStore
from scene_generation import prepare_scene_request
from test_scene_import import item

class SceneInfoTests(unittest.TestCase):
    setUp=base.LibraryApiTests.setUp
    tearDown=base.LibraryApiTests.tearDown
    create_scene=base.LibraryApiTests.create_scene
    upload=base.LibraryApiTests.upload
    asset=base.LibraryApiTests.asset
    attach=base.LibraryApiTests.attach
    prepare_references=base.LibraryApiTests.prepare_references
    generate=base.LibraryApiTests.generate
    def url(self,preview=False):
        return f'/api/admin/projects/{self.project_id}/scene-info/import'+('/validate' if preview else '')
    def payload(self,summary='Management only',number=26):
        return {'scenes':[{'scene_number':number,'summary':summary}]}
    def test_null_and_validate_no_writes(self):
        self.assertIsNone(self.scene['scene_summary'])
        before=self.store._conn.total_changes
        report=self.http.post(self.url(True),json=self.payload()).json()
        self.assertTrue(report['valid']);self.assertEqual(report['matched_count'],1)
        self.assertEqual(self.store._conn.total_changes,before)
    def test_unknown_is_atomic_and_does_not_create(self):
        payload={'scenes':[{'scene_number':26,'summary':'A'},{'scene_number':41,'summary':'B'}]}
        report=self.http.post(self.url(True),json=payload).json()
        self.assertEqual(report['missing_scene_numbers'],[41]);self.assertEqual(report['matched_count'],1)
        self.assertEqual(self.http.post(self.url(),json=payload).status_code,409)
        self.assertIsNone(self.store.get_scene(self.scene['id'])['scene_summary'])
        self.assertEqual(len(self.store.list_project_scenes(self.project_id)),1)
    def test_project_scope(self):
        other=self.store.create_project('Other')
        scene=self.store.create_scene(other['id'],26,prompt='other')
        self.assertEqual(self.http.post(self.url(),json=self.payload()).status_code,200)
        self.assertIsNone(self.store.get_scene(scene['id'])['scene_summary'])
        self.store.create_scene(other['id'],41)
        self.assertEqual(self.http.post(self.url(),json=self.payload(number=41)).status_code,409)
    def test_only_summary_changes_preserving_history_and_selection(self):
        self.prepare_references();task=self.generate();self.store.update(task['id'],status='completed',video_url='/videos/test.mp4')
        self.store.select_scene_task(self.scene['id'],task['id'])
        before={name:[tuple(row) for row in self.store._conn.execute('SELECT * FROM '+name)] for name in ['tasks','assets','scene_assets','scene_reference_requirements','projects']}
        scene=self.store.get_scene(self.scene['id'])
        for summary in ['A','B','']:
            self.assertEqual(self.http.post(self.url(),json=self.payload(summary)).status_code,200)
            current=self.store.get_scene(self.scene['id']);self.assertEqual(current.pop('scene_summary'),summary)
            self.assertEqual(current,{k:v for k,v in scene.items() if k!='scene_summary'})
            for name,rows in before.items():self.assertEqual([tuple(row) for row in self.store._conn.execute('SELECT * FROM '+name)],rows)
    def test_manual_edit_clear_and_strict_fields(self):
        url='/api/admin/scenes/'+self.scene['id']+'/summary';before=self.store.get_scene(self.scene['id'])
        for value in ['edited',None]:
            self.assertEqual(self.http.patch(url,json={'summary':value}).status_code,200)
            current=self.store.get_scene(self.scene['id']);self.assertEqual(current.pop('scene_summary'),value)
            self.assertEqual(current,{k:v for k,v in before.items() if k!='scene_summary'})
        self.assertEqual(self.http.patch(url,json={'summary':'x','prompt':'bad'}).status_code,422)
        self.assertEqual(self.http.patch(url,json={'summary':8}).status_code,422)
        self.assertEqual(self.http.patch('/api/admin/scenes/missing/summary',json={'summary':'x'}).status_code,404)
    def test_invalid_payloads_no_writes(self):
        for payload in [None,{}, {'scenes':[]}, {'scenes':[{'scene_number':True,'summary':'x'}]}, {'scenes':[{'scene_number':26,'summary':3}]}, {'scenes':[{'scene_number':26,'summary':'x','prompt':'bad'}]}, {'scenes':[{'scene_number':26,'summary':'x'},{'scene_number':26,'summary':'y'}]}, {'scenes':[],'project_id':'bad'}]:
            with self.subTest(payload=payload):
                before=self.store.get_scene(self.scene['id'])
                self.assertEqual(self.http.post(self.url(),json=payload).status_code,422)
                self.assertEqual(self.store.get_scene(self.scene['id']),before)
    def test_transaction_rollback(self):
        self.create_scene(27)
        self.store._conn.execute("CREATE TRIGGER fail_summary BEFORE UPDATE OF scene_summary ON scenes WHEN NEW.scene_number=27 BEGIN SELECT RAISE(ABORT,'injected'); END")
        self.store._conn.commit()
        payload={'scenes':[{'scene_number':26,'summary':'A'},{'scene_number':27,'summary':'B'}]}
        self.assertEqual(self.http.post(self.url(),json=payload).status_code,409)
        self.assertIsNone(self.store.get_scene(self.scene['id'])['scene_summary'])
    def test_generation_identical_and_summary_absent(self):
        self.prepare_references()
        scene,refs=self.store.scene_generation_input(self.scene['id'])
        before=prepare_scene_request(scene,refs,self.ns['VideoGenRequest'])
        self.store.update_scene_summary(self.scene['id'],{'summary':'DO NOT SEND THIS TO DOLA'})
        scene,refs=self.store.scene_generation_input(self.scene['id'])
        after=prepare_scene_request(scene,refs,self.ns['VideoGenRequest'])
        self.assertEqual(before[0].model_dump(),after[0].model_dump())
        self.assertNotIn('scene_summary',after[0].model_dump())
        self.assertNotIn('DO NOT SEND',after[0].prompt)
        task=self.generate(count=5)
        rows=task.get('tasks',[task]);self.assertEqual(len(rows),5)
        self.assertTrue(all('DO NOT SEND' not in self.store.get(row['id'])['prompt'] for row in rows))
    def test_summary_revision_does_not_block_generation(self):
        self.store.update_scene(self.scene['id'],prompt='forest')
        original=self.store.scene_generation_input
        def raced(identifier):
            result=original(identifier);self.store.update_scene_summary(identifier,{'summary':'metadata race'});return result
        with patch.object(self.store,'scene_generation_input',raced):self.generate(count=2)
    def test_template5_unchanged(self):
        result=self.http.post(f'/api/admin/projects/{self.project_id}/scenes/import',json={'scenes':[item(1)]})
        self.assertEqual(result.status_code,201,result.text)
        self.assertIsNone(result.json()['scenes'][0]['scene_summary'])
        result=self.http.post(f'/api/admin/projects/{self.project_id}/scenes/import',json={'scenes':[item(2,summary='forbidden')]})
        self.assertEqual(result.status_code,422)
    def test_auth_and_missing_project(self):
        self.assertEqual(self.http.post(self.url(),json=self.payload(),headers={'X-Admin-Key':'wrong'}).status_code,401)
        self.assertEqual(self.http.post('/api/admin/projects/missing/scene-info/import',json=self.payload()).status_code,404)

class SceneInfoMigrationTests(unittest.TestCase):
    def legacy(self,path):
        with patch.object(TaskStore,'_migrate_v6',lambda self:None):store=TaskStore(str(path))
        project=store.create_project('old');scene=store.create_scene(project['id'],1,prompt='preserve')
        rows={table:[tuple(row) for row in store._conn.execute('SELECT * FROM '+table)] for table in ['projects','scenes','tasks','assets','scene_assets','scene_reference_requirements']}
        store._conn.close();return scene,rows
    def test_migration_backup_and_restart(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'tasks.db';scene,rows=self.legacy(path)
            for _ in range(2):
                store=TaskStore(str(path))
                self.assertEqual(store._conn.execute('PRAGMA user_version').fetchone()[0],6)
                self.assertIsNone(store.get_scene(scene['id'])['scene_summary'])
                for table,old in rows.items():
                    current=[tuple(row) for row in store._conn.execute('SELECT * FROM '+table)]
                    self.assertEqual([row[:-1] for row in current] if table=='scenes' else current,old)
                store._conn.close()
            with closing(sqlite3.connect(str(path)+'.before_v6.bak')) as conn:self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],5)
    def test_migration_failure_rolls_back(self):
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'tasks.db';self.legacy(path)
            original=TaskStore._migrate_v6
            def fail(self):original(self);raise sqlite3.OperationalError('injected')
            with patch.object(TaskStore,'_migrate_v6',fail),self.assertRaises(sqlite3.OperationalError):TaskStore(str(path))
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],5)
                self.assertNotIn('scene_summary',[row[1] for row in conn.execute('PRAGMA table_info(scenes)')])

if __name__=='__main__':unittest.main()
