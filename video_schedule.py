"""Persisted ETA scheduling; a waiting task owns an account, not a browser."""
import asyncio
import json
import re
import shutil
import time
from pathlib import Path
from browser_queue import async_playwright
from browser import launch_account_context, cookie_value
from video_worker import POLL_JS, extract_unwatermarked_url, _download
import video_worker_ui as worker
import config

CHECK_SECONDS = 20
CHECK_INTERVAL = 300
MAX_RECHECKS = 3
MAX_RETRIES = 10
DEFAULT_ETA = 1800
EARLY_CHECK_SECONDS = 300

def parse_eta(texts):
    hour = r"\u6642\u9593|\u5c0f\u65f6|\u5c0f\u6642|hours?|hrs?|gi\u1edd"
    minute = r"\u5206(?:\u5206)?(?:\u5f8c|\u540e)?|\u5206\u949f|\u5206\u9418|minutes?|mins?|ph\u00fat"
    second = r"seconds?|\u79d2|gi\u00e2y"
    for text in texts:
        if not re.search(r"Dreamina\s+Seedance", text, re.I): continue
        for sentence in re.split(r"[\u3002\n!?]", text):
            if not re.search(r"\u5f8c|\u540e|\u9884\u8ba1|\u5b8c\u6210|ready|complete|take|wait|generated|finish|ho\u00e0n|sau|m\u1ea5t", sentence, re.I):
                continue
            # Discard preceding descriptions such as "15-second video ... ready in".
            anchor = list(re.finditer(r"(?:ready|completed?|finished?|generated)\s+in\b|\b(?:takes?|wait)\s+(?:about\s+)?|\bsau\b", sentence, re.I))
            if anchor: sentence=sentence[anchor[-1].end():]
            matches=re.findall(r"(\d+(?:[.,]\d+)?)\s*("+hour+"|"+minute+"|"+second+")", sentence, re.I)
            total=0
            for number,unit in matches:
                scale=3600 if re.fullmatch(hour,unit,re.I) else (60 if re.fullmatch(minute,unit,re.I) else 1)
                total+=float(number.replace(",","."))*scale
            if total>0: return total,text
    return DEFAULT_ETA,""

async def inspect_page(page, context, conversation_id, after_index, seconds, stop_on_accept=False):
    deadline = time.monotonic() + seconds
    previous_rejection = None
    last = None
    while time.monotonic() < deadline:
        remaining = deadline-time.monotonic()
        try:
            async def read():
                cookies = await context.cookies("https://www.dola.com")
                return await page.evaluate(POLL_JS, {
                    "conversationId":conversation_id, "afterIndex":after_index,
                    "msToken":cookie_value(cookies,"msToken"),
                    "fp":cookie_value(cookies,"s_v_web_id")})
            result = await asyncio.wait_for(read(), remaining)
            last = result
            if result.get("ok"):
                if result.get("videos"): return {"kind":"completed", "poll":result}
                rejection = result.get("rejection")
                if rejection and result.get("responseFinished") and not result.get("responseGenerating"):
                    index = result.get("latestIndex")
                    if previous_rejection == index:
                        return {"kind":"rejected", "poll":result}
                    previous_rejection = index
                else:
                    previous_rejection = None
                if (stop_on_accept and result.get("accepted") and not rejection
                        and result.get("responseFinished") and not result.get("responseGenerating")):
                    return {"kind":"accepted", "poll":result}
            else:
                previous_rejection = None
        except asyncio.CancelledError:
            raise
        except Exception:
            previous_rejection = None
        await asyncio.sleep(min(2 if stop_on_accept else 5, max(0,deadline-time.monotonic())))
    return {"kind":"pending", "poll":last or {}}

async def inspect_conversation(row):
    async with async_playwright() as p:
        context = await launch_account_context(p, row["account"], headless=False, use_extension=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat/"+row["conversation_id"], timeout=60000, wait_until="domcontentloaded")
            return await inspect_page(page,context,row["conversation_id"],row.get("after_index") or 0,CHECK_SECONDS)
        finally:
            await context.close()

class VideoScheduler:
    def __init__(self,store,pool):
        self.store,self.pool=store,pool
        self.reserved={}
        pool.reservations=self.reserved

    def recover_reservations(self):
        rows=self.store._conn.execute("SELECT * FROM tasks WHERE status IN ('queued','processing','needs_recovery') AND deleted_at IS NULL ORDER BY created_at").fetchall()
        for raw in rows:
            row=dict(raw)
            if row.get("account"):
                self.reserved.setdefault(row["account"],row["id"])

    def review(self,task_id,reason):
        self.store.update(task_id,status="needs_recovery",phase="review",error=reason,next_check_at=None,finished_at=None)

    def restore_eta_wait(self,row):
        # Early manual checks must not consume the scheduled recheck budget.
        accepted,eta=row.get("accepted_at"),row.get("eta_seconds")
        if not accepted or not eta:
            return False
        due=accepted+max(0,eta-EARLY_CHECK_SECONDS)
        if time.time()>=due:
            return False
        self.store.update(row["id"],phase="waiting",status="processing",
                          next_check_at=due,check_round=0)
        return True

    def release(self,row):
        if self.reserved.get(row.get("account"))==row["id"]:
            self.reserved.pop(row["account"],None)
            other=self.store._conn.execute(
                "SELECT id FROM tasks WHERE account=? AND id<>? AND deleted_at IS NULL AND status IN ('queued','processing','needs_recovery') ORDER BY created_at LIMIT 1",
                (row["account"],row["id"])).fetchone()
            if other: self.reserved[row["account"]]=other[0]

    async def references(self,row,uploaded):
        saved=json.loads(row.get("reference_paths") or "[]")
        if saved:
            if not all(Path(p).is_file() for p in saved): raise RuntimeError("Saved reference image is missing")
            return saved
        from media import download_reference_images
        root=Path(".job_media")/row["id"]
        root.mkdir(parents=True,exist_ok=True)
        paths=[]
        sources = json.loads(row.get("reference_images") or "[]")
        if row.get("reference_snapshot") is not None:
            from scene_generation import task_reference_paths
            sources = ["local://" + path for path in task_reference_paths(row)]
        for source in sources:
            cleanup=None
            if source.startswith("local://"):
                originals=[source[8:]]
            elif source.startswith("uploaded://"):
                item=uploaded.get(source)
                if not item: raise RuntimeError("Legacy uploaded reference expired; manual recovery required")
                originals=item[1]
            else:
                cleanup,originals=await download_reference_images([source],row["id"])
            try:
                for original in originals:
                    target=root/(str(len(paths)+1)+Path(original).suffix)
                    if Path(original).resolve()!=target.resolve():
                        shutil.copy2(original,target)
                    paths.append(str(target.resolve()))
            finally:
                if cleanup: shutil.rmtree(cleanup,ignore_errors=True)
        self.store.update(row["id"],reference_paths=json.dumps(paths))
        return paths

    def claim_once(self,row):
        # Quota ledger and counter use the same transaction, so a restart cannot double charge.
        db=self.pool._conn
        db.execute("CREATE TABLE IF NOT EXISTS video_job_account_claims(task_id TEXT,account TEXT,PRIMARY KEY(task_id,account))")
        db.commit()
        if db.execute("SELECT 1 FROM video_job_account_claims WHERE task_id=? AND account=?",(row["id"],row["account"])).fetchone(): return
        used=self.pool.used_today(row["account"])
        now=time.time()
        with db:
            inserted=db.execute("INSERT OR IGNORE INTO video_job_account_claims VALUES (?,?)",(row["id"],row["account"])).rowcount
            if inserted:
                db.execute("UPDATE account_usage SET used=?,last_used_at=? WHERE uuid=?",
                           (used+1,now,self.pool.account_uuid(row["account"])))
                db.execute("UPDATE accounts_meta SET last_used_at=? WHERE name=?",(now,row["account"]))

    async def account(self,row):
        if row.get("account"):
            owner=self.reserved.get(row["account"])
            if owner and owner!=row["id"]: return False
            self.reserved[row["account"]]=row["id"]
            return True
        accounts = self.pool.list_accounts()
        if not accounts:
            self.store.update(row['id'], status='failed', phase='failed', failure_code='NO_USABLE_ACCOUNTS',
                              error='No account profiles in pool', finished_at=time.time())
            return False
        for acc in accounts:
            lock=self.pool._locks.get(acc["name"])
            if self.pool._schedulable(acc) and not (lock and lock.locked()):
                self.reserved[acc["name"]]=row["id"]
                self.store.update(row["id"],account=acc["name"],account_uuid=self.pool.account_uuid(acc["name"]))
                return True
        return False

    def retry(self,row,error):
        if (row.get("retry_count") or 0)>=MAX_RETRIES:
            self.review(row["id"],"Exhausted 10 generation retries: "+str(error))
            return False
        self.store.update(row["id"],phase="ready",status="queued",
                          retry_count=(row.get("retry_count") or 0)+1,
                          error=str(error),next_check_at=None,dispatch_uncertain=0)
        return True

    async def submit(self,row,paths):
        task_id=row["id"]
        async def monitor(account,page,context,conversation_id,timeout,on_poll=None,on_balance=None,after_index=0):
            # Unknown/streaming replies keep this browser open; never resubmit blindly.
            while True:
                outcome=await inspect_page(page,context,conversation_id,after_index,90,True)
                if outcome["kind"] != "pending":
                    break
            # Save acceptance before closing Chromium: shutdown/cleanup must not erase it.
            if outcome["kind"]=="accepted":
                eta,raw=parse_eta(outcome["poll"].get("texts") or [])
                now=time.time()
                self.store.update(task_id,phase="waiting",status="processing",accepted_at=now,
                                  eta_seconds=eta,eta_raw=raw,next_check_at=now+max(0, eta-EARLY_CHECK_SECONDS),
                                  check_round=0,dispatch_uncertain=0,error=None)
            return outcome
        def rejected(poll):
            current = self.store.get(task_id)
            if (poll.get("latestIndex") or 0) <= (current.get("after_index") or 0):
                self.review(task_id, "Cannot safely identify rejected submission; inspect conversation")
                return False
            self.store.update(task_id, after_index=poll["latestIndex"], dispatch_uncertain=0,
                              check_round=0, accepted_at=None)
            return self.retry(self.store.get(task_id), poll.get("rejection"))
        def before_dispatch():
            self.store.update(task_id,phase="dispatching",dispatch_uncertain=1,submitted_at=time.time())
            self.claim_once(self.store.get(task_id))
        def conversation(account,conversation_id,deadline):
            self.store.update(task_id,conversation_id=conversation_id,last_poll_at=time.time())
        async with self.pool.account_activity(row["account"],"generating"):
            return await worker.generate_video(
                row["account"],row["prompt"],ratio=None if row["ratio"]=="default" else row["ratio"],
                duration=row["duration"],model=row["model"],reference_image_paths=paths,
                on_submit=before_dispatch,on_conversation_id=conversation,
                single_attempt=False,monitor=monitor,on_rejected=rejected,
                start_conversation_id=row.get("conversation_id"),after_index_start=row.get("after_index") or 0)

    async def complete(self,row,poll):
        # Browser/driver has already exited before downloading.
        models=poll.get("videoModels") or []
        url=extract_unwatermarked_url(models[0] if models else "",poll["videos"][0])
        self.store.update(row["id"],phase="downloading",result_url=url)
        await self.download(self.store.get(row["id"]))

    async def download(self,row):
        local=await _download(row["result_url"],row["account"])
        from video_worker import sanitize_filename_prefix
        prefix = sanitize_filename_prefix(row.get("name") or "")[:100]
        target=Path(local).with_name(prefix+row["id"]+Path(local).suffix)
        Path(local).replace(target)
        self.store.update(row["id"],status="completed",phase="completed",error=None,
                          video_url=f"{config.PUBLIC_BASE}/videos/{target.name}",finished_at=time.time(),next_check_at=None)
        self.release(row)

    async def run(self,task_id,uploaded):
        row=self.store.get(task_id)
        if not row or row.get("deleted_at") is not None or row["status"] in ("completed","stopped","failed","needs_recovery"):
            return
        while True:
            row=self.store.get(task_id)
            if not row or row.get("deleted_at") is not None:
                return
            if row["status"] in ("completed","stopped","failed","needs_recovery"): return
            if not await self.account(row):
                await asyncio.sleep(1)
                continue
            row=self.store.get(task_id)
            phase=row.get("phase") or "ready"
            if not row.get("started_at"):
                self.store.update(task_id, started_at=time.time())
            if phase=="dispatching":
                if not row.get("conversation_id"):
                    self.review(task_id,"Uncertain submission after interruption; inspect account history before resubmitting")
                    return
                self.store.update(task_id,phase="checking")
                phase="checking"
            if phase=="waiting":
                delay=(row.get("next_check_at") or time.time())-time.time()
                if delay>0:
                    await asyncio.sleep(min(delay,30))
                    continue
                self.store.update(task_id,phase="checking",status="queued")
                phase="checking"
            try:
                if phase=="downloading":
                    await self.download(row)
                    return
                if phase in ("ready","queued",""):
                    paths=await self.references(row,uploaded)
                    self.store.update(task_id,status="processing")
                    outcome=await self.submit(row,paths)
                else:
                    if not row.get("conversation_id"):
                        self.review(task_id,"No saved conversation; inspect account history")
                        return
                    async with self.pool.account_activity(row["account"],"checking"):
                        outcome=await inspect_conversation(row)
                    self.store.update(task_id,last_poll_at=time.time())
                row=self.store.get(task_id)
                if row["status"] == "needs_recovery":
                    return
                kind,poll=outcome["kind"],outcome["poll"]
                if kind=="completed":
                    await self.complete(row,poll)
                    return
                if kind=="rejected":
                    if row.get("dispatch_uncertain") and (poll.get("latestIndex") or 0)<=(row.get("after_index") or 0):
                        self.review(task_id,"Cannot identify the interrupted submission; inspect conversation")
                        return
                    self.store.update(task_id,after_index=poll.get("latestIndex") or 0,dispatch_uncertain=0,check_round=0,accepted_at=None)
                    if not self.retry(self.store.get(task_id),poll.get("rejection")): return
                    # Exit the browser session first; next attempt joins FIFO at its tail.
                    continue
                if kind=="accepted" or (not row.get("accepted_at") and poll.get("accepted")):
                    eta,raw=parse_eta(poll.get("texts") or [])
                    accepted_at=row.get("accepted_at") or time.time()
                    self.store.update(task_id,phase="waiting",status="processing",accepted_at=accepted_at,
                                      eta_seconds=eta,eta_raw=raw,next_check_at=accepted_at+max(0, eta-EARLY_CHECK_SECONDS),check_round=0,dispatch_uncertain=0,error=None)
                    continue
                if not row.get("accepted_at"):
                    self.review(task_id,"Submission status is uncertain; no complete acceptance or rejection observed")
                    return
                if self.restore_eta_wait(row):
                    continue
                rounds=row.get("check_round") or 0
                if rounds>=MAX_RECHECKS:
                    self.review(task_id,"No video after first check and 3 rechecks (5 minutes apart)")
                    return
                self.store.update(task_id,phase="waiting",status="processing",check_round=rounds+1,next_check_at=time.time()+CHECK_INTERVAL)
            except (worker.CreditInsufficientError, worker.AccountLimitedError, worker.CreditError) as exc:
                row=self.store.get(task_id)
                if isinstance(exc,worker.AccountLimitedError):
                    self.pool._mark_daily_limit(row["account"],str(exc))
                else:
                    self.pool._mark_quota_blocked(row["account"],str(exc))
                self.release(row)
                self.store.update(task_id,account=None,account_uuid=None,conversation_id=None,
                                  after_index=0,accepted_at=None,dispatch_uncertain=0)
                if not self.retry(self.store.get(task_id),exc): return
            except asyncio.CancelledError:
                raise
            except Exception as exc:
                row=self.store.get(task_id)
                if row.get("accepted_at") and row.get("phase")=="waiting":
                    # A cleanup error after acceptance is not a failed generation.
                    self.store.update(task_id,error="Browser cleanup after acceptance: "+str(exc))
                    continue
                if row.get("phase")=="downloading":
                    self.review(task_id,"Video generated; download failed: "+str(exc))
                    return
                if row.get("dispatch_uncertain"):
                    if row.get("conversation_id"):
                        self.store.update(task_id,phase="checking",next_check_at=None)
                        await asyncio.sleep(1)
                    else:
                        self.review(task_id,"Uncertain dispatch: "+str(exc))
                        return
                elif phase=="checking":
                    if self.restore_eta_wait(row):
                        self.store.update(task_id,error=str(exc))
                        continue
                    rounds=row.get("check_round") or 0
                    if rounds>=MAX_RECHECKS:
                        self.review(task_id,"Unable to inspect after 3 rechecks: "+str(exc))
                        return
                    self.store.update(task_id,phase="waiting",check_round=rounds+1,next_check_at=time.time()+CHECK_INTERVAL,error=str(exc))
                elif not self.retry(row,exc):
                    return
