import asyncio
import ast
import io
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from PIL import Image
from fastapi import FastAPI, Header, File, UploadFile, HTTPException
from fastapi.responses import FileResponse
from image_library import ImageLibrary

ROOT=Path(__file__).resolve().parent

def png(color='red'):
    out=io.BytesIO()
    Image.new('RGB',(8,8),color).save(out,format='PNG')
    return out.getvalue()

class LibraryTests(unittest.TestCase):
    def test_persistence_dedup_search_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            lib=ImageLibrary(tmp)
            first=lib.save(png(),'original.png',10000)
            self.assertEqual(first['id'],lib.save(png(),'renamed.png',10000)['id'])
            restored=ImageLibrary(tmp)
            self.assertEqual(restored.list()['total'],1)
            self.assertEqual(restored.list(search='original')['images'][0]['name'],'original.png')
            self.assertEqual(restored.path(first['id']).read_bytes(),png())
            for identity in ['../secret','a'*64]:
                with self.assertRaises(ValueError): restored.path(identity)
            with self.assertRaises(ValueError):lib.save(png(),'large.png',2)
            with self.assertRaises(Exception):lib.save(b'invalid','fake.png',10000)
            self.assertEqual(lib.list()['total'],1)

    def test_authenticated_routes_upload_list_and_download(self):
        with tempfile.TemporaryDirectory() as tmp:
            app=FastAPI()
            def auth(key):
                if key!='test': raise HTTPException(401,'denied')
            ns=dict(app=app,Header=Header,File=File,UploadFile=UploadFile,HTTPException=HTTPException,FileResponse=FileResponse,
                    _admin_auth=auth,image_library=ImageLibrary(tmp),config=SimpleNamespace(REFERENCE_IMAGE_MAX_COUNT=9,REFERENCE_IMAGE_MAX_BYTES=10000))
            tree=ast.parse((ROOT/'server.py').read_text(encoding='utf-8'))
            wanted={'list_library_images','save_library_images','library_image'}
            exec(compile(ast.Module(body=[n for n in tree.body if isinstance(n,ast.AsyncFunctionDef) and n.name in wanted],type_ignores=[]),'routes','exec'),ns)
            async def check():
                with self.assertRaises(HTTPException) as denied:
                    await ns['list_library_images'](0,60,'',None)
                self.assertEqual(denied.exception.status_code,401)
                upload=UploadFile(filename='test.png',file=io.BytesIO(png()))
                result=await ns['save_library_images']([upload],'test')
                identity=result['images'][0]['id']
                self.assertEqual((await ns['list_library_images'](0,60,'','test'))['total'],1)
                with self.assertRaises(HTTPException):await ns['library_image'](identity,None)
                response=await ns['library_image'](identity,'test')
                self.assertEqual(Path(response.path).read_bytes(),png())
                with self.assertRaises(HTTPException) as invalid:
                    await ns['save_library_images']([UploadFile(filename='bad.png',file=io.BytesIO(b'no'))],'test')
                self.assertEqual(invalid.exception.status_code,422)
            asyncio.run(check())

class BrowserTests(unittest.IsolatedAsyncioTestCase):
    async def test_upload_then_reuse_in_reference_and_frames(self):
        from patchright.async_api import async_playwright
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            try:
                page=await browser.new_page()
                uploads=[];errors=[];submissions=[];deletes=[]
                page.on('pageerror',lambda e:errors.append(str(e)))
                async def route(r):
                    url=r.request.url
                    if url=='http://fixture/':return await r.fulfill(body=(ROOT/'web/index.html').read_text(encoding='utf-8'),content_type='text/html')
                    if '/api/admin/image-library/' in url:
                        if r.request.method=='DELETE':
                            deletes.append(url)
                            return await r.fulfill(status=409,json={'detail':'Image is used by 2 jobs'})
                        return await r.fulfill(body=png(),content_type='image/png')
                    if '/v1/videos/generations' in url:
                        submissions.append(r.request.post_data_json)
                        return await r.fulfill(json={'id':'job','status':'queued'})
                    if '/api/admin/reference-images' in url:raise AssertionError('Unexpected image reupload')
                    if '/api/admin/image-library' in url:
                        if r.request.method=='POST':
                            uploads.append(r.request.post_data_buffer)
                            return await r.fulfill(json={'images':[{'id':'a','name':'test.png'}]})
                        return await r.fulfill(json={'images':[{'id':'a','name':'test.png'}] if uploads else [],'total':int(bool(uploads))})
                    if '/api/admin/accounts' in url:return await r.fulfill(json={'accounts':[]})
                    if '/api/admin/tasks' in url:return await r.fulfill(json={'tasks':[],'total':0})
                    return await r.fulfill(json={})
                await page.route('**/*',route)
                await page.goto('http://fixture/')
                async def run(source):
                    await page.add_script_tag(content="(async()=>{"+source+";document.body.dataset.done='1'})()")
                    await page.wait_for_function("document.body.dataset.done==='1'")
                    await page.locator('body').evaluate("(el)=>delete el.dataset.done")
                await run("clearInterval(timer);await openImageLibrary('referenceImages')")
                self.assertTrue(await page.locator('#libraryApply').is_disabled())
                await run("hide('dlg')")
                await page.locator('#referencePicker').set_input_files({'name':'test.png','mimeType':'image/png','buffer':png()})
                await page.wait_for_function("document.querySelector('#librarySaveStatus').textContent.includes('Chọn ảnh')")
                self.assertEqual(len(uploads),1)
                await run("removeReferenceImage(0);await openImageLibrary('referenceImages')")
                await page.locator('#libraryGrid button[data-image-id]').click()
                await page.locator('#libraryApply').click()
                await page.locator('#dlg').wait_for(state='hidden')
                self.assertEqual(await page.locator('#referenceImages').evaluate('(el)=>el.files[0].name'),'test.png')
                self.assertEqual(len(uploads),1)
                await run("await openImageLibrary('startImage')")
                await page.locator('#libraryGrid button[data-image-id]').click()
                await page.locator('#libraryApply').click()
                await page.locator('#dlg').wait_for(state='hidden')
                self.assertEqual(await page.locator('#startImage').evaluate('(el)=>el.files.length'),1)
                await run("q('#videoPrompt').value='Original prompt'")
                await run("await submitVideoTask()")
                self.assertEqual(submissions[0]['reference_images'],['library://a'])
                self.assertEqual(len(uploads),1)
                await run("await openImageLibrary('referenceImages')")
                page.on('dialog',lambda dialog:asyncio.create_task(dialog.accept()))
                await page.locator('#libraryGrid button').filter(has_text='Xóa ảnh').click()
                await page.wait_for_function("document.querySelector('#libraryError').textContent.includes('2 jobs')")
                self.assertEqual(len(deletes),1)
                self.assertFalse(errors,errors)
            finally:await browser.close()

if __name__=='__main__':unittest.main()
