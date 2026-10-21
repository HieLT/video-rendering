"""Browser acceptance for progressive disclosure and guided project inputs."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit
from patchright.async_api import async_playwright
from test_project_review import ReviewTests


async def main():
    fixture=ReviewTests();fixture.setUp()
    errors=[]
    try:
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            page=await browser.new_page(viewport={'width':1365,'height':1000})
            await page.add_init_script("localStorage.setItem('dola_admin_key','admin-test')")
            page.on('pageerror',lambda e:errors.append(str(e)))
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
            await page.route('**/*',route)
            await page.goto('http://project.test/')
            await page.wait_for_selector('#loginMask',state='hidden')
            await page.add_script_tag(content='clearInterval(timer)')
            await page.get_by_role('button',name='Projects',exact=True).click()
            await page.locator('#projectList button').first.click()
            await page.locator('#projectGenerateAll').click()
            await page.locator('[data-generation-count]').select_option('3')
            assert await page.locator('[data-action="confirm-generate"]').get_attribute('data-count')=='3'
            assert '3' in await page.locator('#projectGenerationTotal').inner_text()
            await page.locator('#projectPanelBack').click()
            await page.locator('.project-primary [data-action="import"]').click()
            await page.get_by_role('button',name='Chèn mẫu',exact=True).click()
            area=page.locator('#projectImportJSON')
            sample=json.loads(await area.input_value())
            assert sample['scenes'][0]['scene_number']>fixture.scene['scene_number']
            await page.get_by_role('button',name='Kiểm tra',exact=True).click()
            await page.wait_for_function("!document.querySelector('#projectImportSubmit').disabled")
            sample['scenes'][0]['duration']=20
            await area.fill(json.dumps(sample))
            assert await page.locator('#projectImportSubmit').is_disabled()
            await page.get_by_role('button',name='Kiểm tra',exact=True).click()
            await page.get_by_text('Thời lượng không hợp lệ.',exact=False).wait_for()
            sample['scenes'][0]['duration']=15
            await page.locator('[data-input-json]').set_input_files({'name':'scenes.json','mimeType':'application/json','buffer':json.dumps(sample).encode()})
            await page.get_by_text('Đã đọc scenes.json.',exact=False).wait_for()
            await page.get_by_role('button',name='Kiểm tra',exact=True).click()
            await page.wait_for_function("!document.querySelector('#projectImportSubmit').disabled")
            await page.locator('#projectImportSubmit').click()
            await page.wait_for_function("document.querySelectorAll('#projectScenes .project-scene').length===2")
            await page.locator('#projectStatusFilter').select_option('missing')
            assert await page.locator('.project-scene:visible').count()==1
            await page.locator('.project-primary>.project-menu>summary').click()
            await page.locator('[data-action="import-info"]').click()
            await page.get_by_role('button',name='Chèn mẫu',exact=True).click()
            await page.get_by_role('button',name='Kiểm tra',exact=True).click()
            await page.wait_for_function("!document.querySelector('#projectInfoSubmit').disabled")
            await page.locator('#projectInfoSubmit').click()
            await page.locator('#projectStatusFilter').wait_for()
            # Primary selection and secondary actions remain accessible after simplification.
            completed=fixture.attempt()
            await page.add_script_tag(content='ProjectReview.refresh(true)')
            await page.locator('#projectStatusFilter').select_option('all')
            card=page.locator(f'.project-scene[data-scene-id="{fixture.scene["id"]}"]')
            await card.locator('[data-action="select"]').click()
            await page.wait_for_function("document.querySelector('#projectProgress').textContent.includes('1/2')")
            assert fixture.store.get_scene(fixture.scene['id'])['selected_task_id']==completed
            await card.locator('.project-menu>summary').click()
            await card.locator('[data-action="unselect"]').click()
            await page.wait_for_function("document.querySelector('#projectProgress').textContent.includes('0/2')")
            await card.locator('[data-action="review"]').click()
            await page.locator('#projectVersionList .project-version').wait_for()
            await page.locator('#projectCloseVersions').click()
            # Breadcrumb navigation keeps project context, filters and JSON drafts.
            await page.locator('.project-primary [data-action="import"]').click()
            await page.locator('#projectImportJSON').fill('{"scenes": []}')
            await page.locator('#projectBreadcrumb button').filter(has_text='Riven').click()
            await page.locator('.project-primary [data-action="import"]').click()
            assert await page.locator('#projectImportJSON').input_value()=='{"scenes": []}'
            await page.locator('#projectPanelBack').click()
            await page.locator('.project-sections [data-action="library"]').click()
            await page.locator('[data-action="upload"]').click()
            assert await page.locator('#projectBreadcrumb').inner_text() is not None
            assert 'Ảnh tham chiếu' in await page.locator('#projectPanelBack').inner_text()
            await page.locator('#projectPanelBack').click()
            await page.locator('[data-action="upload"]').wait_for()
            await page.locator('#projectPanelBack').click()
            await page.locator('#projectBreadcrumb button').filter(has_text='Projects').click()
            await page.locator('#projectList button').first.click()
            # Empty project should expose the next step without inactive controls.
            empty=fixture.store.create_project('Empty workspace')
            await page.add_script_tag(content='ProjectReview.refresh(true)')
            await page.locator('#projectChapters [data-action="open"]').filter(has_text='Empty workspace').click()
            await page.get_by_role('heading',name='Thêm cảnh để bắt đầu').wait_for()
            assert not await page.locator('#projectGenerateAll').is_visible()
            assert not await page.locator('.project-browser').is_visible()
            Path('diagnostics').mkdir(exist_ok=True)
            await page.screenshot(path='diagnostics/project_simple_empty.png',full_page=True)
            await page.get_by_role('button',name='Xem mẫu',exact=True).click()
            assert await page.locator('.project-input-help details').get_attribute('open') is not None
            await page.screenshot(path='diagnostics/project_simple_import.png',full_page=True)
            await page.set_viewport_size({'width':390,'height':844})
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth')
            assert not errors,errors
            await browser.close()
        print('PASS: guided JSON/file imports, validation, summary import, generation count, filters, empty state, mobile layout; no live generation')
    finally:fixture.tearDown()

if __name__=='__main__':asyncio.run(main())

