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
from video_worker import GenerationRejectedError, POLL_JS, RiskControlError, _download, extract_unwatermarked_url


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



def start_generation_capture(context, account):
    from urllib.parse import urlsplit
    root = Path("diagnostics") / f"{account}_{time.time_ns()}"
    root.mkdir(parents=True, exist_ok=True)
    pending = set()
    counter = 0
    def redact(value):
        if isinstance(value, dict):
            return {k: ("[redacted]" if any(x in k.lower() for x in ("token", "cookie", "authorization")) else redact(v)) for k, v in value.items()}
        if isinstance(value, list):
            return [redact(v) for v in value]
        if isinstance(value, str):
            try:
                decoded = json.loads(value)
                if isinstance(decoded, (dict, list)):
                    return redact(decoded)
            except (ValueError, TypeError):
                pass
            if value.startswith(("https://", "http://")):
                u = urlsplit(value)
                return u.scheme + "://" + u.netloc + u.path
        return value
    async def capture(response):
        nonlocal counter
        u = urlsplit(response.url)
        if not ((u.hostname or "").endswith((".dola.com", ".bytevcloudapi.com", ".byteimg.com", ".ibytedtos.com")) or u.hostname == "dola.com" or "/upload/v1/" in u.path):
            return
        if not any(x in u.path for x in ("/im/", "/alice/resource/", "review", "creation", "generate", "/upload/")) and (u.hostname or "").endswith("dola.com"):
            return
        try:
            body = await response.json()
            counter += 1
            record = {"time": time.time(), "host": u.hostname, "path": u.path, "status": response.status, "body": redact(body)}
            (root / f"response_{counter:05d}.json").write_text(json.dumps(record, ensure_ascii=False), encoding="utf-8")
        except Exception as exc:
            _log(f"[capture] path={u.path} unavailable={type(exc).__name__}")
    def listener(response):
        task = asyncio.create_task(capture(response))
        pending.add(task)
        task.add_done_callback(pending.discard)
    context.on("response", listener)
    _log(f"[capture] account={account} directory={root}")
    async def finish():
        context.remove_listener("response", listener)
        if pending:
            await asyncio.gather(*list(pending), return_exceptions=True)
    return finish


async def attach_reference_images(page, image_paths: list[str]) -> None:
    """Upload once; preserve attachments for manual recovery on failure."""
    if not image_paths:
        return
    root = page.locator('.guidance-input-surface:visible').filter(has=page.locator('[contenteditable="true"]')).last
    try:
        _log(f"[upload] single attempt files={json.dumps([Path(p).name for p in image_paths])}")
        await _upload_reference_attempt(page, image_paths, len(image_paths))
        names = await root.locator('[data-kind="image"] img').evaluate_all("imgs => imgs.map(e => e.alt)")
        expected = [Path(path).name for path in image_paths]
        if names != expected:
            raise RuntimeError(f"Attachment order/name mismatch: expected={expected}, actual={names}")
        _log(f"[upload] batch complete count={len(names)}")
    except Exception as exc:
        if page.is_closed():
            raise
        _log(f"[upload] manual recovery required; no retry or deletion; reason={type(exc).__name__}: {exc}")
        await _wait_for_manual_reference_upload(page, len(image_paths))


async def _wait_for_manual_reference_upload(page, expected: int) -> None:
    """Require explicit user confirmation, with a bounded browser wait."""
    panel_id = "dola-manual-upload-recovery"
    await page.evaluate("""({id, expected}) => {
        document.getElementById(id)?.remove();
        const panel = document.createElement('div');
        panel.id = id;
        panel.style.cssText = 'position:fixed;top:12px;right:12px;z-index:2147483647;background:#fff;color:#111;padding:16px;border:2px solid #e99b20;border-radius:10px;max-width:360px;font:14px sans-serif;box-shadow:0 4px 20px #0004';
        const message = document.createElement('p');
        message.textContent = `Upload t? ??ng g?p l?i. H?y upload ?? ${expected} ?nh theo ??ng th? t? r?i b?m ti?p t?c. Th?i gian ch?: 15 ph?t.`;
        const button = document.createElement('button');
        button.textContent = '?? upload xong ? ti?p t?c';
        button.style.cssText = 'padding:10px;cursor:pointer;background:#2463eb;color:white;border:0;border-radius:6px';
        button.onclick = () => {
            const roots = [...document.querySelectorAll('.guidance-input-surface')].filter(r => r.getClientRects().length && r.querySelector('[contenteditable="true"]'));
            const root = roots.at(-1);
            const cards = root ? [...root.querySelectorAll('[data-kind="image"]')] : [];
            const images = cards.map(c => c.querySelector('img'));
            const busy = cards.some(c => [...c.querySelectorAll('[role="progressbar"],[aria-label],[title]')].some(e => e.getAttribute('role') === 'progressbar' || /upload progress|upload failed|failed to upload|upload error/i.test((e.getAttribute('aria-label') || '') + ' ' + (e.getAttribute('title') || ''))));
            if (cards.length !== expected || !images.every(i => i && i.complete && i.naturalWidth > 0) || busy) {
                message.textContent = `?nh ch?a s?n s?ng. C?n ?? ${expected} ?nh, ho?n t?t upload r?i b?m ti?p t?c.`;
                return;
            }
            panel.dataset.confirmed = 'true';
            button.disabled = true;
        };
        panel.append(message, button);
        document.body.append(panel);
    }""", {"id": panel_id, "expected": expected})
    _log(f"[upload] waiting for manual confirmation expected={expected} timeout=900s")
    try:
        await page.wait_for_function("id => document.getElementById(id)?.dataset.confirmed === 'true'", arg=panel_id, timeout=900000)
        _log("[upload] manual upload confirmed by user; continuing generation")
    except Exception as exc:
        raise RuntimeError("Manual image upload was not confirmed within 15 minutes or browser was closed") from exc
    finally:
        if not page.is_closed():
            try:
                await page.evaluate("id => document.getElementById(id)?.remove()", panel_id)
            except Exception:
                pass


async def _upload_reference_attempt(page, image_paths: list[str], total_expected: int) -> None:
    """Uploads reference images through native file input and waits for TOS upload."""
    if not image_paths:
        return
    file_input = page.locator('.guidance-input-surface:visible').filter(has=page.locator('[contenteditable="true"]')).last.locator('input[type="file"]').first
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
    composer = page.locator('.guidance-input-surface:visible').filter(has=page.locator('[contenteditable="true"]')).last
    if not await composer.count():
        raise RuntimeError("Cannot identify upload composer; refusing to submit")
    input_info = await file_input.evaluate("""e => ({accept:e.accept, multiple:e.multiple,
        inSelectedComposer:e.closest('.guidance-input-surface') ===
            [...document.querySelectorAll('.guidance-input-surface')].filter(r => r.getClientRects().length && r.querySelector('[contenteditable="true"]')).at(-1)})""")
    _log(f"[upload] input={json.dumps(input_info)} page_inputs={await page.locator('input[type=file]').count()}")
    events = []
    failures = []
    snapshot_state = {}
    manifest = [{"index": i + 1, "file": Path(path).name, "bytes": Path(path).stat().st_size} for i, path in enumerate(image_paths)]

    def on_response(response):
        url = response.url
        tracked = "/alice/resource/prepare_upload" in url or "/upload/v1/" in url
        if tracked:
            events.append((response.status, url))
        if tracked or "bytevcloudapi.com" in url:
            from urllib.parse import urlsplit, parse_qs
            endpoint = urlsplit(url)
            action = parse_qs(endpoint.query).get("Action", [])
            _log(f"[upload] response status={response.status} method={response.request.method} host={endpoint.hostname} path={endpoint.path} action={json.dumps(action)} counted={tracked}")

    def on_request_failed(request):
        if any(part in request.url for part in ("/alice/resource/", "/upload/v1/", "bytevcloudapi.com")):
            from urllib.parse import urlsplit
            endpoint = urlsplit(request.url)
            failures.append({"host": endpoint.hostname, "path": endpoint.path,
                             "method": request.method, "resource_type": request.resource_type,
                             "error": request.failure or "unknown network failure"})

    page.on("response", on_response)
    page.on("requestfailed", on_request_failed)
    try:
        _log(f"[upload] start files={json.dumps(manifest)}")
        await file_input.set_input_files(image_paths)
        expected = len(image_paths)
        deadline = time.time() + max(60, expected * 20)
        last_progress = None
        last_check_log = 0.0
        while time.time() < deadline:
            prepare_count = sum("/alice/resource/prepare_upload" in url and 200 <= status < 300
                                for status, url in events)
            tos_count = sum("/upload/v1/" in url and 200 <= status < 300
                            for status, url in events)
            # Wait for thumbnails and TOS completion before sending
            snapshot_state = await composer.evaluate("""root => ({
                text: root.innerText,
                images: [...root.querySelectorAll('[data-kind="image"] > img')].map(e => ({alt:e.alt, complete:e.complete, width:e.naturalWidth})),
                indicators: [...root.querySelectorAll('[title],[aria-label],[role="alert"],[data-state]')].map(e => ({title:e.getAttribute('title'), aria:e.getAttribute('aria-label'), role:e.getAttribute('role'), state:e.getAttribute('data-state'), text:e.getAttribute('role')==='alert'?e.innerText:''}))
            })""")
            thumb_count = len(snapshot_state["images"])
            labels = " ".join((v or "") for item in snapshot_state["indicators"] for v in (item['title'], item['aria'], item['text']))
            checks = {"tos_enough": tos_count >= expected,
                      "thumbnail_count_matches": thumb_count == total_expected,
                      "thumbnails_loaded": all(i["complete"] and i["width"] > 0 for i in snapshot_state["images"])}
            check_detail = {"checks": checks, "expected": expected, "total_expected": total_expected,
                            "prepare": prepare_count, "tos": tos_count, "thumbnails": thumb_count,
                            "images": snapshot_state["images"], "labels": labels,
                            "statuses": [status for status, _ in events], "failures": failures}
            check_serialized = json.dumps(check_detail)
            if check_serialized != last_progress or time.monotonic() - last_check_log >= 5:
                _log(f"[upload] readiness remaining_s={max(0, deadline - time.time()):.1f} {check_serialized}")
                last_progress = check_serialized
                last_check_log = time.monotonic()
            if re.search(r"upload failed|failed to upload|upload error|\u30a2\u30c3\u30d7\u30ed\u30fc\u30c9.*\u5931\u6557|\u4e0a\u4f20\u5931\u8d25", labels, re.I):
                raise RuntimeError("Reference image upload rejected by UI: " + labels)
            if failures or any(status >= 400 for status, _ in events):
                raise RuntimeError(f"Reference image upload network failure: failures={failures}, statuses={[status for status, _ in events]}")
            # Preparation requests may cover multiple images; do not require one per file.
            if tos_count >= expected and thumb_count == total_expected and all(i["complete"] and i["width"] > 0 for i in snapshot_state["images"]):
                _log("[upload] readiness passed; settling 800ms")
                await page.wait_for_timeout(800)
                _log(f"[upload] settle finished statuses={[status for status, _ in events]} failures={json.dumps(failures)}")
                _log(f"[upload] Reference images uploaded: {expected} image(s)")
                return
            await page.wait_for_timeout(250)
        statuses = [status for status, _ in events]
        _log(f"[upload] timeout response_statuses={statuses} network_failures={failures}")
        snapshot_path = f"reference_upload_timeout_{time.time_ns()}.png"
        try:
            await page.screenshot(path=snapshot_path, full_page=True)
            _log(f"[upload] timeout screenshot={snapshot_path}")
        except Exception as exc:
            _log(f"[upload] screenshot failed: {type(exc).__name__}")
        raise TimeoutError(
            f"Reference image upload timeout: prepare={prepare_count}, tos={tos_count}/{expected}, thumbnails={thumb_count}"
        )
    except Exception as exc:
        base = Path("diagnostics") / f"upload_failure_{time.time_ns()}"
        base.parent.mkdir(parents=True, exist_ok=True)
        diagnostic = {"error_type": type(exc).__name__, "error": str(exc), "files": manifest, "composer": snapshot_state, "statuses": [status for status, _ in events], "network_failures": failures}
        base.with_suffix(".json").write_text(json.dumps(diagnostic, ensure_ascii=False, indent=2), encoding="utf-8")
        try:
            await page.screenshot(path=str(base.with_suffix(".png")), full_page=True)
        except Exception as capture_error:
            _log(f"[upload] screenshot unavailable={type(capture_error).__name__}")
        _log(f"[upload] stopped before submission; diagnostic={base}.json error={type(exc).__name__}: {exc}")
        raise
    finally:
        page.remove_listener("response", on_response)
        page.remove_listener("requestfailed", on_request_failed)


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
                            timeout: int, on_poll=None, on_balance=None, after_index=0) -> dict:
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
                POLL_JS, {"conversationId": conversation_id, "msToken": ms_token, "fp": fp,
                          "afterIndex": after_index}), timeout=30)
        except Exception as e:
            poll_failures += 1
            _log(f"  Polling exception attempt={poll_failures}/3: {e}")
            if poll_failures >= 3:
                raise RuntimeError("Conversation monitoring interrupted; open conversation to recover") from e
            continue
        poll_failures = 0
        if poll.get("rejection"):
            rejection = poll["rejection"]
            _log(f"[{account}] generation rejected code={rejection['code']} reason={rejection['reason']}")
            raise GenerationRejectedError(
                f"Dola rejection ({rejection['code']}): {rejection['reason']}",
                code=rejection['code'], latest_index=poll.get("latestIndex", 0))
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


# Both the separate controls and the combined Japanese settings button are used.
VIDEO_DURATION_RE = re.compile(r"(?:^|[\s\u00b7])\d+\s*(?:s|\u79d2)(?:$|\s)")


async def _video_composer_ready(root, needs_upload: bool):
    model = root.get_by_role("button", name=re.compile(r"\u30e2\u30c7\u30eb|Model|Seedance", re.I)).first
    # Older layouts expose the model label outside the button's accessible name.
    has_model = (await model.is_visible()
                 or await root.get_by_text(re.compile(r"^(?:\u30e2\u30c7\u30eb|Model)$", re.I)).first.is_visible())
    has_duration = await root.get_by_text(VIDEO_DURATION_RE).first.is_visible()
    return (has_model and has_duration
            and (not needs_upload or await root.locator('input[type="file"]').count() > 0))


async def _prepare_video_composer(page, account: str, needs_upload: bool):
    try:
        await _clear_composer_draft(page, account)
        # Reuse the active video composer after a rejection. The entry button
        # disappears once video mode opens; its absence is not a click failure.
        opened = False
        for attempt in range(1, 61):
            _, root = await _composer(page)
            if await _video_composer_ready(root, needs_upload):
                _log(f"[{account}] video composer ready attempt={attempt}")
                await _composer_snapshot(page, account, "video_ready")
                return
            button = page.get_by_role("button", name=re.compile(r"^(?:\u52d5\u753b\u3092\u4f5c\u6210|Create (?:a )?video)$", re.I))
            if not opened and await button.is_visible() and await button.is_enabled():
                await button.click(timeout=10000)
                opened = True
            await page.wait_for_timeout(500)
        raise TimeoutError("Video composer controls did not become ready after 60 checks")
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


async def _start_composer_trace(page, account: str):
    """Keep bounded UI evidence without recording prompts or image contents."""
    events = []

    def record(source, event):
        events.append(event)
        del events[:-300]

    await page.context.expose_binding("__dolaComposerTrace", record)
    await page.context.add_init_script(r"""(() => {
        const state = () => {
            const roots = [...document.querySelectorAll('.guidance-input-surface')]
                .filter(e => e.getClientRects().length && e.querySelector('[contenteditable="true"]'));
            const root = roots.at(-1);
            const controls = root ? [...root.querySelectorAll('button')]
                .map(e => (e.innerText || '').trim().slice(0, 80)) : [];
            return {path: location.pathname, controls,
                images: root?.querySelectorAll('[data-kind="image"]').length || 0,
                video: controls.some(t => /(?:^|\s)\d+\s*(?:s|?)(?:$|\s)/.test(t))};
        };
        const emit = event => window.__dolaComposerTrace({time: Date.now(), ...event}).catch(() => {});
        document.addEventListener('click', e => {
            const target = e.target.closest('button, [role="button"], [role="menuitem"], [role="option"], a, svg') || e.target;
            // Never collect editor text, HTML, URLs or attachment contents.
            const named = target.matches('button, [role="button"], [role="menuitem"], [role="option"]');
            emit({type: 'click', trusted: e.isTrusted, target: {
                tag: target.tagName, role: target.getAttribute('role'),
                text: named ? (target.innerText || '').slice(0, 80) : '',
                icon: target.getAttribute('data-dbx-name'),
                classes: typeof target.className === 'string' ? target.className.slice(0, 160) : ''
            }, state: state()});
        }, true);
        document.addEventListener('keydown', e => {
            if (['Escape', 'Enter'].includes(e.key)) emit({type: 'key', key: e.key, state: state()});
        }, true);
        let previous = '';
        setInterval(() => {
            const current = state(), key = JSON.stringify(current);
            if (key !== previous) {
                previous = key;
                emit({type: 'state', state: current});
            }
        }, 100);
    })()""")

    def finish():
        destination = Path("diagnostics") / f"composer_trace_{account}_{time.time_ns()}.json"
        destination.parent.mkdir(parents=True, exist_ok=True)
        destination.write_text(json.dumps(events, ensure_ascii=True, indent=2), encoding="utf-8")
        _log(f"[{account}] composer interaction trace: {destination}")

    return finish


async def _select_video_duration(page, duration: int, account: str):
    expected = re.compile(rf"^{duration}\s*(?:s|\u79d2)$", re.I)
    try:
        _, root = await _composer(page)
        control = root.get_by_role("button", name=VIDEO_DURATION_RE).first
        # Ratio selection may leave the shared settings popup open.
        # Only consider visible exact matches, never hidden or historical text.
        options = page.get_by_text(expected).locator("visible=true")
        if not await options.count():
            await control.click(timeout=5000)
        await options.first.wait_for(state="visible", timeout=10000)
        if await options.count() != 1:
            raise RuntimeError("Multiple visible duration options match the request")
        await options.first.click(timeout=5000)
        # A successful click alone does not prove React retained the selection.
        for _ in range(20):
            _, root = await _composer(page)
            control = root.get_by_role("button", name=VIDEO_DURATION_RE).first
            selected = (await control.inner_text()).strip()
            if expected.fullmatch(selected):
                await page.keyboard.press("Escape")
                _log(f"[{account}] duration verified: {selected}")
                return
            await page.wait_for_timeout(250)
        raise RuntimeError(f"Duration control still shows {selected!r}")
    except Exception as exc:
        _log(f"[{account}] duration selection failed type={type(exc).__name__}: {exc}")
        await _composer_snapshot(page, account, "duration_failed")
        try:
            await page.screenshot(path=f"dbg_duration_{account}_error.png", full_page=True)
        except Exception:
            pass
        raise RuntimeError(
            f"Failed to preserve requested duration: {duration}: "
            f"{type(exc).__name__}: {str(exc)[:500]}"
        ) from exc


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
        finish_capture = start_generation_capture(context, account)
        finish_composer_trace = None
        try:
            page = context.pages[0] if context.pages else await context.new_page()
            finish_composer_trace = await _start_composer_trace(page, account)
            _log(f"[{account}] Playwright context opened for generation")
            await page.goto("https://www.dola.com/chat", timeout=60000, wait_until="domcontentloaded")
            await page.wait_for_timeout(5000)
            cookies = await context.cookies("https://www.dola.com")
            ms_token, fp = cookie_value(cookies, "msToken"), cookie_value(cookies, "s_v_web_id")
            await _preflight_balance(page, ms_token, fp, config.VIDEO_REQUIRED_POINTS)

            after_index = 0
            for retry in range(6):
                # ---- UI Submission ----
                for setup_attempt in range(3):
                    try:
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
                            _, root = await _composer(page)
                            current_model = root.get_by_role(
                                "button", name=re.compile(r"\u30e2\u30c7\u30eb\s|Model\b|Seedance", re.I)
                            )
                            await current_model.click(timeout=5000)
                            await page.wait_for_timeout(500)
                            await model_ui_snapshot("menu")
                            options = (("Dreamina Seedance 2.5",)
                                       if model_key == "seedance_v2.5"
                                       else ("Dreamina Seedance 2.0高速", "Dreamina Seedance 2.0", "Seedance2.0Fast"))
                            # Conversation titles can exactly match a model name.
                            # Only select entries inside the currently open model menu.
                            menu = page.locator('[role="menu"][data-state="open"]:visible')
                            await menu.wait_for(state="visible", timeout=5000)
                            selected = False
                            for option_text in options:
                                loc = menu.get_by_role("menuitem").filter(
                                    has=page.get_by_text(option_text, exact=True)
                                )
                                if await loc.count() and await loc.is_visible():
                                    await loc.click(timeout=5000)
                                    selected = True
                                    break
                            if not selected:
                                raise RuntimeError("Model option not found in open model menu")
                            await menu.wait_for(state="hidden", timeout=5000)
                            expected_version = "2.5" if model_key == "seedance_v2.5" else "2.0"
                            for _ in range(20):
                                _, root = await _composer(page)
                                control = root.get_by_role(
                                    "button", name=re.compile(r"\u30e2\u30c7\u30eb|Model|Seedance", re.I)
                                )
                                if (await _video_composer_ready(root, bool(reference_image_paths))
                                        and expected_version in await control.inner_text()):
                                    break
                                await page.wait_for_timeout(250)
                            else:
                                raise RuntimeError("Video composer did not preserve selected model")
                        except Exception as e:
                            try:
                                await page.screenshot(path=f"dbg_model_{account}_error.png", full_page=True)
                            except Exception:
                                pass
                            _log(f"[{account}] model selection failed type={type(e).__name__}: {e}")
                            raise RuntimeError(f"Failed to set model ({model_key}): {str(e)[:120]}") from e
                        if ratio:
                            try:
                                _, root = await _composer(page)
                                ratio_control = root.get_by_text(re.compile(r"^(?:\u6bd4\u7387|(?:Aspect )?ratio)$", re.I)).first
                                if await ratio_control.is_visible():
                                    await ratio_control.click(timeout=3000)
                                else:
                                    await root.get_by_role("button", name=VIDEO_DURATION_RE).first.click(timeout=3000)
                                await page.wait_for_timeout(500)
                                await page.click(f"text={ratio}", timeout=3000)
                            except Exception as e:
                                _log(f"[{account}] ratio selection failed type={type(e).__name__}: {e}")
                                await _composer_snapshot(page, account, "ratio_failed")
                                raise RuntimeError(
                                    f"Failed to preserve requested ratio: {ratio}: {type(e).__name__}: {str(e)[:300]}"
                                ) from e
                        if duration:
                            await _select_video_duration(page, duration, account)
                        # Upload/model changes can asynchronously reset Dola to chat mode.
                        await page.wait_for_timeout(500)
                        _, root = await _composer(page)
                        if not await _video_composer_ready(root, bool(reference_image_paths)):
                            raise RuntimeError("Video composer reset while applying settings")
                        break
                    except Exception:
                        # Retry only a lost video composer, before any submission.
                        # Rebuild all settings and attachments together on the next attempt.
                        try:
                            _, root = await _composer(page)
                            composer_lost = not await _video_composer_ready(root, bool(reference_image_paths))
                        except Exception:
                            composer_lost = False
                        if not composer_lost or setup_attempt == 2:
                            raise
                        _log(f"[{account}] video composer reset; rebuilding setup attempt={setup_attempt + 2}/3")
                        await _composer_snapshot(page, account, "setup_reset")
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
                try:
                    return await poll_conversation(
                        account, page, context, conv_id, timeout, on_poll, on_balance,
                        after_index=after_index)
                except GenerationRejectedError as exc:
                    if  retry >= 5:
                        raise
                    if exc.latest_index <= after_index:
                        raise RuntimeError("Cannot safely identify the rejected submission for retry") from exc
                    after_index = exc.latest_index
                    _log(f"[{account}] Dola {exc.code}: retry {retry + 1}/5 in conversation {conv_id}; preserving request parameters")
                    await page.wait_for_timeout(5000)
        finally:
            if finish_composer_trace:
                try:
                    finish_composer_trace()
                except Exception as exc:
                    _log(f"[{account}] composer trace save failed: {exc}")
            _log(f"[{account}] Playwright context closing")
            await context.close()
            await finish_capture()


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
