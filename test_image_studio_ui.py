"""Offline browser acceptance: studio settings, submit, preview, download and mobile."""
import asyncio
import io
import time
from pathlib import Path
from urllib.parse import urlsplit

from PIL import Image
from patchright.async_api import async_playwright


async def main():
    from image_worker import MODELS, RATIOS, STYLES
    errors, submitted, jobs, saved = [], [], [], []
    output=Path('diagnostics/image_studio_ui');output.mkdir(parents=True,exist_ok=True)
    fixture=Path('diagnostics/image_research/image_ori_raw.png')
    if fixture.exists():
        image=fixture.read_bytes()
    else:
        data=io.BytesIO();Image.new('RGB',(640,360),'#c7dec5').save(data,format='PNG');image=data.getvalue()
    async with async_playwright() as p:
        browser=await p.chromium.launch(headless=True,channel='chromium')
        try:
            context=await browser.new_context(viewport={'width':1440,'height':1050})
            page=await context.new_page()
            page.on('pageerror',lambda e: errors.append(str(e)))
            async def route(r):
                path=urlsplit(r.request.url).path
                if path in ('/','/image-studio.js','/image-studio.css','/image-gallery.js'):
                    file=Path('web')/('index.html' if path=='/' else path[1:])
                    return await r.fulfill(body=file.read_bytes(),content_type={'/':'text/html','.js':'text/javascript','.css':'text/css'}.get(path,'text/javascript' if path.endswith('.js') else 'text/css'))
                if path=='/api/admin/accounts':return await r.fulfill(json={'accounts':[]})
                if path=='/api/admin/stats':return await r.fulfill(json={'per_day':[],'per_account':[],'today_completed':0,'today_failed':0,'total_accounts':1,'available_accounts':1,'total_remaining':1})
                if path.endswith('/options'):return await r.fulfill(json={'models':MODELS,'ratios':RATIOS,'styles':STYLES,'accounts':[{'name':'test','label':'Studio account','busy':False}]})
                if path=='/api/admin/image-library':return await r.fulfill(status=201,json={'images':[{'id':'a'*64,'name':'clipboard.png'}]})
                if path.startswith('/api/admin/image-library/'):return await r.fulfill(body=image,content_type='image/png')
                if path=='/api/admin/image-studio/batches':
                    body=r.request.post_data_json;submitted.append(body)
                    batch=[{**body,'id':f'batch-{i}','status':'queued','phase':'queued','created_at':time.time(),'outputs':[],'error':None,'conversation_id':None} for i in range(body['count'])]
                    jobs.extend(batch)
                    return await r.fulfill(status=202,json={'jobs':batch,'count':len(batch)})
                if path=='/api/admin/image-studio/jobs':
                    if r.request.method=='POST':
                        body=r.request.post_data_json;submitted.append(body)
                        jobs.insert(0,{**body,'id':'test-job','status':'queued','phase':'queued','created_at':time.time(),'outputs':[],'error':None,'conversation_id':None})
                        return await r.fulfill(status=202,json=jobs[0])
                    return await r.fulfill(json={'jobs':jobs,'total':len(jobs),'counts':{s:sum(j['status']==s for j in jobs) for s in ['queued','completed']}})
                if path=='/api/admin/image-studio/gallery':
                    return await r.fulfill(json={'images':[dict(o,job_id=j['id'],index=n,prompt=j['prompt'],model=j['model'],created_at=j['created_at']) for j in jobs for n,o in enumerate(j['outputs']) if not o.get('deleted_at')], 'total':sum(not o.get('deleted_at',False) for j in jobs for o in j['outputs'])})
                if '/files/' in path:
                    if r.request.method=='DELETE':
                        jobs[0]['outputs'][0]['deleted_at']=time.time()
                        return await r.fulfill(json={'ok':True})
                    return await r.fulfill(body=image,content_type='image/png')
                if '/library/' in path:saved.append(path);return await r.fulfill(json={'id':'saved'})
                return await r.fulfill(json={})
            await page.route('**/*',route)
            await page.goto('http://studio.test/')
            await page.get_by_role('button',name='Tạo ảnh',exact=True).click()
            await page.wait_for_selector('#isGenerate:not([disabled])')
            await page.screenshot(path=str(output/'empty-desktop.png'),full_page=True)
            await page.locator('[data-model="Seedream 4.5"]').click()
            await page.locator('[data-ratio="9:16"]').click()
            await page.locator('#isStyle').select_option('watercolor')
            prompt='Một ấm trà gốm đỏ bên cửa sổ, ánh sáng buổi sáng và cây xanh.'
            await page.locator('#isPrompt').fill(prompt)
            # Clipboard image is attached; text-only clipboard remains native.
            text_paste=await page.locator('#isPrompt').evaluate('''e => {const d=new DataTransfer();d.setData('text/plain','normal text');const event=new ClipboardEvent('paste',{clipboardData:d,bubbles:true,cancelable:true});return e.dispatchEvent(event)}''')
            assert text_paste
            await page.locator('#isPrompt').evaluate('''e => {const d=new DataTransfer();d.items.add(new File(['fixture'],'clipboard.png',{type:'image/png'}));e.dispatchEvent(new ClipboardEvent('paste',{clipboardData:d,bubbles:true,cancelable:true}));}''')
            await page.wait_for_function("document.querySelectorAll('.is-reference').length===1 && !document.querySelector('#isGenerate').disabled")
            await page.locator('[data-remove-reference]').click()
            assert await page.locator('.is-reference').count()==0
            await page.locator('#isReferenceFiles').set_input_files({'name':'reference.png','mimeType':'image/png','buffer':image})
            await page.wait_for_function("document.querySelectorAll('.is-reference').length===1 && !document.querySelector('#isGenerate').disabled")
            await page.locator('#isGenerate').click()
            await page.wait_for_selector('.is-pending')
            assert len(submitted)==1 and submitted[0]['model']=='Seedream 4.5'
            assert submitted[0]['ratio']=='9:16' and submitted[0]['style']=='watercolor'
            assert submitted[0]['references']==['a'*64]
            jobs[0].update(status='completed',phase='completed',outputs=[{'filename':'0.png','url':'/api/admin/image-studio/jobs/test-job/files/0','width':2848,'height':1600}],conversation_id='123')
            await page.locator('#isRefresh').click()
            await page.wait_for_function("document.querySelector('.is-preview img')?.naturalWidth>0")
            await page.evaluate('window.scrollTo(0,0)')
            await page.screenshot(path=str(output/'gallery-desktop.png'),full_page=True)
            await page.locator('[data-action=preview]').click()
            await page.wait_for_selector('#isPreviewDialog[open] img')
            await page.screenshot(path=str(output/'preview-desktop.png'),full_page=True)
            async with page.expect_download() as download:
                await page.locator('#isPreviewDialog [data-action=download]').click()
            assert (await download.value).suggested_filename.endswith('.png')
            await page.locator('#isPreviewDialog [data-action=library]').click()
            await page.wait_for_function("document.getElementById('isToast')?.textContent.includes('tham chiếu')")
            assert saved
            await page.keyboard.press('Escape')
            await page.locator('[data-filter=active]').click()
            assert await page.locator('#isEmpty').is_visible()
            await page.locator('[data-filter=all]').click()
            await page.set_viewport_size({'width':390,'height':844})
            await page.evaluate('window.scrollTo(0,0)')
            await page.screenshot(path=str(output/'gallery-mobile.png'),full_page=True)
            assert await page.evaluate('document.documentElement.scrollWidth<=innerWidth+1')
            await page.locator('[data-action=reuse]').click()
            assert await page.locator('#isPrompt').input_value()==prompt
            await page.reload()
            await page.get_by_role('button',name='Tạo ảnh',exact=True).click()
            await page.wait_for_selector('#isGenerate:not([disabled])')
            assert await page.locator('#isPrompt').input_value()==prompt
            await page.get_by_role('button',name='Kho ảnh',exact=True).click()
            await page.wait_for_function("document.querySelector('.ig-image img')?.naturalWidth>0")
            assert not await page.locator('#isForm').is_visible()
            await page.locator('#igGrid [data-view]').click()
            await page.wait_for_selector('dialog[open] .ig-full img')
            await page.keyboard.press('Escape')
            await page.locator('#igAll').check()
            assert await page.locator('#igDelete').is_enabled()
            page.once('dialog',lambda d:d.dismiss())
            await page.locator('#igDelete').click()
            assert await page.locator('.ig-item').count()==1
            page.once('dialog',lambda d:d.accept())
            await page.locator('#igDelete').click()
            await page.wait_for_selector('#igEmpty',state='visible')
            assert await page.locator('.ig-item').count()==0
            await page.get_by_role('button',name='Tạo ảnh',exact=True).click()
            await page.locator('#isBatchCount').select_option('3')
            await page.locator('#isGenerate').click()
            await page.wait_for_function("document.querySelectorAll('.is-card[data-job^=batch-]').length===3")
            assert submitted[-1]['count']==3
            assert submitted[-1]['prompt']==prompt
            assert submitted[-1]['model']=='Seedream 4.5'
            await page.reload()
            await page.get_by_role('button',name='Tạo ảnh',exact=True).click()
            await page.wait_for_selector('#isGenerate:not([disabled])')
            assert await page.locator('#isBatchCount').input_value()=='3'
            assert not errors, errors
            print('PASS: settings, single submit, queued/completed, preview, download, library, filter, mobile, draft; no JS errors')
        finally:
            await browser.close()


if __name__=='__main__':asyncio.run(main())

