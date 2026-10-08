"""Chapter isolation and shared references, entirely offline."""
import unittest
import test_asset_library as base
from test_scene_import import item

class ChapterTests(unittest.TestCase):
    setUp=base.LibraryApiTests.setUp
    tearDown=base.LibraryApiTests.tearDown
    create_scene=base.LibraryApiTests.create_scene
    upload=base.LibraryApiTests.upload
    asset=base.LibraryApiTests.asset
    generate=base.LibraryApiTests.generate

    def chapter(self,name):
        response=self.http.post(f'/api/admin/projects/{self.project_id}/chapters',json={'name':name})
        self.assertEqual(response.status_code,201,response.text)
        return response.json()

    def import_scene(self,chapter):
        response=self.http.post(f"/api/admin/projects/{chapter['id']}/scenes/import",json={'scenes':[item(1,prompt='@Hero walks',references=[{'name':'Garen','alias':'Hero'}])]})
        self.assertEqual(response.status_code,201,response.text)
        return response.json()['scenes'][0]

    def test_chapters_share_one_file_and_keep_scene_numbers_and_tasks_separate(self):
        first,second=self.chapter('Chapter 1'),self.chapter('Chapter 2')
        asset=self.upload('Garen',project_id=first['id']).json()
        self.assertEqual(asset['project_id'],self.project_id)
        for project in (self.project,first,second):
            self.assertEqual(self.store.list_project_assets(project['id'])[0]['id'],asset['id'])
        a,b=self.import_scene(first),self.import_scene(second)
        self.assertNotEqual(a['id'],b['id'])
        self.assertEqual(a['scene_number'],b['scene_number'])
        self.assertTrue(a['ready'] and b['ready'])
        self.assertEqual(a['references'][0]['asset_id'],b['references'][0]['asset_id'])
        result=self.http.post(f"/api/admin/projects/{first['id']}/generate",json={'count':1})
        self.assertEqual(result.status_code,202,result.text)
        self.assertEqual(result.json()['created'],1)
        self.assertEqual(len(self.store.scene_generations(a['id'])['generations']),1)
        self.assertEqual(len(self.store.scene_generations(b['id'])['generations']),0)
        self.assertEqual(len(self.store.scene_generations(self.scene['id'])['generations']),0)
        self.assertEqual(len(list(self.root.rglob('*.png'))),1)

    def test_account_settings_api_parent_chapter_and_clear(self):
        chapter=self.chapter('Chapter')
        url=f"/api/admin/projects/{chapter['id']}/accounts"
        self.assertTrue(self.http.get(url).json()['all_accounts'])
        response=self.http.put(url,json={'account_uuids':['fixture','fixture']})
        self.assertEqual(response.status_code,200,response.text)
        parent=self.http.get(f'/api/admin/projects/{self.project_id}/accounts').json()
        self.assertEqual(parent['account_uuids'],[])
        self.assertTrue(parent['all_accounts'])
        self.assertEqual(self.http.get(url).json()['account_uuids'],['fixture'])
        self.store.set_project_accounts(self.project_id,['parent-account'])
        self.http.put(url,json={'inherit':True})
        self.assertEqual(self.http.get(url).json()['account_uuids'],['parent-account'])
        self.assertTrue(self.http.get(url).json()['inherited'])
        self.http.put(url,json={'account_uuids':[]})
        self.assertTrue(self.http.get(url).json()['all_accounts'])
        self.assertEqual(self.http.put(url,json={'account_uuids':['']}).status_code,422)
        self.assertEqual(self.http.get('/api/admin/projects/missing/accounts').status_code,404)
        self.assertEqual(self.http.get(url,headers={'X-Admin-Key':'wrong'}).status_code,401)

    def test_retry_stopped_scene_keeps_history_and_chapter(self):
        chapter=self.chapter('chapter7+8')
        self.asset('Garen')
        scene=self.import_scene(chapter)
        url=f"/api/admin/scenes/{scene['id']}/generate"
        self.assertEqual(self.http.post(url,json={'count':1}).status_code,202)
        old=self.store._conn.execute('SELECT id FROM tasks WHERE scene_id=?',(scene['id'],)).fetchone()[0]
        self.store.update(old,status='stopped',phase='stopped')
        for expected in (202,409):
            response=self.http.post(url,json={'count':1,'request_id':'retry-fixture-123'})
            self.assertEqual(response.status_code,expected,response.text)
        rows=self.store._conn.execute('SELECT id,status FROM tasks WHERE scene_id=?',(scene['id'],)).fetchall()
        self.assertEqual(len(rows),2)
        self.assertEqual(self.store.get(old)['status'],'stopped')
        self.assertEqual(self.store.get_scene(scene['id'])['project_id'],chapter['id'])
        self.assertEqual(len(self.store.scene_generations(scene['id'])['generations']),2)

    def test_retry_in_place_preserves_batch_inputs_and_history(self):
        import json
        chapter=self.chapter('chapter7+8')
        self.asset('Garen')
        scene=self.import_scene(chapter)
        response=self.http.post(f"/api/admin/scenes/{scene['id']}/generate",json={'count':2})
        self.assertEqual(response.status_code,202,response.text)
        ids=[r[0] for r in self.store._conn.execute('SELECT id FROM tasks WHERE scene_id=?',(scene['id'],))]
        old=self.store.get(ids[0])
        self.store.update(ids[0],status='failed',error='Policy',conversation_id='old-chat',account='old-account',accepted_at=12,result_url='old-url')
        self.store.retry_task(ids[0])
        new=self.store.get(ids[0])
        for key in ('id','created_at','batch_id','batch_index','batch_count','name','scene_id','prompt','reference_snapshot'):
            self.assertEqual(old[key],new[key],key)
        self.assertEqual(new['status'],'queued')
        self.assertEqual(new['phase'],'ready')
        for key in ('error','conversation_id','account','accepted_at','result_url'):
            self.assertIsNone(new[key],key)
        self.assertEqual(json.loads(new['retry_history'])[0]['error'],'Policy')
        self.assertEqual(len(self.store.scene_generations(scene['id'])['generations']),2)
        with self.assertRaises(ValueError):self.store.retry_task(ids[0])
        self.assertEqual(len(json.loads(self.store.get(ids[0])['retry_history'])),1)

    def test_task_names_include_scene_number_and_chapter(self):
        chapter=self.chapter('chapter1+2')
        self.asset('Garen')
        scene=self.import_scene(chapter)
        response=self.http.post(f"/api/admin/scenes/{scene['id']}/generate",json={'count':2})
        self.assertEqual(response.status_code,202,response.text)
        names=[r[0] for r in self.store._conn.execute('SELECT name FROM tasks WHERE scene_id=? ORDER BY batch_index',(scene['id'],))]
        self.assertEqual(names,['scene1_chapter1+2 (1/2)','scene1_chapter1+2 (2/2)'])
        other=self.chapter('chapter3+4')
        other_scene=self.import_scene(other)
        response=self.http.post(f"/api/admin/projects/{other['id']}/generate",json={'count':1})
        self.assertEqual(response.status_code,202,response.text)
        name=self.store._conn.execute('SELECT name FROM tasks WHERE scene_id=?',(other_scene['id'],)).fetchone()[0]
        self.assertEqual(name,'scene1_chapter3+4')

    def test_reference_resolve_after_upload_and_cross_project_isolation(self):
        chapter=self.chapter('Chapter 1')
        scene=self.import_scene(chapter)
        self.assertFalse(scene['ready'])
        asset=self.asset('Garen')
        self.assertTrue(self.store.resolve_scene_references(scene['id'])['scene']['ready'])
        other=self.store.create_project('Other')
        unrelated=self.store.create_scene(other['id'],1)
        with self.assertRaises(ValueError):
            self.store.attach_asset_to_scene(unrelated['id'],asset['id'],1,'Hero')
        self.assertEqual(self.store.list_project_assets(other['id']),[])
        nested=self.http.post(f"/api/admin/projects/{chapter['id']}/chapters",json={'name':'Nested'})
        self.assertEqual(nested.status_code,422)
        self.assertEqual(self.store.get_scene(self.scene['id'])['project_id'],self.project_id)

class ChapterMigrationTests(unittest.TestCase):
    def test_v6_upgrade_preserves_scenes_and_reopens_chapters(self):
        import tempfile
        from pathlib import Path
        from unittest.mock import patch
        from store import TaskStore
        with tempfile.TemporaryDirectory() as folder:
            path=Path(folder)/'tasks.db'
            with patch.object(TaskStore,'_migrate_v7',lambda self:None):
                store=TaskStore(str(path))
            project=store.create_project('Existing')
            scene=store.create_scene(project['id'],1,prompt='unchanged')
            store._conn.close()
            store=TaskStore(str(path))
            self.assertEqual(store.get_scene(scene['id']),scene)
            self.assertIsNone(store.get_project(project['id'])['parent_project_id'])
            chapter=store.create_chapter(project['id'],'Chapter 1')
            store._conn.close()
            store=TaskStore(str(path))
            try:
                self.assertEqual(store.list_chapters(project['id']),[chapter])
                self.assertEqual(store.library_project_id(chapter['id']),project['id'])
                self.assertEqual(store._conn.execute('PRAGMA foreign_key_check').fetchall(),[])
                self.assertTrue(Path(str(path)+'.before_v7.bak').exists())
            finally: store._conn.close()
