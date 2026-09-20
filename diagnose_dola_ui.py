import asyncio
import json
from pathlib import Path

from patchright.async_api import async_playwright

import config
async def main():
    account = "diagnostic"
    output = Path("dola_ui_diagnostic.json")
    screenshot = "dola_ui_diagnostic.png"
    async with async_playwright() as p:
        kwargs = {
            "headless": False,
            "args": [
                "--disable-blink-features=AutomationControlled",
                "--no-first-run",
                "--no-default-browser-check",
            ],
            "locale": "ja-JP",
            "timezone_id": "Asia/Tokyo",
        }
        if config.PROXY:
            kwargs["proxy"] = {"server": config.PROXY}
        context = await p.chromium.launch_persistent_context(
            str(Path("diagnostic_profile")), **kwargs
        )
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            page.on("console", lambda message: print(f"[browser-console] {message.type}: {message.text}", flush=True))
            page.on("pageerror", lambda error: print(f"[page-error] {error}", flush=True))
            page.on("requestfailed", lambda request: print(
                f"[request-failed] {request.method} {request.url} :: {request.failure}", flush=True
            ))
            page.on("framenavigated", lambda frame: print(
                f"[navigation] {frame.url}", flush=True
            ) if frame == page.main_frame else None)
            await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(8000)
            snapshot = await page.evaluate("""() => ({
                title: document.title,
                url: location.href,
                readyState: document.readyState,
                bodyText: (document.body?.innerText || '').slice(0, 5000),
                buttons: [...document.querySelectorAll('button, [role="button"]')]
                    .map(e => ({text: (e.innerText || '').trim(), aria: e.getAttribute('aria-label')}))
                    .filter(e => e.text || e.aria).slice(0, 100),
                inputs: [...document.querySelectorAll('input, textarea, [contenteditable="true"]')]
                    .map(e => ({tag: e.tagName, type: e.getAttribute('type'), placeholder: e.getAttribute('placeholder')}))
                    .slice(0, 50),
                links: [...document.querySelectorAll('a')]
                    .map(e => ({text: (e.innerText || '').trim(), href: e.href}))
                    .filter(e => e.text || e.href).slice(0, 50)
            })""")
            snapshot["proxy"] = config.PROXY
            output.write_text(json.dumps(snapshot, ensure_ascii=False, indent=2), encoding="utf-8")
            await page.screenshot(path=screenshot, full_page=True)
            print(json.dumps(snapshot, ensure_ascii=False, indent=2))
            print(f"Saved {output} and {screenshot}", flush=True)
            print("Browser is kept open for manual interaction for 15 minutes.", flush=True)
            for _ in range(180):
                await asyncio.sleep(5)
                print(f"[state] url={page.url} title={await page.title()!r}", flush=True)
        finally:
            await context.close()


if __name__ == "__main__":
    asyncio.run(main())
