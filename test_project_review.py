"""Review APIs, immutable history, selection and production-completion fixtures."""
import ast
from pathlib import Path
import unittest
import sqlite3
import test_asset_library as library
from test_scene_import import item

class ReviewTests(unittest.TestCase):
    def setUp(self):
        library.LibraryApiTests.setUp(self)
        self.store.update_scene(self.scene['id'],prompt='forest')
        self.counter=0
    tearDown=library.LibraryApiTests.tearDown
    create_scene=library.LibraryApiTests.create_scene
    generate=library.LibraryApiTests.generate
    def attempt(self, scene=None, status='completed', url='/videos/fixture.mp4'):
        self.counter+=1;task_id=f'attempt_{self.counter:03d}'
        scene=scene or self.scene
        self.store.create(task_id,'seedance-2.5','saved @Image1 prompt','16:9',30,scene_id=scene['id'],reference_snapshot=[])
        self.store.update(task_id,status=status,video_url=url if status=='completed' else None,
                          error='failure fixture' if status=='failed' else None,failure_code='E1' if status=='failed' else None)
        self.store._conn.execute('UPDATE tasks SET created_at=? WHERE id=?',(self.counter,task_id));self.store._conn.commit()
        return task_id
    def selection_url(self, scene=None): return '/api/admin/scenes/'+(scene or self.scene)['id']+'/selected-generation'
    def select(self, task_id, scene=None, expected=200):
        response=self.http.put(self.selection_url(scene),json={'task_id':task_id})
        self.assertEqual(response.status_code,expected,response.text);return response.json()
    def status(self):
        response=self.http.get('/api/admin/projects/'+self.project_id+'/generation-status')
        self.assertEqual(response.status_code,200,response.text);return response.json()
    def generations(self, scene=None):
        response=self.http.get('/api/admin/scenes/'+(scene or self.scene)['id']+'/generations')
        self.assertEqual(response.status_code,200,response.text);return response.json()['generations']
    def test_attempts_order_numbers_and_scene_filter(self):
        first=self.attempt(status='failed');second=self.attempt();third=self.attempt()
        other=self.create_scene(1,'forest');self.attempt(other)
        rows=self.generations()
        self.assertEqual([r['task_id'] for r in rows],[third,second,first])
        self.assertEqual([r['generation_number'] for r in rows],[3,2,1])
        self.assertTrue(all(r['scene_id']==self.scene['id'] for r in rows))
    def test_attempts_output_error_and_snapshot(self):
        failed=self.attempt(status='failed');completed=self.attempt()
        rows=self.generations()
        self.assertEqual(rows[0]['video_url'],'/videos/fixture.mp4')
        self.assertEqual(rows[0]['reference_snapshot'],[])
        self.assertEqual(rows[0]['prompt'],'saved @Image1 prompt')
        self.assertEqual((rows[1]['error'],rows[1]['failure_code']),('failure fixture','E1'))
    def test_select_valid_and_flags(self):
        task=self.attempt();before=self.store.get(task);self.select(task)
        self.assertEqual(self.store.get_scene(self.scene['id'])['selected_task_id'],task)
        self.assertTrue(self.generations()[0]['selected']);self.assertEqual(self.store.get(task),before)
    def test_other_scene_rejected(self):
        other=self.create_scene(1,'forest');task=self.attempt(other)
        self.select(task,expected=422)
    def test_noncompleted_rejected(self):
        for status in ['queued','processing','failed','stopped','needs_recovery']:
            with self.subTest(status=status): self.select(self.attempt(status=status),expected=422)
        self.assertIsNone(self.store.get_scene(self.scene['id'])['selected_task_id'])
    def test_deleted_rejected(self):
        task=self.attempt();self.store.delete_task(task);self.select(task,expected=422)
    def test_empty_video_url_rejected(self):
        for url in [None,'','   ']:
            with self.subTest(url=url): self.select(self.attempt(url=url),expected=422)
    def test_unknown_task_rejected(self): self.select('missing',expected=404)
    def test_switch_keeps_both_attempts(self):
        first=self.attempt();second=self.attempt();before=[self.store.get(t) for t in [first,second]]
        self.select(first);self.select(second)
        self.assertEqual([self.store.get(t) for t in [first,second]],before)
        self.assertEqual([(r['task_id'],r['selected']) for r in self.generations()],[(second,True),(first,False)])
    def test_clear_selection(self):
        task=self.attempt();self.select(task)
        response=self.http.delete(self.selection_url());self.assertEqual(response.status_code,200)
        self.assertIsNone(response.json()['selected_task_id']);self.assertEqual(len(self.generations()),1)
        self.assertEqual(self.status()['scenes'][0]['production_status'],'NEEDS_REVIEW')
    def test_clear_idempotent(self):
        for _ in range(2): self.assertEqual(self.http.delete(self.selection_url()).status_code,200)
    def test_regenerate_and_completion_preserve_selection(self):
        task=self.attempt();self.select(task);new=self.generate(self.scene)
        self.assertEqual(self.store.get_scene(self.scene['id'])['selected_task_id'],task)
        self.store.update(new['id'],status='completed',video_url='/videos/new.mp4')
        self.assertEqual(self.store.get_scene(self.scene['id'])['selected_task_id'],task)
        self.assertEqual(len(self.generations()),2)
    def test_needs_review_completed(self):
        self.attempt();self.assertEqual(self.status()['scenes'][0]['production_status'],'NEEDS_REVIEW')
    def test_failed_latest_older_completed_needs_review(self):
        self.attempt();latest=self.attempt(status='failed');scene=self.status()['scenes'][0]
        self.assertEqual(scene['production_status'],'NEEDS_REVIEW');self.assertEqual(scene['latest_task']['id'],latest)
        self.assertEqual(scene['generation_status'],'FAILED')
    def test_only_failed_is_ready_for_regeneration(self):
        self.attempt(status='failed');scene=self.status()['scenes'][0]
        self.assertEqual(scene['production_status'],'READY');self.assertEqual(scene['latest_task']['status'],'failed')
    def test_selected_with_active_is_still_production_complete(self):
        task=self.attempt();self.select(task);self.attempt(status='queued')
        result=self.status();self.assertTrue(result['production_complete'])
        self.assertEqual(result['scenes'][0]['production_status'],'SELECTED')
        self.assertEqual(result['scenes'][0]['generation_status'],'QUEUED')
        self.assertEqual(result['summary']['selected'],1)
    def test_summary_partition(self):
        self.select(self.attempt())
        review=self.create_scene(1,'forest');self.attempt(review);self.attempt(review,status='failed')
        self.attempt(self.create_scene(2,'forest'),status='processing')
        self.attempt(self.create_scene(3,'forest'),status='queued')
        self.store.import_scenes(self.project_id,{'scenes':[item(4,references=[dict(name='Missing',alias='Hero')])]})
        self.create_scene(5,'');self.create_scene(6,'forest')
        result=self.status()
        self.assertEqual(result['summary'],dict(total_scenes=7,selected=1,needs_review=1,processing=1,queued=1,missing_references=1,invalid_configuration=1,ready=1))
        self.assertFalse(result['production_complete'])
    def test_completion_requires_every_selection(self):
        one=self.attempt();other=self.create_scene(1,'forest');two=self.attempt(other)
        self.assertFalse(self.status()['production_complete']);self.select(one)
        self.assertFalse(self.status()['production_complete']);self.select(two,other)
        self.assertTrue(self.status()['production_complete'])
    def test_empty_project_not_complete(self):
        p=self.store.create_project('Empty');result=self.http.get('/api/admin/projects/'+p['id']+'/generation-status').json()
        self.assertFalse(result['production_complete']);self.assertEqual(result['summary']['total_scenes'],0)
        self.assertFalse(self.http.get('/api/admin/projects/'+p['id']+'/selected-videos').json()['complete'])
    def test_selected_video_order_and_task_duration(self):
        first=self.attempt();self.select(first)
        other=self.create_scene(1,'forest');second=self.attempt(other);self.select(second,other)
        self.store.update_scene(other['id'],duration=10)
        result=self.http.get('/api/admin/projects/'+self.project_id+'/selected-videos').json()
        self.assertEqual([r['scene_number'] for r in result['videos']],[1,26])
        self.assertEqual([r['task_id'] for r in result['videos']],[second,first])
        self.assertEqual(result['videos'][0]['duration'],30)
        self.assertEqual((result['selected_count'],result['total_scenes'],result['complete']),(2,2,True))
        self.assertNotIn('local_path',result['videos'][0])
    def test_unselected_outputs_omitted(self):
        self.attempt();result=self.http.get('/api/admin/projects/'+self.project_id+'/selected-videos').json()
        self.assertEqual(result['videos'],[]);self.assertFalse(result['complete'])
    def test_selected_task_delete_protected(self):
        task=self.attempt();self.select(task)
        with self.assertRaises(ValueError): self.store.delete_task(task)
        self.assertIsNone(self.store.get(task)['deleted_at'])
    def test_history_delete_api_returns_409_for_selected(self):
        task=self.attempt();self.select(task)
        tree=ast.parse(Path('server.py').read_text(encoding='utf-8'))
        node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='admin_task_delete')
        self.ns.update(TASK_ACTION_LOCKS={},_admin_auth=lambda _:None)
        exec(compile(ast.Module(body=[node],type_ignores=[]),'server.py','exec'),self.ns)
        self.assertEqual(self.http.delete('/api/admin/tasks/'+task).status_code,409)
        self.assertIsNone(self.store.get(task)['deleted_at'])
    def test_deleted_history_keeps_version_number(self):
        first=self.attempt();second=self.attempt();self.store.delete_task(first)
        rows=self.generations();self.assertEqual([r['generation_number'] for r in rows],[2,1])
        self.assertIsNotNone(rows[1]['deleted_at']);self.assertFalse(rows[1]['selected'])
    def test_deleted_completed_does_not_need_review(self):
        task=self.attempt();self.store.delete_task(task)
        self.assertEqual(self.status()['scenes'][0]['production_status'],'READY')
    def test_generate_all_still_skips_selected(self):
        self.select(self.attempt())
        result=self.http.post('/api/admin/projects/'+self.project_id+'/generate').json()
        self.assertEqual(result['created'],0);self.assertEqual(result['skipped_scenes'][0]['reason'],'ALREADY_SELECTED')
    def test_bulk_select_latest_preserves_existing(self):
        existing=self.attempt();self.select(existing);self.attempt()
        other=self.create_scene(1,'forest');self.attempt(other);latest=self.attempt(other)
        self.create_scene(2,'forest')
        result=self.http.post('/api/admin/projects/'+self.project_id+'/select-latest-completed').json()
        self.assertEqual(result['changed'],1)
        self.assertEqual(self.store.get_scene(self.scene['id'])['selected_task_id'],existing)
        self.assertEqual(self.store.get_scene(other['id'])['selected_task_id'],latest)
    def test_bulk_select_ignores_deleted_and_empty_url(self):
        good=self.attempt();deleted=self.attempt();self.store.delete_task(deleted);self.attempt(url=' ')
        result=self.http.post('/api/admin/projects/'+self.project_id+'/select-latest-completed').json()
        self.assertEqual(result['changed'],1);self.assertEqual(self.store.get_scene(self.scene['id'])['selected_task_id'],good)
    def test_bulk_selection_atomic_rollback(self):
        self.attempt();other=self.create_scene(1,'forest');self.attempt(other)
        self.store._conn.execute("CREATE TRIGGER fail_selection BEFORE UPDATE OF selected_task_id ON scenes WHEN NEW.scene_number=26 BEGIN SELECT RAISE(ABORT,'fixture'); END");self.store._conn.commit()
        self.assertEqual(self.http.post('/api/admin/projects/'+self.project_id+'/select-latest-completed').status_code,409)
        self.assertTrue(all(s['selected_task_id'] is None for s in self.store.list_project_scenes(self.project_id)))
    def test_read_models_no_writes(self):
        self.attempt();before=self.store._conn.total_changes
        self.generations();self.status();self.http.get('/api/admin/projects/'+self.project_id+'/selected-videos')
        self.assertEqual(self.store._conn.total_changes,before)
    def test_unknown_ids_auth_and_body_validation(self):
        for url in ['/api/admin/scenes/missing/generations','/api/admin/projects/missing/selected-videos']:
            self.assertEqual(self.http.get(url).status_code,404)
        self.assertEqual(self.http.get('/api/admin/scenes/'+self.scene['id']+'/generations',headers={'X-Admin-Key':'bad'}).status_code,401)
        for body in [{},{'task_id':''},{'task_id':123}]: self.assertEqual(self.http.put(self.selection_url(),json=body).status_code,422)

    def test_completed_still_reviewable_after_prompt_becomes_draft(self):
        task=self.attempt();self.store.update_scene(self.scene['id'],prompt='')
        scene=self.status()['scenes'][0]
        self.assertEqual(scene['production_status'],'NEEDS_REVIEW')
        self.assertEqual(scene['readiness'],'INVALID_CONFIGURATION')
        self.select(task);self.assertTrue(self.status()['production_complete'])
    def test_completed_still_reviewable_with_new_missing_requirement(self):
        task=self.attempt()
        self.store._conn.execute('INSERT INTO scene_reference_requirements VALUES (?,?,?,?,?)',(self.scene['id'],1,'New image','New',None));self.store._conn.commit()
        scene=self.status()['scenes'][0]
        self.assertEqual(scene['production_status'],'NEEDS_REVIEW')
        self.assertEqual(scene['readiness'],'MISSING_REFERENCES')
        self.assertFalse(scene['ready']);self.select(task)
    def test_selection_insert_failure_keeps_previous_output(self):
        first=self.attempt();second=self.attempt();self.select(first)
        self.store._conn.execute("CREATE TRIGGER fail_change BEFORE UPDATE OF selected_task_id ON scenes BEGIN SELECT RAISE(ABORT,'fixture'); END");self.store._conn.commit()
        self.select(second,expected=409)
        self.assertEqual(self.store.get_scene(self.scene['id'])['selected_task_id'],first)
        self.assertEqual(len(self.generations()),2)

if __name__=='__main__': unittest.main()
