"""Browser regression checks with entirely mocked APIs; never delete live data."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit
from patchright.async_api import async_playwright


async def main():
    identifiers = ["00000000-0000-4000-8000-00000000000"+str(i) for i in range(1, 4)]
    accounts = [dict(name=identifier, uuid=identifier, email="test"+str(i)+"@example.com", account_type="google", busy=i==2, used_today=1, limit=2, scheduling=False) for i, identifier in enumerate(identifiers)]
    tasks = [dict(id="video_"+str(i), account=identifiers[0], status=status, prompt="Fixture", duration=10) for i,status in enumerate(["completed","failed","processing"])]
    deletes=[]
    errors=[]
    async with async_playwright() as playwright:
        browser=await playwright.chromium.launch(channel="chrome", headless=True)
        page=await browser.new_page()
        async def run_script(source):
            await page.add_script_tag(content="document.body.dataset.testDone=''; (async()=>{try{"+source+";document.body.dataset.testDone='ok';}catch(e){document.body.dataset.testDone=String(e);}})()")
            await page.wait_for_function("document.body.dataset.testDone")
            assert await page.locator("body").get_attribute("data-test-done")=="ok"

        page.on("pageerror", lambda error: errors.append(str(error)))
        page.on("dialog", lambda dialog: dialog.accept())
        async def route(request_route):
            request=request_route.request
            path=urlsplit(request.url).path
            if path=="/":
                return await request_route.fulfill(content_type="text/html",body=Path("web/index.html").read_text(encoding="utf-8"))
            if request.method=="DELETE":
                deletes.append(path)
                identifier=path.rsplit("/",1)[1]
                if identifier==identifiers[1]:
                    return await request_route.fulfill(status=409,json={"detail":"Account is busy"})
                accounts[:]=[a for a in accounts if a["name"]!=identifier]
                tasks[:]=[t for t in tasks if t["id"]!=identifier]
                return await request_route.fulfill(json={"ok":True})
            if path=="/api/admin/accounts": return await request_route.fulfill(json={"accounts":accounts})
            if path=="/api/admin/jobs": return await request_route.fulfill(json={"jobs":{}})
            if path=="/api/admin/tasks": return await request_route.fulfill(json={"tasks":tasks})
            if path=="/api/admin/stats": return await request_route.fulfill(json={"per_day":[],"per_account":accounts,"today_completed":0,"today_failed":0,"total_accounts":3,"available_accounts":2,"total_remaining":2})
            return await request_route.fulfill(json={})
        await page.route("**/*",route)
        await page.goto("http://dashboard.test/")
        await page.wait_for_selector("#loginMask",state="hidden")
        await run_script("clearInterval(timer); switchTab('accounts'); switchAccountGroup('other')")
        await page.wait_for_selector('#accountsTable tbody tr')
        accounts.append(dict(name='gmail-fixture', email=' Scene@GMAIL.COM ', busy=False, ready_to_generate=True, used_today=0, limit=2))
        await run_script('await loadAccounts()')
        assert '1 tài khoản · 1 sẵn sàng gen' in await page.locator('#accountGroupGmail').inner_text()
        assert '3 tài khoản · 0 sẵn sàng gen' in await page.locator('#accountGroupOther').inner_text()
        await page.locator('#selectAllaccounts').check()
        await page.locator('#accountGroupGmail').click()
        assert await page.locator('#accountsTable tbody tr').count()==1
        assert not await page.locator('#deleteSelectedAccounts').is_visible()
        await run_script('await loadAccounts()')
        assert await page.locator('#accountGroupGmail').get_attribute('aria-pressed')=='true'
        await page.locator('#accountGroupOther').click()
        accounts.pop()
        accounts.append(dict(name='gmail-fixture', email=' Scene@GMAIL.COM ', busy=False, ready_to_generate=True, used_today=0, limit=2))
        await run_script('await loadAccounts()')
        assert '1 tài khoản · 1 sẵn sàng gen' in await page.locator('#accountGroupGmail').inner_text()
        assert '3 tài khoản · 0 sẵn sàng gen' in await page.locator('#accountGroupOther').inner_text()
        await page.locator('#selectAllaccounts').check()
        await page.locator('#accountGroupGmail').click()
        assert await page.locator('#accountsTable tbody tr').count()==1
        assert not await page.locator('#deleteSelectedAccounts').is_visible()
        await run_script('await loadAccounts()')
        assert await page.locator('#accountGroupGmail').get_attribute('aria-pressed')=='true'
        await page.locator('#accountGroupOther').click()
        accounts.pop()
        assert await page.locator('#accountsTable tbody tr').count()==3
        assert await page.locator('#accountsTable tbody tr:first-child td:nth-child(2)').inner_text()=="1"
        assert await page.locator('#accountsTable tbody tr:first-child button').all_text_contents()==['Open Web','Verify','Reset','Retry','Delete']
        assert not await page.locator('#deleteSelectedAccounts').is_visible()
        assert await page.locator('#accountsTable th').first.evaluate('(el)=>getComputedStyle(el).textAlign')=='left'
        await page.locator('#selectAllaccounts').check()
        assert await page.locator('#deleteSelectedAccounts').is_visible()
        await page.locator('#selectAllaccounts').uncheck()
        assert not await page.locator('#deleteSelectedAccounts').is_visible()
        await page.locator('#selectAllaccounts').check()
        assert await page.locator('[data-delete-kind="accounts"]:checked').count()==2
        assert await page.locator('[data-delete-kind="accounts"]:disabled').count()==1
        await run_script('await loadAccounts()')
        assert await page.locator('[data-delete-kind="accounts"]:checked').count()==2
        await run_script("await deleteSelectedRows('accounts')")
        assert len(deletes)==2
        assert await page.locator('#accountsTable tbody tr').count()==2
        assert await page.locator('[data-delete-kind="accounts"]:checked').count()==1
        await run_script('openAddAccount()')
        assert await page.locator('#f_name,#f_display_name').count()==0
        assert "UUID" not in await page.locator('#dlgBox').inner_text()
        await run_script("hide('dlg'); switchTab('tasks')")
        await page.wait_for_selector('#tasksTable tbody tr')
        assert await page.locator('#tasksTable tbody tr').count()==3
        assert not await page.locator('#deleteSelectedTasks').is_visible()
        await page.locator('#selectAlltasks').check()
        assert await page.locator('#deleteSelectedTasks').is_visible()
        await page.locator('#selectAlltasks').uncheck()
        assert not await page.locator('#deleteSelectedTasks').is_visible()
        await page.locator('#selectAlltasks').check()
        assert await page.locator('[data-delete-kind="tasks"]:checked').count()==2
        await run_script('await loadTasks()')
        assert await page.locator('[data-delete-kind="tasks"]:checked').count()==2
        await run_script("await deleteSelectedRows('tasks')")
        assert len(deletes)==4
        assert not await page.locator('#deleteSelectedTasks').is_visible()
        assert await page.locator('#tasksTable tbody tr').count()==1
        assert tasks[0]['status']=='processing'
        tasks[:]=[dict(id='batch-video-'+str(i),batch_id=batch,batch_index=idx,batch_count=2,name='Scene (1/2)',status='completed',created_at=stamp,prompt='Fixture') for i,(batch,idx,stamp) in enumerate([('batch-a',2,100),('batch-b',2,200),('batch-a',1,100),('batch-b',1,200)])]
        await run_script('await loadTasks()')
        assert await page.locator('tbody.batch-group').count()==2
        assert await page.locator('tbody.batch-group').first.get_attribute('data-batch-id')=='batch-b'
        assert await page.locator('tbody.batch-group').first.locator('[data-delete-id]').first.get_attribute('data-delete-id')=='batch-video-3'
        assert 'Task ID' not in await page.locator('#tasksTable thead').inner_text()
        assert 'Client' not in await page.locator('#tasksTable thead').inner_text()
        assert '2/2 video' in await page.locator('.batch-toggle').first.inner_text()
        await page.locator('.batch-toggle').first.click()
        assert not await page.locator('tbody.batch-group').first.locator('.task-row').first.is_visible()
        await run_script('await loadTasks()')
        assert await page.locator('.batch-toggle').first.get_attribute('aria-expanded')=='false'
        tasks[:]=tasks[:1]
        await run_script('await loadTasks()')
        assert '1/2 video' in await page.locator('.batch-toggle').inner_text()
        assert not errors, errors
        print('Browser checks passed: STT, actions preserved, no name form, selection survives refresh, bulk delete skips active rows, partial failures remain selected')
        await browser.close()


if __name__=="__main__":
    asyncio.run(main())
