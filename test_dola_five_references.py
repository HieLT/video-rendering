import asyncio,json,re,time,traceback,sys,sqlite3,shutil
from pathlib import Path
from PIL import Image,ImageDraw
from patchright.async_api import async_playwright
from browser import launch_account_context,cookie_value
from video_worker import POLL_JS
from video_worker_ui import find_captcha_frame,solve_slider,_preflight_balance
OUT=Path('diagnostics/five_reference_test_'+str(int(time.time())))
OUT.mkdir(parents=True)
def save(name,data): (OUT/name).write_text(json.dumps(data,ensure_ascii=True,indent=2),encoding='utf-8')
def clean(v):
 if isinstance(v,dict):return {k:('[redacted]' if any(t in k.lower() for t in ['token','cookie','signature','fp']) else clean(x)) for k,x in v.items()}
 if isinstance(v,list):return [clean(x) for x in v]
 return v
sources=sorted(Path('C:/Users/user/.codex/generated_images/01a0cac9-3ff8-7203-9c99-89a20b75a62b').glob('*.png'),key=lambda p:p.stat().st_mtime)
assert len(sources)==5, 'Need exactly five generated references'
for i,src in enumerate(sources,1):shutil.copy2(src,OUT/(str(i)+'.png'))
save('manifest.json',{'order':['red robot','blue bear','yellow rabbit','purple cat','orange fox'],'sources':[str(p) for p in sources]})
PROMPT='One continuous wide shot with all five reference characters visible together, full bodies, standing in a row on a plain gray studio floor. Arrange them left to right: @Image5, @Image2, @Image4, @Image1, @Image3. Only @Image{target} raises one hand and waves throughout the shot. All other characters keep both hands down and stand still. Preserve each reference character appearance. Static camera. No cuts, no transformation, no text.'
async def main():
 print('OUTPUT',OUT,flush=True)
 async with async_playwright() as p:
  account=sys.argv[1] if len(sys.argv)>1 else 'acc16'
  db=sqlite3.connect('file:tasks.db?mode=ro',uri=True)
  active=db.execute("select count(*) from tasks where account=? and status in ('running','processing','pending','queued')",(account,)).fetchone()[0]
  db.close()
  if active:raise RuntimeError('Account has active tasks')
  ctx=await launch_account_context(p,account,headless=True,use_extension=False)
  page=await ctx.new_page();sent=0;results=[];variant='baseline';sent_variants=set();captcha_rejected=False;attempts=0
  async def response_log(response):
   nonlocal captcha_rejected
   if '/chat/completion' not in response.url:return
   try:
    data=await response.text()
    save(variant+'_response_'+str(sent)+'.json',{'status':response.status,'body':data[:100000]})
    captcha_rejected='710022004' in data and 'STREAM_ERROR' in data
    print('RESPONSE',response.status,'captcha' if captcha_rejected else json.dumps(data[:700],ensure_ascii=True),flush=True)
   except Exception as e:print('RESPONSE_READ',str(e),flush=True)
  page.on('response',response_log)
  async def intercept(route):
   nonlocal sent,attempts
   req=route.request
   if req.method!='POST':return await route.continue_()
   if sent>=2 or variant in sent_variants or attempts>=6:return await route.abort()
   body=req.post_data_json
   if not isinstance(body,dict):return await route.abort()
   original=json.loads(json.dumps(body));found=[]
   def walk(v):
    if isinstance(v,dict):
     if isinstance(v.get('image'),dict) and ('identifier' in v or 'type' in v):found.append(v)
     for x in list(v.values()):walk(x)
    elif isinstance(v,list):
     for x in v:walk(x)
   walk(body)
   ability=body.get('chat_ability',{});params=ability.get('ability_param',{})
   encoded=isinstance(params,str)
   if encoded:params=json.loads(params)
   if params.get('duration')!=5 or len(found)!=5:
    save('unexpected_request.json',clean(body));print('BLOCK unexpected duration/images',params.get('duration'),len(found),flush=True);return await route.abort()
   if variant=='roles':
    # Reverse roles relative to attachment order; same prompt as control.
    for obj,role in zip(found,['last_frame','first_frame']):obj['role']=role
    params['image_with_roles']=[{'image':obj['image'],'role':obj['role']} for obj in found]
    ability['ability_param']=json.dumps(params) if encoded else params
   sent+=1;attempts+=1;sent_variants.add(variant);save(variant+'_request_original.json',clean(original));save(variant+'_request_sent.json',clean(body));print('SUBMIT',variant,'count',sent,'images',len(found),flush=True)
   if variant!='roles':await route.continue_()
   else:await route.continue_(post_data=json.dumps(body,separators=(',',':'),ensure_ascii=False))
  await page.route('**/chat/completion*',intercept)
  try:
   for variant in ([sys.argv[2]] if len(sys.argv)>2 else ['ref2','ref4']):
    await page.goto('https://www.dola.com/chat',wait_until='domcontentloaded',timeout=60000);await page.wait_for_timeout(5000)
    ok=page.get_by_role('button',name='OK',exact=True)
    if await ok.is_visible():await ok.click()
    await page.get_by_role('button',name=re.compile(r'^(?:\u52d5\u753b\u3092\u4f5c\u6210|Create (?:a )?video)$',re.I)).click(timeout=10000)
    await page.get_by_role('button',name=re.compile(r'\u30e2\u30c7\u30eb|Model',re.I)).click()
    await page.get_by_text('Dreamina Seedance 2.5',exact=True).last.click()
    duration=page.get_by_role('button',name=re.compile(r'^\d+s$')).first
    if (await duration.inner_text()).strip()!='5s':
     await duration.click();await page.get_by_text('5s',exact=True).last.click()
    box=page.locator('[contenteditable="true"]:visible').first
    await box.fill('')
    while await page.locator('[data-kind="image"] button').count():await page.locator('[data-kind="image"] button').first.click()
    for name in ['1','2','3','4','5']:
     await page.locator('input[type=file]').set_input_files(str(OUT/(name+'.png')))
     for attempt in range(8):
      await page.wait_for_timeout(3000)
      retry=page.locator('[class*="thumb-retry-mask"]')
      if await retry.count():await retry.first.click();continue
      if not await page.locator('[class*="thumb-loading-mask"]').count():break
     if await page.locator('[class*="thumb-retry-mask"],[class*="thumb-loading-mask"]').count():raise RuntimeError('Upload not ready')
    cookies=await ctx.cookies('https://www.dola.com');balance=await _preflight_balance(page,cookie_value(cookies,'msToken'),cookie_value(cookies,'s_v_web_id'),2);print('BALANCE',json.dumps(balance,ensure_ascii=True),flush=True)
    await box.fill(PROMPT.format(target=variant[-1]));await page.screenshot(path=str(OUT/(variant+'_before.png')))
    before=sent;captcha_rejected=False;await box.press('Enter')
    for tick in range(90):
     await page.wait_for_timeout(1000)
     if sent>before and page.url.rstrip('/').split('/')[-1].isdigit():break
     frame=find_captcha_frame(page)
     if frame and captcha_rejected:
      print('SOLVE CAPTCHA',flush=True)
      solved=False
      for attempt in range(1,4):
       frame=find_captcha_frame(page)
       if not frame or await solve_slider(page,frame,attempt):solved=True;break
      if not solved:raise RuntimeError('Captcha unresolved')
      sent-=1;sent_variants.discard(variant);captcha_rejected=False
      await box.press('Enter')
    conv=page.url.rstrip('/').split('/')[-1]
    if not conv.isdigit():raise RuntimeError('No conversation after submit; no automatic resend')
    cookies=await ctx.cookies('https://www.dola.com');args={'conversationId':conv,'msToken':cookie_value(cookies,'msToken'),'fp':cookie_value(cookies,'s_v_web_id')}
    print('POLL',variant,conv,flush=True);start=time.time();last={}
    while time.time()-start<900:
     await page.wait_for_timeout(10000)
     last=await page.evaluate(POLL_JS,args)
     save(variant+'_poll.json',clean(last))
     print('STATUS',variant,int(time.time()-start),json.dumps({'videos':len(last.get('videos',[])),'rejection':last.get('rejection'),'texts':last.get('texts',[])[-1:]},ensure_ascii=True)[:700],flush=True)
     if last.get('videos') or last.get('rejection'):break
    results.append({'variant':variant,'conversation':conv,'result':last});save('results.json',clean(results))
    await page.screenshot(path=str(OUT/(variant+'_result.png')),full_page=True)
    if last.get('videos'):
     response=await ctx.request.get(last['videos'][0],timeout=120000)
     if response.ok:
      (OUT/(variant+'.mp4')).write_bytes(await response.body());print('DOWNLOADED',variant,flush=True)
    if not last.get('videos'):break
  except Exception as e:
   save('error.json',{'error':str(e),'traceback':traceback.format_exc(),'sent':sent});print(traceback.format_exc(),flush=True)
   await page.screenshot(path=str(OUT/'error.png'),full_page=True)
  finally:await ctx.close()
asyncio.run(main())
