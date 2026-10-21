import asyncio,json,re
from pathlib import Path
from browser_queue import async_playwright
from browser import launch_account_context
async def main():
 rows=[]; jobs=[]
 async def inspect(r):
  if r.request.resource_type!='script': return
  try: s=await r.text()
  except Exception: return
  for m in list(re.finditer(r'high demand|high_demand|\.logout\(|passport/web/logout|SESSION_EXPIRED|session expired',s,re.I))[:25]:
   rows.append({'file':r.url.split('?')[0],'snippet':s[max(0,m.start()-450):m.end()+450]})
 async with async_playwright() as p:
  c=await launch_account_context(p,'2c74a3fa-8440-437c-b92f-b4f4c7ac5d12',headless=True)
  try:
   page=c.pages[0]; page.on('response',lambda r: jobs.append(asyncio.create_task(inspect(r))))
   await page.goto('https://www.dola.com/chat',wait_until='domcontentloaded'); await asyncio.sleep(8)
   await asyncio.gather(*jobs,return_exceptions=True)
  finally: await c.close()
 Path('.runtime/dola_logout_frontend.json').write_text(json.dumps(rows,ensure_ascii=False,indent=2),encoding='utf-8')
 for row in rows: print(json.dumps(row,ensure_ascii=True))
asyncio.run(main())
