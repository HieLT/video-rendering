"""Prepare a video draft and inspect 30s selection without submitting it."""
import asyncio
import re

from patchright.async_api import async_playwright

from browser import launch_account_context
from video_worker_ui import (
    _composer, _prepare_video_composer, _fill_video_prompt, attach_reference_images,
)


async def prepare_draft(page, account, prompt, model, ratio, image_paths, report):
    report("Opening video composer")
    await page.goto("https://www.dola.com/chat", timeout=60000,
                    wait_until="domcontentloaded")
    await _prepare_video_composer(page, account, bool(image_paths))
    if image_paths:
        report("Uploading reference images")
        await attach_reference_images(page, image_paths)
    report("Filling prompt")
    await _fill_video_prompt(page, prompt, account)
    report("Selecting model and ratio")
    _, root = await _composer(page)
    await root.get_by_text("モデル", exact=True).click(timeout=5000)
    option = "Dreamina Seedance 2.5" if model == "seedance-2.5" else "Dreamina Seedance 2.0高速"
    await page.get_by_text(option, exact=True).last.click(timeout=5000)
    if ratio:
        await root.get_by_text("比率", exact=True).click(timeout=5000)
        await page.get_by_text(ratio, exact=True).last.click(timeout=5000)
    report("Trying to select 30s")
    duration = root.get_by_text(re.compile(r"^\d+s$")).first
    await duration.wait_for(state="visible", timeout=10000)
    if (await duration.inner_text()).strip() != "30s":
        await duration.click(timeout=5000)
        await page.get_by_text("30s", exact=True).last.click(timeout=5000)
    # Verify the composer selection after the menu has closed.
    for _ in range(10):
        if (await duration.inner_text(timeout=1000)).strip() == "30s":
            break
        await page.wait_for_timeout(500)
    else:
        raise RuntimeError("Clicked 30s but the composer did not confirm 30s")
    report("Selected 30s successfully. Draft only; no video submitted.")


async def run_preview(account, prompt, model, ratio, image_paths, job):
    def report(message):
        job["message"] = message
        print(f"[try-30s:{account}] {message}", flush=True)

    async with async_playwright() as playwright:
        context = await launch_account_context(
            playwright, account, headless=False, use_extension=True)
        closed = asyncio.Event()
        context.on("close", lambda *_: closed.set())
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await prepare_draft(page, account, prompt, model, ratio, image_paths, report)
            job["result"] = "success"
        except Exception as exc:
            job["result"] = "failed"
            job["error"] = f"{type(exc).__name__}: {exc}"[:1000]
            report("Test failed: " + job["error"])
        # Keep the browser alive even if navigation/upload/selection failed.
        # Only the user closing Chrome (or server shutdown) ends the session.
        job["message"] += " Close Chrome when you finish inspecting."
        await closed.wait()
