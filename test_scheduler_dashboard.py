"""Offline dashboard smoke test; does not contact the live server."""
import asyncio
from pathlib import Path
from browser_queue import async_playwright
async def main():
    errors=[]
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True)
        try:
            page=await browser.new_page()
            page.on("pageerror",lambda error:errors.append(str(error)))
            async def route(r):
                if r.request.url=="http://fixture/":
                    return await r.fulfill(body=Path("web/index.html").read_text(encoding="utf-8"),content_type="text/html")
                return await r.fulfill(body='{}',content_type="application/json")
            await page.route("**/*",route)
            await page.goto("http://fixture/")
            await page.add_script_tag(content="document.body.dataset.testControls=taskControls({id:'test',account:'fixture',conversation_id:'123',status:'needs_recovery'});")
            controls=await page.locator("body").get_attribute("data-test-controls")
            assert 'check' in controls and 'open' in controls and 'stop' in controls
            assert 'deleteTask' not in controls
            label=await page.locator('#taskStatus option[value="needs_recovery"]').inner_text()
            assert label=="Needs recovery",repr(label)
            assert not errors,errors
            print("PASS dashboard JavaScript, review filter and queued actions")
        finally:await browser.close()
asyncio.run(main())
