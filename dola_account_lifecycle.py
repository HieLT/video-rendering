"""Dola deletion and re-login for Google and Facebook; selectors never depend on translated text."""
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
FACEBOOK_ICON = 'svg:has(path[fill="#0068FF"][d^="M12 2C6.203 2 1.5 6.73"])'


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


# Dola paints the CTA on an inner node (or a DIV without role=button). Live
# Dola uses oklch()/color() so rgb() regex misses; canvas fillStyle resolves it.
MARK_PAINTED_JS = """(root, mode) => {
    const ctx = document.createElement('canvas').getContext('2d');
    const parse = (c) => {
        const s = String(c || '').trim();
        if (!s || s === 'transparent' || s === 'none' || s === 'rgba(0, 0, 0, 0)') return null;
        const m = s.match(
            /rgba?\\(\\s*([\\d.]+)\\s*[, ]\\s*([\\d.]+)\\s*[, ]\\s*([\\d.]+)(?:\\s*[,/]\\s*([\\d.]+))?/i);
        if (m) return {r:+m[1], g:+m[2], b:+m[3], a: m[4] === undefined ? 1 : +m[4]};
        try {
            ctx.fillStyle = '#000000';
            ctx.fillStyle = s;
            const hex = String(ctx.fillStyle);
            if (hex.charAt(0) === '#' && hex.length >= 7) {
                return {r: parseInt(hex.slice(1, 3), 16), g: parseInt(hex.slice(3, 5), 16),
                        b: parseInt(hex.slice(5, 7), 16), a: 1};
            }
        } catch (e) {}
        return null;
    };
    const isMode = (p) => {
        if (!p || p.a < 0.4) return false;
        return mode === 'red'
            ? p.r > 160 && p.g < 140 && p.b < 140 && p.r - p.g > 40
            : p.b > 140 && p.r < 120 && p.g < 190;
    };
    const nodePainted = (n) => {
        for (const pseudo of [undefined, '::before', '::after']) {
            const st = pseudo ? getComputedStyle(n, pseudo) : getComputedStyle(n);
            if (isMode(parse(st.backgroundColor))) return true;
            const img = st.backgroundImage || '';
            if (img && img !== 'none') {
                const parts = img.match(
                    /rgba?\\([^)]+\\)|oklch\\([^)]+\\)|oklab\\([^)]+\\)|color\\([^)]+\\)|#[0-9a-fA-F]{3,8}/g) || [];
                if (parts.some(part => isMode(parse(part)))) return true;
            }
        }
        return false;
    };
    const painted = (el) => {
        if (el.getAttribute('data-disabled') === 'true') return false;
        const box = el.getBoundingClientRect();
        const minW = mode === 'red' ? 100 : 40;
        const minH = mode === 'red' ? 28 : 24;
        if (box.width < minW || box.height < minH || box.height > 100) return false;
        const st = getComputedStyle(el);
        if (st.visibility === 'hidden' || st.display === 'none' || Number(st.opacity) === 0)
            return false;
        return [el, ...el.querySelectorAll('*')].some(nodePainted);
    };
    const docs = [];
    const collect = (node) => {
        if (!node) return;
        const doc = node.nodeType === 9 ? node : node.ownerDocument;
        if (doc && !docs.includes(doc)) docs.push(doc);
        const scope = node.nodeType === 9 ? node.documentElement : node;
        try {
            for (const frame of scope.querySelectorAll('iframe')) {
                try { collect(frame.contentDocument); } catch (e) {}
            }
        } catch (e) {}
    };
    collect(root);
    for (const doc of docs) {
        doc.querySelectorAll('[data-dola-painted-action]').forEach(
            el => el.removeAttribute('data-dola-painted-action'));
    }
    const cands = [];
    const add = (scope) => {
        for (const el of scope.querySelectorAll(
            'button, [role="button"], [data-dbx-name="button"], a, [class*="danger"]'))
            cands.push(el);
        for (const el of scope.querySelectorAll('div, span')) {
            const box = el.getBoundingClientRect();
            if (box.width >= 100 && box.width <= 560 && box.height >= 28 && box.height <= 88)
                cands.push(el);
        }
    };
    add(root.nodeType === 9 ? root.documentElement : root);
    for (const doc of docs) {
        if (doc !== (root.nodeType === 9 ? root : root.ownerDocument)) add(doc.body || doc);
    }
    const hits = cands.filter(painted);
    const outer = hits.filter(el => !hits.some(o => o !== el && o.contains && o.contains(el)));
    if (!outer.length) return 0;
    outer.sort((a, b) => {
        const ab = a.getBoundingClientRect(), bb = b.getBoundingClientRect();
        return (bb.width * bb.height) - (ab.width * ab.height);
    });
    outer[0].setAttribute('data-dola-painted-action', mode);
    return outer.length;
}"""
RED_ACTION_JS = "red"
BLUE_ACTION_JS = "blue"
_FB_DELETE_DEBUG_AT = 0.0


async def unique_painted_button(root, script):
    mode = script if script in ("red", "blue") else ("blue" if "b > 150" in str(script) else "red")
    try:
        marked = await root.evaluate(MARK_PAINTED_JS, mode)
    except BrowserError as exc:
        if navigation_changed(exc):
            return None
        raise
    if not marked:
        return None
    painted = root.locator(f'[data-dola-painted-action="{mode}"]')
    return painted if await painted.count() == 1 else None


async def confirm_adult_onboarding(page):
    # Operators confirmed these accounts are 18+. Facebook OAuth can show the
    # age prompt on /chat as well as /auth/callback.
    if not is_dola_page(page):
        return False
    dialog = page.locator('[role="dialog"][data-state="open"]:visible').filter(
        has=page.locator('[data-slot="dialog-description"]').filter(has_text="18"))
    if await dialog.count() == 1:
        button = dialog.locator('[data-slot="dialog-footer"] button.bg-dbx-fill-highlight')
        if await button.count() == 1 and await button.is_visible():
            await unique_click(button)
            return True
    matches = []
    for candidate in await page.locator('[role="dialog"]:visible').all():
        try:
            if "18" in (await candidate.inner_text()):
                matches.append(candidate)
        except BrowserError as exc:
            if not navigation_changed(exc):
                raise
    if len(matches) != 1:
        return False
    highlight = matches[0].locator('button.bg-dbx-fill-highlight')
    if await highlight.count() == 1 and await highlight.is_visible():
        await unique_click(highlight)
        return True
    painted = await unique_painted_button(matches[0], BLUE_ACTION_JS)
    if painted is None:
        return False
    await unique_click(painted)
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


def is_facebook_host(page):
    try:
        host = urlsplit(page.url).hostname or ""
    except Exception:
        return False
    return host == "facebook.com" or host.endswith(".facebook.com")


def facebook_targets(context):
    targets = []
    for page in list(context.pages):
        if page.is_closed():
            continue
        if is_facebook_host(page):
            targets.append(page)
        try:
            frames = page.frames
        except Exception:
            continue
        for frame in frames:
            try:
                if frame is not page.main_frame and is_facebook_host(frame):
                    targets.append(frame)
            except Exception:
                continue
    return targets


async def facebook_step(context, account, report, consent_clicked):
    from account_import import continue_facebook_consent
    found = False
    for target in facebook_targets(context):
        found = True
        await continue_facebook_consent(target, account, consent_clicked)
    if found:
        report("waiting_facebook", "Complete Facebook sign-in or verification in the browser")


async def facebook_login_root(page):
    dialog = page.locator('[role="dialog"]:visible').last
    if await dialog.count():
        return dialog
    content = page.locator('[data-testid="login_content"]')
    if await content.count():
        return content
    return None


async def dola_login_visible(page):
    if page.is_closed() or not is_dola_page(page):
        return False
    try:
        if await page.get_by_test_id("to_login_button").is_visible():
            return True
    except BrowserError as exc:
        if not navigation_changed(exc):
            raise
    try:
        if await page.get_by_test_id("login_content").is_visible():
            return True
    except BrowserError as exc:
        if not navigation_changed(exc):
            raise
    if not hasattr(page, "locator"):
        return False
    try:
        root = await facebook_login_root(page)
        if root is None:
            return False
        if await root.locator(FACEBOOK_ICON).count() and await facebook_inline_delete_button(page) is None:
            return True
    except BrowserError as exc:
        if not navigation_changed(exc):
            raise
    return False


async def facebook_inline_delete_button(page):
    # Facebook confirms deletion in a /chat overlay. The CTA is often a painted
    # DIV / inner span, and the overlay may omit role=dialog.
    if page.is_closed() or not is_dola_page(page):
        return None
    scopes = [page]
    try:
        scopes.extend(frame for frame in page.frames if frame is not page.main_frame)
    except Exception:
        pass
    for scope in scopes:
        found = await _facebook_delete_in_scope(scope)
        if found is not None:
            return found
    global _FB_DELETE_DEBUG_AT
    if time.monotonic() - _FB_DELETE_DEBUG_AT > 20:
        _FB_DELETE_DEBUG_AT = time.monotonic()
        try:
            info = await page.evaluate("""() => {
                const nodes = [...document.querySelectorAll('button, [role="button"], [data-dbx-name="button"], div, span')];
                const items = nodes.map(n => {
                    const b = n.getBoundingClientRect();
                    const st = getComputedStyle(n);
                    return {tag: n.tagName, role: n.getAttribute('role'),
                        bg: st.backgroundColor, img: (st.backgroundImage||'').slice(0,80),
                        w: Math.round(b.width), h: Math.round(b.height),
                        cls: String(n.className).slice(0, 70)};
                }).filter(x => x.w >= 80 && x.h >= 24 && x.h <= 90).slice(0, 25);
                return {
                    dialogs: document.querySelectorAll('[role="dialog"], [role="alertdialog"]').length,
                    frames: window.frames.length,
                    items
                };
            }""")
            print(f"[facebook-delete] no confirm control url={page.url} {info}", flush=True)
        except BrowserError:
            pass
    return None


async def _facebook_delete_in_scope(scope):
    roots = []
    overlays = scope.locator(
        '[role="dialog"], [role="alertdialog"], [data-slot="dialog-content"]')
    try:
        count = await overlays.count()
    except BrowserError:
        return None
    for index in range(count):
        roots.append((overlays.nth(index), True))
    roots.append((scope.locator("body"), False))
    for dialog, skip_warning in roots:
        try:
            if await dialog.locator('[data-slot="dialog-title"][status="warning"]').count():
                continue
            if await dialog.locator(GOOGLE_LOGIN).count():
                continue
            danger = dialog.locator(
                'button[data-dbx-name="button"].bg-dbx-function-danger, '
                'button.bg-dbx-function-danger, button[class*="type-danger"], button[class*="function-danger"]')
            if await danger.count() == 1 and await danger.is_visible():
                return danger
            painted = await unique_painted_button(dialog, RED_ACTION_JS)
            if painted is not None:
                return painted
        except BrowserError as exc:
            if navigation_changed(exc):
                continue
            raise
    return None


async def click_facebook_login(page):
    root = await facebook_login_root(page)
    if root is None:
        return False
    named = root.locator('[aria-label*="facebook" i], [title*="facebook" i], button:has-text("Facebook")')
    for candidate in await named.all():
        if await candidate.is_visible():
            return await login_click(candidate)
    icon = root.locator(FACEBOOK_ICON)
    if await icon.count() != 1 or not await icon.first.is_visible():
        return False
    target = icon.locator('xpath=ancestor::*[@data-disabled or self::button or @role="button"][1]')
    return await login_click(target)


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


async def wait_for_logged_out_page(context, timeout=60, account_type="google",
                                  account="", report=None):
    consent_clicked = set()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        if account_type == "facebook":
            try:
                await asyncio.wait_for(
                    facebook_step(context, account, report or (lambda *a: None), consent_clicked),
                    timeout=8)
            except asyncio.TimeoutError:
                pass
        for candidate in list(context.pages):
            if candidate.is_closed() or urlsplit(candidate.url).hostname not in ("dola.com", "www.dola.com"):
                continue
            try:
                if await dola_login_visible(candidate):
                    return candidate
                if account_type == "facebook":
                    confirm = await facebook_inline_delete_button(candidate)
                    if confirm is not None:
                        await unique_click(confirm)
            except BrowserError as exc:
                if not candidate.is_closed() and not navigation_changed(exc):
                    raise
        await asyncio.sleep(0.5)
    raise RuntimeError("Logged-out Dola page was not detected; resume login without deleting again")


async def relogin(context, page, email, report, timeout=600, account_type="google", account=""):
    provider = "Facebook" if account_type == "facebook" else "Google"
    report("logging_in", f"Signing in again with the saved {provider} browser session")
    existing_pages = set(context.pages) - {page}
    # The logout tab may still have redirects queued. A fresh page avoids
    # interrupting those redirects with goto and leaving a stale login DOM.
    if urlsplit(page.url).scheme in ("http", "https"):
        existing_pages.add(page)
        page = await context.new_page()
    await page.goto("https://www.dola.com/chat", wait_until="domcontentloaded", timeout=60000)
    clicked_pages = set()
    consent_clicked = set()
    deadline = time.monotonic() + timeout
    while time.monotonic() < deadline:
        candidates = [pg for pg in context.pages if is_dola_page(pg) and pg not in existing_pages]
        if not candidates and page.is_closed():
            raise RuntimeError("Login browser was closed")
        try:
            if account_type == "facebook":
                await facebook_step(context, account or email, report, consent_clicked)
            else:
                await google_step(context, email, report)
        except BrowserError as exc:
            if not navigation_changed(exc):
                raise
        for candidate in [pg for pg in context.pages if is_dola_page(pg)]:
            try:
                if await confirm_adult_onboarding(candidate):
                    report("onboarding", "Confirmed adult account onboarding; waiting for Dola session")
            except BrowserError as exc:
                if not candidate.is_closed() and not navigation_changed(exc):
                    raise
        for candidate in candidates:
            try:
                if await login_page_step(context, candidate, clicked_pages, report, account_type=account_type):
                    return
            except BrowserError as exc:
                if not candidate.is_closed() and not navigation_changed(exc):
                    raise
        await asyncio.sleep(1)
    raise RuntimeError(f"Login timed out; use Resume {provider} login to continue")


async def login_page_step(context, candidate, clicked_pages, report, account_type="google"):
    if await confirm_adult_onboarding(candidate):
        report("onboarding", "Confirmed adult account onboarding; waiting for Dola session")
        return False
    if (cookie_value(await context.cookies("https://www.dola.com"), "sessionid")
            and await candidate.get_by_test_id("sidebar_footer_setting_entry").is_visible()):
        return True
    if candidate in clicked_pages:
        return False
    if account_type == "facebook":
        await candidate.bring_to_front()
        if await click_facebook_login(candidate):
            clicked_pages.add(candidate)
        elif not await candidate.locator('[role="dialog"]:visible').count():
            if await candidate.get_by_test_id("to_login_button").is_visible():
                await login_click(candidate.get_by_test_id("to_login_button"))
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


async def delete_and_relogin(account, email, report, resume=False, account_type="google"):
    provider = "Facebook" if account_type == "facebook" else "Google"
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
                warning = page.locator('[data-slot="dialog-title"][status="warning"]')
                for _ in range(20):
                    if not await warning.count() or not await warning.first.is_visible():
                        break
                    await asyncio.sleep(0.25)
                report("waiting_delete_tab", f"Waiting for the Dola deletion tab and {provider} verification")
                deadline = time.monotonic() + 600
                target = None
                consent_clicked = set()
                while time.monotonic() < deadline:
                    if account_type == "facebook":
                        try:
                            await asyncio.wait_for(
                                facebook_step(context, account, report, consent_clicked),
                                timeout=12)
                        except asyncio.TimeoutError:
                            report("waiting_facebook",
                                   "Complete Facebook sign-in or verification in the browser")
                        oauth_pending = bool(facebook_targets(context)) and not consent_clicked
                        for candidate in list(context.pages):
                            if oauth_pending or candidate.is_closed() or not is_dola_page(candidate):
                                continue
                            try:
                                confirm = await facebook_inline_delete_button(candidate)
                            except BrowserError as exc:
                                if candidate.is_closed() or navigation_changed(exc):
                                    continue
                                raise
                            if confirm is None:
                                continue
                            report("confirming_delete", "Submitting Dola account deletion")
                            checkpoint(account, "submitting")
                            await unique_click(confirm)
                            report("waiting_logout", "Deletion submitted; waiting for Dola to show the logged-out page")
                            page = await wait_for_logged_out_page(
                                context, timeout=180 if account_type == "facebook" else 60,
                                account_type=account_type, account=account, report=report)
                            checkpoint(account, "deleted")
                            break
                        if read_checkpoint(account).get("phase") == "deleted":
                            break
                        logged_out = None
                        for pg in list(context.pages):
                            if pg.is_closed() or not await dola_login_visible(pg):
                                continue
                            logged_out = pg
                            break
                        if logged_out:
                            page = logged_out
                            checkpoint(account, "deleted")
                            break
                    else:
                        await google_step(context, email, report)
                    target = next((pg for pg in reversed(context.pages) if is_dola_page(pg)
                                   and (pg not in previous_pages or pg is page)
                                   and urlsplit(pg.url).path.rstrip("/") == "/delete-account"), None)
                    if target and await target.locator(FINAL_DELETE).is_visible():
                        break
                    await asyncio.sleep(1)
                else:
                    raise RuntimeError(f"{provider} deletion verification timed out")
                if read_checkpoint(account).get("phase") != "deleted":
                    report("confirming_delete", "Submitting Dola account deletion")
                    await target.bring_to_front()
                    # Persist before irreversible click: failures must not silently retry deletion.
                    checkpoint(account, "submitting")
                    await unique_click(target.locator(FINAL_DELETE))
                    report("waiting_logout", "Deletion submitted; waiting for Dola to show the logged-out page")
                    page = await wait_for_logged_out_page(
                        context, timeout=180 if account_type == "facebook" else 60,
                        account_type=account_type, account=account, report=report)
                    # The visible logged-out UI is sufficient to start login. Cookies may
                    # remain until the next navigation; never treat that as a login blocker.
                    checkpoint(account, "deleted")
            await relogin(context, page, email, report, account_type=account_type, account=account)
        finally:
            await context.close()
