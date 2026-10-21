"""Offline account-switch tests: isolated database and mocked browser requests."""
import ast
import asyncio
import json
import logging
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock
from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel
from store import TaskStore
from video_schedule import VideoScheduler

ROOT = Path(__file__).resolve().parent

class SwitchTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(str(Path(self.tmp.name)/'tasks.db'))
        self.image = Path(self.tmp.name)/'image.png'
        self.image.write_bytes(b'original-image')
        self.store.create('job', 'seedance-2.5', 'Original @Image1', '16:9', 30)
        self.store.update('job', status='needs_recovery', phase='review', account='old',
                          reference_images=json.dumps(['local://'+str(self.image)]),
                          reference_paths=json.dumps([str(self.image)]), conversation_id='previous',
                          dispatch_uncertain=1, accepted_at=123, retry_count=10, start_end=1)
        self.accs = [dict(name=n, uuid=n, busy=False, scheduling=True) for n in ('old','new','other')]
        self.pool = SimpleNamespace(accounts=['old','new','other'], list_accounts=lambda:self.accs,
            public_account=lambda a:dict(a), resolve_account=lambda x:x)
        self.scheduler = VideoScheduler(self.store,self.pool)
        self.scheduler.reserved['old']='job'
        self.pool._schedulable=lambda a:a['scheduling'] and a['name'] not in self.scheduler.reserved
        self.pool.set_scheduling=lambda n,on:next(a for a in self.accs if a['name']==n).update(scheduling=on)
        self.ns=dict(app=FastAPI(),BaseModel=BaseModel,Header=Header,HTTPException=HTTPException,
            asyncio=asyncio,json=json,Path=Path,logging=logging,store=self.store,pool=self.pool,
            scheduler=self.scheduler,WEB_SESSIONS={},JOBS={},TASK_RUNNERS={},TASK_ACTION_LOCKS={},
            _admin_auth=lambda key:None,_run_task=AsyncMock())
        names={'SwitchTaskAccount','_switch_account_candidates','_review_task','task_switch_candidates','task_switch_account'}
        tree=ast.parse((ROOT/'server.py').read_text(encoding='utf-8'))
        exec(compile(ast.Module(body=[n for n in tree.body if getattr(n,'name',None) in names],type_ignores=[]),'server.py','exec'),self.ns)

    async def asyncTearDown(self):
        await asyncio.gather(*self.ns['TASK_RUNNERS'].values())
        self.store._conn.close()
        self.tmp.cleanup()

    async def switch(self, account='new'):
        return await self.ns['task_switch_account']('job',self.ns['SwitchTaskAccount'](account_uuid=account),None)

    async def test_preserves_inputs_and_releases_old(self):
        before=self.store.get('job')
        result=await self.switch()
        after=self.store.get('job')
        for key in ('id','prompt','reference_images','reference_paths','model','duration','ratio','start_end','created_at'):
            self.assertEqual(before[key],after[key],key)
        self.assertEqual(self.image.read_bytes(),b'original-image')
        self.assertEqual(after['status'],'queued')
        self.assertEqual(after['account'],'new')
        self.assertIsNone(after['conversation_id'])
        self.assertEqual(after['dispatch_uncertain'],0)
        self.assertEqual(self.scheduler.reserved,{'new':'job'})
        self.assertFalse(self.accs[0]['scheduling'])
        self.assertEqual(result['account_uuid'],'new')
        with self.assertRaises(HTTPException):
            await self.switch('other')

    async def test_stopped_task_without_conversation_can_switch(self):
        self.store.update('job', status='stopped', phase='stopped',
                          conversation_id=None, finished_at=123, error='Stopped by user')
        candidates=await self.ns['task_switch_candidates']('job',None)
        self.assertIn('new',[a['uuid'] for a in candidates['accounts']])
        await self.test_preserves_inputs_and_releases_old()
        after=self.store.get('job')
        self.assertIsNone(after['finished_at'])
        self.assertIsNone(after['error'])

    async def test_missing_image_does_not_mutate(self):
        self.image.unlink()
        with self.assertRaises(HTTPException) as exc:
            await self.switch()
        self.assertEqual(exc.exception.status_code,409)
        self.assertEqual(self.store.get('job')['account'],'old')
        self.assertTrue(self.accs[0]['scheduling'])

    async def test_shared_library_images_preserved_on_switch(self):
        from image_library import ImageLibrary
        from test_image_library import png
        lib=ImageLibrary(Path(self.tmp.name)/'library')
        identity=lib.save(png(),'original.png',10000)['id']
        self.ns['image_library']=lib
        refs=json.dumps(['library://'+identity])
        self.store.update('job',reference_images=refs,reference_paths='[]')
        await self.switch()
        self.assertEqual(self.store.get('job')['reference_images'],refs)
        self.assertEqual(self.store.get('job')['prompt'],'Original @Image1')
        self.assertTrue(lib.path(identity).is_file())

    async def test_candidate_exclusions_and_stale_choice(self):
        self.accs[2]['busy']=True
        candidates=await self.ns['task_switch_candidates']('job',None)
        self.assertEqual([a['uuid'] for a in candidates['accounts']],['new'])
        self.ns['WEB_SESSIONS']['new']={}
        with self.assertRaises(HTTPException):
            await self.switch()
        self.assertEqual(self.scheduler.reserved,{'old':'job'})

    async def test_live_runner_is_rejected(self):
        self.ns['TASK_RUNNERS']['job']=asyncio.create_task(asyncio.sleep(.01))
        with self.assertRaises(HTTPException):
            await self.switch()

    async def test_wrong_status_and_auth_are_rejected(self):
        self.store.update('job',status='completed')
        with self.assertRaises(HTTPException):
            await self.switch()
        def denied(key):
            raise HTTPException(401,'denied')
        self.ns['_admin_auth']=denied
        with self.assertRaises(HTTPException) as exc:
            await self.ns['task_switch_candidates']('job',None)
        self.assertEqual(exc.exception.status_code,401)

class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_picker_empty_state_selection_and_submission(self):
        from patchright.async_api import async_playwright
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                posts,errors=[],[]
                candidates=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                async def route(r):
                    url=r.request.url
                    if url=='http://fixture/':
                        return await r.fulfill(body=(ROOT/'web/index.html').read_text(encoding='utf-8'),content_type='text/html')
                    if url.endswith('/switch-account'):
                        if r.request.method=='POST':
                            posts.append(r.request.post_data_json)
                            return await r.fulfill(json={'ok':True})
                        return await r.fulfill(json={'accounts':candidates})
                    if '/api/admin/accounts' in url:
                        return await r.fulfill(json={'accounts':[]})
                    if '/api/admin/tasks' in url:
                        return await r.fulfill(json={'tasks':[],'total':0})
                    return await r.fulfill(json={})
                await page.route('**/*',route)
                await page.goto('http://fixture/')
                async def run_script(source):
                    await page.add_script_tag(content="(async()=>{"+source+";document.body.dataset.scriptDone='yes'})()")
                    await page.wait_for_function("document.body.dataset.scriptDone==='yes'")
                    await page.locator('body').evaluate("(el)=>delete el.dataset.scriptDone")
                await run_script("clearInterval(timer)")
                await run_script("document.body.dataset.controls=taskControls({id:'job',status:'needs_recovery'})")
                self.assertIn('openSwitchAccount',await page.locator('body').get_attribute('data-controls'))
                await run_script("document.body.dataset.controls=taskControls({id:'job',status:'stopped',account:'old',conversation_id:null})")
                self.assertIn('openSwitchAccount',await page.locator('body').get_attribute('data-controls'))
                await run_script("document.body.dataset.controls=taskControls({id:'job',status:'processing'})")
                self.assertNotIn('openSwitchAccount',await page.locator('body').get_attribute('data-controls'))
                await run_script("await openSwitchAccount('job')")
                self.assertTrue(await page.locator('#switchAccountSubmit').is_disabled())
                candidates.extend([dict(uuid='new',number=2,email='a@example.test',remaining=2),dict(uuid='other',number=3,email='b@example.test',remaining=1)])
                await run_script("await openSwitchAccount('job')")
                self.assertEqual(await page.locator('#dlgBox h2').inner_text(),'Chuyển account')
                await page.locator('#switchAccountSelect').select_option('other')
                await page.locator('#switchAccountSubmit').click()
                await page.locator('#dlg').wait_for(state='hidden')
                self.assertEqual(posts,[{'account_uuid':'other'}])
                self.assertFalse(errors,errors)
            finally:
                await browser.close()

if __name__=='__main__':
    unittest.main()
