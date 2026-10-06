"""Complete 40-scene production workflow through browser controls; no live generation."""
import asyncio
import base64
import json
from pathlib import Path
from urllib.parse import urlsplit
from patchright.async_api import async_playwright
from test_project_review import ReviewTests
from test_asset_library import image_bytes
from test_scene_import import item

async def main():
    fixture=ReviewTests();fixture.setUp();browser=None
    errors=[];requests=[]
    try:
        async with async_playwright() as playwright:
            browser=await playwright.chromium.launch(headless=True)
            context=await browser.new_context(viewport={'width':1365,'height':1000})
            await context.add_init_script("localStorage.setItem('dola_admin_key','admin-test')")
            page=await context.new_page();page.on('pageerror',lambda e:errors.append(str(e)))
            clip=await page.evaluate("""async()=>{
                const canvas=document.createElement('canvas');canvas.width=128;canvas.height=72;
                const ctx=canvas.getContext('2d');const mime='video/mp4';
                const stream=canvas.captureStream(15);const recorder=new MediaRecorder(stream,{mimeType:mime});
                const chunks=[];recorder.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};
                const done=new Promise(resolve=>recorder.onstop=resolve);recorder.start();
                for(let i=0;i<6;i++){ctx.fillStyle=i%2?'#00a870':'#222';ctx.fillRect(0,0,128,72);await new Promise(resolve=>setTimeout(resolve,80));}
                recorder.stop();await done;stream.getTracks().forEach(t=>t.stop());
                const bytes=new Uint8Array(await new Blob(chunks,{type:mime}).arrayBuffer());let text='';for(const b of bytes)text+=String.fromCharCode(b);return btoa(text);
            }""")
            video=base64.b64decode(clip)
            async def route(r):
                parsed=urlsplit(r.request.url);path=parsed.path;requests.append((r.request.method,path))
                if path=='/':return await r.fulfill(content_type='text/html',body=Path('web/index.html').read_text(encoding='utf-8'))
                if path.startswith('/videos/'):return await r.fulfill(content_type='video/mp4',body=video)
                if path=='/api/admin/accounts':return await r.fulfill(json={'accounts':[]})
                if path=='/api/admin/stats':return await r.fulfill(json=dict(per_day=[],per_account=[],today_completed=0,today_failed=0,total_accounts=0,available_accounts=0,total_remaining=0))
                if path=='/api/admin/tasks':return await r.fulfill(json={'tasks':fixture.store.recent_tasks(-1)})
                if path=='/api/admin/jobs':return await r.fulfill(json={'jobs':{}})
                if path=='/api/admin/keys':return await r.fulfill(json={'keys':[]})
                response=fixture.http.request(r.request.method,path+('?' + parsed.query if parsed.query else ''),content=r.request.post_data_buffer,headers=dict(r.request.headers))
                await r.fulfill(status=response.status_code,content_type=response.headers.get('content-type','application/json'),body=response.content)
            await page.route('**/*',route);await page.goto('http://production.test/')
            await page.wait_for_selector('#loginMask',state='hidden');await page.add_script_tag(content='clearInterval(timer)')
            await page.get_by_role('button',name='Projects',exact=True).click()
            await page.get_by_role('button',name='+ Create Project',exact=True).click()
            await page.fill('#projectNameInput','   ')
            await page.locator('#projectNameForm').get_by_role('button',name='Create Project',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectPanelError').textContent.includes('cannot be empty')")
            await page.fill('#projectNameInput','RIVEN - THE EXILE')
            await page.locator('#projectNameForm').get_by_role('button',name='Create Project',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectTitle').textContent==='RIVEN - THE EXILE'")
            assert 'No scenes' in await page.locator('#projectScenes').inner_text()
            assert '0 / 0 SELECTED' in await page.locator('#projectProgress').inner_text()
            assert await page.locator('#projectGenerateAll').is_disabled()
            project=next(p for p in fixture.store.list_projects() if p['name']=='RIVEN - THE EXILE');project_id=project['id']
            await page.get_by_role('button',name='Import Scenes',exact=True).click()
            await page.fill('#projectImportJSON','{broken')
            await page.get_by_role('button',name='Validate',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectPanelError').textContent.includes('Invalid JSON')")
            payload={'scenes':[item(n,references=[dict(name='Riven Two Years Later',alias='Hero')]+([dict(name='Cabin Interior',alias='Cabin')] if n<=2 else []),prompt='@Hero walks'+(' into @Cabin' if n<=2 else '')) for n in range(40,0,-1)]}
            invalid=json.loads(json.dumps(payload));invalid['scenes'][0]['duration']=99
            await page.fill('#projectImportJSON',json.dumps(invalid));await page.get_by_role('button',name='Validate',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectImportPreview').textContent.includes('duration must')")
            assert await page.locator('#projectImportSubmit').is_disabled()
            invalid=json.loads(json.dumps(payload));invalid['scenes'][0]['references'].append(dict(name='Other',alias='Hero'))
            await page.fill('#projectImportJSON',json.dumps(invalid));await page.get_by_role('button',name='Validate',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectImportPreview').textContent.includes('Duplicate reference alias')")
            await page.fill('#projectImportJSON',json.dumps(payload));await page.get_by_role('button',name='Validate',exact=True).click()
            await page.wait_for_function("!document.querySelector('#projectImportSubmit').disabled")
            preview=await page.locator('#projectImportPreview').inner_text()
            assert 'Scenes: 40' in preview and 'References: 2 unique' in preview and 'Missing: 2' in preview
            assert fixture.store.project_scene_details(project_id)==[]
            await page.get_by_role('button',name='Import 40 Scenes',exact=True).click()
            await page.wait_for_function("document.querySelectorAll('#projectScenes .project-scene').length===40")
            assert 'Scene 01' in await page.locator('#projectScenes .project-scene').first.inner_text()
            assert 'Scene 40' in await page.locator('#projectScenes .project-scene').last.inner_text()
            assert 'Riven Two Years Later' in await page.locator('#projectMissing').inner_text()
            assert await page.locator('#projectScenes .project-scene').first.get_by_role('button',name='Generate',exact=True).is_disabled()
            await page.get_by_role('button',name='Import Scenes',exact=True).click()
            await page.fill('#projectImportJSON',json.dumps(payload));await page.get_by_role('button',name='Validate',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectImportPreview').textContent.includes('Scene 1 already exists')")
            assert await page.locator('#projectImportSubmit').is_disabled()
            await page.get_by_role('button',name='Back to project',exact=True).click()
            await page.get_by_role('button',name='Reference Library',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectPanelBody').textContent.includes('No references yet')")
            await page.get_by_role('button',name='+ Upload Reference',exact=True).click()
            await page.fill('#projectAssetName','Riven Two Years Later')
            await page.set_input_files('#projectAssetFile',{'name':'riven.png','mimeType':'image/png','buffer':image_bytes()})
            await page.locator('#projectUploadForm').get_by_role('button',name='Upload',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectPanelBody img')?.naturalWidth>0")
            assert 'Scenes 1, 2, 3' in await page.locator('#projectPanelBody').inner_text()
            await page.get_by_role('button',name='Back to project',exact=True).click()
            assert 'Cabin Interior' in await page.locator('#projectMissing').inner_text()
            assert 'Riven Two Years Later' not in await page.locator('#projectMissing').inner_text()
            assert 'READY' in await page.locator('#projectScenes .project-scene').nth(2).inner_text()
            await page.locator('#projectGenerateAll').click()
            await page.get_by_role('button',name='Generate 38 Tasks',exact=True).wait_for()
            assert 'Missing References: 2' in await page.locator('#projectPanelBody').inner_text()
            await page.get_by_role('button',name='Cancel',exact=True).click()
            await page.locator('#projectMissing').get_by_role('button',name='Upload',exact=True).click()
            assert await page.input_value('#projectAssetName')=='Cabin Interior'
            await page.select_option('#projectAssetType','environment')
            await page.set_input_files('#projectAssetFile',{'name':'cabin.webp','mimeType':'image/webp','buffer':image_bytes('WEBP')})
            await page.locator('#projectUploadForm').get_by_role('button',name='Upload',exact=True).click()
            await page.wait_for_function("document.querySelectorAll('#projectPanelBody img').length===2 && [...document.querySelectorAll('#projectPanelBody img')].every(i=>i.naturalWidth>0)")
            await page.screenshot(path='diagnostics/project_reference_library_ui.png',full_page=True)
            await page.get_by_role('button',name='Back to project',exact=True).click()
            assert not await page.locator('#projectMissing').inner_text()
            assert all(s['ready'] for s in fixture.store.project_generation_status(project_id))
            await page.locator('#projectGenerateAll').click()
            await page.get_by_role('button',name='Generate 40 Tasks',exact=True).wait_for()
            assert 'Already Selected: 0' in await page.locator('#projectPanelBody').inner_text()
            await page.get_by_role('button',name='Generate 40 Tasks',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectPanelBody').textContent.includes('Created: 40')")
            assert 'Skipped: 0' in await page.locator('#projectPanelBody').inner_text()
            await page.get_by_role('button',name='Back to project',exact=True).click()
            scenes=fixture.store.project_generation_status(project_id)
            assert len([s for s in scenes if s['active_task']])==40
            assert 'QUEUED' in await page.locator('#projectScenes .project-scene').first.inner_text()
            first_task=scenes[0]['active_task']['id'];fixture.store.update(first_task,status='processing')
            await page.add_script_tag(content='ProjectReview.refresh(true)')
            await page.wait_for_function("document.querySelector('#projectScenes .project-scene').innerText.includes('PROCESSING')")
            for scene in scenes:fixture.store.update(scene['active_task']['id'],status='completed',video_url='/videos/fixture.mp4')
            await page.add_script_tag(content='ProjectReview.refresh(true)')
            await page.wait_for_function("document.querySelector('#projectScenes .project-scene').innerText.includes('NEEDS REVIEW')")
            first=page.locator('#projectScenes .project-scene').first
            await first.get_by_role('button',name='Review Versions',exact=True).click()
            await page.locator('#projectVersionList video').wait_for()
            await page.locator('#projectVersionList video').evaluate('(v)=>v.play()')
            await page.wait_for_function("document.querySelector('#projectVersionList video').videoWidth===128")
            await page.locator('#projectVersionList').get_by_role('button',name='Select',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectProgress').textContent.includes('1 / 40 SELECTED')")
            await page.locator('#projectVersionList').get_by_role('button',name='Unselect',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectProgress').textContent.includes('0 / 40 SELECTED')")
            await first.get_by_role('button',name='Regenerate',exact=True).click()
            await page.get_by_role('button',name='Generate 1 Tasks',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectScenes .project-scene').innerText.includes('QUEUED')")
            new=fixture.store.project_generation_status(project_id)[0]['active_task'];fixture.store.update(new['id'],status='completed',video_url='/videos/new.mp4')
            await page.add_script_tag(content='ProjectReview.refresh(true)')
            await page.wait_for_function("document.querySelectorAll('#projectVersionList .project-version').length===2 && document.querySelector('#projectVersionList video')")
            await page.locator('#projectVersionList .project-version').last.get_by_role('button',name='Select',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectProgress').textContent.includes('1 / 40 SELECTED')")
            await page.locator('#projectVersionList .project-version').first.get_by_role('button',name='Select',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectVersionList .project-version').dataset.selected==='true'")
            assert fixture.store.get_scene(scenes[0]['id'])['selected_task_id']==new['id']
            await page.locator('#projectSelectLatest').click()
            await page.wait_for_function("document.querySelector('#projectProgress').textContent.includes('40 / 40 SELECTED')")
            assert 'Production complete' in await page.locator('#projectProgress').inner_text()
            assert await page.locator('#projectSelectLatest').is_disabled()
            await page.screenshot(path='diagnostics/project_workflow_detail_ui.png',full_page=True)
            await page.get_by_role('button',name='View Selected Outputs',exact=True).click()
            await page.wait_for_function("document.querySelectorAll('.project-output').length===40")
            assert '40 / 40 selected' in await page.locator('#projectPanelBody').inner_text()
            assert 'Scene 01' in await page.locator('.project-output').first.inner_text()
            assert 'Scene 40' in await page.locator('.project-output').last.inner_text()
            await page.get_by_role('button',name='Back to project',exact=True).click()
            await page.locator('#projectBack').click()
            await page.wait_for_function("document.querySelector('#projectList').textContent.includes('40 / 40')")
            assert 'COMPLETE' in await page.locator('#projectList').inner_text()
            assert not errors,errors
            assert not any(path=='/api/admin/tasks' for _,path in requests)
            report=dict(result='PASS',scenes=40,checks=['create project','blank name error','empty project','invalid JSON','field validation','duplicate alias','import conflict','preview does not write','import missing references','scene ordering','library empty','upload reference','authenticated thumbnails','auto resolve','missing upload prefill','generate confirmation','missing scenes excluded from count','generate result','queued','processing','needs review','decoded MP4','select/unselect counter','regenerate','switch selection','40/40 complete','selected outputs order','home summary','no history polling'],live_dola_generations=0)
            Path('diagnostics/project_workflow_ui_verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
            await page.screenshot(path='diagnostics/project_workflow_ui.png',full_page=True)
            print(json.dumps(report));await browser.close();browser=None
    finally:
        if browser:await browser.close()
        fixture.tearDown()

if __name__=='__main__':asyncio.run(main())
