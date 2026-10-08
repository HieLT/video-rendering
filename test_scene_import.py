from contextlib import closing
import unittest
import sqlite3
import tempfile
from pathlib import Path
from unittest.mock import patch
import test_asset_library as base
from store import TaskStore
from scene_import import AssetNameCollisionError

def item(n=1, **changes):
    row=dict(scene_number=n,scene_name=f'Scene {n}',prompt='A quiet forest',model='seedance-2.5',ratio='16:9',duration=30,start_end=False,references=[])
    row.update(changes)
    return row

class ImportTests(unittest.TestCase):
    setUp=base.LibraryApiTests.setUp
    tearDown=base.LibraryApiTests.tearDown
    create_scene=base.LibraryApiTests.create_scene
    upload=base.LibraryApiTests.upload
    asset=base.LibraryApiTests.asset
    generate=base.LibraryApiTests.generate
    def endpoint(self, preview=False):
        return f'/api/admin/projects/{self.project_id}/scenes/import'+('/validate' if preview else '')
    def submit(self, rows, expected=201):
        response=self.http.post(self.endpoint(),json={'scenes':rows})
        self.assertEqual(response.status_code,expected,response.text)
        return response.json()
    def test_one_exact_prompt(self):
        prompt='  Unicode Riven\r\n=== REFERENCES ===\nfull text  '
        scene=self.submit([item(prompt=prompt)])['scenes'][0]
        self.assertEqual(scene['prompt'],prompt)
        self.assertEqual(scene['readiness'],'READY')
    def test_many_sorted(self):
        self.store._conn.execute('DELETE FROM scenes WHERE id=?',(self.scene['id'],));self.store._conn.commit()
        report=self.submit([item(n) for n in reversed(range(1,41))])
        self.assertEqual([s['scene_number'] for s in report['scenes']],list(range(1,41)))
        self.assertEqual(report['imported_count'],40)
    def test_reference_order_alias_and_case_match(self):
        a,b=self.asset('Sword'),self.asset('Cabin')
        refs=[dict(name='cABin',alias='@Room'),dict(name='sword',alias='Blade')]
        scene=self.submit([item(prompt='@Room and @Blade',references=refs)])['scenes'][0]
        self.assertEqual([(r['asset_id'],r['position'],r['alias']) for r in scene['references']],[(b['id'],1,'Room'),(a['id'],2,'Blade')])
        self.assertEqual(scene['readiness'],'READY')
    def test_missing_persistent_no_fake_asset(self):
        scene=self.submit([item(references=[dict(name='Missing',alias='Hero')])])['scenes'][0]
        self.assertEqual(scene['readiness'],'MISSING_REFERENCES')
        self.assertEqual(self.store.list_project_assets(self.project_id),[])
        saved=self.http.get('/api/admin/scenes/'+scene['id']).json()
        self.assertIsNone(saved['references'][0]['asset_id'])
        self.assertEqual(saved['references'][0]['name'],'Missing')
        self.assertEqual(self.http.post('/api/admin/scenes/'+scene['id']+'/generate',json={}).status_code,409)
    def test_upload_resolve_generate(self):
        scene=self.submit([item(prompt='@Hero walks',references=[dict(name='Hero',alias='Hero')])])['scenes'][0]
        asset=self.asset('hERO')
        response=self.http.post('/api/admin/scenes/'+scene['id']+'/references/resolve')
        self.assertEqual(response.status_code,200,response.text)
        ready=response.json()['scene']
        self.assertEqual(ready['readiness'],'READY')
        self.assertEqual(ready['references'][0]['asset_id'],asset['id'])
        self.generate(ready)
    def test_no_refs_ready(self):
        self.assertEqual(self.submit([item()])['scenes'][0]['readiness'],'READY')
    def test_draft_empty_prompt(self):
        self.assertEqual(self.submit([item(prompt='')])['scenes'][0]['readiness'],'INVALID_CONFIGURATION')
    def test_duplicate_scene_numbers(self):
        self.submit([item(),item()],422)
        self.assertEqual(len(self.store.list_project_scenes(self.project_id)),1)
    def test_existing_conflict(self):
        self.submit([item(26)],409)
        self.assertEqual(self.store.get_scene(self.scene['id'])['prompt'],self.scene['prompt'])
    def test_invalid_model(self): self.submit([item(model='unknown')],422)
    def test_invalid_ratio(self): self.submit([item(ratio='7:2')],422)
    def test_invalid_duration(self):
        for value in [11,True,'30',30.0]:
            with self.subTest(value=value): self.submit([item(duration=value)],422)
    def test_invalid_start_end(self):
        for value in [1,'false',None]:
            with self.subTest(value=value): self.submit([item(start_end=value)],422)
    def test_invalid_number(self):
        for value in [0,True,1.0,'1']:
            with self.subTest(value=value): self.submit([item(scene_number=value)],422)
    def test_duplicate_alias(self):
        self.submit([item(references=[dict(name='A',alias='Hero'),dict(name='B',alias='hero')])],422)
    def test_duplicate_reference(self):
        self.submit([item(references=[dict(name='A',alias='One'),dict(name='a',alias='Two')])],422)
    def test_invalid_alias(self):
        for alias in ['', '@@Hero','He@ro']:
            with self.subTest(alias=alias): self.submit([item(references=[dict(name='A',alias=alias)])],422)
    def test_invalid_reference_name(self):
        self.submit([item(references=[dict(name=' ',alias='Hero')])],422)
    def test_unique_asset_names(self):
        self.asset('Riven')
        self.assertEqual(self.upload('rIVEN').status_code,409)
        self.assertEqual(len(self.store.list_project_assets(self.project_id)),1)
    def test_unique_unicode_names(self):
        self.asset('Straße')
        self.assertEqual(self.upload('STRASSE').status_code,409)
    def test_preview_no_write(self):
        before=self.store._conn.total_changes
        response=self.http.post(self.endpoint(True),json={'scenes':[item(references=[dict(name='Later',alias='Hero')])]})
        self.assertEqual(response.status_code,200)
        self.assertTrue(response.json()['valid'])
        self.assertEqual(response.json()['missing_reference_count'],1)
        self.assertEqual(self.store._conn.total_changes,before)
    def test_preview_conflict(self):
        report=self.http.post(self.endpoint(True),json={'scenes':[item(26)]}).json()
        self.assertFalse(report['valid'])
        self.assertEqual(report['conflicts'][0]['scene_number'],26)
    def test_invalid_batch_atomic(self):
        self.submit([item(n) for n in range(1,26)]+[item(27,model='bad')],422)
        self.assertEqual(len(self.store.list_project_scenes(self.project_id)),1)
    def test_insert_failure_atomic(self):
        self.store._conn.execute("CREATE TRIGGER fail_import BEFORE INSERT ON scenes WHEN NEW.scene_number=27 BEGIN SELECT RAISE(ABORT,'injected'); END")
        self.store._conn.commit()
        self.submit([item(n) for n in list(range(1,26))+[27,28]],409)
        self.assertEqual(len(self.store.list_project_scenes(self.project_id)),1)
        self.assertEqual(self.store._conn.execute('SELECT count(*) FROM scene_reference_requirements').fetchone()[0],0)
    def test_start_end_count(self): self.submit([item(start_end=True)],422)
    def test_start_end_two(self):
        self.asset('Start');self.asset('End')
        scene=self.submit([item(start_end=True,references=[dict(name='Start',alias='Start'),dict(name='End',alias='End')])])['scenes'][0]
        self.assertEqual(scene['readiness'],'READY')
        self.generate(scene)
    def test_project_get_rename(self):
        url='/api/admin/projects/'+self.project_id
        self.assertEqual(self.http.get(url).json()['id'],self.project_id)
        self.assertEqual(self.http.patch(url,json={'name':'New title'}).json()['name'],'New title')
    def test_scene_patch_and_unique_number(self):
        scene=self.submit([item()])['scenes'][0]
        url='/api/admin/scenes/'+scene['id']
        self.assertEqual(self.http.patch(url,json={'scene_number':26}).status_code,409)
        result=self.http.patch(url,json={'scene_number':5,'prompt':'changed'}).json()
        self.assertEqual(result['scene_number'],5)
        self.assertEqual(result['prompt'],'changed')
    def test_strict_contract(self):
        for payload in [None,[],{}, {'scenes':[]},{'scenes':[{}]}, {'scenes':[item(extra=True)]}]:
            with self.subTest(payload=payload):
                self.assertEqual(self.http.post(self.endpoint(),json=payload).status_code,422)
    def test_missing_project_and_auth(self):
        self.assertEqual(self.http.post('/api/admin/projects/absent/scenes/import',json={'scenes':[item()]}).status_code,404)
        self.assertEqual(self.http.post(self.endpoint(),json={'scenes':[item()]},headers={'X-Admin-Key':'wrong'}).status_code,401)

    def test_snapshot_unchanged_after_edit_and_import(self):
        asset=self.asset('Hero')
        scene=self.submit([item(prompt='@Hero moves',references=[dict(name='Hero',alias='Hero')])])['scenes'][0]
        task=self.generate(scene)
        before=self.store.get(task['id'])
        self.http.patch('/api/admin/scenes/'+scene['id'],json={'prompt':'edited','scene_number':5,'duration':10})
        self.submit([item(2)])
        self.assertEqual(self.store.get(task['id']),before)
    def test_partial_missing_keeps_positions(self):
        asset=self.asset('Second')
        scene=self.submit([item(references=[dict(name='Later',alias='First'),dict(name='Second',alias='Second')])])['scenes'][0]
        self.assertIsNone(scene['references'][0]['asset_id'])
        self.assertEqual(scene['references'][1]['position'],2)
        self.assertEqual(self.store.list_scene_assets(scene['id'])[0]['position'],2)
    def test_cross_project_not_resolved(self):
        other=self.store.create_project('Other')
        self.assertEqual(self.upload('Hero',project_id=other['id']).status_code,201)
        scene=self.submit([item(references=[dict(name='Hero',alias='Hero')])])['scenes'][0]
        result=self.http.post('/api/admin/scenes/'+scene['id']+'/references/resolve').json()
        self.assertEqual(result['resolved_count'],0)
        self.assertEqual(result['scene']['readiness'],'MISSING_REFERENCES')
    def test_missing_reorder_rejected(self):
        asset=self.asset('Second')
        scene=self.submit([item(references=[dict(name='Later',alias='First'),dict(name='Second',alias='Second')])])['scenes'][0]
        url='/api/admin/scenes/'+scene['id']+'/assets/order'
        self.assertEqual(self.http.put(url,json={'asset_ids':[asset['id']]}).status_code,422)
        self.assertEqual(self.store.scene_details(scene['id'])['references'],scene['references'])
    def test_resolve_atomic_on_link_failure(self):
        scene=self.submit([item(references=[dict(name='A',alias='A'),dict(name='B',alias='B')])])['scenes'][0]
        self.asset('A');b=self.asset('B')
        self.store._conn.execute("CREATE TRIGGER fail_resolve BEFORE INSERT ON scene_assets WHEN NEW.position=2 BEGIN SELECT RAISE(ABORT,'injected'); END")
        self.store._conn.commit()
        self.assertEqual(self.http.post('/api/admin/scenes/'+scene['id']+'/references/resolve').status_code,409)
        self.assertTrue(all(r['asset_id'] is None for r in self.store.list_scene_requirements(scene['id'])))
        self.assertEqual(self.store.list_scene_assets(scene['id']),[])
    def test_validate_all_before_insert(self):
        statements=[]
        self.store._conn.set_trace_callback(statements.append)
        try: self.submit([item(),item(2,duration=11)],422)
        finally: self.store._conn.set_trace_callback(None)
        self.assertFalse(any(x.lstrip().upper().startswith('INSERT') for x in statements))

class MigrationTests(unittest.TestCase):
    def test_collision_stops_migration_without_rename(self):
        for names in [('Riven','riven'),('Straße','STRASSE'),('Riven',' RIVEN ')]:
            with self.subTest(names=names),tempfile.TemporaryDirectory() as tmp:
                path=Path(tmp)/'legacy.db'
                with patch('scene_workflow.SceneWorkflowMixin._migrate_v4',lambda self:None):
                    store=TaskStore(str(path))
                project=store.create_project('Legacy')['id']
                for n,name in enumerate(names):
                    store._conn.execute('INSERT INTO assets VALUES (?,?,?,?,?,?)',(str(n),project,name,'character',f'{project}/{n}.png',0))
                store._conn.commit();store._conn.close()
                with self.assertRaises(AssetNameCollisionError) as raised: TaskStore(str(path))
                self.assertEqual(len(raised.exception.collisions),1)
                with closing(sqlite3.connect(path)) as conn:
                    self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],3)
                    self.assertEqual([r[0] for r in conn.execute('SELECT name FROM assets ORDER BY id')],list(names))
                self.assertFalse(Path(str(path)+'.before_v4.bak').exists())
    def test_backfill_and_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'legacy.db'
            with patch('scene_workflow.SceneWorkflowMixin._migrate_v4',lambda self:None): store=TaskStore(str(path))
            p=store.create_project('Legacy')['id']
            s=store.create_scene(p,1,prompt='text')['id']
            store._conn.execute('INSERT INTO assets VALUES (?,?,?,?,?,?)',('asset',p,'Hero','character',f'{p}/asset.png',0))
            store._conn.execute('INSERT INTO scene_assets VALUES (?,?,?,?)',(s,'asset',3,'Hero'))
            store._conn.execute("INSERT INTO tasks(id,prompt,status,video_url,scene_id,reference_snapshot) VALUES ('old','original','completed','/videos/old.mp4',?,'[]')",(s,))
            store._conn.execute("UPDATE scenes SET selected_task_id='old' WHERE id=?",(s,))
            store._conn.commit()
            before=store.get('old');store._conn.close()
            for _ in range(2):
                store=TaskStore(str(path))
                self.assertEqual(store._conn.execute('PRAGMA user_version').fetchone()[0],9)
                self.assertEqual(store.list_scene_requirements(s),[dict(name='Hero',alias='Hero',position=3,asset_id='asset')])
                self.assertEqual(store.get('old'),before)
                self.assertEqual(store.get_scene(s)['selected_task_id'],'old')
                self.assertEqual(store._conn.execute('PRAGMA foreign_key_check').fetchall(),[])
                store._conn.close()
            self.assertTrue(Path(str(path)+'.before_v4.bak').exists())

    def test_missing_requirements_survive_restart(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'tasks.db'
            store=TaskStore(str(path));p=store.create_project('Film')['id']
            scene=store.import_scenes(p,{'scenes':[item(references=[dict(name='Later',alias='Hero')])]})['scenes'][0]
            store._conn.close();store=TaskStore(str(path))
            try:
                restored=store.scene_details(scene['id'])
                self.assertEqual(restored['references'],scene['references'])
                self.assertEqual(restored['readiness'],'MISSING_REFERENCES')
            finally: store._conn.close()
    def test_late_migration_failure_rolls_back_schema(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'tasks.db'
            with patch('scene_workflow.SceneWorkflowMixin._migrate_v4',lambda self:None): store=TaskStore(str(path))
            project=store.create_project('Original');store._conn.close()
            original=TaskStore._migrate_v4
            def fail(instance):
                original(instance)
                raise sqlite3.OperationalError('injected late failure')
            with patch.object(TaskStore,'_migrate_v4',fail),self.assertRaises(sqlite3.OperationalError): TaskStore(str(path))
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],3)
                self.assertEqual(conn.execute("SELECT count(*) FROM sqlite_master WHERE name IN ('scene_reference_requirements','assets_project_name_nocase')").fetchone()[0],0)
                self.assertEqual(conn.execute('SELECT name FROM projects').fetchone()[0],'Original')
                conn.execute('BEGIN IMMEDIATE');conn.rollback()

if __name__=='__main__': unittest.main()
