"""Browser smoke against real in-memory review APIs and a locally recorded fixture video."""
import asyncio
import base64
import json
from pathlib import Path
from urllib.parse import urlsplit
from patchright.async_api import async_playwright
from test_project_review import ReviewTests


async def main():
    fixture=ReviewTests();fixture.setUp()
    browser=None
    errors=[];requests=[];generation_auth=[]
    try:
        selected=fixture.attempt();fixture.select(selected)
        scene=fixture.create_scene(1,'forest')
        fixture.attempt(scene,status='failed')
        older=fixture.attempt(scene)
        completed=fixture.attempt(scene)
        fixture.attempt(scene,status='failed')
        original_count=len(fixture.generations(scene))
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            context=await browser.new_context(viewport={'width':1365,'height':1000})
            await context.add_init_script("localStorage.setItem('dola_admin_key','admin-test')")
            page=await context.new_page()
            page.on('pageerror',lambda error:errors.append(str(error)))
            video=await page.evaluate('''async()=>{
              const canvas=document.createElement('canvas');canvas.width=128;canvas.height=72;
              const ctx=canvas.getContext('2d');
              const mime=['video/mp4','video/webm;codecs=vp8'].find(type=>MediaRecorder.isTypeSupported(type));
              const stream=canvas.captureStream(15);const recorder=new MediaRecorder(stream,{mimeType:mime});
              const chunks=[];recorder.ondataavailable=e=>{if(e.data.size)chunks.push(e.data)};
              const stopped=new Promise(resolve=>recorder.onstop=resolve);recorder.start();
              for(let i=0;i<8;i++){ctx.fillStyle=i%2?'#00a870':'#222';ctx.fillRect(0,0,128,72);await new Promise(resolve=>setTimeout(resolve,80));}
              recorder.stop();await stopped;stream.getTracks().forEach(track=>track.stop());
              const blob=new Blob(chunks,{type:mime});const bytes=new Uint8Array(await blob.arrayBuffer());
              let text='';for(const byte of bytes)text+=String.fromCharCode(byte);
              return {mime,base64:btoa(text)};
            }''')
            video_bytes=base64.b64decode(video['base64'])
            async def route(r):
                parsed=urlsplit(r.request.url);path=parsed.path
                requests.append((r.request.method,path))
                if r.request.method=='POST' and path.endswith('/generate'):
                    generation_auth.append(r.request.headers.get('authorization'))
                if path=='/':
                    return await r.fulfill(content_type='text/html',body=Path('web/index.html').read_text(encoding='utf-8'))
                if path.startswith('/videos/'):
                    return await r.fulfill(content_type=video['mime'],body=video_bytes)
                if path=='/api/admin/accounts': return await r.fulfill(json={'accounts':[]})
                if path=='/api/admin/stats': return await r.fulfill(json=dict(per_day=[],per_account=[],today_completed=0,today_failed=0,total_accounts=0,available_accounts=0,total_remaining=0))
                if path=='/api/admin/tasks': return await r.fulfill(json={'tasks':fixture.store.recent_tasks(-1)})
                if path=='/api/admin/jobs': return await r.fulfill(json={'jobs':{}})
                if path=='/api/admin/keys': return await r.fulfill(json={'keys':[]})
                response=fixture.http.request(r.request.method,path+('?' + parsed.query if parsed.query else ''),
                    content=r.request.post_data,headers=dict(r.request.headers))
                return await r.fulfill(status=response.status_code,content_type=response.headers.get('content-type','application/json'),body=response.content)
            await page.route('**/*',route)
            await page.goto('http://review.test/')
            await page.wait_for_selector('#loginMask',state='hidden')
            await page.add_script_tag(content='clearInterval(timer)')
            await page.get_by_role('button',name='Projects',exact=True).click()
            await page.locator('#projectList').get_by_role('button',name='Riven',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectProgress').textContent.includes('1 / 2 SELECTED')")
            first=page.locator(f'.project-scene[data-scene-id="{scene["id"]}"]')
            assert 'NEEDS REVIEW' in await first.inner_text()
            assert 'Latest generation: FAILED' in await first.inner_text()
            assert 'References: READY' in await first.inner_text()
            await first.get_by_role('button',name='Review Versions',exact=True).click()
            await page.locator('#projectVersionList .project-version').first.wait_for()
            assert await page.locator('#projectVersionList .project-version').count()==4
            assert 'Generation #4' in await page.locator('#projectVersionList .project-version').first.inner_text()
            assert 'failure fixture' in await page.locator('#projectVersionList .project-version').first.inner_text()
            good=page.locator(f'.project-version[data-task-id="{completed}"]')
            preview=good.locator('video')
            await preview.evaluate('(video)=>video.play()')
            await page.wait_for_function("document.querySelector('#projectVersionList video').readyState>=2")
            assert await preview.evaluate('(video)=>video.videoWidth')==128
            await good.get_by_role('button',name='Select',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectProgress').textContent.includes('2 / 2 SELECTED')")
            assert fixture.store.get_scene(scene['id'])['selected_task_id']==completed
            await page.wait_for_function(f"document.querySelector('.project-version[data-task-id=\"{completed}\"]').dataset.selected==='true'")
            assert '\u2605 SELECTED' in await good.inner_text()
            old=page.locator(f'.project-version[data-task-id="{older}"]')
            await old.get_by_role('button',name='Select',exact=True).click()
            await page.wait_for_function(f"document.querySelector('.project-version[data-task-id=\"{older}\"]').dataset.selected==='true'")
            assert fixture.store.get_scene(scene['id'])['selected_task_id']==older
            assert len(fixture.generations(scene))==original_count
            await old.get_by_role('button',name='Unselect',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectProgress').textContent.includes('1 / 2 SELECTED')")
            await page.locator('.project-key summary').click()
            await page.fill('#projectGatewayKey','fixture-gateway-key')
            await first.get_by_role('button',name='Regenerate',exact=True).click()
            await page.get_by_role('button',name='Generate 1 Tasks',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectScenes .project-scene').innerText.includes('QUEUED')")
            queued=fixture.store.project_generation_status(fixture.project_id)[0]['active_task']
            assert queued and queued['scene_id']==scene['id']
            assert generation_auth[-1]=='Bearer fixture-gateway-key'
            assert len(fixture.generations(scene))==original_count+1
            fixture.store.update(queued['id'],status='completed',video_url='/videos/new.mp4')
            await page.add_script_tag(content='ProjectReview.refresh()')
            await page.wait_for_function("document.querySelector('#projectScenes .project-scene').innerText.includes('NEEDS REVIEW')")
            new=page.locator(f'.project-version[data-task-id="{queued["id"]}"]')
            await new.get_by_role('button',name='Select',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectProgress').textContent.includes('Production complete')")
            fixture.store.clear_scene_selection(fixture.scene['id'])
            await page.get_by_role('button',name='Refresh',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectProgress').textContent.includes('1 / 2 SELECTED')")
            fixture.select(selected)
            await page.get_by_role('button',name='Refresh',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectProgress').textContent.includes('2 / 2 SELECTED')")
            # No history polling while reviewing this project.
            assert not any(path=='/api/admin/tasks' for _,path in requests)
            await page.get_by_role('button',name='Video Tasks',exact=True).click()
            await page.wait_for_selector('#videoPrompt')
            await page.fill('#videoPrompt','Legacy generation form fixture')
            await page.click('#generateBtn')
            await page.wait_for_function("!document.querySelector('#generateBtn').disabled")
            assert any(path=='/v1/videos/generations' for _,path in requests)
            await page.wait_for_function("document.querySelector('#tasksTable').innerText.includes('completed') || document.querySelector('#tasksTable').innerText.includes('Completed')")
            assert await page.locator('#tasksTable').get_by_role('button',name='Preview',exact=True).count()>0
            assert not errors,errors
            report=dict(result='PASS',checks=['project list','scene ordering/status','versions ordering','decoded video preview','select','switch selection preserves history','unselect','selected counter/completion','regenerate','poll completion','no project history polling','manual header refresh','shared Gateway key','legacy Generate/History'],fixture_video_mime=video['mime'],live_dola_generations=0)
            Path('diagnostics/project_review_ui_verification.json').write_text(json.dumps(report,indent=2),encoding='utf-8')
            await page.get_by_role('button',name='Projects',exact=True).click()
            await page.screenshot(path='diagnostics/project_review_ui.png',full_page=True)
            print(json.dumps(report))
            await browser.close();browser=None
    finally:
        if browser: await browser.close()
        fixture.tearDown()

if __name__=='__main__': asyncio.run(main())
