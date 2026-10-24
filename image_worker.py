from account_risk import check_risk
"""Dola image UI submission and original-image downloads (no marked fallback)."""
import asyncio
import config
import io
import json
import re
import time
from pathlib import Path
from urllib.parse import urlsplit

import aiohttp
from yarl import URL
from PIL import Image
from patchright.async_api import TimeoutError as PlaywrightTimeoutError

from account_connections import http_proxy
from browser import ACTIVE_CONTEXTS, ensure_account_session, launch_account_context
from browser_queue import async_playwright
from video_worker import POLL_JS
from video_worker_ui import find_captcha_frame, solve_slider

MODELS = ('Seedream 4.5', 'Seedream 5.0 Pro')
RATIOS = ('1:1', '2:3', '3:4', '4:3', '9:16', '16:9')
STYLES = {'auto': 'Tự nhiên', 'anime': 'Anime', '3d': '3D',
          'oil': 'Sơn dầu', 'watercolor': 'Màu nước', 'pixel': 'Pixel art'}
STYLE_LABELS = {'anime': r'^(Anime|Japanese Anime|アニメ)$', '3d': r'^3D$',
                'oil': r'^(Oil [Pp]ainting|油絵)$',
                'watercolor': r'^(Watercolor|水彩)$', 'pixel': r'^(Pixel [Aa]rt|Pixel|ピクセル)$'}
# Reuse the authenticated request protocol, without video-specific completion rules.
IMAGE_POLL_JS = POLL_JS.split('  const latestIndex =', 1)[0] + 'return {ok:true,messages:allMessages}; }'

IMAGE_DAILY_LIMIT_TEXT = '本日分の画像の上限数に到達しました。明日また作成してください。'


class ImageQuotaExceeded(RuntimeError):
    """Dola explicitly reported that this account's daily image limit is exhausted."""


class ImageContentRefused(RuntimeError):
    """Dola refused the prompt/content; changing accounts will not fix it."""


IMAGE_CONTENT_REFUSAL_MARKERS = (
    "?????????????????",
    "cannot generate the requested content",
    "unable to generate the requested content",
    "content policy", "safety policy", "disallowed content",
)


def is_image_content_refusal(text):
    value = str(text or '').lower()
    return any(marker.lower() in value for marker in IMAGE_CONTENT_REFUSAL_MARKERS)


def submitted_image_count(payload):
    """Count uploaded image resources in outgoing message blocks, not UI thumbnails."""
    resources = set()
    for message in payload.get('messages', []):
        blocks = message.get('content_block') or []
        for block in blocks:
            content = block.get('content') or {}
            for attachment in (content.get('attachment_block') or {}).get('attachments', []):
                if not isinstance(attachment, dict):
                    continue
                image = attachment.get('image') or {}
                if not isinstance(image, dict):
                    image = {}
                # Dola has changed the resource field name between clients.
                # Count only typed image attachments with a durable resource id.
                uri = (image.get('uri') or image.get('key') or image.get('resource_uri')
                       or image.get('resource_id') or image.get('id'))
                kind = str(attachment.get('type', '')).lower()
                if (attachment.get('type') == 1 or kind in ('image', 'image_attachment')) and uri:
                    resources.add(str(uri))
    return len(resources)


def submission_structure(payload):
    """Describe attachment shape without persisting prompts, URLs, or identifiers."""
    if not isinstance(payload, dict):
        return {'payload_type': type(payload).__name__}
    messages = payload.get('messages')
    if not isinstance(messages, list):
        return {'root_keys': sorted(map(str, payload.keys())), 'messages_type': type(messages).__name__}
    summary = {'root_keys': sorted(map(str, payload.keys())), 'messages': []}
    for message in messages[:10]:
        if not isinstance(message, dict):
            summary['messages'].append({'type': type(message).__name__})
            continue
        blocks = message.get('content_block')
        info = {'keys': sorted(map(str, message.keys())), 'blocks_type': type(blocks).__name__, 'blocks': []}
        for block in blocks[:20] if isinstance(blocks, list) else []:
            if not isinstance(block, dict):
                continue
            content = block.get('content') if isinstance(block.get('content'), dict) else {}
            attachment_block = content.get('attachment_block') if isinstance(content.get('attachment_block'), dict) else {}
            attachments = attachment_block.get('attachments')
            info['blocks'].append({'block_type': block.get('block_type'), 'content_keys': sorted(map(str, content.keys())),
                'attachments_type': type(attachments).__name__, 'attachments': [
                    {'type': a.get('type'), 'keys': sorted(map(str, a.keys())),
                     'image_keys': sorted(map(str, (a.get('image') or {}).keys())) if isinstance(a.get('image'), dict) else []}
                    for a in attachments[:10] if isinstance(a, dict)] if isinstance(attachments, list) else []})
        summary['messages'].append(info)
    return summary


async def upload_image_references(page, paths):
    """Wait for both image previews and successful Dola storage uploads."""
    composer = page.locator('.guidance-input-surface:visible').filter(has=page.locator('[contenteditable="true"]')).last
    statuses = []
    def on_response(response):
        path = urlsplit(response.url).path.lower()
        request = getattr(response, 'request', None)
        method = getattr(request, 'method', 'POST')
        if not isinstance(method, str):
            method = 'POST'
        if method.upper() in ('POST', 'PUT') and (
                '/upload/' in path or '/file' in path or path.endswith('/upload')):
            statuses.append(response.status)
    page.on('response', on_response)
    deadline = time.monotonic() + 90
    try:
        await composer.locator('input[type="file"]').first.set_input_files(paths, timeout=30000)
        await page.wait_for_function('''expected => {
            const roots=[...document.querySelectorAll('.guidance-input-surface')].filter(e=>e.getClientRects().length&&e.querySelector('[contenteditable="true"]'));
            const root=roots.at(-1); if(!root)return false;
            const cards=[...root.querySelectorAll('[data-kind="image"]')];
            const busy=[...root.querySelectorAll('[role="progressbar"],[aria-label],[title]')].some(e=>e.getAttribute('role')==='progressbar'||/upload progress|upload failed|upload error/i.test((e.getAttribute('aria-label')||'')+' '+(e.getAttribute('title')||'')));
            return cards.length===expected && !busy && cards.every(c=>{const i=c.querySelector('img');return i&&i.complete&&i.naturalWidth>0});
        }''', arg=len(paths), timeout=90000)
        while sum(200 <= status < 300 for status in statuses) < len(paths):
            if time.monotonic() >= deadline:
                raise TimeoutError(f'Chưa xác nhận tải ảnh tham chiếu lên Dola: {sum(200 <= s < 300 for s in statuses)}/{len(paths)}; HTTP={statuses[:20]}')
            await asyncio.sleep(.25)
        await page.wait_for_timeout(800)
    finally:
        page.remove_listener('response', on_response)


_IMAGE_MODE_LABEL = re.compile(r'(?:create\s*image|image\s*create|t[ạa]o\s*[ảa]nh|画像を作成)', re.I)
_RATIO_LABEL = re.compile(r'(?:aspect\s*ratio|t[ỷy]\s*l[ệe]|アスペクト比)\s*(?:1:1|2:3|3:4|4:3|9:16|16:9)?', re.I)


def _image_mode_controls(page):
    """Support both the old test ids and Dola's labelled composer controls."""
    return (
        page.get_by_test_id('skill_bar_button_3'),
        page.get_by_role('button', name=_IMAGE_MODE_LABEL),
        page.locator('button').filter(has_text=_IMAGE_MODE_LABEL),
        # Some current Dola composer variants use a div/span control instead
        # of a native button.
        page.locator('[role="button"]').filter(has_text=_IMAGE_MODE_LABEL),
        page.locator('[aria-haspopup]').filter(has_text=_IMAGE_MODE_LABEL),
        page.get_by_text(_IMAGE_MODE_LABEL),
    )


def _ratio_controls(page):
    return (
        page.get_by_test_id('chat_input_action_image_creation_ratio'),
        page.get_by_role('button', name=_RATIO_LABEL),
        page.locator('button').filter(has_text=_RATIO_LABEL),
        page.locator('[role="button"]').filter(has_text=_RATIO_LABEL),
        page.locator('[aria-haspopup]').filter(has_text=_RATIO_LABEL),
        page.get_by_text(_RATIO_LABEL),
    )


async def _first_visible(controls, timeout):
    """Return the first control that appears within the shared time budget."""
    deadline = time.monotonic() + timeout / 1000
    while time.monotonic() < deadline:
        for control in controls:
            try:
                if await control.is_visible():
                    return control
            except Exception:
                continue
        await asyncio.sleep(.25)
    raise PlaywrightTimeoutError('Image composer control was not visible')


async def activate_image_mode(page, timeout=20000):
    """Open either Dola image-composer variant and wait until ratio is present."""
    try:
        return await _first_visible(_ratio_controls(page), timeout=500)
    except PlaywrightTimeoutError:
        pass
    deadline = time.monotonic() + timeout / 1000
    last_error = None
    while time.monotonic() < deadline:
        for mode in _image_mode_controls(page):
            try:
                if not await mode.is_visible():
                    continue
                try:
                    await mode.click(timeout=4000)
                except PlaywrightTimeoutError:
                    # A transparent menu overlay can intercept the skill bar.
                    # This is the intended control, so use its DOM click.
                    await mode.evaluate('(element) => element.click()')
                last_error = None
                break
            except Exception as exc:
                last_error = exc
        try:
            return await _first_visible(_ratio_controls(page), timeout=3500)
        except PlaywrightTimeoutError:
            await page.wait_for_timeout(500)
    if last_error:
        raise last_error
    raise PlaywrightTimeoutError('Image composer did not expose an aspect-ratio control')


async def select_image_ratio(page, ratio):
    """Set and verify the ratio across the old and labelled Dola composers."""
    try:
        button = await _first_visible(_ratio_controls(page), timeout=10000)
    except PlaywrightTimeoutError as first_exc:
        try:
            button = await activate_image_mode(page, timeout=20000)
        except Exception:
            try:
                state = await page.evaluate('''() => ({
                    ratio_nodes: document.querySelectorAll('[data-testid=\"chat_input_action_image_creation_ratio\"]').length,
                    image_mode_nodes: document.querySelectorAll('[data-testid=\"skill_bar_button_3\"]').length,
                    labelled_image_buttons: [...document.querySelectorAll('button')].filter(e => /create\\s*image|image\\s*create|tạo\\s*ảnh|画像を作成/i.test(e.innerText)).length,
                    labelled_ratio_buttons: [...document.querySelectorAll('button')].filter(e => /aspect\\s*ratio|tỷ\\s*lệ|アスペクト比/i.test(e.innerText)).length,
                    editors: document.querySelectorAll('[contenteditable=\"true\"]').length,
                    seedream_buttons: [...document.querySelectorAll('button')].filter(e => /seedream/i.test(e.innerText)).length,
                    captcha_frames: [...document.querySelectorAll('iframe')].filter(e => /captcha|verify/i.test(e.src)).length
                })''')
                path = urlsplit(page.url).path
                detail = f'path={path}, state={json.dumps(state, ensure_ascii=True)}'
            except Exception:
                detail = 'page state unavailable'
            raise RuntimeError('Kh?ng t?m th?y n?t ch?n t? l? ?nh sau khi kh?i t?o l?i image mode; ' + detail) from first_exc

    def selected(text):
        return bool(re.search(r'(?<!\d)' + re.escape(ratio) + r'(?!\d)', text))

    if selected(await button.inner_text()):
        return
    try:
        await button.click(timeout=4000)
    except PlaywrightTimeoutError:
        await button.evaluate('(element) => element.click()')
    # Dola variants label menu items as `16:9`, `16:9 (Landscape)`, or
    # `16:9 横長`. The composer button starts with "Aspect ratio" instead,
    # so matching a value at the beginning stays unambiguous.
    option = page.get_by_text(
        re.compile(r'^\s*' + re.escape(ratio) + r'(?:\s|$|[\uFF08(])')
    ).filter(visible=True).last
    await option.wait_for(state='visible', timeout=10000)
    try:
        await option.click(timeout=3000)
    except PlaywrightTimeoutError:
        # Some Dola layouts place the editor above the visible menu in hit testing.
        # Dispatch on the uniquely identified option and verify the selected value.
        await option.evaluate('(element) => element.click()')
    if not selected(await button.inner_text()):
        raise RuntimeError('Dola chưa áp dụng tỷ lệ đã chọn.')


def parse_image_result(messages):
    """Only generated assistant creations after the most recent user message."""
    boundary = max((int(m.get('index_in_conv') or 0) for m in messages
                    if int(m.get('user_type') or 0) == 1), default=0)
    images, seen, texts, error_codes = [], set(), [], set()
    finished, found, incomplete = True, False, False
    for message in messages:
        if int(message.get('user_type') or 0) != 2 or int(message.get('index_in_conv') or 0) <= boundary:
            continue
        found = True
        blocks = message.get('content_block')
        if not blocks:
            blocks = message.get('content', [])
            if isinstance(blocks, str):
                try:
                    blocks = json.loads(blocks)
                except ValueError:
                    blocks = []
        if not isinstance(blocks, list):
            blocks = []
        ext = message.get('ext') or {}
        code = str(ext.get('ai_creation_res_code') or '')
        if re.fullmatch(r'[1-9]\d{0,15}', code):
            error_codes.add(code)
        tools = ext.get('ai_creation_tool_list') or []
        if isinstance(tools, str):
            try:
                tools = json.loads(tools)
            except ValueError:
                tools = []
        if isinstance(tools, list):
            for tool in tools:
                if isinstance(tool, dict):
                    code = str(tool.get('fail_code') or '')
                    if re.fullmatch(r'[1-9]\d{0,15}', code):
                        error_codes.add(code)
        ended = ext.get('is_finish') in (True, 1, '1', 'true') or bool(ext.get('finish_reason_chat'))
        finished &= int(message.get('content_status') or 0) == 0 and ended
        for block in blocks:
            if block.get('is_finish') is False:
                finished = False
            content = block.get('content') or {}
            text = (content.get('text_block') or {}).get('text')
            if text:
                texts.append(text)
            if block.get('block_type') != 2074:
                continue
            for creation in (content.get('creation_block') or {}).get('creations', []):
                if creation.get('type') != 1:
                    continue
                image = creation.get('image') or {}
                raw = (image.get('image_ori_raw') or {}).get('url', '')
                if image.get('status') != 2 or not raw.startswith('https://'):
                    incomplete = True
                    continue
                identity = creation.get('id') or raw
                if identity not in seen:
                    images.append({'creation_id': str(identity), 'url': raw})
                    seen.add(identity)
    return {'images': images, 'finished': found and finished, 'incomplete': incomplete,
            'text': '\n'.join(texts)[:1500], 'error_codes': sorted(error_codes)}


async def download_originals(images, account, directory):
    directory = Path(directory)
    directory.mkdir(parents=True, exist_ok=True)
    results = []
    async with aiohttp.ClientSession(timeout=aiohttp.ClientTimeout(total=120)) as session:
        for index, item in enumerate(images):
            # Only server-provided HTTPS originals; URLs are never accepted from API callers.
            # CDN signatures cover the exact escaped URL; requoting breaks some signatures.
            for attempt in range(3):
                try:
                    async with session.get(URL(item['url'], encoded=True), **http_proxy(account)) as response:
                        response.raise_for_status()
                        data = bytearray()
                        async for chunk in response.content.iter_chunked(65536):
                            data.extend(chunk)
                            if len(data) > 40 * 1024 * 1024:
                                raise ValueError('Ảnh vượt quá giới hạn 40 MB.')
                    break
                except (aiohttp.ClientPayloadError, aiohttp.ClientConnectionError, asyncio.TimeoutError):
                    if attempt == 2:
                        raise
                    await asyncio.sleep(attempt + 1)
            with Image.open(io.BytesIO(data)) as image:
                image.verify()
                suffix = {'PNG': '.png', 'JPEG': '.jpg', 'WEBP': '.webp'}.get(image.format)
                width, height = image.size
            if not suffix:
                raise ValueError('Định dạng ảnh Dola chưa được hỗ trợ.')
            filename = f'{index}{suffix}'
            path = directory / filename
            partial = path.with_suffix('.part')
            partial.write_bytes(data)
            partial.replace(path)
            results.append({'filename': filename, 'width': width, 'height': height,
                            'bytes': len(data), 'creation_id': item['creation_id'],
                            'source': 'image_ori_raw'})
    return results


async def disable_media_panel_pointer_events(page):
    """Keep the optional Dola30 media panel from covering the Dola send button."""
    panel = page.locator('#watermark-free-media-panel')
    if await panel.count():
        await panel.evaluate("node => { node.style.pointerEvents = 'none'; }")


async def generate_image(account, prompt, model, ratio, style, directory, checkpoint,
                         conversation_id=None, timeout=600, reference_image_paths=None):
    # Prefer the user's already-open headed Dola context.  It preserves the
    # exact browser, extension, network and session that succeeded manually;
    # do not close it when the image job ends.  Otherwise retain the low-RAM
    # headless worker path.
    active = ACTIVE_CONTEXTS.get(account)
    context = active[0] if active and not active[0].is_closed() and not active[1] else None
    playwright = None
    playwright_session = None
    owns_context = context is None
    if owns_context:
        playwright_session = async_playwright()
        playwright = await playwright_session.__aenter__()
        try:
            context = await launch_account_context(
                playwright, account,
                headless=not config.IMAGE_INTERACTIVE_WORKER,
                use_extension=config.IMAGE_INTERACTIVE_WORKER)
        except BaseException:
            await playwright_session.__aexit__(None, None, None)
            raise
    try:
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto('https://www.dola.com/chat' + ('/' + conversation_id if conversation_id else ''),
                            wait_until='domcontentloaded', timeout=60000)
            await page.wait_for_timeout(3000)
            await ensure_account_session(page, context)
            submission_error = None
            submission_retries = 0
            if not conversation_id:
                checkpoint(phase='preparing')
                cookie = page.get_by_test_id('cookie_banner_notice')
                if await cookie.is_visible():
                    await cookie.click()
                # Dola currently serves both the old test-id composer and a
                # labelled composer ("Create image" / "画像を作成").  Do not
                # proceed until its aspect-ratio control confirms image mode.
                await activate_image_mode(page)
                model_button = page.get_by_role('button').filter(has_text=re.compile('Seedream'))
                await model_button.click()
                model_pattern = r'^Seedream\s*4\.5$' if model == MODELS[0] else r'^Seedream\s*5\.0\s*(Pro|プロ)$'
                await page.get_by_text(re.compile(model_pattern, re.I), exact=True).last.click(timeout=10000)
                if not re.search(model_pattern[1:-1], await model_button.inner_text(), re.I):
                    raise RuntimeError('Dola chưa áp dụng model đã chọn.')
                await select_image_ratio(page, ratio)
                if style != 'auto':
                    await page.get_by_role('button', name=re.compile(r'^(Style|スタイル)$')).click()
                    await page.get_by_text(re.compile(STYLE_LABELS[style], re.I), exact=True).last.click()
                # Filling the rich editor after attaching images clears its image nodes.
                await page.locator('[contenteditable=true]').fill(prompt)
                if reference_image_paths:
                    checkpoint(phase='uploading')
                    await upload_image_references(page, reference_image_paths)
                if reference_image_paths:
                    composer = page.locator('.guidance-input-surface:visible').filter(has=page.locator('[contenteditable="true"]')).last
                    if await composer.locator('[data-kind="image"]').count() != len(reference_image_paths):
                        raise RuntimeError('Ảnh tham chiếu bị mất khi chọn cấu hình. Chưa gửi yêu cầu tạo ảnh.')
                # Persist BEFORE dispatch: an interrupted submission must never auto-resubmit.
                submission_error = None
                if reference_image_paths:
                    async def guard_submission(route):
                        nonlocal submission_error
                        if route.request.method != 'POST':
                            await route.continue_()
                            return
                        try:
                            payload = route.request.post_data_json
                            count = submitted_image_count(payload)
                        except (ValueError, TypeError, AttributeError):
                            payload = None
                            count = 0
                        if count != len(reference_image_paths):
                            detail = json.dumps(submission_structure(payload), ensure_ascii=True, separators=(',', ':'))[:900]
                            submission_error = (f'Đã chặn gửi: request Dola không chứa đủ ảnh tham chiếu đã tải lên '
                                                f'(nhận diện {count}/{len(reference_image_paths)}). Cấu trúc: {detail}')
                            await route.abort()
                            return
                        await route.continue_()
                    await page.route('**/chat/completion*', guard_submission)
                checkpoint(phase='submitting')
                await disable_media_panel_pointer_events(page)
                await page.get_by_test_id('chat_input_send_button').click()
            deadline = time.monotonic() + timeout
            attempts = 0
            while time.monotonic() < deadline:
                check_risk(context)
                if not conversation_id and submission_error:
                    # The UI can lose attachment metadata between preview and
                    # dispatch. Re-upload once and resend the same request;
                    # the route guard prevents a request without references.
                    if submission_retries < 1 and reference_image_paths:
                        submission_retries += 1
                        submission_error = None
                        checkpoint(phase='reuploading')
                        await upload_image_references(page, reference_image_paths)
                        await disable_media_panel_pointer_events(page)
                        await page.get_by_test_id('chat_input_send_button').click()
                        continue
                    checkpoint(phase='upload_failed')
                    raise RuntimeError(submission_error)
                frame = find_captcha_frame(page)
                if frame:
                    attempts += 1
                    checkpoint(phase='verifying')
                    if attempts > 3 or not await solve_slider(page, frame, attempts):
                        if attempts >= 3:
                            raise RuntimeError('Không hoàn thành xác minh Dola. Kiểm tra tài khoản rồi thử lại.')
                    await asyncio.sleep(2)
                    continue
                if not conversation_id:
                    match = re.search(r'/chat/(\d+)(?:$|[?#])', page.url)
                    if match:
                        conversation_id = match[1]
                        checkpoint(conversation_id=conversation_id, phase='generating')
                if conversation_id:
                    payload = await asyncio.wait_for(page.evaluate(IMAGE_POLL_JS, {
                        'conversationId': conversation_id, 'afterIndex': 0}), timeout=35)
                    if not payload.get('ok'):
                        if payload.get('status') in (401, 403):
                            raise RuntimeError('Phiên Dola không còn quyền truy cập. Kiểm tra đăng nhập tài khoản.')
                    else:
                        result = parse_image_result(payload.get('messages', []))
                        # Wait for the whole assistant response, not just the first image.
                        signature = tuple(i['creation_id'] for i in result['images'])
                        if signature and not result['incomplete'] and result['finished']:
                            checkpoint(phase='downloading')
                            return await download_originals(result['images'], account, directory)
                        if result['finished'] and not signature and result['text'] and not result['incomplete']:
                            codes = ','.join(result['error_codes'])
                            message = 'Dola không trả ảnh' + (f' (mã: {codes})' if codes else '') + ': ' + result['text']
                            if IMAGE_DAILY_LIMIT_TEXT in result['text']:
                                raise ImageQuotaExceeded(message)
                            if is_image_content_refusal(result['text']):
                                raise ImageContentRefused(message)
                            raise RuntimeError(message)
                await asyncio.sleep(4)
            raise TimeoutError('Chưa nhận được ảnh gốc không watermark trong 10 phút. Có thể tiếp tục kiểm tra nếu đã có hội thoại.')
    finally:
        if owns_context:
            await context.close()
            await playwright_session.__aexit__(None, None, None)
