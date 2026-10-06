"""Google-only Dola deletion and re-login; selectors never depend on translated text."""
import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlsplit

from browser_queue import async_playwright
from browser import launch_account_context, cookie_value

CONFIRM = ('[role="dialog"][data-state="open"]:not([aria-hidden="true"]):has('
           '[data-slot="dialog-title"][status="warning"]) '
           '[data-slot="dialog-footer"] button[data-dbx-name="button"].bg-dbx-function-danger')
FINAL_DELETE = ('[class*="account-deletion-panel-"] [class*="confirm-button-"]'
                '[class*="type-danger-"][data-disabled="false"]')
GOOGLE_LOGIN = ('[data-testid="login_content"] button[data-dbx-name="button"]'
                ':not([data-testid="modal_close"])')


def checkpoint_path(account):
    root = Path("accounts").resolve()
    profile = (root / account).resolve()
    if profile.parent != root:
        raise ValueError("Invalid account profile")
    return profile / "dola_delete_state.json"


def read_checkpoint(account):
    path = checkpoint_path(account)
    return json.loads(path.read_text(encoding="utf-8")) if path.exists() else {}


def checkpoint(account, phase):
    path = checkpoint_path(account)
    temp = path.with_suffix(".tmp")
    temp.write_text(json.dumps({"phase": phase, "updated_at": time.time()}), encoding="utf-8")
    temp.replace(path)


async def unique_click(locator):
    await locator.wait_for(state="visible", timeout=30000)
    if await locator.count() != 1:
        raise RuntimeError("Ambiguous control; automation stopped")
    await locator.click(timeout=15000)


async def confirm_age(context, report):
    # Owner confirmed 18+. Limit to Dola OAuth onboarding, never generic dialogs.
    for page in list(context.pages):
        if page.is_closed():
            continue
        url = urlsplit(page.url)
        if url.hostname not in ("www.dola.com", "dola.com") or url.path != "/auth/callback":
            continue
        dialog = page.locator('[role="dialog"][data-state="open"]:not([aria-hidden="true"])').filter(
            has=page.locator('h2[data-slot="dialog-title"]'))
        if await dialog.count() != 1:
            continue
        buttons = dialog.locator('[data-slot="dialog-footer"] button[data-dbx-name="button"]')
        confirm = buttons.locator('xpath=self::button[contains(concat(" ", normalize-space(@class), " "), " bg-dbx-fill-highlight ")]')
        if (await buttons.count() == 2 and await confirm.count() == 1
                and await dialog.locator('[data-slot="dialog-close"]').count() == 1
                and await confirm.is_visible()):
            report("confirming_age", "Confirming age as authorized by the account owner")
            await unique_click(confirm)


async def google_step(context, email, report):
    for page in list(context.pages):
        if page.is_closed() or urlsplit(page.url).hostname != "accounts.google.com":
            continue
        # Compare attribute values in Python, never interpolate account input into CSS.
        choices = page.locator("[data-identifier]")
        matches = [el for el in await choices.all()
                   if (await el.get_attribute("data-identifier") or "").casefold() == email.casefold()]
        if len(matches) == 1 and await matches[0].is_visible():
            await matches[0].click()
        else:
            report("waiting_google", "Complete Google sign-in or verification in the browser")


async def wait_for_logged_out_page(context, timeout=60):
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        for candidate in list(context.pages):
            if candidate.is_closed() or urlsplit(candidate.url).hostname not in ("dola.com", "www.dola.com"):
                continue
            try:
                if await candidate.get_by_test_id("to_login_button").is_visible():
                    return candidate
            except Exception:
                if not candidate.is_closed():
                    raise
        await asyncio.sleep(0.5)
    raise RuntimeError("Logged-out Dola page was not detected; resume login without deleting again")


async def relogin(context, page, email, report, timeout=600):
    report("logging_in", "Signing in again with the saved Google browser session")
    await page.goto("https://www.dola.com/chat", wait_until="domcontentloaded", timeout=60000)
    clicked = False
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if page.is_closed():
            raise RuntimeError("Login browser was closed")
        if (cookie_value(await context.cookies("https://www.dola.com"), "sessionid")
                and await page.get_by_test_id("sidebar_footer_setting_entry").is_visible()):
            return
        await google_step(context, email, report)
        await confirm_age(context, report)
        if not clicked:
            google = page.locator(GOOGLE_LOGIN)
            if await google.count() == 1 and await google.is_visible():
                await unique_click(google)
                clicked = True
            elif await page.get_by_test_id("to_login_button").is_visible():
                await unique_click(page.get_by_test_id("to_login_button"))
        if await page.locator('[role="dialog"]:visible').count():
            report("waiting_login", "Complete any Google verification or onboarding in the browser")
        await asyncio.sleep(1)
    raise RuntimeError("Login timed out; use Resume Google login to continue")


async def delete_and_relogin(account, email, report, resume=False):
    phase = read_checkpoint(account).get("phase")
    if resume:
        if phase not in ("deleted", "submitting"):
            raise RuntimeError("No interrupted deletion to resume")
    elif phase in ("submitting", "deleted"):
        raise RuntimeError("Previous deletion needs recovery; do not delete again")
    async with async_playwright() as p:
        context = await launch_account_context(p, account, headless=False)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            if not resume:
                report("deleting", "Opening Dola account deletion")
                await page.goto("https://www.dola.com/chat", wait_until="domcontentloaded", timeout=60000)
                if not cookie_value(await context.cookies("https://www.dola.com"), "sessionid"):
                    raise RuntimeError("Dola session expired; sign in before deleting")
                for tid in ("sidebar_footer_setting_entry", "account_manage", "delete_account"):
                    await unique_click(page.get_by_test_id(tid))
                await unique_click(page.locator(CONFIRM))
                deadline = time.monotonic() + 600
                target = None
                while time.monotonic() < deadline:
                    await google_step(context, email, report)
                    target = next((pg for pg in context.pages if not pg.is_closed()
                                   and urlsplit(pg.url).hostname == "www.dola.com"
                                   and urlsplit(pg.url).path == "/delete-account"), None)
                    if target and await target.locator(FINAL_DELETE).is_visible():
                        break
                    await asyncio.sleep(1)
                else:
                    raise RuntimeError("Google deletion verification timed out")
                report("confirming_delete", "Submitting Dola account deletion")
                # Persist before irreversible click: failures must not silently retry deletion.
                checkpoint(account, "submitting")
                await unique_click(target.locator(FINAL_DELETE))
                page = await wait_for_logged_out_page(context)
                # The visible logged-out UI is sufficient to start login. Cookies may
                # remain until the next navigation; never treat that as a login blocker.
                checkpoint(account, "deleted")
            await relogin(context, page, email, report)
        finally:
            await context.close()
