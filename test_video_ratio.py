"""Offline regression: sidebar drafts must never receive ratio clicks."""
import asyncio
from patchright.async_api import async_playwright
import video_worker_ui as worker

async def run():
    original = worker._composer
    async def composer(page):
        return await original(page)
    worker._composer = composer
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                for mode in ('sidebar', 'fallback', 'combined', 'ambiguous', 'lost_image', 'missing_before', 'ignored', 'reset'):
                    page = await browser.new_page()
                    try:
                        await page.set_content('''<aside><button id="draft">=== TECHNICAL === 16:9 cinematic</button><button>16:9</button></aside>
                        <div class="guidance-input-surface"><div id="composer"><div contenteditable="true"> </div><button>Model 2.5</button><button id="duration">10s</button>
                        <button id="ratio" data-input-engine-actionbar-control-key="video-ratio">Ratio</button>
                        <input type="file"></div><div data-kind="image"><img src="data:image/gif;base64,R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7" alt="reference"></div></div>''')
                        await page.evaluate("""mode => {
                            const card = document.querySelector('[data-kind="image"]');
                            for (let i = 1; i < 4; i++) {
                                const clone = card.cloneNode(true); clone.querySelector('img').alt = 'reference-' + i; card.parentNode.append(clone);
                            }
                            if (mode === 'missing_before') card.remove();
                            document.querySelector('#draft').onclick = () => document.body.dataset.wrongClick = 'true';
                            let control = document.querySelector('#ratio');
                            if (mode === 'fallback') control.removeAttribute('data-input-engine-actionbar-control-key');
                            if (mode === 'combined') {
                                control.remove(); control = document.querySelector('#duration');
                            }
                            control.onclick = () => {
                                const popup = document.createElement('div');
                                popup.id = 'ratio-menu'; popup.setAttribute('role', 'menu');
                                if (mode !== 'fallback') control.setAttribute('aria-controls', popup.id);
                                const option = document.createElement('div'); option.setAttribute('role', 'menuitem');
                                option.innerHTML = '<span>16:9</span>';
                                option.onclick = () => {
                                    if (mode !== 'ignored') control.textContent = mode === 'combined' ? '10s 16:9' : 'Ratio 16:9';
                                    if (mode === 'lost_image') document.querySelector('[data-kind="image"]').remove();
                                    if (mode === 'reset') document.querySelector('#composer').innerHTML = '<div contenteditable="true"> </div><button>Chat</button>';
                                    popup.remove();
                                };
                                popup.append(option);
                                if (mode === 'ambiguous') popup.append(option.cloneNode(true));
                                document.body.append(popup);
                            };
                        }""", mode)
                        failures = {'ambiguous': 'Multiple visible', 'lost_image': 'images changed',
                                    'missing_before': 'expected=4, actual=3', 'ignored': 'did not retain', 'reset': 'composer reset'}
                        try:
                            await worker._select_video_ratio(page, '16:9', 'test', 4)
                        except RuntimeError as exc:
                            assert mode in failures and failures[mode] in str(exc), str(exc)
                        else:
                            assert mode not in failures, mode
                            assert await page.locator('[data-kind="image"] img').count() == 4
                        assert await page.locator('body').get_attribute('data-wrong-click') is None
                        print('PASS', mode, flush=True)
                    finally:
                        await page.close()
            finally:
                await browser.close()
    finally:
        worker._composer = original

if __name__ == '__main__':
    asyncio.run(run())
