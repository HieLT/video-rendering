"""Offline batch tests: isolated SQLite, mocked workers and browser APIs."""
import ast
import asyncio
import json
import logging
import shutil
import sqlite3
import tempfile
import time
import traceback
import uuid
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock

from fastapi import FastAPI, Header, HTTPException
from pydantic import BaseModel, Field, ValidationError
from browser_queue import async_playwright
from reference_aliases import resolve_reference_aliases
from scene_generation import task_reference_paths
from project_generation import submit_project_generation
from store import TaskStore, TaskQuotaExceeded, PendingTaskLimitExceeded, TaskSubmissionConflict


def isolated_api():
    # Load only request/worker code; importing server would open production DBs.
    tree = ast.parse(Path("server.py").read_text(encoding="utf-8"))
    names = {"VideoGenRequest", "TaskResponse", "BatchTaskResponse", "create_video", "_submit_video", "_run_task", "_resolve_ratio"}
    selected = [node for node in tree.body if isinstance(node, (ast.ClassDef, ast.FunctionDef, ast.AsyncFunctionDef)) and node.name in names]
    class LimitedError(Exception): pass
    class CreditsError(Exception): pass
    class RejectedError(Exception): pass
    client = dict(api_key_hash="client", api_key_name="Test", allowed_durations=[10,15,30], daily_limit=0, concurrency_limit=1)
    ns = dict(globals(), app=FastAPI(), store=TaskStore(":memory:"),
              pool=SimpleNamespace(available=True, accounts=["test"], generate_video=AsyncMock()),
              config=SimpleNamespace(REFERENCE_IMAGE_MAX_COUNT=30, MAX_PENDING_TASKS=100, PUBLIC_BASE="http://test"),
              _auth=lambda _: client, SUPPORTED_DURATIONS=(10,15,30), SIZE_TO_RATIO={},
              UPLOADED_REFERENCES={}, TASK_RUNNERS={},
              key_limiter=SimpleNamespace(acquire=AsyncMock(), release=AsyncMock()),
              NoUsableAccountsError=type("NoUsableAccountsError",(RuntimeError,),{}), AllAccountsLimitedError=LimitedError, AllAccountsQuotaBlockedError=CreditsError,
              GenerationRejectedError=RejectedError, validate_reference_urls=AsyncMock(side_effect=lambda x:x))
    exec(compile(ast.Module(body=selected, type_ignores=[]), "server.py", "exec"), ns)
    return ns, client


async def backend_checks():
    ns, client = isolated_api()
    request = ns["VideoGenRequest"]
    for count in [0,6,1.5,True]:
        try: request(prompt="p", count=count)
        except ValidationError: pass
        else: raise AssertionError(count)
    captured=[]
    async def capture(*args): captured.append(args)
    runner=ns["_run_task"]
    ns["_run_task"]=capture
    with tempfile.TemporaryDirectory() as folder:
        ns["Path"] = lambda value: Path(value) if Path(value).is_absolute() else Path(folder) / value
        root=Path(folder)/"source";root.mkdir()
        files=[root/"first.png",root/"second.png"]
        for i,path in enumerate(files): path.write_bytes(bytes([i]))
        token="uploaded://test"
        ns["UPLOADED_REFERENCES"][token]=(root,[str(p) for p in files])
        client["daily_limit"]=4
        req=request(prompt="@hero moves", count=5, reference_images=[token], reference_aliases=["hero","scene"])
        try: await ns["create_video"](req,None)
        except HTTPException as e: assert e.status_code==429
        else: raise AssertionError("Quota should reject entire batch")
        assert ns["store"].pending_task_count()==0
        assert list(ns["UPLOADED_REFERENCES"])==[token] and root.exists()
        client["daily_limit"]=0
        ns["config"].MAX_PENDING_TASKS=4
        try: await ns["create_video"](req,None)
        except HTTPException as e: assert e.status_code==429
        else: raise AssertionError("Queue should reject entire batch")
        assert ns["store"].pending_task_count()==0
        ns["config"].MAX_PENDING_TASKS=100
        result=await ns["create_video"](req,None)
        await asyncio.sleep(0)
        assert len(result.tasks)==5 and len(captured)==5
        assert len({t.id for t in result.tasks})==5
        assert not ns["UPLOADED_REFERENCES"]
        assert not root.exists()
        all_paths=[]
        for index,t in enumerate(result.tasks,1):
            row=ns["store"].get(t.id)
            assert (row["batch_id"],row["batch_index"],row["batch_count"])==(result.batch_id,index,5)
            assert row["prompt"]=="@Image1 moves"
            references=json.loads(row["reference_images"])
            assert len(references)==2 and all(ref.startswith("local://") for ref in references)
            paths=[Path(ref[8:]) for ref in references]
            assert [path.read_bytes() for path in paths]==[bytes([0]),bytes([1])]
            assert all(path.is_relative_to(Path(folder)) for path in paths)
            all_paths.extend(paths)
        assert len(set(all_paths))==10
        async def scheduled_run(task_id,uploads):
            assert uploads=={}
            row=ns["store"].get(task_id)
            paths=[Path(ref[8:]) for ref in json.loads(row["reference_images"])]
            assert [path.read_bytes() for path in paths]==[bytes([0]),bytes([1])]
            if task_id==result.tasks[0].id:
                raise RuntimeError("fixture scheduler failure")
            ns["store"].update(task_id,status="completed")
        def review(task_id,error):
            ns["store"].update(task_id,status="needs_recovery",error=error)
        ns["scheduler"]=SimpleNamespace(run=AsyncMock(side_effect=scheduled_run),review=review)
        for args in captured: await runner(*args)
        assert not ns["UPLOADED_REFERENCES"]
        assert ns["store"].get(result.tasks[0].id)["status"]=="needs_recovery"
        assert ns["scheduler"].run.await_count==5
        assert ns["key_limiter"].release.await_count==5
        assert not ns["TASK_RUNNERS"]
        assert all(ns["store"].get(t.id)["status"]=="completed" for t in result.tasks[1:])
    result=await ns["create_video"](request(prompt="single"),None)
    assert isinstance(result,ns["TaskResponse"])
    await asyncio.sleep(0)
    # SQL errors must roll back earlier inserts in the same group.
    db=ns["store"]
    try: db.create_batch(["new",result.id],"model","prompt","16:9",10)
    except Exception: pass
    else: raise AssertionError("Duplicate ID should fail")
    assert db.get("new") is None
    db._conn.close()
    print("PASS backend: count validation, atomic quota/queue/SQL rollback, batch metadata, independent images/failure, single response")


async def browser_checks():
    requests=[];uploads=[];errors=[];reject=False
    async with async_playwright() as p:
        browser=await p.chromium.launch(channel="chrome",headless=True)
        page=await browser.new_page()
        page.on("pageerror",lambda e:errors.append(str(e)))
        page.on("dialog",lambda d:d.accept())
        async def route(r):
            nonlocal reject
            path=r.request.url.split("http://batch.test")[-1]
            if path=="/": return await r.fulfill(content_type="text/html",body=Path("web/index.html").read_text(encoding="utf-8"))
            if path=="/api/admin/reference-images":
                uploads.append(1)
                return await r.fulfill(json={"reference_images":["uploaded://fixture"]})
            if path=="/v1/videos/generations":
                body=r.request.post_data_json;requests.append(body)
                if reject: return await r.fulfill(status=429,json={"detail":"Not enough quota"})
                return await r.fulfill(json={"batch_id":"batch_test","tasks":[{"id":str(i)} for i in range(body["count"])]})
            if path.startswith("/api/admin/tasks"): return await r.fulfill(json={"tasks":[]})
            if path=="/api/admin/accounts": return await r.fulfill(json={"accounts":[]})
            return await r.fulfill(json={"per_day":[],"per_account":[],"jobs":{}})
        await page.route("**/*",route)
        await page.goto("http://batch.test/")
        await page.add_script_tag(content="clearInterval(timer);switchTab('tasks');")
        assert await page.locator("#videoCount").input_value()=="1"
        await page.select_option("#videoCount","3")
        assert "Generate 3 videos" in await page.locator("#generateBtn").inner_text()
        await page.fill("#videoPrompt","same prompt")
        await page.fill("#videoName", "scene 2")
        assert await page.locator('#try30Btn').count() == 0
        await page.locator("#referenceImages").set_input_files({"name":"test.png","mimeType":"image/png","buffer":b"fixture"})
        await page.click("#generateBtn")
        await page.wait_for_function("!document.querySelector('#generateBtn').disabled")
        assert len(uploads)==1 and len(requests)==1 and requests[0]["count"]==3
        assert requests[0]["reference_images"]==["uploaded://fixture"]
        assert requests[0]['name'] == 'scene 2'
        assert await page.locator('#videoName').input_value() == ''
        await page.click('[data-mode="start_end"]')
        await page.fill('#videoName', 'scene opening')
        await page.fill('#videoPrompt', 'Opening to ending')
        for selector, name in [('#startImage', 'start.png'), ('#endImage', 'end.png')]:
            await page.locator(selector).set_input_files({'name':name,'mimeType':'image/png','buffer':b'fixture'})
        await page.click('#generateBtn')
        await page.wait_for_function("!document.querySelector('#generateBtn').disabled")
        assert requests[-1]['start_end'] is True and requests[-1]['name'] == 'scene opening'
        await page.click('[data-mode="reference"]')
        await page.locator('#referenceImages').set_input_files([
            {'name':'hero.png','mimeType':'image/png','buffer':b'fixture'},
            {'name':'door.png','mimeType':'image/png','buffer':b'fixture'}])
        aliases = page.locator('#imagePreview .ref-name input')
        await aliases.nth(0).fill('Hero')
        await aliases.nth(0).press('Tab')
        await aliases.nth(1).fill('Door')
        await aliases.nth(1).press('Tab')
        await page.fill('#videoPrompt', '@He')
        await page.wait_for_function("!document.querySelector('#refSuggestions').hidden && document.querySelector('#refSuggestions').innerText.includes('Hero')")
        assert 'Hero' in await page.locator('#refSuggestions').inner_text()
        await page.fill('#videoPrompt', '@Hero moves toward @Door')
        await page.add_script_tag(content='reorderReferenceImages(0,1)')
        assert [await aliases.nth(i).input_value() for i in range(2)] == ['Door','Hero']
        await page.fill('#videoName', 'scene references')
        await page.click('#generateBtn')
        await page.wait_for_function("!document.querySelector('#generateBtn').disabled")
        assert requests[-1]['reference_aliases'] == ['Door','Hero']
        assert requests[-1]['prompt'] == '@Hero moves toward @Door'
        assert requests[-1]['name'] == 'scene references'
        reject=True
        await page.fill("#videoPrompt","keep on error")
        await page.fill('#videoName', 'keep name on error')
        await page.select_option("#videoCount","5")
        await page.click("#generateBtn")
        await page.wait_for_function("!document.querySelector('#generateBtn').disabled")
        assert await page.locator("#videoPrompt").input_value()=="keep on error"
        assert await page.locator('#videoName').input_value() == 'keep name on error'
        assert "Generate 5 videos" in await page.locator("#generateBtn").inner_text()
        assert not errors, errors
        await browser.close()
    print("PASS browser: default count, button label, one upload/request, error preserves input")


async def main():
    await backend_checks()
    await browser_checks()

if __name__=="__main__": asyncio.run(main())
