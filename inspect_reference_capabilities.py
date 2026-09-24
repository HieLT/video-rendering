import asyncio,json,re,sys,traceback
from pathlib import Path
from urllib.parse import urlsplit
from patchright.async_api import async_playwright
from browser import launch_account_context
OUT=Path('diagnostics/reference_capabilities');OUT.mkdir(exist_ok=True)
async def main():
 async with async_playwright() as p:
  context=await launch_account_context(p,'acc9',headless=True,use_extension=False)
  page=await context.new_page()
  pending=set();scripts={};configs=[]
  async def capture(resp):
   u=urlsplit(resp.url)
   try:
    if resp.request.resource_type=='script' and resp.status==200:
     body=await resp.text();name='bundle_'+str(len(scripts))+'.js';scripts[name]=u.scheme+'://'+u.netloc+u.path;(OUT/name).write_text(body,encoding='utf-8')
    elif re.search(r'skill|action_bar|model_config',u.path,re.I):
     body=await resp.json();configs.append({'path':u.path,'body':body});(OUT/'configs.json').write_text(json.dumps(configs,ensure_ascii=True),encoding='utf-8')
   except Exception: pass
  def listener(resp):
   t=asyncio.create_task(capture(resp));pending.add(t);t.add_done_callback(pending.discard)
  context.on('response',listener)
  async def snap(name):
   data=await page.evaluate('''() => ({url:location.href,text:document.body.innerText,buttons:[...document.querySelectorAll('button,[role="button"],[role="option"]')].filter(e=>e.getBoundingClientRect().width).map(e=>({text:e.innerText,aria:e.getAttribute('aria-label'),title:e.getAttribute('title')})),editors:[...document.querySelectorAll('[contenteditable="true"],textarea')].map(e=>({html:e.outerHTML})),inputs:[...document.querySelectorAll('input[type="file"]')].map(e=>({accept:e.accept,multiple:e.multiple}))})''')
   (OUT/(name+'.json')).write_text(json.dumps(data,ensure_ascii=True,indent=2),encoding='utf-8');await page.screenshot(path=str(OUT/(name+'.png')),full_page=True)
   print(json.dumps({'stage':name,'text':data['text'][-10000:],'buttons':data['buttons'],'inputs':data['inputs']},ensure_ascii=True),flush=True)
  try:
   await page.goto('https://www.dola.com/chat',wait_until='domcontentloaded',timeout=60000);await page.wait_for_timeout(6000);await snap('initial')
   scope=locals()
   print('READY',flush=True)
   while True:
    line=await asyncio.to_thread(sys.stdin.readline)
    if not line or line.strip()=='EXIT':break
    try:
     cmd=json.loads(line);body='async def step():\n'+ '\n'.join(' '+l for l in cmd.splitlines());exec(body,scope);await scope['step']()
    except Exception:traceback.print_exc()
    print('READY',flush=True)
  finally:
   if pending:await asyncio.gather(*list(pending),return_exceptions=True)
   (OUT/'scripts.json').write_text(json.dumps(scripts,indent=2),encoding='utf-8');await context.close()
asyncio.run(main())
