"""Offline browser regressions for duration selection; no Dola requests."""
import asyncio
from browser_queue import async_playwright
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

            for combined, maximum, step, requested, valid, offset in (
                (True, 30, 1, 30, True, 0), (True, 30, 1, 15, True, 0),
                (True, 26, 1, 30, True, 4),
                (False, 30, 1, 15, True, 0), (True, 10, 1, 30, False, 0),
                (True, 30, 4, 15, False, 0),
            ):
                await page.set_content('<div id="composer"><button id="duration">10s</button></div>')
                await page.evaluate("""({combined, maximum, step, offset}) => {
                    const control = document.getElementById('duration');
                    let value = 10;
                    const render = () => control.textContent = combined ? `16:9 \u00b7 ${value}\u79d2` : `${value}s`;
                    render();
                    control.onclick = () => {
                        const menu = document.createElement('div'); menu.id='duration-menu'; menu.role='menu';
                        control.setAttribute('aria-controls',menu.id);
                        const slider = document.createElement('input'); slider.type='range';
                        slider.min=offset ? 0 : 4; slider.max=maximum; slider.step=step; slider.value=value-offset;
                        slider.oninput=()=>{ value=Number(slider.value)+offset; render(); };
                        menu.append(slider); document.body.append(menu);
                    };
                    document.addEventListener('keydown', e => {
                        if(e.key==='Escape') document.getElementById('duration-menu')?.remove();
                    });
                }""", dict(combined=combined, maximum=maximum, step=step, offset=offset))
                try:
                    await worker._select_video_duration(page, requested, 'test-slider')
                except RuntimeError as exc:
                    assert not valid, str(exc)
                    assert 'slider' in str(exc), str(exc)
                else:
                    assert valid
                    assert str(requested) in await page.locator('#duration').inner_text()
                print('PASS slider', combined, maximum, step, requested, flush=True)

        finally:
            await browser.close()

if __name__ == '__main__':
    asyncio.run(run())
