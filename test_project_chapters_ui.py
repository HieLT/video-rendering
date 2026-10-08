"""Offline UI check for chapter navigation; never contacts Dola."""
import asyncio
import json
from pathlib import Path
from urllib.parse import urlsplit
from patchright.async_api import async_playwright
from test_project_chapters import ChapterTests

async def main():
    fixture=ChapterTests();fixture.setUp()
    import ast
    from unittest.mock import Mock
    tree=ast.parse(Path('server.py').read_text(encoding='utf-8'))
    action=next(n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name=='admin_task_action')
    fixture.ns.update(_admin_auth=lambda key:None,TASK_ACTION_LOCKS={},scheduler=Mock())
    exec(compile(ast.Module(body=[action],type_ignores=[]),'server.py','exec'),fixture.ns)
    errors=[]
    accounts=[dict(name=name,uuid=name,email=email,created_at=created,scheduling=True,login_ok=1,auth_state='active',used_today=0,limit=2,remaining=2,ready_to_generate=True) for name,email,created in [('older','old@mail8686.us',1),('newer','new@trusticloud.us',3),('middle','mid@mail8686.us',2),('gmail','a@gmail.com',4)]]
    dispatch=[]
    try:
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            page=await browser.new_page(viewport={"width":1400,"height":1000})
            await page.add_init_script("localStorage.setItem('dola_admin_key','admin-test')")
            page.on('pageerror',lambda e:errors.append(str(e)))
            async def route(r):
                parsed=urlsplit(r.request.url);path=parsed.path
                if path=='/':return await r.fulfill(content_type='text/html',body=Path('web/index.html').read_text(encoding='utf-8'))
                if path=='/api/admin/accounts':return await r.fulfill(json={'accounts':accounts})
                if path=='/api/admin/accounts/google-bulk/progress':return await r.fulfill(json={'imports':{}})
                if path=='/api/admin/account-groups/other/dispatch':
                    body=json.loads(r.request.post_data);dispatch.append(body)
                    for account in accounts:
                        if account['email'].split('@')[1]==body['domain']:account['scheduling']=body['enabled']
                    return await r.fulfill(json={'ok':True})
                if path=='/api/admin/stats':return await r.fulfill(json=dict(per_day=[],per_account=[],today_completed=0,today_failed=0,total_accounts=0,available_accounts=0,total_remaining=0))
                if path=='/api/admin/tasks':return await r.fulfill(json={'tasks':[]})
                if path=='/api/admin/jobs':return await r.fulfill(json={'jobs':{}})
                if path=='/api/admin/keys':return await r.fulfill(json={'keys':[]})
                response=fixture.http.request(r.request.method,path,content=r.request.post_data,headers={'Content-Type':r.request.headers.get('content-type','application/json')})
                return await r.fulfill(status=response.status_code,body=response.content,content_type=response.headers.get('content-type','application/json'))
            await page.route('**/*',route)
            await page.goto('http://chapter.test/')
            await page.get_by_role('button',name='Projects',exact=True).click()
            await page.locator('#projectList button[data-action="open"]').first.click()
            for name in ('Chapter 1','Chapter 2'):
                await page.get_by_role('button',name='+ Create Chapter',exact=True).click()
                await page.fill('#projectNameInput',name)
                await page.get_by_role('button',name='Create Chapter',exact=True).click()
                await page.wait_for_function("document.querySelector('#projectTitle').textContent.includes("+repr(name)+")")
                assert 'References shared from' in await page.locator('#projectChapters').inner_text()
                assert 'No scenes' in await page.locator('#projectScenes').inner_text()
                await page.get_by_role('button',name='Back to project',exact=True).click()
                await page.get_by_role('button',name='+ Create Chapter',exact=True).wait_for()
            await page.locator('#projectChapters').get_by_role('button',name='Chapter 1',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectTitle').textContent.includes('Chapter 1')")
            await page.locator('#projectChapters').get_by_role('button',name='Chapter 2',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectTitle').textContent.includes('Chapter 2')")
            await page.get_by_role('button',name='Run Accounts (optional)',exact=True).click()
            assert await page.locator('#projectAccountChoices label:visible').count()==0
            await page.locator('#projectAccountInherit').uncheck()
            await page.locator('#runAccountDomains [data-domain="trusticloud.us"]').click()
            await page.locator('#projectAccountChoices input[value="newer"]').check()
            await page.get_by_role('button',name='Save accounts',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectMessage').textContent.includes('restricted')")
            assert fixture.store.project_accounts(fixture.project_id)['account_uuids']==[]
            chapter=next(x for x in fixture.store.list_chapters(fixture.project_id) if x['name']=='Chapter 2')
            assert fixture.store.project_accounts(chapter['id'])['account_uuids']==['newer']
            await page.get_by_role('button',name='Run Accounts (optional)',exact=True).click()
            await page.locator('#runAccountDomains [data-domain="mail8686.us"]').click()
            await page.fill('#projectAccountSearch','mail8686.us')
            assert await page.locator('#projectAccountChoices label:visible').count()==2
            await page.get_by_role('button',name='Clear selection (All accounts)',exact=True).click()
            await page.get_by_role('button',name='Save accounts',exact=True).click()
            await page.wait_for_function("document.querySelector('#projectMessage').textContent.includes('all eligible')")
            assert fixture.store.project_accounts(fixture.project_id)['all_accounts']
            await page.get_by_role('button',name='Accounts',exact=True).click()
            await page.locator('#accountGroupOther').click()
            assert await page.locator('[data-account-domain]').count()==0
            assert 'old@mail8686.us' not in await page.locator('#accountsTable').inner_text()
            await page.locator('[data-domain-action="filter"][data-domain="mail8686.us"]').click()
            assert await page.locator('[data-account-domain]').count()==1
            mail=await page.locator('#accountsTable').inner_text()
            assert mail.index('mid@mail8686.us') < mail.index('old@mail8686.us')
            assert 'new@trusticloud.us' not in mail
            await page.locator('[data-domain-action="filter"][data-domain="trusticloud.us"]').click()
            trust=await page.locator('#accountsTable').inner_text()
            assert 'new@trusticloud.us' in trust and 'old@mail8686.us' not in trust
            assert await page.locator('#selectAllaccounts').count()==1
            await page.locator('[data-domain-action="filter"][data-domain="mail8686.us"]').click()
            await page.locator('[data-domain-action="dispatch"][data-domain="mail8686.us"]').click()
            await page.wait_for_function("document.querySelector('[data-domain-action=dispatch][data-domain=\"mail8686.us\"]').textContent.includes('(0/2)')")
            assert dispatch==[{'enabled':False,'domain':'mail8686.us'}]
            assert accounts[1]['scheduling'] and accounts[3]['scheduling']
            fixture.asset('Garen')
            retry_chapter=fixture.chapter('Retry chapter')
            retry_scene=fixture.import_scene(retry_chapter)
            endpoint=f"/api/admin/scenes/{retry_scene['id']}/generate"
            assert fixture.http.post(endpoint,json={'count':1}).status_code==202
            old=fixture.store._conn.execute('SELECT id FROM tasks WHERE scene_id=?',(retry_scene['id'],)).fetchone()[0]
            fixture.store.update(old,status='stopped',phase='stopped')
            await page.add_script_tag(content="const retryBox=document.createElement('div');retryBox.id='retryFixture';retryBox.innerHTML=taskControls("+json.dumps({'id':old,'scene_id':retry_scene['id'],'status':'stopped'})+");document.body.prepend(retryBox);")
            await page.locator('#retryFixture').get_by_role('button',name='Try again',exact=True).click()
            await page.locator('#retryFixture').get_by_role('button',name='Queued',exact=True).wait_for()
            assert len(fixture.store.scene_generations(retry_scene['id'])['generations'])==1
            assert fixture.store.get_scene(retry_scene['id'])['project_id']==retry_chapter['id']
            assert fixture.store.get(old)['status']=='queued'
            assert len(json.loads(fixture.store.get(old)['retry_history']))==1
            await page.add_script_tag(content="document.querySelector('#retryFixture').innerHTML=taskControls({id:'active',scene_id:'scene',status:'processing'})+taskControls({id:'plain',status:'failed'});")
            assert await page.locator('#retryFixture [data-scene-retry]').count()==0
            await page.locator('#retryFixture').evaluate('(el)=>el.remove()')
            await page.screenshot(path='account-domains-ui-preview.png',full_page=True)
            assert not errors,errors
            await browser.close()
            print('PASS chapter create, sibling navigation, parent navigation and shared library label')
    finally: fixture.tearDown()

if __name__=='__main__': asyncio.run(main())
