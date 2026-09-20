"""Automated Google OAuth login for account profile setup.

Usage: python add_account.py <account_name> "email----password----totp_secret"
"""
import asyncio
import base64
import hashlib
import hmac
import struct
import sys
import time
import traceback
from pathlib import Path

from patchright.async_api import async_playwright

from browser import LAUNCH_ARGS
import config


LOG_FILE = Path(__file__).with_name("account_add.log")
try:
    LOG_FILE.touch(exist_ok=True)
except OSError:
    pass


def log(message: str):
    """Writes account setup diagnostics without logging credentials."""
    line = f"{time.strftime('%Y-%m-%d %H:%M:%S')} {message}"
    print(line.encode("ascii", "backslashreplace").decode("ascii"), flush=True)
    try:
        with LOG_FILE.open("a", encoding="utf-8") as f:
            f.write(line + "\n")
    except OSError:
        pass


def page_summary(context) -> str:
    return ", ".join(
        f"{index}:{page.url or '<blank>'}"
        for index, page in enumerate(context.pages)
    ) or "<none>"


def track_page(account: str, page):
    log(f"[{account}] page opened url={page.url or '<blank>'}")
    page.on("close", lambda: log(f"[{account}] page closed url={page.url or '<blank>'}"))
    page.on("crash", lambda: log(f"[{account}] page crashed url={page.url or '<blank>'}"))


def totp(secret: str, period: int = 30, digits: int = 6) -> str:
    """Standard TOTP (RFC 6238), Google Authenticator compatible."""
    secret = secret.replace(" ", "").upper()
    key = base64.b32decode(secret + "=" * ((8 - len(secret) % 8) % 8))
    counter = int(time.time() // period)
    h = hmac.new(key, struct.pack(">Q", counter), hashlib.sha1).digest()
    o = h[19] & 15
    code = (struct.unpack(">I", h[o:o + 4])[0] & 0x7fffffff) % (10 ** digits)
    return str(code).zfill(digits)


async def google_login(g, email: str, password: str, secret: str):
    """Executes Google OAuth login state machine until callback."""
    log(f"[google] login started email={email} url={g.url}")
    deadline = time.monotonic() + 180
    manual_deadline = None
    last_wait_log = 0
    totp_submitted = False
    step = 0
    while time.monotonic() < (manual_deadline or deadline):
        await asyncio.sleep(2.5)
        if g.is_closed():
            log("[google] OAuth page closed; checking Dola session next")
            return
        step += 1
        log(f"[google] step={step} url={g.url}")
        if "accounts.google.com" not in g.url:
            log("[google] Redirected out of Google domain (OAuth callback)")
            return
        # 1) Account chooser page
        if "accountchooser" in g.url:
            acc = g.locator(f"text={email}").first
            if await acc.count() and await acc.is_visible():
                await acc.click(timeout=5000)
                log("[google] Account chooser page -> clicked account")
                continue
        # 2) Email page
        identifier = g.locator("#identifierId").first
        if await identifier.count() and await identifier.is_visible():
            await identifier.fill(email)
            # Use DOM click to safely trigger submit event
            await g.locator("#identifierNext").evaluate("e => e.click()")
            log("[google] Email page -> submit")
            await g.wait_for_timeout(1500)
            continue
        # 3) Password page
        pwd = g.locator('input[name="Passwd"]').first
        if await pwd.count() and await pwd.is_visible():
            await g.wait_for_timeout(600)
            await pwd.fill(password)
            await g.locator("#passwordNext").evaluate("e => e.click()")
            log("[google] Password page -> submit")
            await g.wait_for_timeout(1500)
            continue
        # Challenge pages require manual handling unless this is specifically TOTP.
        challenge = "/challenge/" in g.url
        code_input = g.locator('input[type="tel"]:visible, input[autocomplete="one-time-code"]:visible').first
        if challenge or await code_input.count():
            totp_input = g.locator('#totpPin:visible').first
            totp_next = g.locator('#totpNext:visible').first
            if secret and not totp_submitted and await totp_input.count() and await totp_next.count():
                try:
                    code = totp(secret)
                except (ValueError, base64.binascii.Error):
                    log("[google] Invalid TOTP secret; waiting for manual verification")
                    totp_submitted = True
                else:
                    await totp_input.fill(code)
                    await totp_next.click()
                    totp_submitted = True
                    log("[google] Authenticator challenge -> submitted TOTP once")
                    continue
            if manual_deadline is None:
                manual_deadline = time.monotonic() + 300
                log("[google] Waiting for manual verification in browser (up to 300 seconds)")
            if time.monotonic() - last_wait_log >= 15:
                remaining = max(0, int(manual_deadline - time.monotonic()))
                log(f"[google] Manual verification pending remaining_seconds={remaining}")
                last_wait_log = time.monotonic()
            continue
        # 4) Consent page
        clicked = False
        for sel in ["#submit_button",
                    "[role='button']:has-text('続行')", "button:has-text('続行')",
                    "[role='button']:has-text('继续')", "button:has-text('继续')",
                    "[role='button']:has-text('Continue')", "button:has-text('Continue')"]:
            try:
                loc = g.locator(sel).first
                if await loc.count() and await loc.is_visible():
                    await loc.click(timeout=3000)
                    log(f"[google] Consent page -> clicked {sel}")
                    clicked = True
                    break
            except Exception:
                continue
        if clicked:
            continue
        # 6) Dola 18+ age confirmation popup
        if await g.locator("text=18").count():
            ok = await g.evaluate("""() => {
                const els = [...document.querySelectorAll('button, [role="button"], div, span')];
                const t = els.find(e => (e.textContent || '').trim() === 'OK' && e.childElementCount === 0);
                if (t) { t.click(); return true; }
                return false;
            }""")
            log(f"[dola] Age confirmation -> JS click OK = {ok}")
            await g.wait_for_timeout(1500)
            continue
        txt = await g.evaluate("() => (document.body && document.body.innerText || '').slice(0, 300)")
        log(f"[google] step={step} unrecognized url={g.url[:120]} text={txt[:300]}")
    if "accounts.google.com" in g.url:
        await g.screenshot(path="dbg_google2.png")
        raise RuntimeError("Manual verification timed out after 5 minutes (saved dbg_google2.png)"
                           if manual_deadline else "Google login timed out after 3 minutes (saved dbg_google2.png)")


async def add_account_flow(account: str, email: str, password: str, secret: str) -> bool:
    """Full account addition flow; returns True on success."""
    profile_dir = Path("accounts") / account
    profile_dir.mkdir(parents=True, exist_ok=True)

    log(f"[{account}] add flow started email={email} profile={profile_dir.resolve()}")
    async with async_playwright() as p:
        kwargs = {"headless": False, "args": LAUNCH_ARGS,
                  "locale": "ja-JP", "timezone_id": "Asia/Tokyo"}
        if config.PROXY:
            kwargs["proxy"] = {"server": config.PROXY}
        log(f"[{account}] launching Chromium proxy={'configured' if config.PROXY else 'disabled'}")
        context = await p.chromium.launch_persistent_context(str(profile_dir), **kwargs)
        context.on("page", lambda opened: track_page(account, opened))
        context.on("close", lambda: log(f"[{account}] browser context closed"))
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            for opened in context.pages:
                track_page(account, opened)
            await page.goto("https://www.dola.com/chat", timeout=60000)
            log(f"[{account}] Dola loaded url={page.url} pages={page_summary(context)}")
            await page.wait_for_timeout(4000)

            cookies = await context.cookies("https://www.dola.com")
            log(f"[{account}] initial cookies sessionid={any(c['name'] == 'sessionid' and c['value'] for c in cookies)}")
            if any(c["name"] == "sessionid" and c["value"] for c in cookies):
                log(f"[{account}] Active session exists, login not required")
                return True

            ui_snapshot = await page.evaluate("""() => ({
                title: document.title,
                bodyText: (document.body?.innerText || '').slice(0, 3000),
                buttons: [...document.querySelectorAll('button, [role="button"]')]
                    .map(e => (e.innerText || e.getAttribute('aria-label') || '').trim())
                    .filter(Boolean).slice(0, 80),
                links: [...document.querySelectorAll('a')]
                    .map(e => ({text: (e.innerText || '').trim(), href: e.href}))
                    .filter(e => e.text || e.href).slice(0, 50)
            })""")
            log(f"[{account}] UI snapshot title={ui_snapshot['title']!r} url={page.url}")
            log(f"[{account}] UI visible text={ui_snapshot['bodyText']!r}")
            log(f"[{account}] UI buttons={ui_snapshot['buttons']!r}")
            log(f"[{account}] UI links={ui_snapshot['links']!r}")
            await page.screenshot(path=f"dbg_before_login_{account}.png", full_page=True)

            # Dola may open its modal first or redirect directly to Google OAuth.
            google_page = next(
                (opened for opened in context.pages if "accounts.google.com" in opened.url),
                None,
            )
            if google_page is None and "accounts.google.com" in page.url:
                google_page = page

            if google_page is None:
                google_button = page.locator("text=Googleで続ける").first
                if await google_button.count() and await google_button.is_visible():
                    await google_button.click(timeout=10000)
                    log(f"[{account}] Google login clicked from visible modal")
                else:
                    log(f"[{account}] login modal not visible; clicking login entry url={page.url}")
                    await page.locator("text=ログイン").first.click(timeout=10000)
                    for _ in range(30):
                        await page.wait_for_timeout(500)
                        google_page = next(
                            (opened for opened in context.pages
                             if "accounts.google.com" in opened.url),
                            None,
                        )
                        if google_page is not None:
                            log(f"[{account}] direct Google redirect detected")
                            break
                        google_button = page.locator("text=Googleで続ける").first
                        if await google_button.count() and await google_button.is_visible():
                            await google_button.click(timeout=10000)
                            log(f"[{account}] Google login clicked from modal")
                            break
                    else:
                        raise RuntimeError("Dola login did not open modal or Google OAuth")
            log(f"[{account}] OAuth launch state pages={page_summary(context)}")

            # Google page may open in popup or active tab
            await page.wait_for_timeout(3000)
            log(f"[{account}] after OAuth wait pages={page_summary(context)}")
            g = google_page or next((pg for pg in context.pages if "accounts.google.com" in pg.url), None)
            if g is None and "accounts.google.com" in page.url:
                g = page
            if g is None:
                screenshot = f"dbg_add_account_{account}.png"
                await page.screenshot(path=screenshot)
                raise RuntimeError(f"Failed to redirect to Google login page (saved {screenshot})")

            await google_login(g, email, password, secret)

            # Wait for Dola sessionid after OAuth callback
            for _ in range(60):
                await asyncio.sleep(3)
                cookies = await context.cookies("https://www.dola.com")
                if any(c["name"] == "sessionid" and c["value"] for c in cookies):
                    log(f"[{account}] Login successful, sessionid saved to {profile_dir}")
                    await asyncio.sleep(3)
                    return True
            screenshot = f"dbg_add_account_{account}.png"
            await page.screenshot(path=screenshot)
            raise RuntimeError(f"sessionid not acquired within 3 minutes (saved {screenshot})")
        except Exception as exc:
            log(f"[{account}] add flow failed type={type(exc).__name__} error={exc}")
            log(traceback.format_exc().rstrip())
            for index, opened in enumerate(context.pages):
                try:
                    await opened.screenshot(path=f"dbg_add_account_{account}_{index}.png")
                except Exception:
                    pass
            raise
        finally:
            log(f"[{account}] closing context pages={page_summary(context)}")
            await context.close()


async def main():
    account = sys.argv[1]
    email, password, secret = sys.argv[2].split("----")
    await add_account_flow(account, email, password, secret)
    print(f"[{account}] Account added successfully!")


if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"✗ {e}")
        sys.exit(1)