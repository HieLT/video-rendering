"""Server action and cross-process queue smoke tests, isolated from live data."""
import asyncio
import importlib
import json
import os
import sys
import tempfile
from pathlib import Path
from unittest.mock import AsyncMock, patch
from browser_queue import BrowserQueue
import config

async def process_queue():
    with tempfile.TemporaryDirectory() as root:
        code = """import asyncio,sys
from browser_queue import BrowserQueue
async def main():
 async with BrowserQueue(sys.argv[1],1).slot():
  print('ACQUIRED',flush=True)
  await asyncio.to_thread(sys.stdin.readline)
asyncio.run(main())
"""
        first=await asyncio.create_subprocess_exec(sys.executable,"-B","-c",code,root,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
        second=None
        try:
            assert await asyncio.wait_for(first.stdout.readline(),10)==b"ACQUIRED\r\n" or False
            second=await asyncio.create_subprocess_exec(sys.executable,"-B","-c",code,root,stdin=asyncio.subprocess.PIPE,stdout=asyncio.subprocess.PIPE,stderr=asyncio.subprocess.PIPE)
            await asyncio.sleep(.7)
            db=BrowserQueue(root,1).connect()
            assert db.execute("select count(*) from tickets where active=1").fetchone()[0]==1
            assert db.execute("select count(*) from tickets where active=0").fetchone()[0]==1
            db.close()
            first.kill();await first.wait()
            assert (await asyncio.wait_for(second.stdout.readline(),10)).strip()==b"ACQUIRED"
            second.stdin.write(b"\n");await second.stdin.drain()
            await asyncio.wait_for(second.wait(),10)
            print("PASS process-shared ceiling and recovery after owner crash")
        finally:
            for p in (first,second):
                if p and p.returncode is None:p.kill();await p.wait()

async def server_actions():
    previous=os.getcwd()
    with tempfile.TemporaryDirectory() as root:
        try:
            os.chdir(root)
            config.DB_PATH=str(Path(root)/"tasks.db")
            config.DOWNLOAD_DIR=str(Path(root)/"downloads")
            config.ADMIN_KEY=""
            server=importlib.import_module("server")
            Path("accounts/fixture").mkdir(parents=True)
            server.pool._ensure_meta("fixture")
            with server.pool._conn:
                server.pool._conn.execute("update accounts_meta set login_ok=1,auth_state='active',scheduling=1 where name='fixture'")
            upload=Path(root)/"upload";upload.mkdir()
            image=upload/"source.png";image.write_bytes(b"fixture")
            server.UPLOADED_REFERENCES["uploaded://fixture"]=(upload,[str(image)])
            client={"api_key_hash":None,"api_key_name":"fixture","daily_limit":0,"concurrency_limit":0,"allowed_durations":[10,15,30]}
            runner=AsyncMock()
            with patch.object(server,"_auth",return_value=client),patch.object(server,"_run_task",runner):
                result=await server.create_video(server.VideoGenRequest(prompt="original",duration=30,reference_images=["uploaded://fixture"]),None)
                await asyncio.sleep(0)
                row=server.store.get(result.id)
                refs=json.loads(row["reference_images"])
                assert len(refs)==1 and refs[0].startswith("local://")
                assert Path(refs[0][8:]).is_file()
                assert not upload.exists()
                server.store.update(result.id,status="needs_recovery",phase="review",account="fixture",conversation_id="123",retry_count=4,check_round=3,accepted_at=100)
                server.scheduler.reserved["fixture"]=result.id
                outcome=await server.admin_task_action(result.id,"check",None)
                assert outcome["status"]=="queued"
                await asyncio.sleep(0)
                row=server.store.get(result.id)
                assert row["retry_count"]==4 and row["check_round"]==0 and row["phase"]=="checking"
                await server.admin_task_action(result.id,"stop",None)
                assert server.store.get(result.id)["status"]=="stopped"
                assert "fixture" not in server.scheduler.reserved
                print("PASS persistent uploads, manual queue, retained retry budget and stop/release")
            # Verify quota accounting is idempotent per account/job.
            server.store.update(result.id,account="fixture")
            row=server.store.get(result.id)
            server.pool.used_today("fixture")
            before=server.pool.used_today("fixture")
            server.scheduler.claim_once(row);server.scheduler.claim_once(row)
            assert server.pool.used_today("fixture")==before+1
            print("PASS quota claim is idempotent")
            server.store._conn.close();server.pool._conn.close()
        finally:os.chdir(previous)

async def main():
    await process_queue()
    await server_actions()
asyncio.run(main())
