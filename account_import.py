"""Cookie import and interactive Facebook login. Never log cookie values."""
import asyncio
import json
import hashlib
from urllib.parse import urlsplit
import re
import time
from pathlib import Path
from patchright.async_api import async_playwright
from browser import launch_account_context, cookie_value


def parse_netscape(text):
    if len(text.encode("utf-8")) > 1_000_000:
        raise ValueError("Cookie file exceeds 1 MB")
    cookies = {}
    for number, line in enumerate(text.lstrip("\ufeff").splitlines(), 1):
        line = line.strip("\r\n ")
        http_only = line.startswith("#HttpOnly_")
        if http_only:
            line = line[len("#HttpOnly_"):]
        elif not line or line.startswith("#"):
            continue
        fields = line.split("\t", 6)
        if len(fields) != 7:
            raise ValueError(f"Invalid Netscape cookie format at line {number}; use the original exported file")
        domain, subdomains, path, secure, expires, name, value = fields
        if domain.lstrip(".") != "dola.com" and not domain.lstrip(".").endswith(".dola.com"):
            continue
        if subdomains not in ("TRUE", "FALSE") or secure not in ("TRUE", "FALSE") or not path.startswith("/") or not name:
            raise ValueError(f"Invalid cookie attributes at line {number}")
        try:
            expiry = int(expires)
        except ValueError:
            raise ValueError(f"Invalid cookie expiration at line {number}") from None
        if expiry < 0 or (expiry and expiry <= time.time()):
            continue
        domain = ("." + domain.lstrip(".")) if subdomains == "TRUE" else domain.lstrip(".")
        item = dict(domain=domain, path=path, name=name, value=value,
                    secure=secure == "TRUE", httpOnly=http_only)
        if expiry:
            item["expires"] = expiry
        cookies[(domain, path, name)] = item
    result = list(cookies.values())
    if not any(c["name"] == "sessionid" and c["value"] for c in result):
        raise ValueError("No unexpired Dola sessionid cookie found")
    return result


def cookie_identity(cookies):
    values = {c["name"]: c["value"] for c in cookies if c.get("value")}
    uid = values.get("uid_tt") or values.get("uid_tt_ss")
    if not uid:
        return ""
    return hashlib.sha256(uid.encode()).hexdigest()


async def capture_identity(context, page, account):
    identity = cookie_identity(await context.cookies("https://www.dola.com"))
    # The account menu is a button with a user avatar and visible display name.
    names = await page.locator('button:has(img), [role="button"]:has(img)').all_text_contents()
    names = [n.strip() for n in names if n.strip() and len(n.strip()) < 100]
    result = {"identity_hash": identity, "display_name": names[0] if len(names) == 1 else ""}
    Path("accounts", account, "gateway_identity.json").write_text(json.dumps(result), encoding="utf-8")
    return result


async def continue_facebook_consent(page, account, clicked):
    url = urlsplit(page.url)
    if not (url.hostname == "facebook.com" or (url.hostname or "").endswith(".facebook.com")):
        return
    if not (url.path.startswith("/privacy/consent") or url.path.startswith("/dialog/oauth")):
        return
    if url.path in clicked:
        return
    body = await page.locator('body').inner_text()
    if "Dola" not in body:
        return
    button = page.get_by_role("button", name=re.compile(r"^(Continue as .+|Continue|\u7d9a\u884c|.+\u3068\u3057\u3066\u7d9a\u884c|Ti\u1ebfp t\u1ee5c.*)$", re.I))
    if await button.count() == 1 and await button.is_visible():
        clicked.add(url.path)
        await button.click(timeout=10000)
        print(f"[facebook:{account}] Dola consent Continue clicked once", flush=True)


async def session_ready(context, page):
    if page.is_closed() or not page.url.startswith("https://www.dola.com/"):
        return False
    if not cookie_value(await context.cookies("https://www.dola.com"), "sessionid"):
        return False
    editor = page.locator('textarea:visible, [contenteditable="true"]:visible')
    login = page.get_by_text(re.compile(r"^(Log in|Log In|Sign in|Sign In|\u30ed\u30b0\u30a4\u30f3)$"))
    return bool(await editor.count()) and not any([await e.is_visible() for e in await login.all()])


async def facebook_snapshot(page, account, stage):
    prefix = f"dbg_facebook_{account}_{stage}"
    data = await page.evaluate("""() => ({
        path: location.origin + location.pathname,
        dialogs: [...document.querySelectorAll('[role="dialog"]')].map(e => ({
            text: (e.innerText || '').slice(0, 1500),
            controls: [...e.querySelectorAll('button, [role="button"], svg')].map(n => ({
                tag: n.tagName, aria: n.getAttribute('aria-label'), title: n.getAttribute('title'),
                text: (n.innerText || '').slice(0, 100), classes: String(n.className),
                paths: [...n.querySelectorAll('path')].map(p => ({fill:p.getAttribute('fill'), d:(p.getAttribute('d')||'').slice(0,80)})),
                parentTag: n.parentElement?.tagName, parentClass: String(n.parentElement?.className)
            })).slice(0, 35)
        }))
    })""")
    Path(prefix + ".json").write_text(json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
    await page.screenshot(path=prefix + ".png")
    print(f"[facebook:{account}] stage={stage} path={data['path']} dialogs={len(data['dialogs'])} snapshot={prefix}", flush=True)


async def open_facebook_login(context, page, account):
    dialog = page.locator('[role="dialog"]:visible').last
    try:
        # Dola can open the modal asynchronously; don't click through its overlay.
        try:
            await dialog.wait_for(state="visible", timeout=10000)
        except Exception:
            await page.get_by_text(re.compile(r"^(Log in|Log In|Sign in|\u30ed\u30b0\u30a4\u30f3)$")).first.click(timeout=5000)
            await dialog.wait_for(state="visible", timeout=10000)
        await facebook_snapshot(page, account, "before_click")
        named = dialog.locator('[aria-label*="facebook" i], [title*="facebook" i], button:has-text("Facebook")')
        target = None
        for candidate in await named.all():
            if await candidate.is_visible():
                target = candidate
                break
        if target is None:
            # Observed Dola Facebook SVG: unlabeled icon in a clickable DIV.
            icon = dialog.locator('svg:has(path[fill="#0068FF"][d^="M12 2C6.203 2 1.5 6.73"])')
            if await icon.count() != 1:
                raise RuntimeError("Facebook provider icon not uniquely identified; see Facebook snapshot")
            target = icon.locator('xpath=ancestor::*[@data-disabled or self::button or @role="button"][1]')
        await target.click(timeout=10000)
        print(f"[facebook:{account}] provider clicked; waiting for Facebook navigation", flush=True)
        for _ in range(60):
            for candidate in context.pages:
                host = urlsplit(candidate.url).hostname or ""
                if host == "facebook.com" or host.endswith(".facebook.com"):
                    await facebook_snapshot(candidate, account, "after_click")
                    print(f"[facebook:{account}] Facebook page opened; waiting for manual login", flush=True)
                    return candidate
            if await session_ready(context, page):
                return
            await asyncio.sleep(0.5)
        raise RuntimeError("Facebook click did not open OAuth within 30 seconds")
    except Exception:
        if not page.is_closed():
            await facebook_snapshot(page, account, "failed")
        raise


async def fill_facebook_login(page, account, username, password):
    host = urlsplit(page.url).hostname or ""
    if host != "facebook.com" and not host.endswith(".facebook.com"):
        raise RuntimeError("Refusing to enter Facebook credentials on another domain")
    email_box = page.locator('input[name="email"]:visible').first
    password_box = page.locator('input[name="pass"]:visible').first
    try:
        await email_box.wait_for(state="visible", timeout=15000)
        await password_box.wait_for(state="visible", timeout=15000)
    except Exception:
        print(f"[facebook:{account}] Login fields unavailable; continue verification in browser", flush=True)
        return
    await page.wait_for_load_state("domcontentloaded")
    await asyncio.sleep(1)
    for attempt in range(2):
        await email_box.fill(username)
        await password_box.fill(password)
        await asyncio.sleep(0.6)
        fields_match = (await email_box.input_value() == username
                        and await password_box.input_value() == password)
        print(f"[facebook:{account}] fill attempt={attempt + 1} fields_match={fields_match}", flush=True)
        if fields_match:
            break
    else:
        raise RuntimeError("Facebook login fields did not retain entered credentials")
    # Prefer the visible login button over Enter, which may not submit this form.
    submit = page.locator('button[name="login"]:visible, input[name="login"]:visible, #loginbutton:visible').first
    if not await submit.count():
        submit = page.get_by_role("button", name=re.compile(r"^(Log in|Log In|Login|\u30ed\u30b0\u30a4\u30f3|\u0110\u0103ng nh\u1eadp)$", re.I)).first
    if not await submit.count():
        raise RuntimeError("Facebook login button not identified; please continue in browser")
    await submit.click(timeout=10000)
    print(f"[facebook:{account}] Login button clicked once; checking resulting screen", flush=True)
    await asyncio.sleep(3)
    if page.is_closed():
        print(f"[facebook:{account}] OAuth window closed; checking Dola session", flush=True)
        return
    # Record structural state only; never save input values or credentials.
    data = await page.evaluate("""() => ({
        path: location.origin + location.pathname,
        inputs: [...document.querySelectorAll('input')].filter(e=>e.getBoundingClientRect().width).map(e=>({
            type:e.type,name:e.name,id:e.id,filled:!!e.value
        })),
        buttons: [...document.querySelectorAll('button,[role="button"],input[type="submit"]')]
            .filter(e=>e.getBoundingClientRect().width).map(e=>({
                tag:e.tagName,name:e.getAttribute('name'),text:(e.innerText||'').slice(0,80)
            }))
    })""")
    Path(f"dbg_facebook_{account}_after_submit.json").write_text(
        json.dumps(data, ensure_ascii=True, indent=2), encoding="utf-8")
    await page.screenshot(path=f"dbg_facebook_{account}_after_submit.png",
                          mask=[page.locator('input')])
    print(f"[facebook:{account}] after_submit={json.dumps(data, ensure_ascii=True)}", flush=True)


async def imported_account_flow(account, method, cookies=None, username="", password=""):
    async with async_playwright() as p:
        context = await launch_account_context(p, account, headless=method == "cookies")
        try:
            if cookies:
                await context.add_cookies(cookies)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
            await asyncio.sleep(5)
            if method == "facebook" and not await session_ready(context, page):
                facebook_page = await open_facebook_login(context, page, account)
                if facebook_page and username and password:
                    await fill_facebook_login(facebook_page, account, username, password)
                print(f"[account-add:{account}] Complete Facebook login in the browser; waiting up to 5 minutes", flush=True)
            deadline = time.monotonic() + (300 if method == "facebook" else 30)
            consent_clicked = set()
            while time.monotonic() < deadline:
                for candidate in context.pages:
                    if method == "facebook" and not candidate.is_closed():
                        await continue_facebook_consent(candidate, account, consent_clicked)
                    if await session_ready(context, candidate):
                        await capture_identity(context, candidate, account)
                        # Flush Chromium's persistent profile before reporting success.
                        await asyncio.sleep(2)
                        return True
                if not context.pages:
                    raise RuntimeError("Login browser was closed before verification completed")
                await asyncio.sleep(2)
            raise RuntimeError("Dola session was not verified; cookies may be expired or login is incomplete")
        finally:
            await context.close()
