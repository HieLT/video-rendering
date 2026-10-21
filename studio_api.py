"""Authenticated workflow endpoints; submitted agent results are validated artifacts."""
import json
import re
import time
from pathlib import Path

from fastapi import Depends, File, Header, HTTPException, UploadFile
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field, ValidationError
from typing import Literal

from film_studio import checked_url
from studio_workflow import Conflict, Workflow, digest
from studio_runtime import StudioRuntime
from studio_exports import sync_reference_export


class NewFilm(BaseModel):
    title: str = Field(min_length=1, max_length=120)
    screenplay: str = Field(min_length=1, max_length=500000)
    request_id: str = Field(min_length=1, max_length=100)
    approval_mode: Literal['manual', 'agent'] = 'manual'


class ApprovalMode(BaseModel):
    approval_mode: Literal['manual', 'agent']


class Approval(BaseModel):
    gate: str
    artifact_id: str
    revision: int


class ChangeRequest(BaseModel):
    gate: str
    notes: str = Field(min_length=1, max_length=10000)
    revision: int


class ScreenplayRestore(BaseModel):
    artifact_id: str = Field(min_length=1, max_length=100)
    notes: str = Field(min_length=1, max_length=10000)


class AgentResult(BaseModel):
    output: dict


class SkillSource(BaseModel):
    source: str = Field(min_length=1, max_length=100000)
    version: str = Field(min_length=1, max_length=100)


class BrowserConnection(BaseModel):
    endpoint: str = Field(default='http://127.0.0.1:9222', max_length=100)


SKILLS = ('emily2040', 'sjinn-ai', 'sd25-pe')
ROLE_SKILLS = {'reference_selector': ('reference_selection',)}


def register_studio_workflow(app, auth, registry, image_library, submit_video, get_video, reference_limit=30, image_studio=None):
    flow = Workflow(registry.database, reference_limit)
    reference_export_root = Path(__file__).resolve().parent / 'exports' / 'film-studio'
    runtime = StudioRuntime(flow, submit_video, get_video, image_studio, image_library,
                            lambda run_id: sync_reference_export(flow, image_library, run_id, reference_export_root))
    app.state.film_studio_runtime = runtime
    app.router.add_event_handler('startup', runtime.start)
    app.router.add_event_handler('shutdown', runtime.stop)

    def authenticated(x_admin_key: str | None = Header(default=None)):
        auth(x_admin_key)

    def call(fn, *args):
        try:
            return fn(*args)
        except (KeyError, StopIteration):
            raise HTTPException(404, 'Không tìm thấy run/task/artifact.')
        except Conflict as exc:
            raise HTTPException(409, str(exc))
        except (ValidationError, ValueError) as exc:
            raise HTTPException(422, str(exc))

    def skill_sources():
        with flow.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS studio_skill_sources (name TEXT PRIMARY KEY, version TEXT NOT NULL, source TEXT NOT NULL)')
            return {r[0]: {'version': r[1], 'source': r[2], 'hash': digest(r[2])}
                    for r in db.execute('SELECT name,version,source FROM studio_skill_sources')}

    def envelope(run_id, task_id):
        result = flow.envelope(run_id, task_id)
        common = Path(__file__).parent / 'docs/film-studio/project-instructions.txt'
        result['project_instructions'] = common.read_text(encoding='utf-8')
        if result['role'] == 'reference_selector':
            selector_path = Path(__file__).parent / 'docs/film-studio/skills/reference-selection/SKILL.md'
            source = selector_path.read_text(encoding='utf-8')
            sources = {'reference_selection': {'version': 'local-' + digest(source)[:16],
                                               'source': source, 'hash': digest(source)}}
            result['skills'] = flow.bind_skills(run_id, task_id, sources)
            result['missing_skills'] = [name for name in ROLE_SKILLS[result['role']] if name not in result['skills']]
        elif result['role'] == 'prompt_engineer':
            sources = skill_sources()
            result['skills'] = (flow.bind_skills(run_id, task_id, sources) if set(SKILLS) <= set(sources) else sources)
            result['missing_skills'] = [s for s in SKILLS if s not in result['skills']]
        return result

    prefix = '/api/admin/film-studio'
    deps = [Depends(authenticated)]

    @app.get(prefix + '/status', dependencies=deps)
    def status():
        chat_status = chat_worker.status()
        result = {**runtime.status(), **chat_status, 'skills': {k: {'version': v['version'], 'hash': v['hash']}
                                              for k, v in skill_sources().items()}}
        if chat_status['chatgpt_transport'] == 'connected':
            result['blockers'] = [b for b in result.get('blockers', []) if 'Chưa có kết nối ChatGPT' not in b]
        return result

    @app.put(prefix + '/skills/{name}', dependencies=deps)
    def save_skill(name: str, body: SkillSource):
        if name not in SKILLS:
            raise HTTPException(422, 'Tên skill không hợp lệ.')
        skill_sources()
        with flow.connection() as db:
            db.execute('INSERT INTO studio_skill_sources VALUES (?,?,?) ON CONFLICT(name) DO UPDATE SET version=excluded.version,source=excluded.source',
                       (name, body.version, body.source))
        return {'name': name, 'hash': digest(body.source)}

    @app.get(prefix + '/runs', dependencies=deps)
    def list_runs():
        return flow.list()

    @app.post(prefix + '/runs', dependencies=deps, status_code=201)
    def new_run(body: NewFilm):
        settings = registry.read()
        if not settings.project_url or not settings.profile_id or not settings.instructions_confirmed:
            raise HTTPException(409, 'Lưu profile, Project URL và xác nhận instruction trước.')
        call(checked_url, settings.project_url, True)
        return call(flow.create, body.title, body.screenplay, body.request_id, settings.project_url, body.approval_mode)

    @app.get(prefix + '/runs/{run_id}', dependencies=deps)
    def read_run(run_id: str):
        return call(flow.get, run_id)

    @app.post(prefix + '/runs/{run_id}/approve', dependencies=deps)
    def approve(run_id: str, body: Approval):
        call(flow.approve, run_id, body.gate, body.artifact_id, body.revision)
        return flow.get(run_id)

    @app.put(prefix + '/runs/{run_id}/approval-mode', dependencies=deps)
    def approval_mode(run_id: str, body: ApprovalMode):
        call(flow.set_approval_mode, run_id, body.approval_mode)
        return flow.get(run_id)

    @app.post(prefix + '/runs/{run_id}/rewind-screenplay', dependencies=deps)
    def rewind_screenplay(run_id: str):
        call(flow.rewind_to_script_review, run_id)
        return flow.get(run_id)

    @app.post(prefix + '/runs/{run_id}/restore-screenplay-source', dependencies=deps)
    def restore_screenplay_source(run_id: str, body: ScreenplayRestore):
        task_id = call(flow.restore_screenplay_source, run_id, body.artifact_id, body.notes)
        return {'task_id': task_id, 'run': flow.get(run_id)}

    @app.post(prefix + '/runs/{run_id}/tasks/{task_id}/retry', dependencies=deps)
    def retry_task(run_id: str, task_id: str):
        call(flow.retry_task, run_id, task_id)
        return flow.get(run_id)

    @app.post(prefix + '/runs/{run_id}/changes', dependencies=deps)
    def changes(run_id: str, body: ChangeRequest):
        call(flow.request_changes, run_id, body.gate, body.notes, body.revision)
        return flow.get(run_id)

    @app.post(prefix + '/runs/{run_id}/control/{action}', dependencies=deps)
    def control(run_id: str, action: str):
        call(flow.control, run_id, action)
        return flow.get(run_id)

    @app.get(prefix + '/runs/{run_id}/tasks/{task_id}/input', dependencies=deps)
    def task_input(run_id: str, task_id: str):
        return call(envelope, run_id, task_id)

    @app.post(prefix + '/runs/{run_id}/tasks/{task_id}/result', dependencies=deps)
    def task_result(run_id: str, task_id: str, body: AgentResult):
        run = call(flow.get, run_id)
        task = next((t for t in run['tasks'] if t['id'] == task_id), None)
        if not task:
            raise HTTPException(404, 'Task không tồn tại.')
        if task['kind'] == 'image_create':
            image_id = body.output.get('image_id', '')
            call(image_library.path, image_id)
            # Actual generated file must have been received for this exact task.
            receipts = [a['content']['image_id'] for a in run['artifacts']
                        if a['kind'] == 'image_upload' and a['content']['task_id'] == task_id]
            if image_id not in receipts:
                raise HTTPException(409, 'Ảnh phải được tải vào đúng nhiệm vụ tạo ảnh trước.')
        if task['kind'] in ('prompt', 'prompt_repair') and task['status'] != 'completed':
            sources = flow.content(run, task['skill_artifact']) if task.get('skill_artifact') else {}
            hashes = body.output.get('skill_sources', {})
            if set(SKILLS) - set(sources) or any(hashes.get(s) != sources[s]['hash'] for s in SKILLS):
                raise HTTPException(409, 'Chưa đủ ba skill source hoặc hash phiên bản prompt không khớp.')
        call(flow.submit, run_id, task_id, body.output)
        if task['kind'] == 'image_create':
            call(sync_reference_export, flow, image_library, run_id, reference_export_root)
        return flow.get(run_id)

    @app.post(prefix + '/runs/{run_id}/export-references', dependencies=deps)
    def export_references(run_id: str):
        folder = call(sync_reference_export, flow, image_library, run_id, reference_export_root)
        return {'folder': str(folder)}

    @app.post(prefix + '/runs/{run_id}/tasks/{task_id}/image', dependencies=deps)
    async def upload_image(run_id: str, task_id: str, file: UploadFile = File(...)):
        run = call(flow.get, run_id)
        task = next((t for t in run['tasks'] if t['id'] == task_id), None)
        if not task or task['kind'] != 'image_create' or task['status'] != 'pending' or run['status'] == 'cancelled':
            raise HTTPException(409, 'Nhiệm vụ không nhận ảnh.')
        data = await file.read(20 * 1024 * 1024 + 1)
        try:
            item = image_library.save(data, file.filename, 20 * 1024 * 1024)
        except (ValueError, OSError) as exc:
            raise HTTPException(422, str(exc))
        with flow.edit(run_id) as current:
            current_task = next(t for t in current['tasks'] if t['id'] == task_id)
            if current_task['status'] != 'pending' or current['status'] == 'cancelled':
                raise HTTPException(409, 'Nhiệm vụ đã thay đổi trong lúc tải ảnh.')
            flow.artifact(current, 'image_upload', {'task_id': task_id, 'image_id': item['id']}, task['inputs'])
        return {'image_id': item['id']}

    @app.get(prefix + '/runs/{run_id}/images/{image_id}', dependencies=deps)
    def read_image(run_id: str, image_id: str):
        run = call(flow.get, run_id)
        if not re.fullmatch('[a-f0-9]{64}', image_id) or not any(
                a['kind'] == 'image_upload' and a['content']['image_id'] == image_id for a in run['artifacts']):
            raise HTTPException(404, 'Ảnh không thuộc dự án.')
        return FileResponse(call(image_library.path, image_id), headers={'Cache-Control': 'private, no-store'})

    @app.post(prefix + '/runs/{run_id}/inspect', dependencies=deps)
    def inspect_again(run_id: str, clip_number: int | None = None):
        call(flow.retry_inspection, run_id, clip_number)
        return flow.get(run_id)

    @app.post(prefix + '/runs/{run_id}/clips/{number}/retry-admission', dependencies=deps)
    def retry_admission(run_id: str, number: int):
        call(flow.retry_admission, run_id, number)
        return flow.get(run_id)

    from studio_chatgpt import ChatGPTWorker
    import config
    chat_worker = ChatGPTWorker(flow, registry, envelope,
                               lambda run_id, task_id, output: task_result(run_id, task_id, AgentResult(output=output)),
                               image_library, get_video, config.DOWNLOAD_DIR)
    chat_worker.use_image_studio = image_studio is not None
    app.state.film_studio_chatgpt = chat_worker
    app.router.add_event_handler('shutdown', chat_worker.stop)

    @app.post(prefix + '/browser/connect', dependencies=deps)
    async def connect_browser(body: BrowserConnection):
        try:
            await chat_worker.connect(body.endpoint)
        except Exception as exc:
            chat_worker.error = str(exc)[:1000]
            raise HTTPException(409, chat_worker.error) from exc
        return chat_worker.status()

    @app.post(prefix + '/browser/connect-profile', dependencies=deps)
    async def connect_profile_browser():
        try:
            await chat_worker.connect_profile()
        except Exception as exc:
            chat_worker.error = str(exc)[:1000]
            raise HTTPException(409, chat_worker.error) from exc
        return chat_worker.status()

    @app.post(prefix + '/browser/disconnect', dependencies=deps)
    async def disconnect_browser():
        await chat_worker.stop()
        return chat_worker.status()

    @app.post(prefix + '/runs/{run_id}/tasks/{task_id}/stop-chat-generation', dependencies=deps)
    async def stop_chat_generation(run_id: str, task_id: str):
        """Explicitly stop a busy chat with a matching, unsent task draft."""
        async with chat_worker.lock:
            run = call(flow.get, run_id)
            task = next((t for t in run['tasks'] if t['id'] == task_id), None)
            if not task or task['status'] != 'pending' or task['delivery'] != 'not_sent':
                raise HTTPException(409, 'Task is not an unsent pending draft.')
            settings = registry.read()
            url = settings.sessions.get(task['role'])
            if not url:
                raise HTTPException(409, 'No session is assigned to this role.')
            page = await chat_worker.driver.page(url)
            marker = 'FILM_STUDIO_TASK:' + task_id
            observed = await chat_worker.driver.inspect(page, marker)
            if not observed.get('draft') or observed.get('sent'):
                raise HTTPException(409, 'The matching task is not an unsent draft.')
            controls = page.locator('[data-testid="stop-button"], button[aria-label="停止"], '
                                    'button[aria-label="Stop streaming"], button[aria-label="Stop generating"]')
            visible = [controls.nth(i) for i in range(await controls.count()) if await controls.nth(i).is_visible()]
            if len(visible) != 1:
                raise HTTPException(409, 'Expected exactly one visible stop control.')
            await visible[0].click(timeout=10000)
            return {'task_id': task_id, 'stop_clicked': True}

    @app.get(prefix + '/browser/diagnostics', dependencies=deps)
    async def browser_diagnostics():
        """Read-only UI signals for diagnosing Project navigation failures."""
        context = chat_worker.driver.context
        if not context:
            return {'pages': []}
        pages = []
        for page in list(context.pages):
            if not page.url.startswith('https://chatgpt.com/'):
                continue
            try:
                editors = page.locator('#prompt-textarea, textarea, [contenteditable="true"]')
                visible = 0
                for index in range(await editors.count()):
                    visible += bool(await editors.nth(index).is_visible())
                headings = await page.locator('h1, h2').all_inner_texts()
                controls = await page.locator('button, a[download]').evaluate_all('''nodes => nodes.map(n => ({
                    text:(n.innerText || '').slice(0,80), aria:n.getAttribute('aria-label'),
                    title:n.getAttribute('title'), testid:n.getAttribute('data-testid'),
                    download:n.getAttribute('download')
                })).filter(x => x.text || x.aria || x.title || x.testid || x.download).slice(-80)''')
                images = await page.locator('img').evaluate_all('''nodes => nodes.map(n => ({
                    alt:n.getAttribute('alt'), src:(n.getAttribute('src') || '').slice(0,160)
                })).slice(-20)''')
                attachment_nodes = await page.locator('img[alt*="添付"], img[alt*="attachment" i]').evaluate_all('''nodes => nodes.map(n => {
                    let e = n, chain = [];
                    for (let i=0; e && i<5; i++, e=e.parentElement) {
                        chain.push({tag:e.tagName, role:e.getAttribute('role'), aria:e.getAttribute('aria-label'),
                            testid:e.getAttribute('data-testid'), html:(e.outerHTML || '').slice(0,900)});
                    }
                    return {alt:n.getAttribute('alt'), chain};
                }).slice(-12)''')
                composer_attachments = await page.locator('form img').evaluate_all('''nodes => nodes.map(n => ({
                    alt:n.getAttribute('alt'), src:(n.getAttribute('src') || '').slice(0,120),
                    parent:(n.parentElement?.outerHTML || '').slice(0,1200)
                }))''')
                file_inputs = await page.locator('input[type="file"]').evaluate_all('''nodes => nodes.map(n => ({
                    accept:n.getAttribute('accept'), multiple:n.multiple, id:n.id, files:n.files ? n.files.length : null,
                    ancestors:Array.from((function*(e){for(let i=0;e && i<6;i++,e=e.parentElement) yield e})(n))
                        .map(e => ({tag:e.tagName, id:e.id, role:e.getAttribute('role'),
                            testid:e.getAttribute('data-testid'), className:String(e.className).slice(0,160)}))
                }))''')
                composers = await page.locator('form').evaluate_all('''nodes => nodes.map(n => ({
                    editors:n.querySelectorAll('#prompt-textarea, textarea, [contenteditable="true"]').length,
                    files:n.querySelectorAll('input[type="file"]').length,
                    visible:!!(n.offsetWidth || n.offsetHeight || n.getClientRects().length),
                    buttons:Array.from(n.querySelectorAll('button')).map(b => ({aria:b.getAttribute('aria-label'), testid:b.getAttribute('data-testid')}))
                }))''')
                body_text = await page.locator('body').inner_text()
                notices = await page.locator('[role="alert"], [data-sonner-toast], [data-state="open"][role="status"]').all_inner_texts()
                markers = list(dict.fromkeys(re.findall(r'FILM_STUDIO_TASK:agent_[a-zA-Z0-9_-]+', body_text)))[-6:]
                image_matches = {marker: bool(await chat_worker.driver.image_preview_for_marker(page, marker))
                                 for marker in markers}
                pages.append({'url': page.url, 'title': await page.title(),
                              'visible_editors': visible, 'headings': headings[:5],
                              'controls': controls, 'images': images, 'image_matches': image_matches,
                              'attachment_nodes': attachment_nodes,
                              'composer_attachments': composer_attachments,
                              'file_inputs': file_inputs, 'composers': composers, 'notices': notices[:10]})
            except Exception as exc:
                pages.append({'url': page.url, 'error': str(exc)[:200]})
        return {'pages': pages}

    @app.get(prefix + '/runs/{run_id}/tasks/{task_id}/chat-controls', dependencies=deps)
    async def chat_controls(run_id: str, task_id: str, menu: bool = False,
                            delete_preview: bool = False):
        """Inspect controls on a completed Film Studio task chat before cleanup."""
        run = call(flow.get, run_id)
        task = next((t for t in run['tasks'] if t['id'] == task_id), None)
        if not task or task['status'] != 'completed' or not task.get('chat_url'):
            raise HTTPException(409, 'Task chat is not a completed delivery.')
        url = task['chat_url']
        if url in registry.read().sessions.values():
            raise HTTPException(409, 'This chat is an active agent session.')
        context = chat_worker.driver.context
        if not context:
            raise HTTPException(409, 'ChatGPT browser is disconnected.')
        page = await context.new_page()
        try:
            await page.goto(url, wait_until='domcontentloaded', timeout=30000)
            await page.wait_for_timeout(1500)
            if menu or delete_preview:
                more = page.get_by_role('button', name='その他', exact=True)
                if await more.count() != 1:
                    raise HTTPException(409, 'Conversation menu is unavailable or ambiguous.')
                await more.click()
            if delete_preview:
                remove = page.get_by_role('menuitem', name='削除', exact=True)
                if await remove.count() != 1:
                    raise HTTPException(409, 'Delete action is unavailable or ambiguous.')
                await remove.click()
            controls = await page.locator('button').evaluate_all('''nodes => nodes.map(n => ({
                text:(n.innerText || '').slice(0,80), aria:n.getAttribute('aria-label'),
                testid:n.getAttribute('data-testid'), title:n.getAttribute('title')
            })).filter(x => x.text || x.aria || x.testid || x.title)''')
            menu_items = await page.locator('[role="menuitem"]').evaluate_all('''nodes => nodes.map(n => ({
                text:(n.innerText || '').slice(0,100), aria:n.getAttribute('aria-label')
            }))''')
            dialogs = await page.locator('[role="dialog"], [role="alertdialog"]').evaluate_all('''nodes => nodes.map(n => ({
                text:(n.innerText || '').slice(0,600), buttons:Array.from(n.querySelectorAll('button')).map(b => (b.innerText || '').slice(0,80))
            }))''')
            return {'url': page.url, 'title': await page.title(), 'buttons': controls[:120],
                    'menu_items': menu_items[:30], 'dialogs': dialogs[:3]}
        finally:
            await page.close()

    @app.post(prefix + '/runs/{run_id}/tasks/{task_id}/chat-delete', dependencies=deps)
    async def delete_unused_task_chat(run_id: str, task_id: str):
        """Delete only a completed, superseded Script Editor chat via ChatGPT UI."""
        run = call(flow.get, run_id)
        if not run['paused']:
            raise HTTPException(409, 'Pause the run before deleting old chats.')
        task = next((t for t in run['tasks'] if t['id'] == task_id), None)
        if not task or task['role'] != 'script_editor' or task['status'] != 'completed':
            raise HTTPException(409, 'Only completed Script Editor chats can be cleaned up.')
        url = task.get('chat_url', '')
        if not re.fullmatch(r'https://chatgpt\.com/c/[0-9a-fA-F-]{36}', url):
            raise HTTPException(409, 'This is not an old external chat.')
        if url in registry.read().sessions.values():
            raise HTTPException(409, 'This chat is an active agent session.')
        for summary in flow.list():
            other = flow.get(summary['id'])
            if any(t.get('chat_url') == url and t['status'] != 'completed' for t in other['tasks']):
                raise HTTPException(409, 'A pending task still uses this chat.')
        with flow.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS studio_chat_cleanup (url TEXT PRIMARY KEY, run_id TEXT, task_id TEXT, deleted_at REAL)')
            previous = db.execute('SELECT deleted_at FROM studio_chat_cleanup WHERE url=?', (url,)).fetchone()
            if previous:
                return {'url': url, 'status': 'already_deleted' if previous[0] else 'already_requested'}
        context = chat_worker.driver.context
        if not context:
            raise HTTPException(409, 'ChatGPT browser is disconnected.')
        async with chat_worker.lock:
            page = await context.new_page()
            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                if page.url != url:
                    return {'url': url, 'status': 'already_gone'}
                await page.wait_for_timeout(1500)
                title = await page.title()
                if page.url != url:
                    return {'url': url, 'status': 'already_gone'}
                if not re.search(r'screenplay|kịch bản', title, re.I):
                    raise HTTPException(409, 'Chat title does not match a Script Editor task.')
                more = page.get_by_role('button', name='その他', exact=True)
                if await more.count() != 1:
                    raise HTTPException(409, 'Conversation menu is unavailable or ambiguous.')
                await more.click()
                remove = page.get_by_role('menuitem', name='削除', exact=True)
                if await remove.count() != 1:
                    raise HTTPException(409, 'Delete action is unavailable or ambiguous.')
                await remove.click()
                dialog = page.get_by_role('alertdialog').filter(has_text='チャットを削除しますか？')
                if await dialog.count() != 1:
                    dialog = page.get_by_role('dialog').filter(has_text='チャットを削除しますか？')
                if await dialog.count() != 1:
                    raise HTTPException(409, 'Delete confirmation is unavailable or ambiguous.')
                if title not in await dialog.inner_text():
                    raise HTTPException(409, 'Delete dialog refers to a different chat.')
                confirm = dialog.get_by_role('button', name='チャットを削除', exact=True)
                if await confirm.count() != 1:
                    raise HTTPException(409, 'Delete confirmation button is unavailable or ambiguous.')
                await confirm.click()
                with flow.connection() as db:
                    db.execute('INSERT INTO studio_chat_cleanup VALUES (?,?,?,?)',
                               (url, run_id, task_id, None))
                return {'url': url, 'status': 'requested'}
            finally:
                await page.close()

    @app.post(prefix + '/runs/{run_id}/tasks/{task_id}/chat-delete-verify', dependencies=deps)
    async def verify_deleted_task_chat(run_id: str, task_id: str):
        run = call(flow.get, run_id)
        task = next((t for t in run['tasks'] if t['id'] == task_id), None)
        if not task or task['role'] != 'script_editor' or task['status'] != 'completed':
            raise HTTPException(409, 'Task is not a completed Script Editor delivery.')
        url = task.get('chat_url', '')
        if not re.fullmatch(r'https://chatgpt\.com/c/[0-9a-fA-F-]{36}', url):
            raise HTTPException(409, 'This is not an old external chat.')
        context = chat_worker.driver.context
        if not context:
            raise HTTPException(409, 'ChatGPT browser is disconnected.')
        async with chat_worker.lock:
            page = await context.new_page()
            try:
                await page.goto(url, wait_until='domcontentloaded', timeout=30000)
                if page.url != 'https://chatgpt.com/':
                    try:
                        await page.wait_for_url('https://chatgpt.com/', timeout=15000)
                    except Exception:
                        pass
                gone = page.url == 'https://chatgpt.com/'
            finally:
                await page.close()
        if gone:
            with flow.connection() as db:
                db.execute('CREATE TABLE IF NOT EXISTS studio_chat_cleanup (url TEXT PRIMARY KEY, run_id TEXT, task_id TEXT, deleted_at REAL)')
                db.execute('INSERT INTO studio_chat_cleanup VALUES (?,?,?,?) ON CONFLICT(url) DO UPDATE SET deleted_at=excluded.deleted_at',
                           (url, run_id, task_id, time.time()))
        return {'url': url, 'status': 'deleted' if gone else 'still_present'}

    return runtime
