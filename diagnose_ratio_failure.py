import asyncio,json,sqlite3,time,urllib.request,sys
from pathlib import Path
from patchright.async_api import async_playwright
import config
import video_worker_ui as worker
from browser import LAUNCH_ARGS

ACCOUNTS=['33249588-4738-4d35-a0e0-be57d7adb425','56508520-83ef-490a-be67-953d99608448']
FILES=[str(Path('D:/dola_backup/film_shots')/n) for n in ['invasion settlement.png','yasuo_new.png','suvivor.png','yasumasa_new.png','yasumasa....png']]
OUT=Path('diagnostics')/('ratio_probe_'+str(time.time_ns()))
OUT.mkdir(parents=True)
original_ratio=worker._select_video_ratio
class ProbeDone(BaseException): pass
SAMPLE=r'''() => {
 const out={time:Date.now(),visibility:document.visibilityState,focus:document.hasFocus(),raf:window.__probeRaf||0};
 out.items=[...document.querySelectorAll('[role="menuitem"]')].filter(e=>e.textContent.trim()==='16:9').map(e=>{
 const r=e.getBoundingClientRect(),c=getComputedStyle(e),hit=document.elementFromPoint(r.x+r.width/2,r.y+r.height/2);
 return {rect:[r.x,r.y,r.width,r.height],transform:c.transform,animation:c.animationName,transition:c.transition,connected:e.isConnected,html:e.outerHTML.slice(0,1500),hit:hit?.outerHTML.slice(0,500)};
 });
 out.animations=document.getAnimations().map(a=>({state:a.playState,time:a.currentTime,target:a.effect?.target?.tagName}));
 return out;
}'''
async def launch(p,account,headless=None,use_extension=False):
 args=list(LAUNCH_ARGS)
 if use_extension:
  ext=str(Path(config.EXTENSION_DIR).resolve()); args += ['--disable-extensions-except='+ext,'--load-extension='+ext]
 kw=dict(headless=False,args=args,locale='ja-JP',timezone_id='Asia/Tokyo',record_video_dir=str(OUT/account))
 if '--lean' in sys.argv: kw.pop('record_video_dir')
 if config.PROXY: kw['proxy']={'server':config.PROXY}
 ctx=await p.chromium.launch_persistent_context(str(Path('accounts')/account),**kw)
 if '--lean' not in sys.argv: await ctx.tracing.start(screenshots=True,snapshots=True,sources=True)
 page=ctx.pages[0]
 errors=[]
 page.on('pageerror',lambda e:errors.append(str(e)))
 ctx._probe_errors=errors
 return ctx
async def ratio(page,value,account,expected_images=0):
 samples=[]; result={}; running=True
 cdp=None
 if '--minimized' in sys.argv:
  cdp=await page.context.new_cdp_session(page)
  win=await cdp.send('Browser.getWindowForTarget')
  await cdp.send('Browser.setWindowBounds',{'windowId':win['windowId'],'bounds':{'windowState':'minimized'}})
 await page.evaluate('''() => {window.__probeRaf=0; const tick=()=>{window.__probeRaf++;requestAnimationFrame(tick)};requestAnimationFrame(tick)}''')
 async def sample():
  while running:
   try: samples.append(await page.evaluate(SAMPLE))
   except Exception as e: samples.append({'error':str(e)})
   await asyncio.sleep(.1)
 if '--passive' in sys.argv:
  await page.evaluate('window.__probeSamples=[];window.__probeTimer=setInterval(()=>window.__probeSamples.push(('+SAMPLE+')()),100)')
  task=None
 else:
  task=asyncio.create_task(sample())
 try:
  try:
   for trial in range(3 if '--repeat' in sys.argv else 1):
    if cdp:
     await cdp.send('Browser.setWindowBounds',{'windowId':win['windowId'],'bounds':{'windowState':'minimized'}})
     await asyncio.sleep(1.5)
    await original_ratio(page,value,account,expected_images)
   result['baseline']='passed'
  except Exception as e:
   result['baseline']=str(e)
   result['failure_state']=await page.evaluate(SAMPLE)
   await page.screenshot(path=str(OUT/(account+'_failure.png')))
   if cdp: await cdp.send('Browser.setWindowBounds',{'windowId':win['windowId'],'bounds':{'windowState':'normal'}})
   await page.keyboard.press('Escape')
   await page.bring_to_front()
   await page.wait_for_timeout(1000)
   try:
    await original_ratio(page,value,account,expected_images)
    result['foreground_retry']='passed'
   except Exception as retry:
    result['foreground_retry']=str(retry)
  result['page_errors']=page.context._probe_errors
 finally:
  running=False
  if task: await task
  else: samples=await page.evaluate('() => {clearInterval(window.__probeTimer);return window.__probeSamples}')
  result['samples']=samples
  (OUT/(account+'.json')).write_text(json.dumps(result,indent=2),encoding='utf-8')
  if '--lean' not in sys.argv: await page.context.tracing.stop(path=str(OUT/(account+'_trace.zip')))
  print('RESULT',account,json.dumps({k:v for k,v in result.items() if k not in ['samples','failure_state']}),flush=True)
 raise ProbeDone()
if '--passive' in sys.argv:
 async def no_trace(*args): return lambda: None
 worker._start_composer_trace=no_trace
if '--minimized' in sys.argv: ACCOUNTS=ACCOUNTS[1:]
if '--no-images' in sys.argv: FILES=[]
async def stop_on_upload_failure(*args): raise RuntimeError('Probe stopped: upload failed; no manual recovery')
worker._wait_for_manual_reference_upload=stop_on_upload_failure
worker.launch_account_context=launch
worker._select_video_ratio=ratio
async def main():
 req=urllib.request.Request('http://127.0.0.1:8000/api/admin/accounts',headers={'X-Admin-Key':config.ADMIN_KEY})
 d=json.load(urllib.request.urlopen(req)); rows=d if isinstance(d,list) else d['accounts']
 for a in rows:
  if a['name'] in ACCOUNTS and a.get('busy'): raise RuntimeError('Account busy: '+a['name'])
 c=sqlite3.connect('file:tasks.db?mode=ro',uri=True)
 prompt=c.execute('select prompt from tasks where id=?',('video_e239ade9c1cf42f8bfc6b1f91649c83c',)).fetchone()[0]
 print('ARTIFACTS',OUT,flush=True)
 async def run(account):
  try: await worker.generate_video(account,prompt,'16:9',30,model='seedance-2.5',reference_image_paths=FILES)
  except ProbeDone: pass
 if "--parallel" in sys.argv:
  await asyncio.gather(*(run(a) for a in ACCOUNTS))
 else:
  for a in ACCOUNTS: await run(a)
asyncio.run(main())
