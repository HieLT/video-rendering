"""Offline integration tests: FIFO admission and durable video lifecycle."""
import asyncio
import json
import sqlite3
import tempfile
import unittest
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
from browser_queue import BrowserQueue
from store import TaskStore, PendingTaskLimitExceeded
from video_schedule import VideoScheduler, parse_eta
import video_schedule as schedule

class Pool:
    def __init__(self):
        self.reservations={}
        self._locks={}
    def list_accounts(self): return [{"name":"fixture"}]
    def _schedulable(self,a): return a["name"] not in self.reservations
    def account_uuid(self,a): return "uuid-fixture"
    @asynccontextmanager
    async def account_activity(self,*args): yield

class ScheduleTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp=tempfile.TemporaryDirectory()
        self.store=TaskStore(str(Path(self.tmp.name)/"tasks.db"))
        self.pool=Pool()
        self.scheduler=VideoScheduler(self.store,self.pool)
        self.store.create("job","seedance-2.5","original prompt","16:9",30)
        self.now=1000.
        self.sleeps=[]
    def tearDown(self):
        self.store._conn.close()
        self.tmp.cleanup()
    async def sleep(self,seconds):
        self.sleeps.append(seconds)
        self.now+=seconds
    def clock(self):
        return patch.object(schedule,"time",SimpleNamespace(time=lambda:self.now))
    def accepted(self):
        return {"kind":"accepted","poll":{"accepted":True,"texts":["**Dreamina Seedance** ready in 25 minutes"]}}
    async def drive(self,submit,checks):
        with self.clock(),patch.object(schedule.asyncio,"sleep",self.sleep),patch.object(self.scheduler,"submit",submit),patch.object(schedule,"inspect_conversation",checks),patch.object(self.scheduler,"references",AsyncMock(return_value=["one","two","three"])):
            await self.scheduler.run("job",{})
    def test_eta(self):
        samples=[
            ("**Dreamina Seedance** 25\u5206\u5f8c",1500),
            ("**Dreamina Seedance** 15\u5206\u5206\u5f8c",900),
            ("**Dreamina Seedance** ready in 25 minutes",1500),
            ("15-second video with **Dreamina Seedance** ready in 25 minutes",1500),
            ("**Dreamina Seedance** 3\u6642\u9593\u5206\u5f8c",10800),
            ("**Dreamina Seedance** \u9884\u8ba1 25\u5206\u949f",1500),
            ("**Dreamina Seedance** ready in 1 hour 30 minutes",5400),
            ("**Dreamina Seedance**",1800)]
        for text,expected in samples:
            self.assertEqual(parse_eta([text])[0],expected,text)
    async def test_first_check_then_three_rechecks(self):
        self.store.update("job",conversation_id="123")
        submit=AsyncMock(return_value=self.accepted())
        checks=AsyncMock(return_value={"kind":"pending","poll":{}})
        await self.drive(submit,checks)
        row=self.store.get("job")
        self.assertEqual(row["status"],"needs_recovery")
        self.assertEqual(row["check_round"],3)
        self.assertEqual(checks.await_count,4)
        self.assertAlmostEqual(self.now,1000+1500-schedule.EARLY_CHECK_SECONDS+900)
        self.assertEqual(self.scheduler.reserved,{"fixture":"job"})
    async def test_early_manual_check_restores_eta_before_spending_rechecks(self):
        for inspection_error in (False, True):
            with self.subTest(inspection_error=inspection_error):
                self.now=1000.
                self.store.update("job",account="fixture",conversation_id="123",
                                  status="queued",phase="checking",accepted_at=900,
                                  eta_seconds=7200,next_check_at=None,check_round=3,retry_count=6)
                observed=[]
                async def inspect(row):
                    observed.append((self.now,row["check_round"]))
                    if inspection_error and len(observed)==1:
                        raise RuntimeError("temporary inspection failure")
                    return {"kind":"pending","poll":{"accepted":True}}
                submit=AsyncMock()
                await self.drive(submit,AsyncMock(side_effect=inspect))
                submit.assert_not_called()
                self.assertEqual(observed,[(1000.,3),(7800.,0),(8100.,1),(8400.,2),(8700.,3)])
                row=self.store.get("job")
                self.assertEqual(row["status"],"needs_recovery")
                self.assertEqual(row["retry_count"],6)

    async def test_video_available_before_eta_completes_immediately(self):
        self.store.update("job",account="fixture",conversation_id="123",phase="checking",
                          accepted_at=900,eta_seconds=7200)
        checks=AsyncMock(return_value={"kind":"completed","poll":{"videos":["https://fixture/video.mp4"]}})
        complete=AsyncMock()
        with patch.object(self.scheduler,"complete",complete):
            await self.drive(AsyncMock(),checks)
        complete.assert_awaited_once()
        self.assertEqual(self.now,1000.)

    async def test_restart_preserves_eta_and_account(self):
        self.store.update("job",account="fixture",conversation_id="123",phase="waiting",accepted_at=900,next_check_at=1500,check_round=2,retry_count=4,status="processing")
        self.scheduler=VideoScheduler(self.store,self.pool)
        self.scheduler.recover_reservations()
        checks=AsyncMock(return_value={"kind":"pending","poll":{}})
        submit=AsyncMock()
        await self.drive(submit,checks)
        submit.assert_not_called()
        self.assertEqual(checks.await_count,2)
        self.assertEqual(self.store.get("job")["retry_count"],4)
    async def test_uncertain_dispatch_never_resubmits(self):
        self.store.update("job",account="fixture",phase="dispatching",dispatch_uncertain=1)
        submit=AsyncMock()
        await self.drive(submit,AsyncMock())
        submit.assert_not_called()
        self.assertEqual(self.store.get("job")["status"],"needs_recovery")
    async def test_cleanup_failure_after_acceptance_does_not_resubmit(self):
        async def submit(row,paths):
            self.store.update("job",conversation_id="123",phase="waiting",accepted_at=self.now,
                              next_check_at=self.now+1500,dispatch_uncertain=0)
            raise RuntimeError("browser cleanup interrupted")
        submit_mock=AsyncMock(side_effect=submit)
        await self.drive(submit_mock,AsyncMock(return_value={"kind":"pending","poll":{}}))
        self.assertEqual(submit_mock.await_count,1)
        self.assertEqual(self.store.get("job")["retry_count"],0)

    async def test_live_rejections_share_session_and_persist_budget(self):
        self.store.update("job", account="fixture", conversation_id="123", retry_count=8)
        async def generate(*args, **kwargs):
            self.assertFalse(kwargs["single_attempt"])
            for index in (2, 4):
                self.assertTrue(kwargs["on_rejected"]({"latestIndex":index, "rejection":{"code":"error"}}))
            self.assertEqual(self.store.get("job")["retry_count"], 10)
            self.assertFalse(kwargs["on_rejected"]({"latestIndex":6, "rejection":{"code":"error"}}))
            return {"kind":"rejected", "poll":{}}
        with patch.object(schedule.worker, "generate_video", AsyncMock(side_effect=generate)) as generate_mock:
            await self.scheduler.submit(self.store.get("job"), [])
        self.assertEqual(generate_mock.await_count, 1)
        self.assertEqual(self.store.get("job")["status"], "needs_recovery")
        self.assertEqual(self.store.get("job")["after_index"], 6)

    async def test_uncertain_response_keeps_monitoring_until_acceptance(self):
        self.store.update("job", account="fixture", conversation_id="123")
        async def generate(*args, **kwargs):
            return await kwargs["monitor"]("fixture", None, None, "123", 90)
        outcomes = [{"kind":"pending", "poll":{}}, self.accepted()]
        with patch.object(schedule.worker, "generate_video", AsyncMock(side_effect=generate)), patch.object(schedule, "inspect_page", AsyncMock(side_effect=outcomes)) as inspect:
            outcome = await self.scheduler.submit(self.store.get("job"), [])
        self.assertEqual(inspect.await_count, 2)
        self.assertEqual(outcome["kind"], "accepted")
        self.assertEqual(self.store.get("job")["phase"], "waiting")
        self.assertEqual(self.store.get("job")["retry_count"], 0)

    async def test_generation_budget_persists(self):
        self.store.update("job",retry_count=9)
        submit=AsyncMock(side_effect=RuntimeError("setup error"))
        await self.drive(submit,AsyncMock())
        self.assertEqual(submit.await_count,2)
        self.assertEqual(self.store.get("job")["retry_count"],10)
        self.assertEqual(self.store.get("job")["status"],"needs_recovery")
    async def test_rejection_retains_boundary_and_parameters(self):
        async def submit(row,paths):
            if row.get("retry_count")==0:
                self.store.update("job",conversation_id="123")
                return {"kind":"rejected","poll":{"rejection":{"code":"fixture"},"latestIndex":7}}
            self.assertEqual(row["after_index"],7)
            self.assertEqual(row["prompt"],"original prompt")
            self.assertEqual(row["duration"],30)
            self.assertEqual(paths,["one","two","three"])
            return self.accepted()
        checks=AsyncMock(return_value={"kind":"pending","poll":{}})
        await self.drive(AsyncMock(side_effect=submit),checks)
        self.assertEqual(self.store.get("job")["retry_count"],1)
    async def test_delayed_copyright_rejection_retries_saved_request(self):
        self.store.update("job", account="fixture", conversation_id="123", phase="checking",
                          status="processing", accepted_at=900, retry_count=9)
        rejected={"kind":"rejected", "poll":{"latestIndex":12,
            "rejection":{"code":"710092007", "reason":"Copyright refusal", "terminal":True}}}
        checks=AsyncMock(return_value=rejected)
        async def generate(account, prompt, **kwargs):
            self.assertEqual(account,"fixture")
            self.assertEqual(prompt,"original prompt")
            self.assertEqual(kwargs["reference_image_paths"],["one","two","three"])
            self.assertEqual((kwargs["ratio"],kwargs["duration"],kwargs["model"]),("16:9",30,"seedance-2.5"))
            self.assertEqual(kwargs["start_conversation_id"],"123")
            self.assertEqual(kwargs["after_index_start"],12)
            self.assertEqual(self.store.get("job")["retry_count"],10)
            return {"kind":"rejected", "poll":{**rejected["poll"],"latestIndex":14}}
        with self.clock(), patch.object(schedule,"inspect_conversation",checks), \
                patch.object(self.scheduler,"references",AsyncMock(return_value=["one","two","three"])), \
                patch.object(schedule.worker,"generate_video",AsyncMock(side_effect=generate)) as generate_mock:
            await self.scheduler.run("job",{})
        generate_mock.assert_awaited_once()
        self.assertEqual(self.store.get("job")["status"],"needs_recovery")
        self.assertEqual(self.store.get("job")["retry_count"],10)
        self.assertIn("710092007",self.store.get("job")["error"])

    async def test_video_wins_and_download_happens_after_inspection(self):
        self.store.update("job",account="fixture",conversation_id="123",phase="checking",accepted_at=900)
        poll={"videos":["https://fixture/video.mp4"]}
        checks=AsyncMock(return_value={"kind":"completed","poll":poll})
        complete=AsyncMock()
        with patch.object(self.scheduler,"complete",complete):
            await self.drive(AsyncMock(),checks)
        complete.assert_awaited_once()
    def test_review_counts_against_admission_limit(self):
        self.store.update("job",status="needs_recovery")
        with self.assertRaises(PendingTaskLimitExceeded):
            self.store.create("second","seedance-2.5","prompt","16:9",30,max_pending=1)
    async def test_local_references_survive_restart(self):
        source=Path(self.tmp.name)/"image.png";source.write_bytes(b"fixture")
        # Use the temp directory as cwd; task copies stay outside the workspace.
        import os
        previous=os.getcwd()
        try:
            os.chdir(self.tmp.name)
            self.store.update("job",reference_images=json.dumps(["local://"+str(source)]))
            first=await self.scheduler.references(self.store.get("job"),{})
            second=await VideoScheduler(self.store,self.pool).references(self.store.get("job"),{})
            self.assertEqual(first,second)
            self.assertEqual(Path(second[0]).read_bytes(),b"fixture")
        finally:os.chdir(previous)

class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_launch_spacing_shared_between_queue_instances(self):
        with tempfile.TemporaryDirectory() as root:
            queues = [BrowserQueue(root) for _ in range(3)]
            starts = []
            release = asyncio.Event()
            all_started = asyncio.Event()
            async def job(queue):
                async with queue.slot():
                    starts.append(asyncio.get_running_loop().time())
                    if len(starts) == 3:
                        all_started.set()
                    await release.wait()
            tasks = [asyncio.create_task(job(queue)) for queue in queues]
            try:
                await asyncio.wait_for(all_started.wait(), 4)
                self.assertTrue(all(b - a >= .49 for a, b in zip(starts, starts[1:])))
                self.assertTrue(all(not task.done() for task in tasks))
            finally:
                release.set()
                await asyncio.gather(*tasks)

    async def test_limit_fifo_and_cancelled_waiter(self):
        with tempfile.TemporaryDirectory() as root:
            queue=BrowserQueue(root,limit=2)
            active=0;peak=0;order=[]
            release=asyncio.Event()
            two_started=asyncio.Event()
            async def job(index):
                nonlocal active,peak
                async with queue.slot():
                    order.append(index);active+=1;peak=max(peak,active)
                    if len(order)==2: two_started.set()
                    if index<2: await release.wait()
                    await asyncio.sleep(.01)
                    active-=1
            tasks=[]
            for index in range(6):
                tasks.append(asyncio.create_task(job(index)))
                await asyncio.sleep(.03)
            await asyncio.wait_for(two_started.wait(), 3)
            self.assertEqual(order,[0,1])
            tasks[3].cancel()
            with self.assertRaises(asyncio.CancelledError): await tasks[3]
            release.set()
            await asyncio.gather(*(t for i,t in enumerate(tasks) if i!=3))
            self.assertEqual(order,[0,1,2,4,5])
            self.assertEqual(peak,2)
            db=queue.connect()
            self.assertEqual(db.execute("select count(*) from tickets").fetchone()[0],0)
            db.close()
    async def test_stale_ticket_is_reclaimed(self):
        with tempfile.TemporaryDirectory() as root:
            queue=BrowserQueue(root,limit=1)
            db=queue.connect()
            with db:db.execute("insert into tickets(owner,active) values ('dead',1)")
            db.close()
            async with queue.slot(): pass

if __name__=="__main__":unittest.main(verbosity=2)
