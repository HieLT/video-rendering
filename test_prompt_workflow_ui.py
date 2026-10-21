"""Exercise the real dashboard and workflow APIs offline in Chromium."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit

from patchright.async_api import async_playwright
from prompt_workflow import register_prompt_routes
from test_asset_library import LibraryApiTests


async def main():
    fixture = LibraryApiTests()
    fixture.setUp()
    register_prompt_routes(fixture.ns["app"], fixture.store, lambda key: None)
    errors = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page(viewport={"width": 1360, "height": 1000})
            page.on("pageerror", lambda e: errors.append(str(e)))
            await page.add_init_script("localStorage.setItem('dola_admin_key','admin-test')")
            async def route(r):
                path = urlsplit(r.request.url).path
                static = {"/": ("web/index.html", "text/html"),
                          "/prompt-workflow.js": ("web/prompt-workflow.js", "application/javascript"),
                          "/prompt-workflow.css": ("web/prompt-workflow.css", "text/css")}
                if path in static:
                    file, mime = static[path]
                    return await r.fulfill(content_type=mime, body=Path(file).read_text(encoding="utf-8"))
                mocks = {
                    "/api/admin/accounts": {"accounts": []}, "/api/admin/jobs": {"jobs": {}},
                    "/api/admin/tasks": {"tasks": []}, "/api/admin/keys": {"keys": []},
                    "/api/admin/accounts/google-bulk/progress": {"imports": {}},
                    "/api/admin/stats": {"per_day": [], "per_account": []},
                }
                if path in mocks:
                    return await r.fulfill(json=mocks[path])
                response = fixture.http.request(r.request.method, path, content=r.request.post_data,
                                                headers={"Content-Type": "application/json"})
                await r.fulfill(status=response.status_code, body=response.content,
                                content_type=response.headers.get("content-type", "application/json"))
            await page.route("**/*", route)
            await page.goto("http://workflow.test/")
            await page.get_by_role("button", name="Projects", exact=True).click()
            await page.locator('#projectList button[data-action="open"]').first.click()
            await page.get_by_role("button", name="Prompt Workshop", exact=True).click()
            await page.locator('[data-pw="tab"][data-stage="t2"]').click()
            await page.locator('[data-pw-field="idea"]').fill("A warrior returns home")
            await page.locator('[data-pw="generate"]').click()
            await page.wait_for_function("document.querySelector('#pwNotice').textContent.includes('Prompt đã tạo')")
            assert "A warrior returns home" in await page.locator("#pwPrompt").input_value()
            await page.locator('[data-pw-field="story"]').fill("Story complete")
            await page.locator('[data-pw-field="outline"]').fill("CHAPTER 1 ? Return\nCHAPTER 2 ? Finale")
            await page.locator('[data-pw="approve"]').click()
            await page.wait_for_function("document.querySelector('#pwStatus').textContent==='Đã duyệt'")
            await page.locator('[data-pw="tab"][data-stage="t3"]').click()
            for n in (1, 2):
                if n == 2:
                    await page.locator("#pwGroup").fill("2")
                    await page.locator('[data-pw="open-group"]').click()
                await page.locator('[data-pw="generate"]').click()
                await page.wait_for_function("document.querySelector('#pwNotice').textContent.includes('Prompt đã tạo')")
                await page.locator('[data-pw-field="output"]').fill(f"SCENE {n} — Scene\nAction {n}")
                await page.locator('[data-pw="approve"]').click()
                await page.wait_for_function("document.querySelector('#pwStatus').textContent==='Đã duyệt'")
            await page.locator('[data-pw="tab"][data-stage="t4"]').click()
            await page.locator('[data-pw="generate"]').click()
            await page.wait_for_function("document.querySelector('#pwNotice').textContent.includes('Prompt đã tạo')")
            await page.locator('[data-pw-field="output"]').fill("Reference plan")
            await page.locator('[data-pw-field="reference_plan"]').fill("Hero and Castle")
            await page.locator('[data-pw-field="reference_map"]').fill("SCENE 1 — Intro\nHero\nSCENE 2 — End\nCastle")
            await page.locator('[data-pw="approve"]').click()
            await page.wait_for_function("document.querySelector('#pwStatus').textContent==='Đã duyệt'")
            await page.locator('[data-pw="tab"][data-stage="t5"]').click()
            await page.locator('[data-pw="generate"]').click()
            await page.wait_for_function("document.querySelector('#pwNotice').textContent.includes('Prompt đã tạo')")
            prompt = await page.locator("#pwPrompt").input_value()
            assert "Action 1" in prompt and "Action 2" not in prompt
            assert "Castle" not in prompt
            output = json.dumps({"scenes": [{"scene_number": 1, "prompt": "Hero walks", "references": []}]})
            await page.locator('[data-pw-field="output"]').fill(output)
            # Back saves the dirty draft; reopen proves storage, not just DOM state.
            await page.get_by_role("button", name="Back to project", exact=True).click()
            await page.get_by_role("button", name="Prompt Workshop", exact=True).click()
            await page.locator('[data-pw="tab"][data-stage="t5"]').click()
            assert await page.locator('[data-pw-field="output"]').input_value() == output
            Path(".runtime").mkdir(exist_ok=True)
            await page.screenshot(path=".runtime/prompt-workflow-ui.png", full_page=True)
            assert not errors, errors
            await browser.close()
    finally:
        fixture.tearDown()
    print("PASS: workflow UI, real template assembly, chapter filtering, draft persistence, no browser errors")


if __name__ == "__main__":
    asyncio.run(main())
