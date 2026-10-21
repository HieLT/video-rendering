"""Google-only Dola deletion and re-login; selectors never depend on translated text."""
import asyncio
import json
import time
from pathlib import Path
from urllib.parse import urlsplit
from patchright.async_api import Error as BrowserError, TimeoutError as BrowserTimeout

from browser_queue import async_playwright
from browser import launch_account_context, cookie_value

CONFIRM = ('[role="dialog"][data-state="open"]:not([aria-hidden="true"]):has('
           '[data-slot="dialog-title"][status="warning"]) '
           '[data-slot="dialog-footer"] button[data-dbx-name="button"].bg-dbx-function-danger')
FINAL_DELETE = ('[class*="account-deletion-panel-"] [class*="confirm-button-"]'
                '[class*="type-danger-"][data-disabled="false"]')
GOOGLE_LOGIN = ('[data-testid="login_content"] button[data-dbx-name="button"]'
                ':not([data-testid="modal_close"])')


def is_dola_page(page):
    return (not page.is_closed()
            and urlsplit(page.url).hostname in ("dola.com", "www.dola.com"))


def navigation_changed(exc):
    return any(text in str(exc).lower() for text in (
        "execution context was destroyed", "cannot find context",
        "interrupted by another navigation", "frame was detached"))


async def login_click(locator):
    # Login overlays can open between the visibility check and the click.
    # Re-read the UI on the next poll instead of failing the whole recovery.
    try:
        await locator.click(timeout=1500)
        return True
    except BrowserTimeout:
        return False


async def confirm_adult_onboarding(page):
    # Account owners confirmed by the operator to be 18+. Only handle the
    # age prompt on the OAuth callback, never arbitrary confirmation dialogs.
    if not is_dola_page(page) or urlsplit(page.url).path.rstrip("/") != "/auth/callback":
        return False
    dialog = page.locator('[role="dialog"][data-state="open"]:visible').filter(
        has=page.locator('[data-slot="dialog-description"]').filter(has_text="18"))
    if await dialog.count() != 1:
        return False
    button = dialog.locator('[data-slot="dialog-footer"] button.bg-dbx-fill-highlight')
    if await button.count() != 1 or not await button.is_visible():
        return False
    await unique_click(button)
    return True


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
                if await candidate.get_by_test_id("login_content").is_visible():
                    return candidate
            except BrowserError as exc:
                if not candidate.is_closed() and not navigation_changed(exc):
                    raise
        await asyncio.sleep(0.5)
    raise RuntimeError("Logged-out Dola page was not detected; resume login without deleting again")


async def relogin(context, page, email, report, timeout=600):
    report("logging_in", "Signing in again with the saved Google browser session")
    existing_pages = set(context.pages) - {page}
    # The logout tab may still have redirects queued. A fresh page avoids
    # interrupting those redirects with goto and leaving a stale login DOM.
    if urlsplit(page.url).scheme in ("http", "https"):
        existing_pages.add(page)
        page = await context.new_page()
    await page.goto("https://www.dola.com/chat", wait_until="domcontentloaded", timeout=60000)
    clicked_pages = set()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        candidates = [pg for pg in context.pages if is_dola_page(pg) and pg not in existing_pages]
        if not candidates and page.is_closed():
            raise RuntimeError("Login browser was closed")
        try:
            await google_step(context, email, report)
        except BrowserError as exc:
            if not navigation_changed(exc):
                raise
        for candidate in candidates:
            try:
                if await login_page_step(context, candidate, clicked_pages, report):
                    return
            except BrowserError as exc:
                if not candidate.is_closed() and not navigation_changed(exc):
                    raise
        await asyncio.sleep(1)
    raise RuntimeError("Login timed out; use Resume Google login to continue")


async def login_page_step(context, candidate, clicked_pages, report):
    if await confirm_adult_onboarding(candidate):
        report("onboarding", "Confirmed adult account onboarding; waiting for Dola session")
        return False
    if (cookie_value(await context.cookies("https://www.dola.com"), "sessionid")
            and await candidate.get_by_test_id("sidebar_footer_setting_entry").is_visible()):
        return True
    if candidate in clicked_pages:
        return False
    google = candidate.locator(GOOGLE_LOGIN)
    if await google.count() == 1 and await google.is_visible():
        await candidate.bring_to_front()
        if await login_click(google):
            clicked_pages.add(candidate)
    elif not await candidate.locator('[role="dialog"]:visible').count():
        if await candidate.get_by_test_id("to_login_button").is_visible():
            await login_click(candidate.get_by_test_id("to_login_button"))
    return False

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
                previous_pages = set(context.pages)
                await unique_click(page.locator(CONFIRM))
                report("waiting_delete_tab", "Waiting for the Dola deletion tab and Google verification")
                deadline = time.monotonic() + 600
                target = None
                while time.monotonic() < deadline:
                    await google_step(context, email, report)
                    target = next((pg for pg in reversed(context.pages) if is_dola_page(pg)
                                   and (pg not in previous_pages or pg is page)
                                   and urlsplit(pg.url).path.rstrip("/") == "/delete-account"), None)
                    if target and await target.locator(FINAL_DELETE).is_visible():
                        break
                    await asyncio.sleep(1)
                else:
                    raise RuntimeError("Google deletion verification timed out")
                report("confirming_delete", "Submitting Dola account deletion")
                await target.bring_to_front()
                # Persist before irreversible click: failures must not silently retry deletion.
                checkpoint(account, "submitting")
                await unique_click(target.locator(FINAL_DELETE))
                report("waiting_logout", "Deletion submitted; waiting for Dola to show the logged-out page")
                page = await wait_for_logged_out_page(context)
                # The visible logged-out UI is sufficient to start login. Cookies may
                # remain until the next navigation; never treat that as a login blocker.
                checkpoint(account, "deleted")
            await relogin(context, page, email, report)
        finally:
            await context.close()
