"""Browser production UX acceptance: bulk mapping, historical assets and 40 x3 candidates."""
import asyncio
import base64
import json
from pathlib import Path
from urllib.parse import urlsplit
from patchright.async_api import async_playwright
from test_production_ux import ProductionUXTests
from test_asset_library import image_bytes
from test_scene_import import item
from asset_storage import resolve_asset_path

async def main():
    fixture=ProductionUXTests();fixture.setUp();fixture.ns['config'].MAX_PENDING_TASKS=500
    browser=None;errors=[];requests=[]
    try:
        async with async_playwright() as playwright:
            browser=await playwright.chromium.launch(headless=True)
            context=await browser.new_context(viewport={'width':1440,'height':1000})
            await context.add_init_script("localStorage.setItem('dola_admin_key','admin-test')")
            page=await context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
            async def route(r):
                parsed=urlsplit(r.request.url);path=parsed.path;requests.append((r.request.method,path))
                if path=='/':return await r.fulfill(content_type='text/html',body=Path('web/index.html').read_text(encoding='utf-8'))
                if path.startswith('/videos/'):return await r.fulfill(content_type='video/mp4',body=b'fixture')
                if path=='/api/admin/accounts':return await r.fulfill(json={'accounts':[]})
                if path=='/api/admin/stats':return await r.fulfill(json=dict(per_day=[],per_account=[],today_completed=0,today_failed=0,total_accounts=0,available_accounts=0,total_remaining=0))
                if path=='/api/admin/jobs':return await r.fulfill(json={'jobs':{}})
                if path.endswith('/generate') and r.request.method=='POST':await asyncio.sleep(.08)
                response=fixture.http.request(r.request.method,path,content=r.request.post_data_buffer,headers=dict(r.request.headers))
                await r.fulfill(status=response.status_code,content_type=response.headers.get('content-type','application/json'),body=response.content)
            await page.route('**/*',route);await page.goto('http://production.test/')
            await page.wait_for_selector('#loginMask',state='hidden');await page.add_script_tag(content='clearInterval(timer)')
            await page.get_by_role('button',name='Projects',exact=True).click()
            await page.get_by_role('button',name='+ Create Project',exact=True).click();await page.fill('#projectNameInput','Production UX')
            await page.locator('#projectNameForm').get_by_role('button',name='Create Project',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectTitle').textContent==='Production UX'")
            project=next(p for p in fixture.store.list_projects() if p['name']=='Production UX');project_id=project['id']
            names=['Riven Two Years Later','Cabin Interior','Broken Sword']+[f'Reference {n:02d}' for n in range(4,19)]
            payload={'scenes':[item(n,references=[dict(name=names[(n-1)%18],alias='Ref')],prompt='@Ref moves') for n in range(40,0,-1)]}
            await page.get_by_role('button',name='Import Scenes',exact=True).click();await page.fill('#projectImportJSON',json.dumps(payload));await page.get_by_role('button',name='Validate',exact=True).click()
            await page.get_by_role('button',name='Import 40 Scenes',exact=True).click()
            await page.wait_for_function("document.querySelectorAll('#projectMissing [data-drop-reference]').length===18")
            await page.locator('#projectMissing').get_by_role('button',name='Bulk Upload References',exact=True).click()
            files=[dict(name=(name.replace(' ','_')+'.png' if index else 'riven_final.png'),mimeType='image/png',buffer=image_bytes()) for index,name in enumerate(names[:15])]
            files.append(dict(name='Reference_16.png',mimeType='image/png',buffer=b'not an image'))
            await page.set_input_files('#projectBulkFiles',files)
            await page.wait_for_function("document.querySelectorAll('[data-stage-id]').length===16")
            first=page.locator('[data-stage-id]').first
            assert await first.locator('select[data-stage-map]').input_value()==''
            assert await page.locator('[data-stage-id]').nth(1).locator('select[data-stage-map]').input_value()=='Cabin Interior'
            await first.locator('select[data-stage-map]').select_option('Riven Two Years Later')
            await page.locator('[data-stage-id]').nth(1).locator('select[data-stage-type]').select_option('environment')
            assert not any(method=='POST' and path.endswith('/assets') for method,path in requests)
            await page.get_by_role('button',name='Upload 16 References',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectMessage').textContent.includes('Uploaded 15 references; 1 failed')")
            assert await page.locator('[data-stage-id]').count()==16
            assert 'FAILED' in await page.locator('[data-stage-id]').last.inner_text()
            assert 'SUCCESS' in await page.locator('[data-stage-id]').first.inner_text()
            await page.locator('#projectPanel').evaluate('(el)=>el.scrollIntoView({block:"start"})')
            await page.screenshot(path='diagnostics/production_ux_bulk.png',full_page=False)
            assert len(fixture.store.list_project_assets(project_id))==15
            await page.get_by_role('button',name='Back to project',exact=True).click()
            assert await page.locator('#projectMissing [data-drop-reference]').count()==3
            async def drop(selector,items):
                data=[dict(name=name,data=base64.b64encode(content).decode()) for name,content in items]
                await page.locator(selector).evaluate("""(element,items)=>{const transfer=new DataTransfer();for(const item of items){const bytes=Uint8Array.from(atob(item.data),c=>c.charCodeAt(0));transfer.items.add(new File([bytes],item.name,{type:'image/png'}));}element.dispatchEvent(new DragEvent('drop',{bubbles:true,cancelable:true,dataTransfer:transfer}));}""",data)
            await drop('#projectMissing [data-drop-reference="Reference 16"]',[('unknown.png',image_bytes())])
            assert await page.locator('[data-stage-map]').first.input_value()=='Reference 16'
            await drop('[data-bulk-drop]',[('Reference-17.png',image_bytes()),('Reference_18.webp',image_bytes('WEBP'))])
            assert await page.locator('[data-stage-id]').count()==3
            await page.get_by_role('button',name='Upload 3 References',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectMessage').textContent.includes('Uploaded 3 references; 0 failed')")
            await page.get_by_role('button',name='Back to project',exact=True).click()
            assert not await page.locator('#projectMissing').inner_text()
            assert all(s['ready'] for s in fixture.store.project_generation_status(project_id))
            await page.select_option('#projectCandidateCount','3');await page.locator('#projectGenerateAll').click()
            await page.get_by_role('button',name='Generate 120 Tasks',exact=True).wait_for()
            assert 'Ready Scenes: 40' in await page.locator('#projectPanelBody').inner_text()
            assert 'Candidates per Scene: 3' in await page.locator('#projectPanelBody').inner_text()
            assert 'Total generation tasks: 120' in await page.locator('#projectPanelBody').inner_text()
            await page.get_by_role('button',name='Generate 120 Tasks',exact=True).dblclick()
            await page.wait_for_function("document.querySelector('#projectPanelBody').textContent.includes('Created: 120')")
            assert sum(method=='POST' and path==f'/api/admin/projects/{project_id}/generate' for method,path in requests)==1
            task_rows=[fixture.store.get(task['task_id']) for scene in fixture.store.project_generation_status(project_id) for task in fixture.store.scene_generations(scene['id'])['generations']]
            assert len(task_rows)==120;assert len({r['batch_id'] for r in task_rows})==1
            await page.get_by_role('button',name='Back to project',exact=True).click()
            assert 'Candidates: 3' in await page.locator('#projectScenes .project-scene').first.inner_text()
            assert await page.locator('#projectScenes .project-scene').first.get_by_role('button',name='Regenerate',exact=True).is_disabled()
            before={r['id']:fixture.store.get(r['id'])['reference_snapshot'] for r in task_rows}
            old=next(a for a in fixture.store.list_project_assets(project_id) if a['name']==names[0]);old_bytes=resolve_asset_path(old['file_path']).read_bytes()
            await page.get_by_role('button',name='Reference Library',exact=True).click()
            card=page.locator('.project-asset').filter(has=page.get_by_role('heading',name=names[0],exact=True))
            await card.get_by_role('button',name='Replace Image',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectReplaceOld')?.naturalWidth>0")
            await page.set_input_files('#projectReplaceFile',dict(name='replacement.jpg',mimeType='image/jpeg',buffer=image_bytes('JPEG')))
            await page.wait_for_function("document.querySelector('#projectReplaceNew')?.naturalWidth>0")
            assert 'will NOT be changed' in await page.locator('#projectPanelBody').inner_text()
            await page.screenshot(path='diagnostics/production_ux_replace.png',full_page=False)
            await page.get_by_role('button',name='Replace',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectPanelTitle').textContent==='Reference Library'")
            new=next(a for a in fixture.store.list_project_assets(project_id) if a['name']==names[0]);assert new['id']!=old['id']
            assert resolve_asset_path(old['file_path']).read_bytes()==old_bytes
            assert {r['id']:fixture.store.get(r['id'])['reference_snapshot'] for r in task_rows}==before
            removed=next(a for a in fixture.store.list_project_assets(project_id) if a['name']=='Cabin Interior')
            await page.locator('.project-asset').filter(has=page.get_by_role('heading',name='Cabin Interior',exact=True)).get_by_role('button',name='Delete',exact=True).click()
            assert 'historical reference snapshots' in await page.locator('#projectPanelBody').inner_text()
            await page.get_by_role('button',name='Remove',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectPanelTitle').textContent==='Reference Library'")
            assert resolve_asset_path(removed['file_path']).exists()
            await page.get_by_role('button',name='Back to project',exact=True).click()
            assert 'Cabin Interior' in await page.locator('#projectMissing').inner_text()
            await drop('#projectMissing [data-drop-reference="Cabin Interior"]',[('cabin_new.png',image_bytes())])
            await page.get_by_role('button',name='Upload 1 References',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectMessage').textContent.includes('Uploaded 1 references; 0 failed')")
            await page.get_by_role('button',name='Back to project',exact=True).click()
            first_scene=fixture.store.project_generation_status(project_id)[0];first_tasks=[r for r in task_rows if r['scene_id']==first_scene['id']]
            fixture.store.update(first_tasks[0]['id'],status='completed',video_url='/videos/old.mp4')
            await page.add_script_tag(content='ProjectReview.refresh(true)')
            first=page.locator('#projectScenes .project-scene').first
            await first.get_by_role('button',name='Review Versions',exact=True).click()
            await page.wait_for_function("document.querySelectorAll('#projectVersionList .project-version').length===3")
            assert await page.locator('#projectVersionList').get_by_role('button',name='Select',exact=True).count()==1
            await page.locator('#projectVersionList').get_by_role('button',name='Select',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectProgress').textContent.includes('1 / 40 SELECTED')")
            for row in first_tasks:fixture.store.update(row['id'],status='completed',video_url='/videos/old.mp4')
            await page.add_script_tag(content='ProjectReview.refresh(true)')
            await first.locator('[data-scene-candidates]').select_option('5')
            await first.get_by_role('button',name='Regenerate',exact=True).click()
            assert 'Total generation tasks: 5' in await page.locator('#projectPanelBody').inner_text()
            await page.get_by_role('button',name='Generate 5 Tasks',exact=True).dblclick()
            await page.wait_for_function("document.querySelector('#projectMessage').textContent.includes('5 candidates queued')")
            generations=fixture.store.scene_generations(first_scene['id'])['generations'];assert len(generations)==8
            selected_id=first_tasks[0]['id'];assert fixture.store.get_scene(first_scene['id'])['selected_task_id']==selected_id
            fresh=[r for r in generations if r['task_id'] not in {t['id'] for t in first_tasks}]
            assert all(r['reference_snapshot'][0]['asset_id']==new['id'] for r in fresh)
            fixture.store.update(fresh[0]['task_id'],status='completed',video_url='/videos/new.mp4');fixture.store.update(fresh[-1]['task_id'],status='processing')
            await page.add_script_tag(content='ProjectReview.refresh(true)')
            await page.wait_for_function("document.querySelectorAll('#projectVersionList .project-version').length===8")
            assert 'Processing: 1' in await first.inner_text()
            assert 'Queued: 3' in await first.inner_text()
            await page.locator(f'.project-version[data-task-id="{fresh[0]["task_id"]}"]').get_by_role('button',name='Select',exact=True).click()
            await page.wait_for_function(f"document.querySelector('.project-version[data-task-id=\"{fresh[0]['task_id']}\"]').dataset.selected==='true'")
            assert fixture.store.get_scene(first_scene['id'])['selected_task_id']==fresh[0]['task_id']
            await page.screenshot(path='diagnostics/production_ux_candidates.png',full_page=False)
            for scene in fixture.store.project_generation_status(project_id):
                for task in fixture.store.scene_generations(scene['id'])['generations']:fixture.store.update(task['task_id'],status='completed',video_url='/videos/fixture.mp4')
            await page.add_script_tag(content='ProjectReview.refresh(true)')
            await page.locator('#projectSelectLatest').click()
            await page.wait_for_function("document.querySelector('#projectProgress').textContent.includes('40 / 40 SELECTED')")
            await page.get_by_role('button',name='View Selected Outputs',exact=True).click()
            await page.wait_for_function("document.querySelectorAll('.project-output').length===40")
            await page.get_by_role('button',name='Back to project',exact=True).click();await page.locator('#projectBack').click()
            await page.get_by_role('button',name='+ Create Project',exact=True).click();await page.fill('#projectNameInput','Ambiguous names')
            await page.locator('#projectNameForm').get_by_role('button',name='Create Project',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectTitle').textContent==='Ambiguous names'")
            await page.get_by_role('button',name='Import Scenes',exact=True).click()
            ambiguous={'scenes':[item(1,references=[dict(name='Foo-Bar',alias='A'),dict(name='Foo Bar',alias='B')],prompt='@A with @B')]}
            await page.fill('#projectImportJSON',json.dumps(ambiguous));await page.get_by_role('button',name='Validate',exact=True).click();await page.get_by_role('button',name='Import 1 Scenes',exact=True).click()
            await page.wait_for_function("document.querySelectorAll('#projectMissing [data-drop-reference]').length===2")
            await page.locator('#projectMissing').get_by_role('button',name='Bulk Upload References',exact=True).click()
            await page.set_input_files('#projectBulkFiles',[dict(name='Foo_Bar.png',mimeType='image/png',buffer=image_bytes()),dict(name='other.png',mimeType='image/png',buffer=image_bytes()),dict(name='unsupported.gif',mimeType='image/gif',buffer=image_bytes())])
            assert await page.locator('[data-stage-id]').first.locator('[data-stage-map]').input_value()==''
            assert 'Unsupported format' in await page.locator('[data-stage-id]').last.inner_text()
            await page.locator('[data-stage-id]').first.locator('[data-stage-map]').select_option('Foo-Bar')
            await page.locator('[data-stage-id]').nth(1).locator('[data-stage-map]').select_option('Foo-Bar')
            assert 'Two files map to this reference' in await page.locator('#projectPanelBody').inner_text()
            assert await page.get_by_role('button',name='Upload 0 References',exact=True).is_disabled()
            await page.locator('[data-stage-id]').nth(1).locator('[data-stage-map]').select_option('Foo Bar')
            assert await page.get_by_role('button',name='Upload 2 References',exact=True).is_enabled()
            assert not errors,errors
            assert not any(path=='/api/admin/tasks' for _,path in requests)
            report=dict(result='PASS',eligible_scenes=40,initial_candidates=120,regenerate_candidates=5,checks=['multi-file local staging','no early upload','exact filename suggestion','uncertain filename unmapped','manual mapping/type','subset upload','per-item failure preserved','automatic resolve','multi-file drop','direct missing-reference drop','readiness refresh','40 x3 confirmation =120','double-click one request','same batch independent tasks','replace old/new previews','current bindings replaced','historical snapshots/files retained','remove current asset keeps history','remove becomes missing','upload after remove','review completed while siblings queued','per-scene x5 confirmation','regenerate old selection survives','8 versions','processing priority and candidate counts','switch selection','40/40 selected','ordered outputs','ambiguous filename not auto-bound','unsupported extension blocked','duplicate mapping excluded','mapping correction'],live_dola_generations=0)
            Path('diagnostics/production_ux_ui_verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8');print(json.dumps(report))
            await browser.close();browser=None
    finally:
        if browser:await browser.close()
        fixture.tearDown()

if __name__=='__main__':asyncio.run(main())
