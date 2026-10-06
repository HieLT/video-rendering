"""Browser acceptance on isolated test_riven data; never accesses live server."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit
from patchright.async_api import async_playwright
from test_scene_info import SceneInfoTests

async def main():
    fixture=SceneInfoTests();fixture.setUp();errors=[];browser=None
    try:
        fixture.store.update_project(fixture.project_id,name='test_riven')
        first=fixture.create_scene(1,'forest',scene_name='Cabin Test')
        second=fixture.create_scene(2,'forest',scene_name='Burning Settlement Test')
        payload={'scenes':[
            {'scene_number':1,'summary':'Nhân vật bước vào căn nhà giữa khu rừng.'},
            {'scene_number':2,'summary':'Ngôi làng đang cháy; nhân vật tìm đường thoát.'},
        ]}
        before={table:[tuple(row) for row in fixture.store._conn.execute('SELECT * FROM '+table)] for table in ['tasks','scene_assets','scene_reference_requirements']}
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            context=await browser.new_context(viewport={'width':1365,'height':1000})
            await context.add_init_script("localStorage.setItem('dola_admin_key','admin-test')")
            page=await context.new_page();page.on('pageerror',lambda error:errors.append(str(error)))
            async def route(r):
                path=urlsplit(r.request.url).path
                if path=='/':return await r.fulfill(content_type='text/html',body=Path('web/index.html').read_text(encoding='utf-8'))
                if path=='/api/admin/accounts':return await r.fulfill(json={'accounts':[]})
                if path=='/api/admin/stats':return await r.fulfill(json=dict(per_day=[],per_account=[],today_completed=0,today_failed=0,total_accounts=0,available_accounts=0,total_remaining=0))
                if path=='/api/admin/tasks':return await r.fulfill(json={'tasks':[]})
                if path=='/api/admin/jobs':return await r.fulfill(json={'jobs':{}})
                if path=='/api/admin/keys':return await r.fulfill(json={'keys':[]})
                response=fixture.http.request(r.request.method,path,content=r.request.post_data,headers=dict(r.request.headers))
                await r.fulfill(status=response.status_code,content_type=response.headers.get('content-type','application/json'),body=response.content)
            await page.route('**/*',route);await page.goto('http://scene-info.test/')
            await page.wait_for_selector('#loginMask',state='hidden')
            await page.add_script_tag(content='clearInterval(timer)')
            await page.get_by_role('button',name='Projects',exact=True).click()
            await page.locator('#projectList').get_by_role('button',name='test_riven',exact=True).click()
            card=page.locator(f'.project-scene[data-scene-id="{first["id"]}"]')
            await card.get_by_text('No scene info imported.',exact=True).wait_for()
            await page.get_by_role('button',name='Import Scene Info',exact=True).click()
            await page.locator('#projectInfoJSON').fill(json.dumps({'scenes':[{'scene_number':41,'summary':'unknown'}]}))
            await page.get_by_role('button',name='Validate',exact=True).click()
            await page.get_by_text('Scene 41 not found in this Project.',exact=True).wait_for()
            assert await page.locator('#projectInfoSubmit').is_disabled()
            await page.locator('#projectInfoJSON').fill(json.dumps(payload,ensure_ascii=False))
            await page.get_by_role('button',name='Validate',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectInfoSubmit').disabled===false")
            assert fixture.store.get_scene(first['id'])['scene_summary'] is None
            await page.locator('#projectInfoSubmit').click()
            await card.locator('.project-scene-summary').wait_for()
            for scene,row in [(first,payload['scenes'][0]),(second,payload['scenes'][1])]:
                node=page.locator(f'.project-scene[data-scene-id="{scene["id"]}"] .project-scene-summary')
                assert await node.inner_text()==row['summary']
                assert await node.evaluate('(node)=>getComputedStyle(node).color')=='rgb(31, 42, 40)'
                assert await node.evaluate('(node)=>node.previousElementSibling.tagName')=='H4'
            for table,rows in before.items():assert [tuple(row) for row in fixture.store._conn.execute('SELECT * FROM '+table)]==rows
            Path('diagnostics').mkdir(exist_ok=True)
            await page.screenshot(path='diagnostics/scene_info_acceptance.png',full_page=True)
            await card.get_by_role('button',name='Edit summary',exact=True).click()
            long_summary='Production metadata only. '*120
            await page.locator('#projectSummaryInput').fill(long_summary)
            await page.get_by_role('button',name='Save summary',exact=True).click()
            await card.get_by_role('button',name='Show more',exact=True).wait_for()
            node=card.locator('.project-scene-summary')
            assert await node.evaluate('(node)=>node.clientHeight<=parseFloat(getComputedStyle(node).lineHeight)*3+1')
            await card.get_by_role('button',name='Show more',exact=True).click()
            await card.get_by_role('button',name='Show less',exact=True).wait_for()
            assert await node.evaluate('(node)=>node.clientHeight>parseFloat(getComputedStyle(node).lineHeight)*3')
            await card.get_by_role('button',name='Show less',exact=True).click()
            await card.get_by_role('button',name='Edit summary',exact=True).click()
            await page.locator('#projectSummaryInput').fill('<script>bad()</script>')
            await page.get_by_role('button',name='Save summary',exact=True).click()
            await card.get_by_text('<script>bad()</script>',exact=True).wait_for()
            assert not errors,errors
            Path('diagnostics/scene_info_ui_verification.json').write_text(json.dumps({'project':'test_riven (isolated fixture)','acceptance_summaries':payload,'unknown_blocked':True,'validate_read_only':True,'compact_expand_collapse':True,'manual_edit':True,'html_escaped':True,'page_errors':errors},ensure_ascii=False,indent=2),encoding='utf-8')
            print('Scene Info browser acceptance passed: import, Vietnamese summaries, unknown scene, clamp, expand/collapse, edit, escaping.')
    finally:
        if browser:await browser.close()
        fixture.tearDown()

if __name__=='__main__':asyncio.run(main())
