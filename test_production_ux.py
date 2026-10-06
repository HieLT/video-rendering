"""Offline production UX tests: reference lifecycle and sibling candidate batches."""
import json
from contextlib import closing
import sqlite3
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch
import test_asset_library as library
from asset_storage import resolve_asset_path
from store import TaskStore
from scene_import import asset_name_key

class ProductionUXTests(unittest.TestCase):
    create_scene=library.LibraryApiTests.create_scene
    upload=library.LibraryApiTests.upload
    asset=library.LibraryApiTests.asset
    attach=library.LibraryApiTests.attach
    prepare_references=library.LibraryApiTests.prepare_references
    tearDown=library.LibraryApiTests.tearDown
    def setUp(self):
        library.LibraryApiTests.setUp(self)
        self.store.update_scene(self.scene['id'],prompt='forest')
    def candidates(self,count=1,scene=None,request_id=None,expected=202):
        body={'count':count}
        if request_id:body['request_id']=request_id
        response=self.http.post('/api/admin/scenes/'+(scene or self.scene)['id']+'/generate',json=body)
        self.assertEqual(response.status_code,expected,response.text)
        data=response.json()
        return data.get('tasks',[data]) if expected==202 else data
    def finish(self,rows,status='completed'):
        for row in rows:self.store.update(row['id'],status=status,video_url='/videos/fixture.mp4' if status=='completed' else None)
    def replace(self,asset,data=None,expected=200):
        response=self.http.post('/api/admin/assets/'+asset['id']+'/replace',files={'file':('new.png',data or library.image_bytes('PNG'),'image/png')})
        self.assertEqual(response.status_code,expected,response.text);return response.json()
    def remove(self,asset,expected=200):
        response=self.http.post('/api/admin/assets/'+asset['id']+'/remove')
        self.assertEqual(response.status_code,expected,response.text);return response.json()
    def project_generate(self,count=1,request_id=None,expected=202):
        response=self.http.post('/api/admin/projects/'+self.project_id+'/generate',json={'count':count,**({'request_id':request_id} if request_id else {})})
        self.assertEqual(response.status_code,expected,response.text);return response.json()
    def status(self):return self.http.get('/api/admin/projects/'+self.project_id+'/generation-status').json()

    def test_scene_counts_one_two_five(self):
        for count in [1,2,5]:
            with self.subTest(count=count):
                rows=self.candidates(count);self.assertEqual(len(rows),count);self.finish(rows)
                tasks=[self.store.get(row['id']) for row in rows]
                self.assertEqual(len({t['id'] for t in tasks}),count)
                self.assertTrue(all(t['scene_id']==self.scene['id'] for t in tasks))
                self.assertEqual(len({t['batch_id'] for t in tasks}),1)
                self.assertEqual([t['batch_index'] for t in tasks],list(range(1,count+1)))
                self.assertTrue(all(t['batch_count']==count for t in tasks))
    def test_candidates_identical_ordered_immutable_snapshots(self):
        refs=self.prepare_references();rows=self.candidates(5)
        tasks=[self.store.get(r['id']) for r in rows]
        values={(t['prompt'],t['model'],t['ratio'],t['duration'],t['start_end'],t['reference_snapshot']) for t in tasks}
        self.assertEqual(len(values),1)
        snapshot=json.loads(tasks[0]['reference_snapshot'])
        self.assertEqual([r['reference_alias'] for r in snapshot],['Riven','Cabin','Sword'])
        self.store.update_scene(self.scene['id'],prompt='new prompt')
        self.assertEqual([self.store.get(t['id'])['reference_snapshot'] for t in tasks],[t['reference_snapshot'] for t in tasks])
    def test_start_end_candidates_reuse_existing_prompt_rules(self):
        scene=self.create_scene(1,'motion',start_end=True)
        for position,name in enumerate(['Start','End'],1):self.attach(self.asset(name),name,position,scene)
        rows=self.candidates(2,scene)
        self.assertTrue(all('opening frame' in self.store.get(r['id'])['prompt'] for r in rows))
    def test_active_batch_blocks_repeated_request(self):
        rows=self.candidates(5);self.candidates(5,expected=409)
        self.assertEqual(self.store.pending_task_count(),5);self.assertEqual(len(self.queued),5)
    def test_request_id_blocks_replay_after_completion(self):
        rows=self.candidates(2,request_id='same-action-123');self.finish(rows)
        self.candidates(2,request_id='same-action-123',expected=409)
        new=self.candidates(2,request_id='new-action-456')
        self.assertEqual(len(self.http.get('/api/admin/scenes/'+self.scene['id']+'/generations').json()['generations']),4)
    def test_request_id_scoped_to_scene(self):
        first=self.candidates(2,request_id='shared-action-123');other=self.create_scene(1,'forest')
        second=self.candidates(2,other,request_id='shared-action-123')
        self.assertNotEqual(self.store.get(first[0]['id'])['batch_id'],self.store.get(second[0]['id'])['batch_id'])
    def test_invalid_counts_rejected_no_write(self):
        for count in [0,6,True,1.5,'2']:
            self.candidates(count,expected=422);self.project_generate(count,expected=422)
        self.assertEqual(self.store.pending_task_count(),0)
    def test_quota_checks_all_siblings(self):
        self.client_policy['daily_limit']=4;self.candidates(5,expected=429)
        self.assertEqual(self.store.pending_task_count(),0);self.assertFalse(self.queued)
    def test_pending_limit_checks_all_siblings(self):
        self.ns['config'].MAX_PENDING_TASKS=3;self.candidates(5,expected=429)
        self.assertEqual(self.store.pending_task_count(),0)
    def test_insert_failure_rolls_back_entire_candidate_batch(self):
        original=self.store._insert_task_rows
        def fail(rows):original(rows[:1]);raise sqlite3.OperationalError('injected failure')
        with patch.object(self.store,'_insert_task_rows',fail):self.candidates(5,expected=500)
        self.assertEqual(self.store.pending_task_count(),0);self.assertFalse(self.queued)
    def test_regenerate_retains_history_and_selection(self):
        first=self.candidates(2);self.finish(first);self.store.select_scene_task(self.scene['id'],first[1]['id'])
        before=[self.store.get(r['id']) for r in first];new=self.candidates(3)
        self.assertEqual(self.store.get_scene(self.scene['id'])['selected_task_id'],first[1]['id'])
        self.assertEqual([self.store.get(r['id']) for r in first],before)
        self.finish(new);self.store.select_scene_task(self.scene['id'],new[2]['id'])
        rows=self.http.get('/api/admin/scenes/'+self.scene['id']+'/generations').json()['generations']
        self.assertEqual([r['generation_number'] for r in rows],[5,4,3,2,1]);self.assertEqual(sum(r['selected'] for r in rows),1)
    def test_processing_candidate_wins_over_latest_queued(self):
        rows=self.candidates(5);self.store.update(rows[0]['id'],status='processing');self.finish(rows[1:3])
        scene=self.status()['scenes'][0]
        self.assertEqual(scene['production_status'],'PROCESSING')
        self.assertEqual({k:scene['candidate_counts'][k] for k in ['total','completed','processing','queued']},{'total':5,'completed':2,'processing':1,'queued':2})
    def test_selected_status_survives_multiple_active_candidates(self):
        old=self.candidates();self.finish(old);self.store.select_scene_task(self.scene['id'],old[0]['id']);rows=self.candidates(5)
        self.store.update(rows[0]['id'],status='processing');scene=self.status()['scenes'][0]
        self.assertEqual(scene['production_status'],'SELECTED');self.assertEqual(scene['candidate_counts']['processing'],1)
    def test_candidate_revision_changes_for_older_candidate(self):
        rows=self.candidates(5);before=self.status()['scenes'][0]['candidate_revision'];self.finish(rows[:1])
        self.assertNotEqual(self.status()['scenes'][0]['candidate_revision'],before)
    def test_scene_changed_during_preparation_blocks_submit(self):
        original=self.store.scene_generation_input
        def raced(identifier):
            result=original(identifier);self.store.update_scene(identifier,prompt='changed');return result
        with patch.object(self.store,'scene_generation_input',raced):self.candidates(2,expected=409)
        self.assertEqual(self.store.pending_task_count(),0)
    def test_project_multiplier_only_eligible(self):
        ready=self.create_scene(1,'forest');selected=self.create_scene(2,'forest');old=self.candidates(1,selected);self.finish(old);self.store.select_scene_task(selected['id'],old[0]['id'])
        self.create_scene(3,'')
        active=self.create_scene(4,'forest');self.candidates(1,active)
        result=self.project_generate(3)
        self.assertEqual((result['created'],result['created_scenes'],result['candidates_per_scene']),(6,2,3))
        counts={identifier:sum(r['scene_id']==identifier for r in result['tasks']) for identifier in [self.scene['id'],ready['id'],selected['id'],active['id']]}
        self.assertEqual(list(counts.values()),[3,3,0,0])
        tasks=[self.store.get(r['task_id']) for r in result['tasks']]
        self.assertTrue(all(t['batch_id']==result['batch_id'] and t['batch_count']==6 for t in tasks))
    def test_project_x5_pending_atomic(self):
        self.create_scene(1,'forest');self.ns['config'].MAX_PENDING_TASKS=9;self.project_generate(5,expected=429)
        self.assertEqual(self.store.pending_task_count(),0);self.assertFalse(self.queued)
    def test_project_request_id_replay_terminal(self):
        result=self.project_generate(2,request_id='project-action-123')
        for row in result['tasks']:self.store.update(row['task_id'],status='failed')
        self.project_generate(2,request_id='project-action-123',expected=409)
        self.assertEqual(len(self.queued),2)
    def test_busy_pool_candidates_are_queued(self):
        self.ns['pool'].available=False;self.ns['pool'].all_accounts_limited=False;self.ns['pool'].all_accounts_quota_blocked=False
        rows=self.candidates(5);self.assertEqual(len(rows),5);self.assertTrue(all(r['status']=='queued' for r in rows))

    def test_replace_rebinds_all_current_references(self):
        asset=self.asset('Hero');self.attach(asset,'Riven',2)
        other=self.create_scene(1,'forest');self.attach(asset,'Hero',4,other)
        result=self.replace(asset);new=result['asset'];self.assertNotEqual(new['id'],asset['id'])
        self.assertEqual(new['name'],asset['name']);self.assertEqual(result['affected_scenes'],2)
        for scene,position,alias in [(self.scene,2,'Riven'),(other,4,'Hero')]:
            self.assertEqual(self.store.list_scene_requirements(scene['id']),[dict(name='Hero',alias=alias,position=position,asset_id=new['id'])])
            self.assertEqual(self.store.list_scene_assets(scene['id'])[0]['asset_id'],new['id'])
        self.assertIsNone(self.store.get_asset(asset['id']));self.assertFalse(resolve_asset_path(asset['file_path']).exists())
    def test_replace_preserves_historical_snapshots_file_and_preview(self):
        asset=self.asset('Hero');self.attach(asset,'Hero',1);rows=self.candidates(2);self.finish(rows,status='failed')
        before=[self.store.get(r['id']) for r in rows];source=resolve_asset_path(asset['file_path']);old_bytes=source.read_bytes()
        result=self.replace(asset,library.image_bytes('JPEG'))
        self.assertTrue(result['historical_backing_retained']);self.assertEqual(source.read_bytes(),old_bytes)
        self.assertEqual([self.store.get(r['id']) for r in rows],before)
        self.assertEqual(self.http.get('/api/admin/assets/'+asset['id']+'/image').content,old_bytes)
        self.assertEqual([r['id'] for r in self.store.list_project_assets(self.project_id)],[result['asset']['id']])
        new=self.candidates(1);self.assertEqual(json.loads(self.store.get(new[0]['id'])['reference_snapshot'])[0]['asset_id'],result['asset']['id'])
    def test_queued_old_candidate_uses_old_file_after_replace(self):
        from scene_generation import task_reference_paths
        asset=self.asset('Hero');self.attach(asset,'Hero',1);rows=self.candidates(5);self.replace(asset)
        paths=task_reference_paths(self.store.get(rows[0]['id']))
        self.assertEqual(paths,[str(resolve_asset_path(asset['file_path']))]);self.assertTrue(Path(paths[0]).exists())
    def test_repeated_replace_retains_each_history_identity(self):
        asset=self.asset('Hero');self.attach(asset,'Hero',1)
        for _ in range(3):
            rows=self.candidates();self.finish(rows);old=asset;asset=self.replace(old)['asset']
            self.assertTrue(resolve_asset_path(old['file_path']).exists())
        self.assertEqual(len(self.store.list_project_assets(self.project_id)),1)
        self.assertEqual(len({json.loads(self.store.get(r['task_id'])['reference_snapshot'])[0]['asset_id'] for r in self.http.get('/api/admin/scenes/'+self.scene['id']+'/generations').json()['generations']}),3)
    def test_replace_bad_image_no_change(self):
        asset=self.asset('Hero');self.attach(asset,'Hero',1);before=self.store.list_scene_requirements(self.scene['id'])
        self.replace(asset,b'corrupt',expected=422)
        self.assertEqual(self.store.list_scene_requirements(self.scene['id']),before);self.assertEqual(self.store.get_asset(asset['id']),asset)
    def test_replace_transaction_failure_rolls_back_and_cleans_new_file(self):
        asset=self.asset('Hero');self.attach(asset,'Hero',1);before=list(self.root.rglob('*.png'));original=self.store._retire_asset_bindings
        def fail(*args):original(*args);raise sqlite3.OperationalError('injected failure')
        with patch.object(self.store,'_retire_asset_bindings',fail),self.assertLogs('uvicorn.error',level='ERROR'):self.replace(asset,expected=500)
        self.assertEqual(list(self.root.rglob('*.png')),before);self.assertEqual(self.store.get_asset(asset['id']),asset)
        self.assertEqual(self.store.list_scene_requirements(self.scene['id'])[0]['asset_id'],asset['id'])
    def test_remove_unused_unresolves_but_keeps_requirements(self):
        asset=self.asset('Hero');self.attach(asset,'Hero',2);result=self.remove(asset)
        self.assertFalse(result['historical_backing_retained']);self.assertIsNone(self.store.get_asset(asset['id']))
        self.assertEqual(self.store.list_scene_requirements(self.scene['id']),[dict(name='Hero',alias='Hero',position=2,asset_id=None)])
        self.assertEqual(self.store.scene_details(self.scene['id'])['readiness'],'MISSING_REFERENCES')
        new=self.asset('Hero');self.store.resolve_scene_references(self.scene['id'])
        self.assertEqual(self.store.list_scene_requirements(self.scene['id'])[0]['asset_id'],new['id'])
    def test_remove_historical_asset_retains_review_and_selected_output(self):
        asset=self.asset('Hero');self.attach(asset,'Hero',1);rows=self.candidates();self.finish(rows);self.store.select_scene_task(self.scene['id'],rows[0]['id'])
        before=self.store.get(rows[0]['id']);result=self.remove(asset)
        self.assertTrue(result['historical_backing_retained']);self.assertEqual(self.store.get(rows[0]['id']),before)
        self.assertTrue(resolve_asset_path(asset['file_path']).exists());self.assertEqual(self.store.list_project_assets(self.project_id),[])
        self.assertEqual(self.store.scene_details(self.scene['id'])['readiness'],'MISSING_REFERENCES')
        self.assertEqual(self.status()['scenes'][0]['production_status'],'SELECTED')
    def test_soft_deleted_attempt_still_retains_old_image(self):
        asset=self.asset('Hero');self.attach(asset,'Hero',1);rows=self.candidates();self.finish(rows,status='failed');self.store.delete_task(rows[0]['id'])
        self.assertTrue(self.remove(asset)['historical_backing_retained']);self.assertTrue(resolve_asset_path(asset['file_path']).exists())
    def test_retired_names_not_resolved_or_matched(self):
        asset=self.asset('Hero');self.attach(asset,'Hero',1);rows=self.candidates();self.finish(rows);self.remove(asset)
        result=self.store.resolve_scene_references(self.scene['id']);self.assertEqual(result['resolved_count'],0)
        from test_scene_import import item
        report=self.store.validate_scene_import(self.project_id,{'scenes':[item(1,references=[dict(name='Hero',alias='Hero')])]})
        self.assertEqual(report['missing_reference_count'],1)
    def test_retired_asset_cannot_be_attached(self):
        asset=self.asset('Hero');self.attach(asset,'Hero',1);rows=self.candidates();self.finish(rows);self.remove(asset)
        other=self.create_scene(1,'forest')
        response=self.http.post('/api/admin/scenes/'+other['id']+'/assets',json={'asset_id':asset['id'],'position':1,'reference_alias':'Hero'})
        self.assertEqual(response.status_code,409)
    def test_physical_delete_protection_still_active(self):
        asset=self.asset('Hero');self.attach(asset,'Hero',1)
        self.assertEqual(self.http.delete('/api/admin/assets/'+asset['id']).status_code,409)
        rows=self.candidates();self.finish(rows);self.remove(asset)
        self.assertEqual(self.http.delete('/api/admin/assets/'+asset['id']).status_code,409)
    def test_concurrent_scene_actions_create_one_batch(self):
        from concurrent.futures import ThreadPoolExecutor
        def submit(_):return self.http.post('/api/admin/scenes/'+self.scene['id']+'/generate',json={'count':5})
        with ThreadPoolExecutor(max_workers=2) as executor:responses=list(executor.map(submit,range(2)))
        self.assertEqual(sorted(r.status_code for r in responses),[202,409])
        self.assertEqual(self.store.pending_task_count(),5);self.assertEqual(len(self.queued),5)
    def test_remove_transaction_failure_keeps_bindings_and_file(self):
        asset=self.asset('Hero');self.attach(asset,'Hero',1);original=self.store._retire_asset_bindings
        def fail(*args):original(*args);raise sqlite3.OperationalError('injected failure')
        with patch.object(self.store,'_retire_asset_bindings',fail),self.assertLogs('uvicorn.error',level='ERROR'):self.remove(asset,expected=500)
        self.assertEqual(self.store.get_asset(asset['id']),asset)
        self.assertEqual(self.store.list_scene_requirements(self.scene['id'])[0]['asset_id'],asset['id'])
        self.assertTrue(resolve_asset_path(asset['file_path']).exists())
    def test_failed_unused_cleanup_retains_backing_and_allows_reupload(self):
        asset=self.asset('Hero');self.attach(asset,'Hero',1)
        with patch.object(self.library,'delete',side_effect=sqlite3.OperationalError('cleanup failure')):result=self.remove(asset)
        self.assertIn('cleanup_warning',result)
        self.assertIsNotNone(self.store.get_asset(asset['id'])['retired_at'])
        self.assertTrue(resolve_asset_path(asset['file_path']).exists())
        self.assertEqual(self.store.scene_details(self.scene['id'])['readiness'],'MISSING_REFERENCES')
        self.assertEqual(self.asset('Hero')['name'],'Hero')
    def test_retired_backing_survives_restart_without_name_collision(self):
        # The active partial index accepts history with the same name, but not two current assets.
        asset=self.asset('Hero');self.attach(asset,'Hero',1);rows=self.candidates();self.finish(rows)
        new=self.replace(asset)['asset']
        self.store._check_asset_name_collisions()
        self.assertEqual(self.upload(name='HERO').status_code,409)
        self.assertEqual(self.store.list_project_assets(self.project_id),[new])

    def test_lifecycle_auth_unknown_and_repeat(self):
        asset=self.asset('Hero')
        self.assertEqual(self.http.post('/api/admin/assets/'+asset['id']+'/remove',headers={'X-Admin-Key':'wrong'}).status_code,401)
        self.assertEqual(self.http.post('/api/admin/assets/missing/remove').status_code,404)
        self.replace(asset);self.replace(asset,expected=404)

class RetirementMigrationTests(unittest.TestCase):
    def legacy(self,path):
        with patch.object(TaskStore,'_migrate_v5',lambda self:None):store=TaskStore(str(path))
        project=store.create_project('Film')['id'];scene=store.create_scene(project,1,prompt='forest')['id']
        store._conn.execute('INSERT INTO assets VALUES (?,?,?,?,?,?)',('hero',project,'Hero','character',project+'/hero.png',1))
        store._conn.execute("INSERT INTO tasks(id,status,prompt,reference_snapshot) VALUES ('old','failed','saved','[]')");store._conn.commit()
        before=store.get('old');store._conn.close();return before
    def test_v4_v5_backup_history_and_restarts(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'tasks.db';before=self.legacy(path)
            for _ in range(2):
                store=TaskStore(str(path));self.assertEqual(store._conn.execute('PRAGMA user_version').fetchone()[0],5)
                self.assertEqual(store.get('old'),before);self.assertIsNone(store.get_asset('hero')['retired_at'])
                self.assertFalse(store._conn.execute('PRAGMA foreign_key_check').fetchall());store._conn.close()
            backup=Path(str(path)+'.before_v5.bak');self.assertTrue(backup.exists())
            with closing(sqlite3.connect(backup)) as conn:self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],4)
    def test_failed_v5_rolls_back_and_preserves_backup(self):
        with tempfile.TemporaryDirectory() as tmp:
            path=Path(tmp)/'tasks.db';self.legacy(path);original=TaskStore._migrate_v5
            def fail(self):original(self);raise sqlite3.OperationalError('injected migration')
            with patch.object(TaskStore,'_migrate_v5',fail),self.assertRaises(sqlite3.OperationalError):TaskStore(str(path))
            with closing(sqlite3.connect(path)) as conn:
                self.assertEqual(conn.execute('PRAGMA user_version').fetchone()[0],4)
                self.assertNotIn('retired_at',{r[1] for r in conn.execute('PRAGMA table_info(assets)')})
                self.assertEqual(conn.execute('SELECT count(*) FROM tasks').fetchone()[0],1)

if __name__=='__main__':unittest.main()
