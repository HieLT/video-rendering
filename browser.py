"""Patchright persistent context launcher: Explicit proxy and anti-detection parameters."""
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


LAUNCH_ARGS = [
    "--disable-blink-features=AutomationControlled",
    "--no-first-run",
    "--no-default-browser-check",
]


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
    args = list(LAUNCH_ARGS)
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
        "args": args,
        "locale": "ja-JP",
        "timezone_id": "Asia/Tokyo",
    }
    if config.PROXY:
        kwargs["proxy"] = {"server": config.PROXY}
    context = await p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
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
    from patchright.async_api import async_playwright
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
    from patchright.async_api import async_playwright
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
