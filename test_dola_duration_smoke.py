"""Live duration setup only. Never submits generation. Explicit account arguments required."""
import asyncio
import re
import sqlite3
import sys
from patchright.async_api import async_playwright
from browser import launch_account_context
import video_worker_ui as worker

async def run(accounts):
    original_log = worker._log
    worker._log = lambda message: original_log(message) if 'composer stage=' not in message else None
    async with async_playwright() as p:
        for account in accounts:
            with sqlite3.connect('file:tasks.db?mode=ro', uri=True) as db:
                busy = db.execute("select count(*) from tasks where account=? and status='processing'", (account,)).fetchone()[0]
            if busy:
                raise RuntimeError(f'{account} has an active job')
            context = await launch_account_context(p, account, use_extension=True)
            try:
                page = context.pages[0]
                await page.goto('https://www.dola.com/chat', wait_until='domcontentloaded')
                await page.wait_for_timeout(5000)
                await worker._prepare_video_composer(page, account, False)
                _, root = await worker._composer(page)
                await root.get_by_role('button', name=re.compile(r'\u30e2\u30c7\u30eb|Model|Seedance', re.I)).first.click()
                menu = page.locator('[role="menu"][data-state="open"]:visible')
                await menu.get_by_role('menuitem').filter(has=page.get_by_text('Dreamina Seedance 2.5', exact=True)).click()
                await page.wait_for_timeout(500)
                await worker._select_video_duration(page, 30, account)
                print(f'PASS LIVE {account}: 30s retained; no generation submitted', flush=True)
            finally:
                await context.close()

if __name__ == '__main__':
    if len(sys.argv) < 2:
        raise SystemExit('Specify idle accounts to inspect; this test clears the composer draft.')
    asyncio.run(run(sys.argv[1:]))
