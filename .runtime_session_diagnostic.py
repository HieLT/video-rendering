import asyncio,json,time
from pathlib import Path
from urllib.parse import urlsplit
from browser_queue import async_playwright
from browser import launch_account_context
ACCOUNT='2c74a3fa-8440-437c-b92f-b4f4c7ac5d12'
OUT=Path('.runtime/session_diagnostic_doanmongnguyet.json')
async def main():
 events=[]; tasks=[]
 def emit(kind,**data):
  row=dict(t=round(time.monotonic()-start,2),kind=kind,**data); events.append(row); print(json.dumps(row,ensure_ascii=True),flush=True)
 async def response(r):
  u=urlsplit(r.url)
  if not u.hostname or not u.hostname.endswith('dola.com'): return
  if not any(s in u.path for s in ('alice','passport','chat','conversation')): return
  data=dict(path=u.path,status=r.status)
  try:
   j=await r.json()
   if isinstance(j,dict):
    data['result']={k:j[k] for k in ('code','message','msg','error_code') if k in j}
    d=j.get('data')
    if isinstance(d,dict): data['data_status']={k:d[k] for k in ('error_code','description','message') if k in d}
  except Exception: pass
  emit('response',**data)
 async with async_playwright() as p:
  c=await launch_account_context(p,ACCOUNT,headless=True,use_extension=False)
  try:
   page=c.pages[0] if c.pages else await c.new_page()
   page.on('response',lambda r:tasks.append(asyncio.create_task(response(r))))
   async def snapshot(stage):
    cookies=await c.cookies('https://www.dola.com')
    emit('state',stage=stage,path=urlsplit(page.url).path,cookies=[dict(name=x['name'],expires=x['expires']) for x in cookies if x['name'] in ('sessionid','sessionid_ss','sid_guard')],text=(await page.locator('body').inner_text())[:3500])
   await snapshot('before_navigation')
   await page.goto('https://www.dola.com/chat',wait_until='domcontentloaded',timeout=60000)
   await asyncio.sleep(10)
   await snapshot('loaded')
   editor=page.locator('textarea:visible, [contenteditable="true"]:visible').first
   cookies=await c.cookies('https://www.dola.com')
   if any(x['name']=='sessionid' and x['value'] for x in cookies) and await editor.count():
    await editor.fill('say hi'); await editor.press('Enter'); emit('submitted',text='say hi')
    await asyncio.sleep(20); await snapshot('after_chat')
   else: emit('skipped_chat',reason='No session cookie or visible editor')
  finally:
   await c.close()
   await asyncio.gather(*tasks,return_exceptions=True)
   OUT.write_text(json.dumps(events,ensure_ascii=False,indent=2),encoding='utf-8')
start=time.monotonic()
asyncio.run(main())
