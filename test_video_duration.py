"""Offline browser regressions for duration selection; no Dola requests."""
import asyncio
from patchright.async_api import async_playwright
import video_worker_ui as worker

async def run():
    async def composer(page):
        return None, page.locator('#composer')
    async def noop(*args, **kwargs):
        pass
    worker._composer = composer
    worker._composer_snapshot = noop
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            page.screenshot = noop
            for mode in ('selected', 'duplicate_history', 'detached_popup', 'ambiguous'):
                await page.set_content('<div>30s</div><div id="composer"><button id="duration">10s</button></div>')
                await page.evaluate("""mode => {
                    let opens = 0;
                    const control = document.getElementById('duration');
                    if (mode === 'selected') control.textContent = '30s';
                    control.addEventListener('click', () => {
                        opens++;
                        const menu = document.createElement('div');
                        menu.id = 'menu-' + opens;
                        menu.setAttribute('role', 'menu');
                        control.setAttribute('aria-controls', menu.id);
                        control.dataset.opens = opens;
                        const item = document.createElement('div');
                        item.setAttribute('role', 'menuitem');
                        item.innerHTML = '<span>30s</span>';
                        item.addEventListener('click', () => {
                            control.textContent = '30s';
                            menu.remove();
                        });
                        menu.append(item);
                        if (mode === 'ambiguous') menu.append(item.cloneNode(true));
                        document.body.append(menu);
                        if (mode === 'detached_popup' && opens === 1) {
                            menu.animate([{transform:'translateX(0px)'}, {transform:'translateX(100px)'}], {duration:1000, iterations:Infinity});
                            setTimeout(() => menu.remove(), 450);
                        }
                    });
                    document.addEventListener('keydown', e => {
                        if (e.key === 'Escape') document.querySelectorAll('[role="menu"]').forEach(e => e.remove());
                    });
                }""", mode)
                try:
                    await worker._select_video_duration(page, 30, 'test')
                except RuntimeError as exc:
                    assert mode == 'ambiguous' and 'Multiple visible' in str(exc), str(exc)
                else:
                    assert mode != 'ambiguous'
                    assert await page.locator('#duration').inner_text() == '30s'
                    if mode == 'detached_popup':
                        assert await page.locator('#duration').get_attribute('data-opens') == '2'
                    if mode == 'selected':
                        assert await page.locator('#duration').get_attribute('data-opens') is None
                print('PASS', mode, flush=True)
        finally:
            await browser.close()

if __name__ == '__main__':
    asyncio.run(run())
