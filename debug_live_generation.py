import asyncio, json, sys, traceback, sqlite3
from pathlib import Path
import config
import video_worker_ui as worker
from browser_pool import BrowserPool

async def main():
    db = sqlite3.connect("file:" + config.DB_PATH + "?mode=ro", uri=True)
    active = db.execute("select id,status,account from tasks where status in ('queued','processing','pending','running')").fetchall()
    print("ACTIVE_TASKS", active, flush=True)
    if active:
        raise RuntimeError("Existing active tasks; stop diagnostic to avoid profile collision")
    original = worker._composer
    async def traced_composer(page):
        try:
            return await original(page)
        except Exception:
            state = await page.evaluate("""() => ({url:location.href, editors:[...document.querySelectorAll('textarea,[contenteditable=true]')].map(e=>({html:e.outerHTML,ancestors:(()=>{let a=[],n=e.parentElement;while(n&&a.length<8){a.push({tag:n.tagName,classes:n.className,buttons:n.querySelectorAll('button,[role=button]').length});n=n.parentElement;}return a})()}))})""")
            Path('diagnostics/live_composer.json').write_text(json.dumps(state,ensure_ascii=True,indent=2),encoding='utf-8')
            await page.screenshot(path='diagnostics/live_composer.png',full_page=True)
            raise
    worker._composer = traced_composer
    root=Path('C:/Users/user/Desktop/yasuo_phim')
    names=['1ccbfd8e-e766-47e5-82f1-7aa46997d8b2.png','6b5dfc5d-c536-47ff-a2f1-ff1176b27c3f.png','27a7637d-e01e-4461-be07-9426d6871f81.png','a8cabaed-9511-473c-924a-2add0e1cc2af.png','Video Generation.png']
    paths=[str(root/n) for n in names]
    for path in paths:
        assert Path(path).is_file(),path
    prompt=Path('C:/Users/user/.codex/attachments/ccfc5863-0565-4653-aaba-5093eaaded28/pasted-text.txt').read_text(encoding='utf-8-sig')
    pool=BrowserPool()
    result=await pool.generate_video(prompt,ratio='16:9',duration=10,model='seedance_v2.0',reference_image_paths=paths,on_conversation_id=lambda *args: print('CONVERSATION',args,flush=True))
    Path('diagnostics/live_result.json').write_text(json.dumps(result,ensure_ascii=True,indent=2),encoding='utf-8')
    print('GENERATION_COMPLETE',flush=True)

if __name__=='__main__':
    Path('diagnostics').mkdir(exist_ok=True)
    with open('diagnostics/live_generation.log','a',encoding='utf-8',buffering=1) as log:
        sys.stdout=sys.stderr=log
        try: asyncio.run(main())
        except Exception: traceback.print_exc(); sys.exit(1)
