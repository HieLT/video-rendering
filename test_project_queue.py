"""Offline queue and project orchestration tests; no live DB or browser calls."""
import asyncio
import ast
import json
import sqlite3
import time
from video_schedule import VideoScheduler
import unittest
from pathlib import Path
from unittest.mock import patch, AsyncMock
import browser_pool as pools
import test_asset_library as library
from test_video_batch import isolated_api

class FixturePool(pools.BrowserPool):
    @property
    def accounts(self): return self._names
    @accounts.setter
    def accounts(self, value): self._names=value

class QueueTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.pool=FixturePool.__new__(FixturePool)
        self.pool.semaphore=asyncio.Semaphore(2)
        self.pool.reservations={};self.pool._locks={};self.pool._activities={};self.pool.accounts=['one']
        self.pool._conn=sqlite3.connect(':memory:')
        self.pool._conn.execute('CREATE TABLE accounts_meta(name TEXT PRIMARY KEY,last_used_at REAL,cooldown_until REAL)')
        self.pool._conn.execute("INSERT INTO accounts_meta VALUES ('one',0,0)");self.pool._conn.commit()
        self.row=dict(name='one',scheduling=True,login_ok=1,auth_state='active',rate_limited=False,quota_blocked=False,used_today=0,credit_balance=None,cooling=False,busy=False)
        def accounts():
            lock=self.pool._locks.get('one')
            cooldown=self.pool._conn.execute("SELECT cooldown_until FROM accounts_meta WHERE name='one'").fetchone()[0]
            return [{**self.row,'busy':bool(lock and lock.locked()),'cooling':self.row['cooling'] or cooldown>time.time()}] if self.pool.accounts else []
        self.pool.list_accounts=accounts
        self.pool._claim=lambda _:None
        self.calls=[]
        async def worker(*args,**kwargs): self.calls.append(args[0]);return {'local_path':'mock.mp4'}
        self.worker=AsyncMock(side_effect=worker)
        self.patch=patch('browser_pool.generate_video',self.worker);self.patch.start()
    def tearDown(self): self.patch.stop();self.pool._conn.close()
    async def test_free_runs_once(self):
        await self.pool.generate_video('p');self.assertEqual(self.calls,['one'])
    async def test_busy_waits_then_release(self):
        lock=self.pool._locks.setdefault('one',asyncio.Lock());await lock.acquire()
        task=asyncio.create_task(self.pool.generate_video('p'))
        await asyncio.sleep(.12)
        self.assertFalse(task.done());self.assertEqual(self.calls,[])
        lock.release();await asyncio.wait_for(task,2)
        self.assertEqual(self.calls,['one'])
    async def test_multiple_eventually_execute(self):
        gate=asyncio.Event();started=asyncio.Event()
        async def worker(*args,**kwargs):
            started.set();await gate.wait();await asyncio.sleep(.01);return {'local_path':'mock.mp4'}
        self.worker.side_effect=worker
        tasks=[asyncio.create_task(self.pool.generate_video(str(i))) for i in range(5)]
        await started.wait();gate.set()
        await asyncio.wait_for(asyncio.gather(*tasks),6)
        self.assertEqual(self.worker.await_count,5)
    async def test_no_busy_loop(self):
        lock=self.pool._locks.setdefault('one',asyncio.Lock());await lock.acquire()
        waits=[];task=asyncio.create_task(self.pool.generate_video('p',on_wait=lambda:waits.append(time.monotonic())))
        await asyncio.sleep(.15)
        self.assertEqual(len(waits),1);task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
        lock.release()
    async def test_empty_pool_fails(self):
        self.pool.accounts=[]
        with self.assertRaises(pools.NoUsableAccountsError): await self.pool.generate_video('p')
    async def test_disabled_and_invalid_credentials_fail(self):
        for changes in [dict(scheduling=False),dict(login_ok=0),dict(auth_state='invalid')]:
            old=dict(self.row);self.row.update(changes)
            with self.subTest(changes=changes),self.assertRaises(pools.NoUsableAccountsError): await self.pool.generate_video('p')
            self.row=old
    async def test_daily_limit_fails(self):
        self.row['used_today']=2
        with self.assertRaises(pools.AllAccountsLimitedError): await self.pool.generate_video('p')
    async def test_cooldown_waits_then_expires(self):
        self.pool._conn.execute("UPDATE accounts_meta SET cooldown_until=?",(time.time()+.15,));self.pool._conn.commit()
        task=asyncio.create_task(self.pool.generate_video('p'))
        await asyncio.sleep(.08);self.assertEqual(self.worker.await_count,0)
        await asyncio.wait_for(task,2);self.assertEqual(self.worker.await_count,1)
    async def test_worker_error_is_not_resubmitted(self):
        self.worker.side_effect=RuntimeError('generation rejected')
        with self.assertRaises(RuntimeError): await self.pool.generate_video('p')
        self.assertEqual(self.worker.await_count,1)
    async def test_missing_profile_fails_no_wait(self):
        self.worker.side_effect=FileNotFoundError('profile')
        with self.assertRaises(pools.NoUsableAccountsError): await self.pool.generate_video('p')
        self.assertEqual(self.worker.await_count,1)
    async def test_status_changes_only_when_account_locked(self):
        ns,client=isolated_api();store=ns['store'];ns['pool']=self.pool;ns['scheduler']=VideoScheduler(store,self.pool)
        self.pool.account_uuid=lambda _: 'uuid'
        store.create('test','seedance-2.5','prompt','16:9',10)
        lock=self.pool._locks.setdefault('one',asyncio.Lock());await lock.acquire()
        async def worker(*args,**kwargs):
            self.assertTrue(lock.locked());self.assertEqual(store.get('test')['status'],'processing')
            raise RuntimeError('fixture ends')
        self.worker.side_effect=worker
        task=asyncio.create_task(ns['_run_task']('test','seedance-2.5','prompt','16:9',10,[],client))
        try:
            await asyncio.sleep(.12)
            self.assertEqual(store.get('test')['status'],'queued');self.assertIsNone(store.get('test')['started_at'])
            store.update('test',status='stopped');lock.release();await asyncio.wait_for(task,2)
        finally: store._conn.close()
    async def test_stopped_before_runner_starts_does_not_execute(self):
        ns,client=isolated_api();store=ns['store'];ns['pool']=self.pool;ns['scheduler']=VideoScheduler(store,self.pool)
        store.create('test','seedance-2.5','prompt','16:9',10);store.update('test',status='stopped')
        try:
            with self.assertRaises(asyncio.CancelledError): await ns['_run_task']('test','seedance-2.5','prompt','16:9',10,[],client)
            self.assertEqual(self.worker.await_count,0);self.assertEqual(store.get('test')['status'],'stopped')
        finally: store._conn.close()

    async def test_stop_busy_queued_via_existing_action(self):
        ns,client=isolated_api();store=ns['store'];ns['pool']=self.pool;ns['scheduler']=VideoScheduler(store,self.pool)
        tree=ast.parse(Path('server.py').read_text(encoding='utf-8'))
        node=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='admin_task_action')
        ns.update(TASK_ACTION_LOCKS={},_admin_auth=lambda _:None)
        exec(compile(ast.Module(body=[node],type_ignores=[]),'server.py','exec'),ns)
        self.pool.account_uuid=lambda _:'uuid'
        store.create('test','seedance-2.5','prompt','16:9',10)
        lock=self.pool._locks.setdefault('one',asyncio.Lock());await lock.acquire()
        runner=asyncio.create_task(ns['_run_task']('test','seedance-2.5','prompt','16:9',10,[],client))
        try:
            await asyncio.sleep(.12)
            result=await ns['admin_task_action']('test','stop',None)
            self.assertEqual(result['status'],'stopped');self.assertTrue(runner.cancelled())
            lock.release();await asyncio.sleep(.05)
            self.assertEqual(self.worker.await_count,0);self.assertEqual(store.get('test')['status'],'stopped')
            self.assertFalse(ns['scheduler'].reserved)
        finally:
            if lock.locked(): lock.release()
            store._conn.close()
    async def test_fifo_admission_and_parallel_capacity(self):
        order=[];gate=asyncio.Event();first=asyncio.Event()
        async def worker(account,prompt,*args,**kwargs):
            order.append(prompt)
            if prompt=='first': first.set();await gate.wait()
            return {'local_path':'mock.mp4'}
        self.worker.side_effect=worker
        task=asyncio.create_task(self.pool.generate_video('first'));await first.wait()
        waiting=[asyncio.create_task(self.pool.generate_video(str(i))) for i in range(3)]
        await asyncio.sleep(.1);gate.set()
        await asyncio.wait_for(asyncio.gather(task,*waiting),5)
        self.assertEqual(order[0],'first')
        self.assertEqual(set(order[1:]),{'0','1','2'})
        # The busy head rotates once after a bounded recheck. Existing waiters
        # stay ahead of later arrivals; exact FIFO is not promised across retries.
    async def test_no_usable_failure_code(self):
        ns,client=isolated_api();store=ns['store'];ns['pool']=self.pool;ns['scheduler']=VideoScheduler(store,self.pool)
        ns['NoUsableAccountsError']=pools.NoUsableAccountsError;self.pool.accounts=[]
        store.create('test','seedance-2.5','prompt','16:9',10)
        try:
            await ns['_run_task']('test','seedance-2.5','prompt','16:9',10,[],client)
            self.assertEqual(store.get('test')['status'],'failed')
            self.assertEqual(store.get('test')['failure_code'],'NO_USABLE_ACCOUNTS')
            self.assertIsNone(store.get('test')['started_at'])
        finally: store._conn.close()
    async def test_risk_cooldown_requeues_before_retry(self):
        calls=[];waits=[]
        async def worker(*args,**kwargs):
            calls.append(1)
            if len(calls)==1: raise pools.RiskControlError('risk fixture')
            return {'local_path':'mock.mp4'}
        self.worker.side_effect=worker
        with patch('browser_pool.COOLDOWN_SEC',.1):
            await asyncio.wait_for(self.pool.generate_video('p',on_wait=lambda:waits.append(1)),2)
        self.assertEqual(len(calls),2);self.assertEqual(len(waits),1)

    async def test_two_free_accounts_execute_in_parallel(self):
        self.pool.accounts=['one','two']
        self.pool._conn.execute("INSERT INTO accounts_meta VALUES ('two',0,0)");self.pool._conn.commit()
        self.pool.list_accounts=lambda:[{**self.row,'name':name,'busy':bool(self.pool._locks.get(name) and self.pool._locks[name].locked())} for name in self.pool.accounts]
        started=[];gate=asyncio.Event()
        async def worker(account,*args,**kwargs):
            started.append(account)
            if len(started)==2: gate.set()
            await gate.wait()
            return {'local_path':'mock.mp4'}
        self.worker.side_effect=worker
        await asyncio.wait_for(asyncio.gather(self.pool.generate_video('a'),self.pool.generate_video('b')),2)
        self.assertEqual(set(started),{'one','two'})
    async def test_waiting_scheduler_account_remains_queued(self):
        ns,client=isolated_api();store=ns['store'];ns['pool']=self.pool;ns['scheduler']=VideoScheduler(store,self.pool);self.pool.account_uuid=lambda _:'uuid'
        ns['scheduler'].account=AsyncMock(return_value=False)
        store.create('test','seedance-2.5','prompt','16:9',10)
        runner=asyncio.create_task(ns['_run_task']('test','seedance-2.5','prompt','16:9',10,[],client))
        try:
            await asyncio.sleep(.08)
            self.assertEqual(store.get('test')['status'],'queued');self.assertEqual(self.worker.await_count,0)
            runner.cancel()
            with self.assertRaises(asyncio.CancelledError): await runner
        finally: store._conn.close()
    async def test_per_key_capacity_wait_keeps_queued_and_honors_stop(self):
        from collections import defaultdict
        ns,client=isolated_api();store=ns['store'];ns['pool']=self.pool;ns['scheduler']=VideoScheduler(store,self.pool)
        tree=ast.parse(Path('server.py').read_text(encoding='utf-8'))
        node=next(n for n in tree.body if isinstance(n,ast.ClassDef) and n.name=='KeyConcurrencyLimiter')
        ns['defaultdict']=defaultdict
        exec(compile(ast.Module(body=[node],type_ignores=[]),'server.py','exec'),ns)
        limiter=ns['key_limiter']=ns['KeyConcurrencyLimiter']()
        await limiter.acquire(client['api_key_hash'],1)
        store.create('test','seedance-2.5','prompt','16:9',10)
        runner=asyncio.create_task(ns['_run_task']('test','seedance-2.5','prompt','16:9',10,[],client))
        try:
            await asyncio.sleep(.08);self.assertEqual(store.get('test')['status'],'queued')
            store.update('test',status='stopped');await limiter.release(client['api_key_hash'])
            with self.assertRaises(asyncio.CancelledError): await runner
            self.assertEqual(self.worker.await_count,0)
            self.assertEqual(limiter._active[client['api_key_hash']],0)
        finally: store._conn.close()

    async def test_cooling_eligible_prevents_false_daily_exhaustion(self):
        self.pool.list_accounts=lambda:[{**self.row,'cooling':True}, {**self.row,'name':'limited','used_today':2}]
        self.assertFalse(self.pool.available)
        self.assertFalse(self.pool.all_accounts_limited)
        task=asyncio.create_task(self.pool.generate_video('p'))
        await asyncio.sleep(.06);self.assertFalse(task.done());task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task
    async def test_cooling_eligible_prevents_false_credit_exhaustion(self):
        self.pool.list_accounts=lambda:[{**self.row,'cooling':True}, {**self.row,'name':'empty','quota_blocked':True}]
        self.assertFalse(self.pool.available)
        self.assertFalse(self.pool.all_accounts_quota_blocked)
        task=asyncio.create_task(self.pool.generate_video('p'))
        await asyncio.sleep(.06);self.assertFalse(task.done());task.cancel()
        with self.assertRaises(asyncio.CancelledError): await task

class ProjectTests(unittest.TestCase):
    def setUp(self):
        library.LibraryApiTests.setUp(self)
        self.store.update_scene(self.scene["id"],prompt="")
    tearDown=library.LibraryApiTests.tearDown
    create_scene=library.LibraryApiTests.create_scene
    upload=library.LibraryApiTests.upload
    asset=library.LibraryApiTests.asset
    generate=library.LibraryApiTests.generate
    def ready(self): return [self.create_scene(n,'A quiet forest') for n in [3,1,2]]
    def project_generate(self,expected=202):
        response=self.http.post('/api/admin/projects/'+self.project_id+'/generate')
        self.assertEqual(response.status_code,expected,response.text)
        return response.json()
    def test_three_ready_sorted_batch(self):
        self.ready();result=self.project_generate()
        self.assertEqual((result['requested'],result['created'],result['skipped']),(4,3,1))
        self.assertEqual([r['scene_number'] for r in result['tasks']],[1,2,3])
        self.assertEqual(result['skipped_scenes'][0]['reason'],'INVALID_CONFIGURATION')
        for i,task in enumerate(result['tasks'],1):
            row=self.store.get(task['task_id'])
            self.assertEqual((row['scene_id'],row['batch_id'],row['batch_index'],row['batch_count']),(task['scene_id'],result['batch_id'],i,3))
            self.assertEqual(row['prompt'],'A quiet forest');self.assertEqual(row['reference_snapshot'],'[]')
        self.assertEqual(len(self.queued),3)

    def test_generate_all_runs_through_scheduler_and_keeps_scene_links(self):
        self.ready()
        result = self.project_generate()
        async def worker(*args, **kwargs):
            output = Path(self.tmp.name) / 'fixture.mp4'
            output.write_bytes(b'fake video')
            return {'local_path': str(output)}
        self.ns['pool'].generate_video = worker
        for args in self.queued:
            library.run_saved_task(self, args)
        for item in result['tasks']:
            row = self.store.get(item['task_id'])
            self.assertEqual(row['status'], 'completed')
            self.assertEqual(row['phase'], 'completed')
            self.assertEqual(row['scene_id'], item['scene_id'])
            self.assertEqual(row['batch_id'], result['batch_id'])
            self.assertIn(item['task_id'], row['video_url'])
            self.assertTrue(row['reference_paths'])
    def test_duplicate_calls_skip_active(self):
        self.ready();first=self.project_generate();second=self.project_generate()
        self.assertEqual(first['created'],3);self.assertEqual(second['created'],0)
        self.assertEqual(sum(r['reason']=='ACTIVE_GENERATION_EXISTS' for r in second['skipped_scenes']),3)
    def test_processing_skip(self):
        scene=self.create_scene(1,'forest');task=self.generate(scene)
        self.store.update(task['id'],status='processing')
        result=self.project_generate();self.assertEqual(result['created'],0)
        self.assertIn('ACTIVE_GENERATION_EXISTS',[r['reason'] for r in result['skipped_scenes']])
    def test_selected_skip_but_regenerate_allowed(self):
        scene=self.create_scene(1,'forest');task=self.generate(scene)
        self.store.update(task['id'],status='completed',video_url='/videos/fixture.mp4')
        self.store.select_scene_task(scene['id'],task['id'])
        result=self.project_generate();self.assertEqual(result['created'],0)
        self.assertIn('ALREADY_SELECTED',[r['reason'] for r in result['skipped_scenes']])
        self.generate(scene)
    def test_old_failed_completed_stopped_allow_attempts(self):
        for n,status in enumerate(['failed','completed','stopped'],1):
            scene=self.create_scene(n,'forest');task=self.generate(scene);self.store.update(task['id'],status=status)
        self.assertEqual(self.project_generate()['created'],3)
    def test_missing_skipped_and_snapshot_exact(self):
        a=self.asset('Hero');b=self.asset('Cabin')
        def row(n,refs): return dict(scene_number=n,scene_name='exact',prompt='@Hero enters',model='seedance-2.5',ratio='9:16',duration=15,start_end=False,references=refs)
        refs=[dict(name='Cabin',alias='Cabin'),dict(name='Hero',alias='Hero')]
        payload={'scenes':[row(1,refs),row(2,[dict(name='Absent',alias='Hero')])]}
        imported=self.http.post('/api/admin/projects/'+self.project_id+'/scenes/import',json=payload).json()['scenes']
        before=[self.store.scene_details(s['id']) for s in imported]
        result=self.project_generate();self.assertEqual(result['created'],1)
        self.assertIn('MISSING_REFERENCES',[r['reason'] for r in result['skipped_scenes']])
        task=self.store.get(result['tasks'][0]['task_id'])
        self.assertEqual(task['prompt'],'@Image2 enters')
        self.assertEqual((task['ratio'],task['duration'],task['model']),('9:16',15,'seedance-2.5'))
        snapshot=json.loads(task['reference_snapshot'])
        self.assertEqual([r['asset_id'] for r in snapshot],[b['id'],a['id']])
        self.assertEqual([self.store.scene_details(s['id']) for s in imported],before)
    def test_creation_sql_failure_rolls_back_and_schedules_none(self):
        self.ready()
        self.store._conn.execute("CREATE TRIGGER fail_batch BEFORE INSERT ON tasks WHEN NEW.batch_index=2 BEGIN SELECT RAISE(ABORT,'injected'); END");self.store._conn.commit()
        self.project_generate(409)
        self.assertEqual(self.store.pending_task_count(),0);self.assertEqual(self.queued,[])
    def test_queue_limit_atomic(self):
        self.ready();self.ns['config'].MAX_PENDING_TASKS=2
        self.project_generate(429);self.assertEqual(self.store.pending_task_count(),0)
    def test_client_quota_atomic(self):
        self.ready();self.client_policy['daily_limit']=2
        self.project_generate(429);self.assertEqual(self.store.pending_task_count(),0)
    def test_read_model_latest_active_selected(self):
        scene=self.create_scene(1,'forest');task=self.generate(scene)
        url='/api/admin/projects/'+self.project_id+'/generation-status'
        status=self.http.get(url).json()['scenes'][0]
        self.assertEqual(status['generation_status'],'QUEUED');self.assertEqual(status['latest_task']['id'],task['id'])
        self.store.update(task['id'],status='failed')
        self.assertEqual(self.http.get(url).json()['scenes'][0]['generation_status'],'FAILED')
        self.store.update(task['id'],status='completed',video_url='/videos/x.mp4');self.store.select_scene_task(scene['id'],task['id'])
        self.assertEqual(self.http.get(url).json()['scenes'][0]['generation_status'],'SELECTED')
    def test_empty_project_no_admission_required(self):
        p=self.store.create_project('Empty')
        self.ns['pool'].accounts=[]
        response=self.http.post('/api/admin/projects/'+p['id']+'/generate')
        self.assertEqual(response.status_code,202,response.text);self.assertEqual(response.json()['created'],0)
    def test_project_auth_missing(self):
        self.assertEqual(self.http.post('/api/admin/projects/missing/generate').status_code,404)
        self.assertEqual(self.http.post('/api/admin/projects/'+self.project_id+'/generate',headers={'X-Admin-Key':'wrong'}).status_code,401)

    def test_duplicate_rechecked_at_commit(self):
        scene=self.create_scene(1,'forest')
        original=self.store.scene_generation_input
        def raced(scene_id):
            result=original(scene_id)
            self.store.create('racing','seedance-2.5','forest','16:9',30,scene_id=scene_id,reference_snapshot=[])
            return result
        with patch.object(self.store,'scene_generation_input',raced): result=self.project_generate()
        self.assertEqual(result['created'],0)
        self.assertEqual(self.store.pending_task_count(),1)
        self.assertEqual(self.queued,[])
    def test_selected_rechecked_at_commit(self):
        scene=self.create_scene(1,'forest');task=self.generate(scene)
        self.store.update(task['id'],status='completed',video_url='/videos/x.mp4')
        original=self.store.scene_generation_input
        def raced(scene_id):
            result=original(scene_id);self.store.select_scene_task(scene_id,task['id']);return result
        with patch.object(self.store,'scene_generation_input',raced): result=self.project_generate()
        self.assertEqual(result['created'],0)
        self.assertIn('ALREADY_SELECTED',[r['reason'] for r in result['skipped_scenes']])
    def test_scene_changed_during_preparation_rolls_back(self):
        self.ready();original=self.store.scene_generation_input
        def changed(scene_id):
            result=original(scene_id);self.store.update_scene(scene_id,prompt='changed');return result
        with patch.object(self.store,'scene_generation_input',changed): self.project_generate(409)
        self.assertEqual(self.store.pending_task_count(),0);self.assertEqual(self.queued,[])
    def test_client_duration_policy_reused(self):
        self.create_scene(1,'forest',duration=30)
        self.client_policy['allowed_durations']=[10]
        self.project_generate(422);self.assertEqual(self.store.pending_task_count(),0)
    def test_single_task_project_batch_and_start_end_snapshot(self):
        a=self.asset('Start');b=self.asset('End');scene=self.create_scene(1,'motion',start_end=True)
        self.store.attach_asset_to_scene(scene['id'],a['id'],1,'Start')
        self.store.attach_asset_to_scene(scene['id'],b['id'],2,'End')
        result=self.project_generate();self.assertEqual(result['created'],1)
        task=self.store.get(result['tasks'][0]['task_id'])
        self.assertEqual((task['batch_count'],task['batch_index'],task['start_end']),(1,1,1))
        self.assertEqual(task['batch_id'],result['batch_id']);self.assertTrue(result['batch_id'])
        self.assertIn('opening frame',task['prompt']);self.assertEqual(len(json.loads(task['reference_snapshot'])),2)

    def test_selected_skip_even_if_scene_draft_now(self):
        scene=self.create_scene(1,'forest');task=self.generate(scene)
        self.store.update(task['id'],status='completed',video_url='/videos/x.mp4')
        self.store.select_scene_task(scene['id'],task['id']);self.store.update_scene(scene['id'],prompt='')
        result=self.project_generate()
        reason=next(r['reason'] for r in result['skipped_scenes'] if r['scene_id']==scene['id'])
        self.assertEqual(reason,'ALREADY_SELECTED')
    def test_stopped_durable_copies_remain_without_execution(self):
        root=Path(self.tmp.name)/'temporary';root.mkdir();image=root/'image.png';image.write_bytes(library.image_bytes())
        self.ns['UPLOADED_REFERENCES']['uploaded://fixture']=(root,[str(image)])
        request=self.ns['VideoGenRequest'](prompt='forest',reference_images=['uploaded://fixture'])
        response=asyncio.run(self.ns['_submit_video'](request,self.client_policy))
        task=self.store.get(response.id);self.store.update(response.id,status='stopped')
        copies=[Path(value[8:]) for value in json.loads(task['reference_images'])]
        self.assertTrue(copies)
        with self.assertRaises(asyncio.CancelledError):
            asyncio.run(self.runner(task['id'],task['model'],task['prompt'],task['ratio'],task['duration'],json.loads(task['reference_images']),self.client_policy))
        self.assertFalse(self.ns['UPLOADED_REFERENCES']);self.assertTrue(all(path.is_file() for path in copies))
        self.assertEqual(self.store.get(task['id'])['status'],'stopped')

if __name__=='__main__': unittest.main()
