"""Video generation worker v2: UI automation with OpenCV slider puzzle solver."""
import asyncio
import json
import random
import re
import sys
import time
from pathlib import Path

import aiohttp
from patchright.async_api import async_playwright

from gap import find_gap_x

import config
from browser import cookie_value, launch_account_context
from dola_client import CREDIT_FAIL_PATTERN, CreditError
from video_worker import POLL_JS, RiskControlError, _download, extract_unwatermarked_url


def _log(message: str):
    """Keeps worker diagnostics printable on Windows cp1252 consoles."""
    safe = str(message).encode("ascii", "backslashreplace").decode("ascii")
    print(safe, flush=True)

# Daily limit pattern matching response text
DAILY_LIMIT_PATTERN = re.compile(
    r"動画生成の\s*1日あたりの上限|每日(?:视频|影片)?生成.*(?:上限|限额|额度)|"
    r"daily.*(?:limit|quota)|(?:limit|quota).*per\s*day",
    re.IGNORECASE,
)


class AccountLimitedError(Exception):
    """Account reached daily video generation limit."""


class CreditInsufficientError(Exception):
    """Insufficient points prior to generation."""


VIDEO_BTN = "text=動画を作成"          # Entry point button in ja-JP locale
CAPTCHA_FRAME_KEY = "bdcaptcha.html"   # Captcha verifycenter iframe


# Read-only balance pre-check from recent conversations
BALANCE_JS = r"""
async ({msToken, fp}) => {
  const params = new URLSearchParams({
    version_code: "20800", language: "ja", device_platform: "web",
    doubao_device_platform: "web", aid: "495671", real_aid: "495671",
    pkg_type: "release_version", pc_version: "3.32.62", doubao_pc_version: "3.32.62",
    region: "JP", sys_region: "JP", samantha_web: "1", web_platform: "browser",
    "use-olympus-account": "1", web_tab_id: crypto.randomUUID(),
  });
  if (msToken) params.set("msToken", msToken);
  if (fp) params.set("fp", fp);
  const headers = {
    "Content-Type": "application/json; encoding=utf-8",
    "agw-js-conv": "str", "Accept": "*/*",
  };
  const recent = await fetch("/im/chain/recent_conv?" + params.toString(), {
    method: "POST", headers,
    body: JSON.stringify({
      cmd: 3200,
      uplink_body: {pull_recent_conv_chain_uplink_body: {
        limit: 20, message_count_per_conv: 10, api_version: 1, conv_version: 0,
        direction: 3,
        option: {not_need_message: false, need_complete_conversation: true,
          need_coco_conversation: true, need_coco_bot: true,
          need_pc_pin_chain: true, pc_pin_query_type: 0},
      }},
      sequence_id: crypto.randomUUID(), channel: 2, version: "1",
    }), credentials: "include",
  });
  if (!recent.ok) return {ok: false, texts: []};
  const recentData = await recent.json();
  const body = recentData.downlink_body || {};
  const down = body.pull_recent_conv_chain_downlink_body || {};
  const cells = down.cells || [];
  const ids = cells.map(c => (c.conversation || {}).conversation_id || c.id)
    .filter(Boolean).slice(0, 10);
  if (!ids.length) return {ok: true, texts: []};

  const batch = await fetch("/im/chain/batch_single?" + params.toString(), {
    method: "POST", headers,
    body: JSON.stringify({
      cmd: 3101,
      uplink_body: {batch_pull_singe_chain_uplink_body: {
        conversation_type: 3, direction: 3, limit: 1,
        params: ids.map(conversation_id => ({conversation_id})),
        evaluate_ab_params: "", evaluate_common_params: "", ext: {},
      }},
      sequence_id: crypto.randomUUID(), channel: 2, version: "1",
    }), credentials: "include",
  });
  if (!batch.ok) return {ok: true, texts: []};
  const data = await batch.json();
  const texts = [];
  const seen = new Set();
  const walk = (v) => {
    if (typeof v === "string") {
      if ((v.includes("ポイント") || v.includes("积分") || v.toLowerCase().includes("points")
        || v.includes("上限") || v.includes("limit")) && v.length < 1200 && !seen.has(v)) {
        seen.add(v); texts.push(v);
      }
      try {
        const t = v.trim();
        if (t.startsWith("{") || t.startsWith("[")) walk(JSON.parse(t));
      } catch (e) {}
      return;
    }
    if (!v || typeof v !== "object") return;
    if (Array.isArray(v)) { for (const x of v) walk(x); return; }
    for (const x of Object.values(v)) walk(x);
  };
  walk(data);
  return {ok: true, texts: texts.slice(-80)};
}
""";


def find_captcha_frame(page):
    for f in page.frames:
        if CAPTCHA_FRAME_KEY in f.url:
            return f
    return None


async def _fetch_bytes(url: str) -> bytes:
    async with aiohttp.ClientSession() as s:
        async with s.get(url, proxy=config.PROXY or None) as r:
            return await r.read()



async def attach_reference_images(page, image_paths: list[str]) -> None:
    """Uploads reference images through native file input and waits for TOS upload."""
    if not image_paths:
        return
    file_input = page.locator('input[type="file"]').first
    for _ in range(30):
        if await file_input.count():
            break
        await page.wait_for_timeout(1000)
    if not await file_input.count():
        snapshot = await page.evaluate("""() => ({
            url: location.href,
            text: (document.body?.innerText || '').slice(0, 4000),
            controls: [...document.querySelectorAll('button, [role="button"]')]
                .map(e => ({text: (e.innerText || '').trim(), aria: e.getAttribute('aria-label'), title: e.getAttribute('title')}))
                .filter(e => e.text || e.aria || e.title)
                .slice(0, 100)
        })""")
        safe = json.dumps(snapshot, ensure_ascii=False).encode("ascii", "backslashreplace").decode("ascii")
        _log(f"[upload] file input missing after 30s: {safe[:7000]}")
        await page.screenshot(path="reference_upload_input_missing.png", full_page=True)
        raise TimeoutError("Reference image upload control did not appear")
    events = []

    def on_response(response):
        url = response.url
        if "/alice/resource/prepare_upload" in url or "/upload/v1/" in url:
            events.append((response.status, url))

    page.on("response", on_response)
    try:
        await file_input.set_input_files(image_paths)
        expected = len(image_paths)
        deadline = time.time() + max(60, expected * 20)
        while time.time() < deadline:
            prepare_count = sum("/alice/resource/prepare_upload" in url and 200 <= status < 300
                                for status, url in events)
            tos_count = sum("/upload/v1/" in url and 200 <= status < 300
                            for status, url in events)
            # Wait for thumbnails and TOS completion before sending
            thumb_count = await page.locator('img[alt]').count()
            if prepare_count >= expected and tos_count >= expected and thumb_count >= expected:
                await page.wait_for_timeout(800)
                _log(f"[upload] Reference images uploaded: {expected} image(s)")
                return
            await page.wait_for_timeout(250)
        raise TimeoutError(
            f"Reference image upload timeout: prepare={prepare_count}/{expected}, tos={tos_count}/{expected}"
        )
    finally:
        page.remove_listener("response", on_response)


def _gen_track(distance: float):
    """Generates humanized mouse drag trajectory."""
    steps = random.randint(45, 65)
    overshoot = random.uniform(3, 9)
    pts = []
    for i in range(1, steps + 1):
        t = i / steps
        s = 10 * t**3 - 15 * t**4 + 6 * t**5
        x = (distance + overshoot) * s
        y = random.uniform(-1.5, 1.5) if 0.1 < t < 0.95 else 0
        pts.append((x, y, random.randint(8, 22)))
    for i in range(1, random.randint(3, 5) + 1):
        pts.append((distance + overshoot * (1 - i / 5), random.uniform(-0.8, 0.8), random.randint(15, 30)))
    return pts


async def solve_slider(page, frame, attempt: int) -> bool:
    """Solves captcha slider notch within iframe and performs drag."""
    await frame.wait_for_selector("img", timeout=15000)
    await frame.evaluate("""async () => {
        const t0 = Date.now();
        while (Date.now() - t0 < 10000) {
            const imgs = [...document.images];
            if (imgs.length >= 2 && imgs.every(im => im.complete && im.naturalWidth > 0)) return;
            await new Promise(r => setTimeout(r, 200));
        }
        throw new Error("captcha images load timeout");
    }""")
    await page.wait_for_timeout(800)

    imgs = await frame.evaluate("""() => [...document.images].map(im => ({
        src: im.src, w: im.naturalWidth, h: im.naturalHeight,
        bw: im.getBoundingClientRect().width,
        left: im.getBoundingClientRect().left,
    }))""")
    bg = next((i for i in imgs if ".jpeg" in i["src"] or "-2." in i["src"]), None)
    piece = next((i for i in imgs if i is not bg and (".png" in i["src"] or "-1." in i["src"])), None)
    if not bg or not piece:
        _log("  Captcha background or puzzle image not found")
        return False

    bg_bytes = await _fetch_bytes(bg["src"])
    piece_bytes = await _fetch_bytes(piece["src"])
    Path("dbg_bg.jpg").write_bytes(bg_bytes)
    Path("dbg_piece.png").write_bytes(piece_bytes)

    gap_x, conf = find_gap_x(bg_bytes, piece_bytes)
    scale = bg["bw"] / bg["w"] if bg["w"] else 340 / 552
    distance = (gap_x - (piece["left"] - bg["left"]) / scale) * scale
    _log(f"  [solve#{attempt}] gap_x={gap_x} conf={conf:.3f} scale={scale:.2f} distance={distance:.0f}px")

    btn = frame.locator(".captcha-slider-btn")
    bb = await btn.bounding_box()
    if not bb:
        _log("  Drag handle .captcha-slider-btn not found")
        return False
    sx, sy = bb["x"] + bb["width"] / 2, bb["y"] + bb["height"] / 2
    await page.mouse.move(sx, sy)
    await page.wait_for_timeout(random.randint(150, 350))
    await page.mouse.down()
    await page.wait_for_timeout(random.randint(80, 180))
    for dx, dy, dt in _gen_track(distance):
        await page.mouse.move(sx + dx, sy + dy)
        await asyncio.sleep(dt / 1000)
    await page.wait_for_timeout(random.randint(120, 260))
    await page.mouse.up()

    for _ in range(10):
        await page.wait_for_timeout(700)
        if not find_captcha_frame(page):
            return True
    return False



_BALANCE_PATTERNS = (
    re.compile(r"(?:本日は|今日(?:还剩|剩余)?|今天).*?(\d+)\s*(?:ポイント|积分|points?)", re.I),
    re.compile(r"(?:remaining|left)\s*[:：]?\s*(\d+)\s*points?", re.I),
    re.compile(r"(?:还剩|剩余|还有)\s*(\d+)\s*(?:积分|点)", re.I),
)


def _parse_balance_texts(texts: list[str]) -> tuple[int | None, bool, str]:
    for text in texts:
        if DAILY_LIMIT_PATTERN.search(text):
            return None, True, text
    for text in texts:
        for pattern in _BALANCE_PATTERNS:
            match = pattern.search(text)
            if match:
                return int(match.group(1)), False, text
    return None, False, ""


async def _preflight_balance(page, ms_token: str, fp: str, required: int) -> dict:
    """Reads known credit balance from chat history."""
    try:
        result = await asyncio.wait_for(page.evaluate(
            BALANCE_JS, {"msToken": ms_token, "fp": fp}), timeout=30)
        balance, daily_limited, source = _parse_balance_texts(result.get("texts", []))
        if daily_limited:
            raise AccountLimitedError(f"Account daily generation limit: {source[:120]}")
        if balance is not None and balance < required:
            raise CreditInsufficientError(
                f"Insufficient points: current {balance}, required {required} (source: {source[:120]})"
            )
        return {"balance": balance, "source": source}
    except (AccountLimitedError, CreditInsufficientError):
        raise
    except Exception as e:
        _log(f"  Balance pre-check indeterminate (proceeding with submit): {str(e)[:120]}")
        return {"balance": None, "source": ""}


async def poll_conversation(account: str, page, context, conversation_id: str,
                            timeout: int, on_poll=None, on_balance=None) -> dict:
    """Polls accepted conversation for video completion."""
    cookies = await context.cookies("https://www.dola.com")
    ms_token, fp = cookie_value(cookies, "msToken"), cookie_value(cookies, "s_v_web_id")
    start = time.time()
    last_callback = 0.0
    poll_failures = 0
    tab_recoveries = 0
    while time.time() - start < timeout:
        await asyncio.sleep(5)
        try:
            if page.is_closed():
                if tab_recoveries >= 1:
                    raise RuntimeError("Browser tab closed again; manual recovery required")
                tab_recoveries += 1
                page = await context.new_page()
                await page.goto(f"https://www.dola.com/chat/{conversation_id}",
                                timeout=30000, wait_until="domcontentloaded")
                _log(f"[{account}] restored conversation tab {conversation_id}")
            poll = await asyncio.wait_for(page.evaluate(
                POLL_JS, {"conversationId": conversation_id, "msToken": ms_token, "fp": fp}), timeout=30)
        except Exception as e:
            poll_failures += 1
            _log(f"  Polling exception attempt={poll_failures}/3: {e}")
            if poll_failures >= 3:
                raise RuntimeError("Conversation monitoring interrupted; open conversation to recover") from e
            continue
        poll_failures = 0
        now = time.time()
        if on_poll and now - last_callback >= 30:
            on_poll(now)
            last_callback = now
        for text in poll.get("texts", []):
            balance, _, source = _parse_balance_texts([text])
            if balance is not None and on_balance:
                on_balance(balance, source)
            if DAILY_LIMIT_PATTERN.search(text):
                raise AccountLimitedError(f"Account daily limit reached: {text[:120]}")
            if CREDIT_FAIL_PATTERN.search(text):
                raise CreditError(f"Insufficient quota: {text[:80]}")
        if poll.get("videos"):
            video_models = poll.get("videoModels", [])
            url = extract_unwatermarked_url(
                video_models[0] if video_models else "", poll["videos"][0])
            _log(f"[{account}] Completed! Downloading (unwatermarked priority)...")
            local = await _download(url, account)
            _log(f"[{account}] Downloaded {local} ({local.stat().st_size / 1e6:.1f} MB)")
            return {"video_url": url, "local_path": str(local),
                    "conversation_id": conversation_id, "account": account}
        _log(f"  ...Generating ({int(time.time() - start)}s)")
    raise TimeoutError(f"No video generated within {timeout}s (conversation_id={conversation_id})")


async def resume_video(account: str, conversation_id: str, timeout: int,
                       on_poll=None, on_balance=None) -> dict:
    """Recovers accepted session after server restart without re-sending prompt."""
    async with async_playwright() as p:
        context = await launch_account_context(p, account, headless=False, use_extension=True)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto(f"https://www.dola.com/chat/{conversation_id}",
                            timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            return await poll_conversation(account, page, context, conversation_id, timeout, on_poll, on_balance)
        finally:
            await context.close()




EDITOR_SELECTOR = ('textarea:visible:not([disabled]):not([readonly]), '
                   '[contenteditable="true"]:visible')


async def _composer(page):
    editors = page.locator(EDITOR_SELECTOR)
    await editors.first.wait_for(state="visible", timeout=10000)
    # Mode-switch animations briefly keep both old and new editors visible.
    for _ in range(25):
        if await editors.count() == 1:
            break
        await page.wait_for_timeout(200)
    else:
        raise RuntimeError("Expected exactly one visible composer editor after transition")
    box = editors.first
    root = box.locator(
        'xpath=ancestor::*[not(self::body) and not(self::html)]'
        '[.//button or .//*[@role="button"]][1]'
    )
    if not await root.count():
        raise RuntimeError("Could not identify composer controls for draft cleanup")
    return box, root


async def _composer_snapshot(page, account: str, stage: str):
    try:
        _, root = await _composer(page)
        snapshot = await root.evaluate("""element => ({
            editors: [...element.querySelectorAll('textarea, [contenteditable="true"]')]
                .map(e => ({tag: e.tagName, chars: (e.value ?? e.innerText ?? '').length})),
            images: element.querySelectorAll('img').length,
            media: [...element.querySelectorAll('img, video')].slice(0, 30).map(e => ({
                tag: e.tagName, alt: e.getAttribute('alt'), ariaHidden: e.getAttribute('aria-hidden'),
                role: e.getAttribute('role'), classes: String(e.className),
                width: e.getBoundingClientRect().width, height: e.getBoundingClientRect().height
            })),
            fileInputs: element.querySelectorAll('input[type="file"]').length,
            controls: [...element.querySelectorAll('button, [role="button"], [aria-label], [title], [class*="close"], [class*="delete"]')]
                .map(e => ({tag: e.tagName, text: (e.innerText || '').slice(0, 100),
                    aria: e.getAttribute('aria-label'), title: e.getAttribute('title'),
                    testid: e.getAttribute('data-testid'), classes: String(e.className)}))
                .slice(0, 60)
        })""")
        _log(f"[{account}] composer stage={stage}: {json.dumps(snapshot, ensure_ascii=True)}")
    except Exception as exc:
        _log(f"[{account}] composer snapshot stage={stage} failed: {exc}")


async def _clear_composer_draft(page, account: str):
    await _composer_snapshot(page, account, "before_cleanup")
    box, root = await _composer(page)
    await box.fill("")
    # Update frontend state through native input events and removal controls.
    for file_input in await root.locator('input[type="file"]').all():
        await file_input.set_input_files([])
    # Dola's toolbar icons are IMG elements with aria-hidden=true.
    # Keep real previews (even ones inside buttons), exclude decorative media.
    previews = root.locator(
        'img:visible:not([aria-hidden="true"]):not([role="presentation"]), '
        'video:visible:not([aria-hidden="true"])'
    )
    _log(f"[{account}] draft media total_images={await root.locator('img').count()} "
         f"attachment_candidates={await previews.count()}")
    remove_name = re.compile(r"remove|delete|close|\u524a\u9664|\u9664\u53bb|\u9589\u3058\u308b|\u79fb\u9664|\u5220\u9664|\u5173\u95ed", re.I)
    removed = 0
    while await previews.count():
        if removed >= 30:
            raise RuntimeError("Too many draft attachments to clear safely")
        preview = previews.first
        await preview.hover()
        before = await previews.count()
        container = preview.locator('xpath=..')
        clicked = False
        for _ in range(4):
            if not await container.evaluate("(e, root) => root.contains(e)", await root.element_handle()):
                break
            named = container.get_by_role("button", name=remove_name)
            candidates = container.locator(
                '[aria-label*="remove" i], [aria-label*="delete" i], '
                '[aria-label*="close" i], [data-testid*="remove" i], '
                '[data-testid*="delete" i], [data-testid*="close" i], '
                '[class*="remove" i], [class*="delete" i], [class*="close" i]'
            )
            for choices in (named, candidates):
                for control in await choices.all():
                    if await control.is_visible():
                        await control.click(timeout=3000)
                        clicked = True
                        break
                if clicked:
                    break
            if clicked:
                break
            container = container.locator('xpath=..')
        if not clicked:
            raise RuntimeError("Draft attachment remove control not found; refusing to reuse old images")
        for _ in range(20):
            if await previews.count() < before:
                break
            await page.wait_for_timeout(100)
        else:
            raise RuntimeError("Draft attachment remained after clicking remove")
        removed += 1
    await page.wait_for_timeout(500)
    remaining = await box.evaluate("e => e.value ?? e.innerText ?? ''")
    if _normalize_composer_text(remaining) or await previews.count():
        raise RuntimeError("Composer draft was restored after cleanup")
    _log(f"[{account}] draft cleared text_chars=0 remaining_images=0 removed_images={removed}")
    await _composer_snapshot(page, account, "after_cleanup")


async def _prepare_video_composer(page, account: str, needs_upload: bool):
    try:
        await _clear_composer_draft(page, account)
        for attempt in range(1, 3):
            _, root = await _composer(page)
            async def ready():
                return (await root.get_by_text("\u30e2\u30c7\u30eb", exact=True).is_visible()
                        and await root.get_by_text("\u6bd4\u7387", exact=True).is_visible()
                        and await root.get_by_text(re.compile(r"^\d+s$")).first.is_visible()
                        and (not needs_upload or await root.locator('input[type="file"]').count() > 0))
            if not await ready():
                button = page.get_by_role("button", name="\u52d5\u753b\u3092\u4f5c\u6210", exact=True)
                await button.click(timeout=10000)
            for _ in range(30):
                _, root = await _composer(page)
                if await ready():
                    _log(f"[{account}] video composer ready attempt={attempt}")
                    await _composer_snapshot(page, account, "video_ready")
                    return
                await page.wait_for_timeout(500)
            await _composer_snapshot(page, account, f"video_not_ready_{attempt}")
            if attempt == 1:
                await _clear_composer_draft(page, account)
        raise TimeoutError("Video composer did not open before reference upload")
    except Exception:
        await _composer_snapshot(page, account, "prepare_failed")
        await page.screenshot(path=f"dbg_video_composer_missing_{account}.png", full_page=True)
        raise


def _normalize_composer_text(value: str) -> str:
    # Rich-text editors can render paragraph boundaries as extra newlines.
    return " ".join((value or "").replace("\u200b", "").split())


async def _fill_video_prompt(page, prompt: str, account: str):
    expected = _normalize_composer_text(prompt)
    if not expected:
        raise ValueError("Video prompt must not be empty")
    editors = page.locator(EDITOR_SELECTOR)
    await editors.first.wait_for(state="visible", timeout=10000)
    # Refuse ambiguous targets instead of typing into an unrelated editor.
    if await editors.count() != 1:
        raise RuntimeError("Expected exactly one visible prompt editor")
    box = editors.first
    entered = ""
    for attempt in range(2):
        await box.fill("")
        await box.fill(prompt)
        # Allow the frontend to reconcile its editor state before reading back.
        await page.wait_for_timeout(600)
        entered = await box.evaluate(
            "element => element.value ?? element.innerText ?? element.textContent ?? ''"
        )
        matches = _normalize_composer_text(entered) == expected
        _log(f"[{account}] prompt attempt={attempt + 1} "
             f"chars={len(entered)}/{len(prompt)} matches={matches}")
        if matches:
            return box
    raise RuntimeError(
        f"Prompt was not preserved in composer: entered {len(entered)}/{len(prompt)} characters"
    )


async def generate_video(account: str, prompt: str, ratio: str = None,
                         duration: int = None, timeout: int = None,
                         model: str = "seedance_v2.0", use_extension: bool = True,
                         on_conversation_id=None, on_poll=None, on_balance=None,
                         reference_image_paths: list[str] | None = None,
                         on_submit=None) -> dict:
    """Full generation flow via UI automation."""
    timeout = timeout or config.VIDEO_TIMEOUT
    model_key = model.lower().replace("-", "_")
    if model_key in ("seedance_2.5", "seedance_v2.5", "seedance_25", "seedance_v25"):
        model_key = "seedance_v2.5"
    elif model_key in ("seedance_2.0", "seedance_v2.0", "seedance_20", "seedance_v20"):
        model_key = "seedance_v2.0"
    else:
        raise ValueError(f"Unsupported model: {model} (supported: seedance-2.0 / seedance-2.5)")
    if duration is not None and duration not in (10, 15, 30):
        raise ValueError("Dola supports durations of 10s, 15s, and 30s via extension")
    if duration == 30 and not use_extension:
        raise ValueError("30s generation requires Dola30 extension enabled")
    # 30s videos require extended generation timeout
    if duration == 30:
        timeout = max(timeout, 1800)
    if reference_image_paths:
        timeout = max(timeout, config.REFERENCE_VIDEO_TIMEOUT)
    async with async_playwright() as p:
        context = await launch_account_context(
            p, account, headless=False if use_extension else None,
            use_extension=use_extension)
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            _log(f"[{account}] Playwright context opened for generation")
            await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            cookies = await context.cookies("https://www.dola.com")
            ms_token, fp = cookie_value(cookies, "msToken"), cookie_value(cookies, "s_v_web_id")
            await _preflight_balance(page, ms_token, fp, config.VIDEO_REQUIRED_POINTS)

            # ---- UI Submission ----
            await _prepare_video_composer(page, account, bool(reference_image_paths))
            if reference_image_paths:
                await attach_reference_images(page, reference_image_paths)
            # Select model in UI
            try:
                async def model_ui_snapshot(stage):
                    snapshot = await page.evaluate("""() => ({
                        url: location.href,
                        text: (document.body?.innerText || '').slice(0, 5000),
                        candidates: [...document.querySelectorAll('button, [role="button"], [role="option"], li, div, span')]
                            .map(e => ({text: (e.innerText || '').trim(), role: e.getAttribute('role')}))
                            .filter(e => e.text && /model|seedance|2\\.0|2\\.5|高速|モデル/i.test(e.text))
                            .slice(0, 120)
                    })""")
                    safe = json.dumps(snapshot, ensure_ascii=False).encode("ascii", "backslashreplace").decode("ascii")
                    _log(f"[{account}] model UI snapshot stage={stage}: {safe[:8000]}")
                    await page.screenshot(path=f"dbg_model_{account}_{stage}.png", full_page=True)

                await model_ui_snapshot("before")
                current_model = None
                for label in ("モデル 2.0高速", "モデル 2.5"):
                    loc = page.get_by_text(label, exact=True).first
                    if await loc.count() and await loc.is_visible():
                        current_model = loc
                        break
                if current_model is None:
                    _log(f"[{account}] model selector labels not found; trying generic model selector")
                    current_model = page.get_by_text(re.compile(r"^モデル "), exact=False).first
                await current_model.click(timeout=5000)
                await page.wait_for_timeout(500)
                await model_ui_snapshot("menu")
                options = (("Dreamina Seedance 2.5",)
                           if model_key == "seedance_v2.5"
                           else ("Dreamina Seedance 2.0高速", "Dreamina Seedance 2.0", "Seedance2.0Fast"))
                selected = False
                for option_text in options:
                    loc = page.get_by_text(option_text, exact=False).first
                    if await loc.count() and await loc.is_visible():
                        await loc.click(timeout=5000)
                        selected = True
                        break
                if not selected:
                    raise RuntimeError("Model option not found")
                await page.wait_for_timeout(500)
            except Exception as e:
                try:
                    await page.screenshot(path=f"dbg_model_{account}_error.png", full_page=True)
                except Exception:
                    pass
                _log(f"[{account}] model selection failed type={type(e).__name__}: {e}")
                raise RuntimeError(f"Failed to set model ({model_key}): {str(e)[:120]}") from e
            if ratio:
                try:
                    await page.click("text=比率", timeout=3000)
                    await page.wait_for_timeout(500)
                    await page.click(f"text={ratio}", timeout=3000)
                except Exception as e:
                    _log(f"  (Failed to set ratio, using default: {str(e)[:80]})")
            if duration:
                try:
                    await page.click(f"text={duration}s", timeout=3000)
                except Exception:
                    try:  # Open duration dropdown
                        await page.get_by_text(re.compile(r"^\d+s$")).first.click(timeout=3000)
                        await page.wait_for_timeout(500)
                        await page.click(f"text={duration}s", timeout=3000)
                    except Exception as e:
                        _log(f"  (Failed to set duration, using default: {str(e)[:80]})")
            box = await _fill_video_prompt(page, prompt, account)
            # Mark the attempt before Enter: a timeout may occur after dispatch.
            if on_submit:
                on_submit()
            await box.press("Enter")
            _log(f"[{account}] UI submitted prompt: {prompt[:40]}")

            # ---- Captcha Solver (up to 3 attempts) ----
            solved_or_absent = False
            for attempt in range(1, 4):
                frame = None
                for _ in range(20):
                    await page.wait_for_timeout(1000)
                    frame = find_captcha_frame(page)
                    if frame:
                        break
                if not frame:
                    solved_or_absent = True
                    break
                _log(f"[{account}] Captcha detected, attempt {attempt} solving...")
                if await solve_slider(page, frame, attempt):
                    _log(f"[{account}] Captcha passed")
                    await page.wait_for_timeout(3000)  # Wait for frontend auto-retry
                    solved_or_absent = True
                    break
                _log(f"[{account}] Captcha not passed, retrying...")
            if not solved_or_absent:
                await page.screenshot(path="solve_fail.png")
                raise RiskControlError("Captcha failed 3 times")

            # ---- Wait for real conversation_id ----
            conv_id = ""
            for _ in range(30):
                await page.wait_for_timeout(1000)
                tail = page.url.rstrip("/").split("/")[-1]
                if tail.isdigit():
                    conv_id = tail
                    break
            if not conv_id:
                await page.screenshot(path="no_conv.png")
                raise TimeoutError("conversation_id not acquired within 30s")
            _log(f"[{account}] conversation_id={conv_id}, polling for video...")

            deadline = time.time() + timeout
            if on_conversation_id:
                on_conversation_id(account, conv_id, deadline)
            return await poll_conversation(account, page, context, conv_id, timeout, on_poll, on_balance)
        finally:
            _log(f"[{account}] Playwright context closing")
            await context.close()


async def _main():
    account = sys.argv[1] if len(sys.argv) > 1 else "acc1"
    prompt = sys.argv[2] if len(sys.argv) > 2 else "An orange cat napping on a sunny windowsill"
    ratio = sys.argv[3] if len(sys.argv) > 3 else None
    duration = int(sys.argv[4]) if len(sys.argv) > 4 else None
    model = sys.argv[5] if len(sys.argv) > 5 else "seedance_v2.0"
    result = await generate_video(account, prompt, ratio, duration, model=model)
    print(json.dumps(result, ensure_ascii=False, indent=2))


if __name__ == "__main__":
    try:
        asyncio.run(_main())
    except Exception:
        import traceback
        traceback.print_exc()