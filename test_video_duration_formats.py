"""Offline integration fixtures for both duration control formats; no generation."""
import asyncio
from unittest.mock import patch, AsyncMock
from browser_queue import async_playwright
import video_worker_ui as worker

async def composer(page):
    return None, page.locator('#composer')

async def main():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            for mode, target in [('flow', 30), ('indexed', 30), ('indexed', 15), ('indexed', 10),
                                 ('native', 30), ('no_aria_controls', 30), ('combined_menu', 30),
                                 ('limited', 30), ('ambiguous', 30), ('ignored', 30)]:
                page = await browser.new_page()
                await page.set_content('<aside><button>30s</button></aside><div id="composer"><button>Model 2.5</button><button id="duration">16:9 · 10秒</button></div>')
                await page.evaluate("""mode => {
                    const control = document.querySelector('#duration');
                    if (mode === 'flow') control.textContent = 'Auto 10s';
                    control.onclick = () => {
                        const popup = document.createElement('div');
                        popup.id = 'settings'; popup.setAttribute('role', 'menu');
                        if (mode !== 'no_aria_controls') control.setAttribute('aria-controls', popup.id);
                        if (mode === 'combined_menu') {
                            popup.innerHTML = '<button>30秒</button>';
                            popup.firstChild.onclick = () => { control.textContent = '16:9 · 30秒'; popup.remove(); };
                        } else {
                            const native = mode === 'native';
                            const slider = document.createElement(native ? 'input' : 'span');
                            const max = mode === 'limited' ? 11 : 26;
                            if (native) { slider.type = 'range'; slider.min = 4; slider.max = 30; slider.step = 1; slider.value = 10; }
                            else {
                                slider.setAttribute('role', 'slider'); slider.tabIndex = 0; slider.textContent = 'thumb';
                                slider.setAttribute('aria-valuemin', 0); slider.setAttribute('aria-valuemax', max);
                                slider.setAttribute('aria-valuenow', 6);
                            }
                            const commit = value => { if (mode !== 'ignored') control.textContent = `16:9 · ${value}秒`; };
                            if (native) slider.oninput = () => commit(slider.value);
                            else slider.onkeydown = e => {
                                let index = Number(slider.getAttribute('aria-valuenow'));
                                if (e.key === 'Home') index = 0;
                                else if (e.key === 'ArrowRight') index = Math.min(index + 1, max);
                                else return;
                                e.preventDefault(); slider.setAttribute('aria-valuenow', index); commit(index + 4);
                            };
                            popup.append(slider);
                            if (mode === 'ambiguous') popup.append(slider.cloneNode(true));
                        }
                        if (mode === 'flow') {
                            const ratio = document.createElement('button'); ratio.textContent = '16:9';
                            ratio.onclick = () => { control.textContent = '16:9 10s'; };
                            popup.append(ratio);
                        }
                        document.body.append(popup);
                    };
                    document.addEventListener('keydown', e => {
                        if (e.key === 'Escape') document.querySelector('#settings')?.remove();
                    });
                }""", mode)
                with patch.object(worker, '_composer', composer), patch.object(worker, '_composer_snapshot', AsyncMock()), patch.object(page, 'screenshot', AsyncMock()):
                    try:
                        if mode == 'flow':
                            await worker._select_video_ratio(page, '16:9', 'offline', 0)
                        await worker._select_video_duration(page, target, 'offline')
                    except RuntimeError as exc:
                        assert mode in ('limited', 'ambiguous', 'ignored'), str(exc)
                        assert 'slider' in str(exc), str(exc)
                    else:
                        assert mode not in ('limited', 'ambiguous', 'ignored')
                        assert await page.locator('#duration').inner_text() == f'16:9 · {target}秒'
                        assert not await page.locator('#settings').count()
                print('PASS', mode, target, flush=True)
                await page.close()
        finally:
            await browser.close()

if __name__ == '__main__':
    asyncio.run(main())
