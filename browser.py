"""Patchright persistent context launcher: Explicit proxy and anti-detection parameters."""
import asyncio
from urllib.parse import urlsplit
from pathlib import Path

import config

ACTIVE_CONTEXTS = {}


async def focus_account_context(account: str) -> bool:
    entry = ACTIVE_CONTEXTS.get(account)
    if not entry:
        return False
    context, headless = entry
    if headless:
        raise RuntimeError("Account browser is running headless and cannot be displayed")
    pages = [page for page in context.pages if not page.is_closed()]
    if not pages:
        return False
    page = next((page for page in pages if "dola.com" in page.url), pages[0])
    await page.bring_to_front()
    return True


class BrowserClosedByUserError(RuntimeError):
    """Submission browser disappeared before acceptance."""


def is_browser_closed_error(error):
    seen=set()
    while error is not None and id(error) not in seen:
        seen.add(id(error))
        if isinstance(error, BrowserClosedByUserError) or type(error).__name__ == 'TargetClosedError':
            return True
        if 'Target page, context or browser has been closed' in str(error):
            return True
        error=error.__cause__ or error.__context__
    return False


class AccountSessionExpiredError(RuntimeError):
    """Dola explicitly requires a new login."""

async def ensure_account_session(page, context):
    import re
    login = page.get_by_text(re.compile(r"^(Log in|Log In|Sign in|Sign In|\u30ed\u30b0\u30a4\u30f3)$"))
    visible = any([await item.is_visible() for item in await login.all()])
    if not visible:
        return
    cookies = await context.cookies("https://www.dola.com")
    url = urlsplit(page.url)
    login_page = url.hostname == "www.dola.com" and url.path.rstrip("/") in ("/login", "/passport/login")
    if login_page or not cookie_value(cookies, "sessionid"):
        raise AccountSessionExpiredError("Dola requires login again")


CHAT_OPEN_LOCKS = {}

async def focus_task_conversation(account: str, conversation_id: str) -> bool:
    """Reuse the exact chat tab, without navigating an automation-owned page."""
    async with CHAT_OPEN_LOCKS.setdefault(account, asyncio.Lock()):
        entry = ACTIVE_CONTEXTS.get(account)
        if not entry:
            return False
        context, headless = entry
        if headless:
            raise RuntimeError("Account browser is running headless and cannot be displayed")
        target_path = "/chat/" + str(conversation_id)
        for page in context.pages:
            if page.is_closed():
                continue
            url = urlsplit(page.url)
            if url.hostname == "www.dola.com" and url.path.rstrip("/") == target_path:
                await page.bring_to_front()
                return True
        page = await context.new_page()
        try:
            await page.goto("https://www.dola.com" + target_path, timeout=60000,
                            wait_until="domcontentloaded")
            await page.bring_to_front()
        except Exception:
            await page.close()
            raise
        return True


LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]


def account_launch_args(profile_dir: Path) -> list[str]:
    """Use the same Chrome profile as interactive login, including secondary profiles."""
    import json
    profile = "Default"
    try:
        state = json.loads((profile_dir / "Local State").read_text(encoding="utf-8"))
        candidate = state.get("profile", {}).get("last_used", "Default")
        if (isinstance(candidate, str) and candidate
                and candidate not in (".", "..")
                and not any(char in candidate for char in '/\\:')
                and (profile_dir / candidate).is_dir()):
            profile = candidate
    except (OSError, ValueError, AttributeError, TypeError):
        pass
    return [*LAUNCH_ARGS, f"--profile-directory={profile}"]


async def _ready_extension(context):
    import asyncio
    for _ in range(40):
        for worker in context.service_workers:
            if worker.url.startswith('chrome-extension://') and worker.url.endswith('/service-worker-v2.js'):
                if await worker.evaluate("""() => {
                    if (typeof ensureAttached !== 'function' || typeof patchActionBarDuration !== 'function') return false;
                    const probe = {key:'video-duration', lower_bound:4, upper_bound:10, step_length:1};
                    return JSON.parse(patchActionBarDuration(JSON.stringify(probe))).upper_bound === 30;
                }"""):
                    return worker
        await asyncio.sleep(0.2)
    raise RuntimeError("Dola30 background worker did not start")


async def _reload_extension(context):
    page = await context.new_page()
    session = None
    try:
        await page.goto('chrome://extensions')
        session = await context.new_cdp_session(page)
        result = await session.send('Runtime.evaluate', {
            'expression': """new Promise((resolve, reject) => {
                chrome.developerPrivate.getExtensionsInfo({}, entries => {
                    const matches = entries.filter(e => e.name === 'Dola Studio Profile Extension');
                    if (matches.length !== 1) { reject(new Error('Dola30 extension not uniquely installed')); return; }
                    chrome.developerPrivate.reload(matches[0].id, {}, () => {
                        const error = chrome.runtime.lastError;
                        if (error) reject(new Error(error.message)); else resolve(true);
                    });
                });
            })""",
            'awaitPromise': True, 'returnByValue': True,
        })
        if result.get('exceptionDetails') or result.get('result', {}).get('value') is not True:
            raise RuntimeError('Could not reload Dola30 extension')
    finally:
        if session:
            await session.detach()
        await page.close()


async def _attach_extension_before_navigation(context, worker):
    # Enable response interception before Dola can load its action bar config.
    import uuid
    marker = 'https://www.dola.com/?dola30_bootstrap=' + uuid.uuid4().hex
    page = context.pages[0] if context.pages else await context.new_page()
    async def bootstrap(route):
        await route.fulfill(status=200, content_type='text/html', body='<html><body></body></html>')
    await page.route(marker, bootstrap)
    try:
        await page.goto(marker, wait_until='domcontentloaded', timeout=15000)
    finally:
        await page.unroute(marker, bootstrap)
    attached = await worker.evaluate("""async marker => {
        const tabs = await chrome.tabs.query({});
        const tab = tabs.find(t => t.url === marker);
        if (!tab) return false;
        await ensureAttached(tab.id);
        return attachedTabs.has(tab.id);
    }""", marker)
    if not attached:
        raise RuntimeError('Dola30 could not attach response interception to generation tab')


async def launch_account_context(p, account: str, headless: bool = None, use_extension: bool = False):
    """Launches accounts/<account> profile, returns BrowserContext. Caller must close.

    p: async_playwright() instance
    headless: None = uses config.HEADLESS
    """
    profile_dir = Path("accounts") / account
    if not profile_dir.exists():
        raise FileNotFoundError(
            f"Account profile does not exist: {profile_dir} (run python add_account.py {account} first)"
        )
    launch_headless = config.HEADLESS if headless is None else headless
    args = account_launch_args(profile_dir)
    if use_extension:
        if not config.EXTENSION_ENABLED:
            raise RuntimeError("Dola extension is disabled (DOLA_EXTENSION_ENABLED=0)")
        extension_dir = Path(config.EXTENSION_DIR).resolve()
        if not extension_dir.exists():
            raise FileNotFoundError(f"Dola extension directory does not exist: {extension_dir}")
        # Chromium debugger extension requires headed window to intercept skill/action-bar responses
        launch_headless = False
        args.extend([
            f"--disable-extensions-except={extension_dir}",
            f"--load-extension={extension_dir}",
        ])
    kwargs = {
        "headless": launch_headless,
        "channel": "chromium",
        "args": args,
        "locale": "ja-JP",
        "timezone_id": "Asia/Tokyo",
    }
    if config.PROXY:
        kwargs["proxy"] = config.browser_proxy()
    context = await p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
    if use_extension:
        try:
            try:
                worker = await _ready_extension(context)
                await _attach_extension_before_navigation(context, worker)
            except Exception as first_error:
                print(f"[{account}] Dola30 startup failed ({type(first_error).__name__}); reload and reopen once", flush=True)
                await _reload_extension(context)
                await context.close()
                context = await p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
                worker = await _ready_extension(context)
                await _attach_extension_before_navigation(context, worker)
        except Exception as exc:
            await context.close()
            raise RuntimeError(f"Dola30 extension unavailable for {account}: {exc}") from exc
    ACTIVE_CONTEXTS[account] = (context, launch_headless)

    def unregister(*_):
        entry = ACTIVE_CONTEXTS.get(account)
        if entry and entry[0] is context:
            ACTIVE_CONTEXTS.pop(account, None)

    context.on("close", unregister)
    return context


def cookie_value(cookies: list, name: str) -> str:
    """Extracts cookie value from context.cookies() result."""
    return next((c["value"] for c in cookies if c["name"] == name and c["value"]), "")


async def check_login_state(account: str) -> bool:
    """Opens Dola in headless mode and checks whether session is active."""
    from browser_queue import async_playwright
    async with async_playwright() as p:
        context = await launch_account_context(p, account)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            cookies = await context.cookies("https://www.dola.com")
            if not cookie_value(cookies, "sessionid"):
                return False
            from account_import import capture_identity
            await capture_identity(context, page, account)
            return bool(await page.evaluate(
                """() => !!(document.querySelector('textarea')
                        || document.querySelector('[contenteditable="true"]')
                        || document.querySelector('input[type="text"]'))"""
            ))
        finally:
            await context.close()


async def inspect_account_session(account):
    import asyncio
    from urllib.parse import urlsplit
    from browser_queue import async_playwright
    from account_import import capture_identity
    async with async_playwright() as p:
        context = await launch_account_context(p, account, headless=True)
        pending = []
        evidence = {}
        async def inspect_response(response):
            u = urlsplit(response.url)
            if u.hostname != "www.dola.com":
                return
            try:
                if u.path == "/alice/user/launch":
                    j = await response.json()
                    data = j.get("data") or {}
                    uid = data.get("sec_user_id") if isinstance(data, dict) else None
                    if j.get("code") == 0 and isinstance(uid, str) and uid.strip():
                        evidence["dola_user_id"] = uid.strip()
                elif u.path == "/passport/token/beat/web/":
                    j = await response.json()
                    evidence["authenticated"] = j.get("message") == "success" and (j.get("data") or {}).get("error_code") in (None, 0, "0")
            except Exception:
                pass
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            page.on("response", lambda response: pending.append(asyncio.create_task(inspect_response(response))))
            response = await page.goto("https://www.dola.com/chat", wait_until="domcontentloaded", timeout=60000)
            if not response or response.status >= 400:
                raise RuntimeError("Dola page unavailable")
            for attempt in range(20):
                await asyncio.sleep(1)
                if evidence.get("authenticated") and cookie_value(await context.cookies("https://www.dola.com"), "sessionid"):
                    if not evidence.get("dola_user_id") and attempt < 19:
                        continue
                    identity = await capture_identity(context, page, account)
                    identity["dola_user_id"] = evidence.get("dola_user_id", "")
                    print(f"[verify:{account}] identity_source={'dola_user_id' if identity['dola_user_id'] else 'cookie_hash'}", flush=True)
                    return {**identity, "state":"active"}
            import re
            login = page.get_by_text(re.compile(r"^(Log in|Log In|Sign in|\u30ed\u30b0\u30a4\u30f3)$"))
            if evidence.get("authenticated") is False and any([await e.is_visible() for e in await login.all()]):
                return {"state":"expired", "error":"Dola requires login again"}
            print(f"[verify:{account}] authenticated={evidence.get('authenticated')}", flush=True)
            raise RuntimeError("Authentication response unavailable; retry verification")
        finally:
            await context.close()
            if pending:
                await asyncio.gather(*pending, return_exceptions=True)
