"""Persistent image jobs sharing the gateway's account/browser locks."""
import asyncio
import hashlib
import json
import sqlite3
import shutil
import time
import uuid
from pathlib import Path
from typing import Literal

from fastapi import Header, HTTPException
from fastapi.responses import FileResponse
from pydantic import BaseModel, Field

from image_worker import MODELS, RATIOS, STYLES, generate_image, ImageQuotaExceeded, IMAGE_DAILY_LIMIT_TEXT

IMAGE_QUOTA_COOLDOWN_SECONDS = 24 * 60 * 60


def is_network_access_denied_error(exc):
    """True only for the browser-level Dola navigation denial."""
    text = str(exc or '')
    return 'ERR_NETWORK_ACCESS_DENIED' in text and ('Page.goto' in text or 'navigating to' in text)


class ImageRequest(BaseModel):
    prompt: str = Field(min_length=1, max_length=12000)
    model: Literal['Seedream 4.5', 'Seedream 5.0 Pro'] = 'Seedream 5.0 Pro'
    ratio: Literal['1:1', '2:3', '3:4', '4:3', '9:16', '16:9'] = '16:9'
    style: Literal['auto', 'anime', '3d', 'oil', 'watercolor', 'pixel'] = 'auto'
    account: str = Field(default='', max_length=100)
    references: list[str] = Field(default_factory=list, max_length=6)
    request_id: str = Field(min_length=8, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')


class ImageRetry(BaseModel):
    account: str | None = Field(default=None, max_length=100)
    request_id: str = Field(min_length=8, max_length=100, pattern=r'^[a-zA-Z0-9_-]+$')


class ImageBatchRequest(ImageRequest):
    count: int = Field(default=1, ge=1, le=10)


class ImageStudio:
    def __init__(self, root, pool, library, unavailable=None, worker=generate_image, max_concurrency=20):
        self.root, self.pool, self.library = Path(root), pool, library
        self.unavailable = unavailable or (lambda name: False)
        self.worker = worker
        self.runners = {}
        self.slots = asyncio.Semaphore(max(1, max_concurrency))
        self.root.mkdir(parents=True, exist_ok=True)
        self.db = sqlite3.connect(self.root / 'jobs.db', check_same_thread=False)
        self.db.row_factory = sqlite3.Row
        self.db.execute('''CREATE TABLE IF NOT EXISTS image_jobs (
            id TEXT PRIMARY KEY, request_id TEXT UNIQUE NOT NULL, prompt TEXT NOT NULL,
            model TEXT NOT NULL, ratio TEXT NOT NULL, style TEXT NOT NULL,
            requested_account TEXT NOT NULL, account TEXT, conversation_id TEXT,
            status TEXT NOT NULL, phase TEXT NOT NULL, error TEXT,
            outputs TEXT NOT NULL DEFAULT '[]', created_at REAL NOT NULL, updated_at REAL NOT NULL)''')
        self.db.commit()
        if 'references_json' not in {r[1] for r in self.db.execute('PRAGMA table_info(image_jobs)')}:
            self.db.execute("ALTER TABLE image_jobs ADD COLUMN references_json TEXT NOT NULL DEFAULT '[]'")
            self.db.commit()
        with self.db:
            self.db.execute('''CREATE TABLE IF NOT EXISTS image_account_limits (
                account TEXT PRIMARY KEY, detected_at REAL NOT NULL, retry_at REAL NOT NULL)''')
            # Preserve the original observation time when importing previous explicit quota errors.
            self.db.execute('''INSERT INTO image_account_limits(account, detected_at, retry_at)
                SELECT account, MAX(updated_at), MAX(updated_at) + ? FROM image_jobs
                WHERE account IS NOT NULL AND instr(coalesce(error,''), ?) > 0 GROUP BY account
                ON CONFLICT(account) DO UPDATE SET
                    detected_at=MAX(image_account_limits.detected_at, excluded.detected_at),
                    retry_at=MAX(image_account_limits.retry_at, excluded.retry_at)''',
                (IMAGE_QUOTA_COOLDOWN_SECONDS, IMAGE_DAILY_LIMIT_TEXT))

    def get(self, identity):
        row = self.db.execute('SELECT * FROM image_jobs WHERE id=?', (identity,)).fetchone()
        if not row:
            raise HTTPException(404, 'Không tìm thấy tác vụ ảnh.')
        row = dict(row)
        row['outputs'] = json.loads(row['outputs'])
        row['references'] = json.loads(row.pop('references_json'))
        for index, image in enumerate(row['outputs']):
            image['url'] = f'/api/admin/image-studio/jobs/{identity}/files/{index}?v={row["updated_at"]}'
        return row

    def update(self, identity, **fields):
        allowed = {'account', 'requested_account', 'conversation_id', 'status', 'phase', 'error', 'outputs'}
        if fields.keys() - allowed:
            raise ValueError('Invalid job fields')
        if 'outputs' in fields:
            fields['outputs'] = json.dumps(fields['outputs'])
        fields['updated_at'] = time.time()
        with self.db:
            self.db.execute('UPDATE image_jobs SET ' + ','.join(k + '=?' for k in fields) + ' WHERE id=?',
                            (*fields.values(), identity))

    def limited_accounts(self):
        return [dict(row) for row in self.db.execute(
            'SELECT account, detected_at, retry_at FROM image_account_limits WHERE retry_at > ?', (time.time(),))]

    def mark_image_limit(self, account, detected_at=None):
        now = time.time() if detected_at is None else detected_at
        with self.db:
            self.db.execute('''INSERT INTO image_account_limits(account, detected_at, retry_at) VALUES (?,?,?)
                ON CONFLICT(account) DO UPDATE SET detected_at=MAX(detected_at,excluded.detected_at),
                retry_at=MAX(retry_at,excluded.retry_at)''',
                (account, now, now + IMAGE_QUOTA_COOLDOWN_SECONDS))

    def accounts(self, include_image_limited=False):
        # Video daily usage is deliberately not used as an image quota.
        limited = set() if include_image_limited else {row['account'] for row in self.limited_accounts()}
        return [a for a in self.pool.list_accounts() if a.get('auth_state') == 'active'
                and a.get('scheduling') and not a.get('cooling')
                and not a.get('busy') and not a.get('active_requests', 0)
                and a.get('ready_to_generate', True)
                and not a.get('rate_limited') and not a.get('quota_blocked') and a['name'] not in limited]

    def create(self, body):
        prompt = body.prompt.strip()
        if not prompt:
            raise HTTPException(422, 'Nhập mô tả ảnh trước khi tạo.')
        previous = self.db.execute('SELECT id FROM image_jobs WHERE request_id=?', (body.request_id,)).fetchone()
        if previous:
            job = self.get(previous['id'])
            if any(job[k] != v for k, v in {'prompt': prompt, 'model': body.model, 'ratio': body.ratio,
                                           'style': body.style, 'requested_account': body.account,
                                           'references': body.references}.items()):
                raise HTTPException(409, 'Mã yêu cầu đã được dùng cho nội dung khác.')
            return job
        eligible = self.accounts()
        if body.account:
            eligible = [a for a in eligible if a['name'] == body.account]
        if not eligible:
            raise HTTPException(409, 'Chưa có tài khoản Dola hoạt động được bật Dispatch. Kiểm tra tab Accounts.')
        count = self.db.execute("SELECT count(*) FROM image_jobs WHERE status IN ('queued','running')").fetchone()[0]
        if count >= 30:
            raise HTTPException(429, 'Hàng đợi ảnh đã đầy (30 tác vụ).')
        identity, now = uuid.uuid4().hex, time.time()
        try:
            reference_paths = [self.library.path(ref) for ref in body.references]
        except ValueError as exc:
            raise HTTPException(422, 'Ảnh tham chiếu không tồn tại. Vui lòng dán hoặc chọn lại ảnh.') from exc
        # Snapshot inputs so deleting a library item cannot break queued/recovered jobs.
        if reference_paths:
            inputs = self.root / identity / 'inputs'
            inputs.mkdir(parents=True)
            for index, path in enumerate(reference_paths):
                shutil.copyfile(path, inputs / f'{index}{path.suffix}')
        with self.db:
            self.db.execute('''INSERT INTO image_jobs
                (id,request_id,prompt,model,ratio,style,requested_account,status,phase,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,'queued','queued',?,?)''',
                (identity, body.request_id, prompt, body.model, body.ratio, body.style, body.account, now, now))
            self.db.execute('UPDATE image_jobs SET references_json=? WHERE id=?', (json.dumps(body.references), identity))
        self.start(identity)
        return self.get(identity)

    def start(self, identity):
        if identity not in self.runners or self.runners[identity].done():
            task = asyncio.create_task(self.run(identity))
            self.runners[identity] = task
            task.add_done_callback(lambda done: self.runners.pop(identity, None) if self.runners.get(identity) is done else None)

    def resume(self, identity):
        row = self.get(identity)
        if row['status'] not in ('failed', 'interrupted') or not row['conversation_id']:
            raise HTTPException(409, 'Only failed jobs with a Dola conversation can be resumed.')
        self.update(identity, status='queued', phase='queued', error=None)
        self.start(identity)
        return self.get(identity)

    def retry(self, identity, body):
        source = self.get(identity)
        target = body.account if body.account is not None else (source['account'] or source['requested_account'])
        token = 'retry-' + identity + '-' + body.request_id
        existing = self.db.execute('SELECT id,requested_account FROM image_jobs WHERE request_id=?', (token,)).fetchone()
        if existing:
            if existing['requested_account'] != target:
                raise HTTPException(409, 'Yêu cầu retry đã được dùng cho tài khoản khác.')
            return self.get(existing['id'])
        if source['status'] in ('queued','running'):
            raise HTTPException(409, 'Tác vụ đang chạy. Chờ hoàn tất hoặc hủy chờ trước khi retry.')
        accounts = self.accounts()
        if not any(not target or a['name'] == target for a in accounts):
            raise HTTPException(409, 'Tài khoản không sẵn sàng. Chọn tài khoản khác đang bật Dispatch.')
        if self.db.execute("SELECT count(*) FROM image_jobs WHERE status IN ('queued','running')").fetchone()[0] >= 30:
            raise HTTPException(429, 'Hàng đợi ảnh đã đầy.')
        paths = sorted((self.root / identity / 'inputs').glob('*'))
        if len(paths) != len(source['references']) or any(not p.is_file() for p in paths):
            raise HTTPException(409, 'Thiếu bản sao ảnh tham chiếu. Vui lòng tạo tác vụ mới và đính kèm lại ảnh.')
        # Keep the original identity and input snapshots; replace its previous result.
        with self.db:
            self.db.execute('''UPDATE image_jobs SET request_id=?,requested_account=?,account=NULL,
                conversation_id=NULL,status='queued',phase='queued',error=NULL,outputs='[]',updated_at=? WHERE id=?''',
                (token,target,time.time(),identity))
        self.start(identity)
        return self.get(identity)

    def disable_network_denied_account(self, account, error):
        """Remove a blocked account from image dispatch immediately.

        This is a dispatch disable only: credentials and the account record are
        preserved so an operator can re-enable it after fixing the route.
        """
        setter = getattr(self.pool, 'set_scheduling', None)
        if callable(setter):
            setter(account, False, manual=True)
        return {'account': account, 'reason': str(error)[:500], 'scheduling': False}

    async def run(self, identity):
        try:
            deadline = time.monotonic() + 600
            while time.monotonic() < deadline:
                job = self.get(identity)
                if job['status'] != 'queued':
                    return
                candidates = self.accounts(include_image_limited=bool(job['conversation_id']))
                pinned = job['account'] if job['conversation_id'] else job['requested_account']
                if pinned:
                    candidates = [a for a in candidates if a['name'] == pinned]
                    if not job['conversation_id'] and any(a['account'] == pinned for a in self.limited_accounts()):
                        raise ImageQuotaExceeded('Tài khoản đã hết lượt tạo ảnh; chuyển sang tài khoản khác.')
                # Prefer recently verified sessions; never launch on a video reservation.
                candidates.sort(key=lambda a: a.get('login_checked_at') or 0, reverse=True)
                for account in candidates:
                    name = account['name']
                    lock = self.pool._locks.get(name)
                    if account.get('busy') or name in self.pool.reservations or self.unavailable(name) or (lock and lock.locked()):
                        continue
                    self.update(identity, phase='waiting_capacity')
                    async with self.slots, self.pool.semaphore:
                        if self.get(identity)['status'] != 'queued':
                            return
                        lock = self.pool._locks.get(name)
                        if (lock and lock.locked()) or name in self.pool.reservations or self.unavailable(name):
                            continue
                        async with self.pool.account_activity(name, 'generating_image'):
                            if name in self.pool.reservations or self.unavailable(name):
                                continue
                            if not job['conversation_id'] and any(a['account'] == name for a in self.limited_accounts()):
                                continue
                            self.update(identity, account=name, status='running', phase='preparing', error=None)
                            try:
                                outputs = await self.worker(name, job['prompt'], job['model'], job['ratio'],
                                    job['style'], self.root / identity,
                                    lambda **fields: self.update(identity, **fields),
                                    conversation_id=job['conversation_id'],
                                    **({'reference_image_paths': [str(p) for p in sorted((self.root / identity / 'inputs').glob('*'))]}
                                       if job['references'] else {}))
                            except Exception as exc:
                                if is_network_access_denied_error(exc):
                                    self.disable_network_denied_account(name, exc)
                                    self.update(identity, account=None, requested_account='', status='queued', phase='waiting_account',
                                                 error=f'Dola navigation denied; account disabled and task moved to another account: {exc}')
                                    continue
                                raise
                            self.update(identity, outputs=outputs, status='completed', phase='completed')
                            return
                self.update(identity, phase='waiting_account')
                await asyncio.sleep(2)
            raise TimeoutError('Chưa có tài khoản rảnh sau 10 phút. Kiểm tra Dispatch, kết nối và tác vụ video.')
        except asyncio.CancelledError:
            # Shutdown recovery handles persisted checkpoints without generating twice.
            raise
        except Exception as exc:
            job = self.get(identity)
            if isinstance(exc, ImageQuotaExceeded) and job['account']:
                self.mark_image_limit(job['account'])
            uncertain = not job['conversation_id'] and job['phase'] in ('submitting', 'verifying')
            self.update(identity, status='interrupted' if uncertain else 'failed',
                        phase='interrupted' if uncertain else 'failed', error=str(exc)[:1500])

    def recover(self):
        for row in self.db.execute("SELECT * FROM image_jobs WHERE status IN ('queued','running')").fetchall():
            if row['status'] == 'running' and not row['conversation_id'] and row['phase'] in ('submitting','verifying'):
                self.update(row['id'], status='interrupted', phase='interrupted',
                            error='Máy chủ dừng trong lúc gửi. Kiểm tra hội thoại Dola trước khi tạo lại để tránh trùng ảnh.')
            else:
                self.update(row['id'], status='queued', phase='queued')
                self.start(row['id'])

    async def stop(self):
        tasks = list(self.runners.values())
        for task in tasks:
            task.cancel()
        await asyncio.gather(*tasks, return_exceptions=True)

    def output_path(self, identity, index):
        job = self.get(identity)
        if job['status'] != 'completed' or not 0 <= index < len(job['outputs']):
            raise HTTPException(404, 'Ảnh chưa sẵn sàng.')
        if job['outputs'][index].get('deleted_at'):
            raise HTTPException(404, 'Ảnh đã được xóa.')
        path = self.root / identity / job['outputs'][index]['filename']
        if not path.is_file():
            raise HTTPException(404, 'Không tìm thấy file ảnh.')
        return path

    def delete_output(self, identity, index):
        job = self.get(identity)
        if job['status'] != 'completed' or not 0 <= index < len(job['outputs']):
            raise HTTPException(404, 'Không tìm thấy ảnh.')
        output = job['outputs'][index]
        if output.get('deleted_at'):
            return
        directory = (self.root / identity).resolve()
        path = (directory / output['filename']).resolve()
        if directory.parent != self.root.resolve() or path.parent != directory:
            raise HTTPException(400, 'Đường dẫn ảnh không hợp lệ.')
        path.unlink(missing_ok=True)
        output['deleted_at'] = time.time()
        # Keep original indexes stable: deleting image 0 must not renumber image 1.
        self.update(identity, outputs=job['outputs'])


def register_image_studio(app, studio, auth):
    @app.post('/api/admin/image-studio/batches', status_code=202)
    async def batch(body: ImageBatchRequest, x_admin_key: str | None = Header(default=None)):
        auth(x_admin_key)
        base = body.model_dump(exclude={'count'})
        batch_key = hashlib.sha256(body.request_id.encode()).hexdigest()
        requests = [ImageRequest(**{**base, 'request_id': f'{batch_key}-batch-{body.count}-{i}'})
                    for i in range(body.count)]
        new_count = sum(not studio.db.execute('SELECT 1 FROM image_jobs WHERE request_id=?', (r.request_id,)).fetchone()
                        for r in requests)
        pending = studio.db.execute("SELECT count(*) FROM image_jobs WHERE status IN ('queued','running')").fetchone()[0]
        if pending + new_count > 30:
            raise HTTPException(429, f'Hàng đợi chỉ còn {max(0,30-pending)} chỗ. Giảm số lượt hoặc chờ tác vụ hoàn tất.')
        return {'jobs': [studio.create(request) for request in requests], 'count': body.count}

    @app.post('/api/admin/image-studio/jobs/{identity}/retry', status_code=202)
    async def retry(identity: str, body: ImageRetry, x_admin_key: str | None = Header(default=None)):
        auth(x_admin_key)
        return studio.retry(identity, body)

    @app.get('/api/admin/image-studio/gallery')
    async def gallery(offset: int = 0, limit: int = 48, x_admin_key: str | None = Header(default=None)):
        auth(x_admin_key)
        source = "FROM image_jobs j, json_each(j.outputs) o WHERE j.status='completed' AND json_extract(o.value,'$.deleted_at') IS NULL"
        total = studio.db.execute('SELECT count(*) ' + source).fetchone()[0]
        rows = studio.db.execute('SELECT j.id,j.prompt,j.model,j.created_at,o.key AS output_index,o.value ' + source +
                                ' ORDER BY j.created_at DESC,j.id,o.key LIMIT ? OFFSET ?',
                                (min(100,max(1,limit)),max(0,offset))).fetchall()
        images = []
        for row in rows:
            item = json.loads(row['value'])
            item.update(job_id=row['id'], index=row['output_index'], prompt=row['prompt'],
                        model=row['model'], created_at=row['created_at'],
                        url=f"/api/admin/image-studio/jobs/{row['id']}/files/{row['output_index']}")
            images.append(item)
        return {'images': images, 'total': total}

    @app.delete('/api/admin/image-studio/jobs/{identity}/files/{index}')
    async def delete_image(identity: str, index: int, x_admin_key: str | None = Header(default=None)):
        auth(x_admin_key)
        studio.delete_output(identity, index)
        return {'ok': True}

    @app.get('/api/admin/image-studio/options')
    async def options(x_admin_key: str | None = Header(default=None)):
        auth(x_admin_key)
        labels = {a['name']: a.get('email') or a.get('display_name') or a['name']
                  for a in studio.pool.list_accounts()}
        return {'models': MODELS, 'ratios': RATIOS, 'styles': STYLES,
                'accounts': [{'name': a['name'], 'label': a.get('email') or a.get('display_name') or a['name'],
                              'busy': a.get('busy', False)} for a in studio.accounts()],
                'image_limited_accounts': [{**entry, 'label': labels.get(entry['account'], entry['account'])}
                                           for entry in studio.limited_accounts()]}

    @app.get('/api/admin/image-studio/jobs')
    async def jobs(offset: int = 0, limit: int = 30, x_admin_key: str | None = Header(default=None)):
        auth(x_admin_key)
        rows = studio.db.execute('SELECT id FROM image_jobs ORDER BY created_at DESC,id LIMIT ? OFFSET ?',
                                 (min(60, max(1, limit)), max(0, offset))).fetchall()
        counts = dict(studio.db.execute('SELECT status,count(*) FROM image_jobs GROUP BY status').fetchall())
        return {'jobs': [studio.get(r['id']) for r in rows], 'counts': counts, 'total': sum(counts.values())}

    @app.post('/api/admin/image-studio/jobs', status_code=202)
    async def create(body: ImageRequest, x_admin_key: str | None = Header(default=None)):
        auth(x_admin_key)
        return studio.create(body)

    @app.get('/api/admin/image-studio/jobs/{identity}')
    async def get(identity: str, x_admin_key: str | None = Header(default=None)):
        auth(x_admin_key)
        return studio.get(identity)

    @app.delete('/api/admin/image-studio/jobs/{identity}')
    async def delete_job(identity: str, x_admin_key: str | None = Header(default=None)):
        auth(x_admin_key)
        job = studio.get(identity)
        if job['status'] == 'running':
            raise HTTPException(409, 'Tác vụ đang tạo ảnh. Hãy chờ hoàn tất trước khi xóa.')
        if any(not output.get('deleted_at') for output in job['outputs']):
            raise HTTPException(409, 'Hãy xóa các ảnh của tác vụ trước.')
        folder = (studio.root / identity).resolve()
        if folder.parent != studio.root.resolve():
            raise HTTPException(400, 'Đường dẫn tác vụ không hợp lệ.')
        studio.update(identity, status='cancelled', phase='cancelled')
        runner = studio.runners.get(identity)
        if runner:
            runner.cancel()
            await asyncio.gather(runner, return_exceptions=True)
        if folder.exists():
            shutil.rmtree(folder)
        studio.db.execute('DELETE FROM image_jobs WHERE id=?', (identity,))
        studio.db.commit()
        return {'deleted': identity}

    @app.post('/api/admin/image-studio/jobs/{identity}/resume', status_code=202)
    async def resume(identity: str, x_admin_key: str | None = Header(default=None)):
        auth(x_admin_key)
        return studio.resume(identity)

    @app.post('/api/admin/image-studio/jobs/{identity}/cancel')
    async def cancel(identity: str, x_admin_key: str | None = Header(default=None)):
        auth(x_admin_key)
        if studio.get(identity)['status'] != 'queued':
            raise HTTPException(409, 'Chỉ có thể hủy tác vụ chưa gửi tới Dola.')
        studio.update(identity, status='cancelled', phase='cancelled')
        runner = studio.runners.get(identity)
        if runner:
            runner.cancel()
        return studio.get(identity)

    @app.get('/api/admin/image-studio/jobs/{identity}/files/{index}')
    async def file(identity: str, index: int, download: bool = False, x_admin_key: str | None = Header(default=None)):
        auth(x_admin_key)
        path = studio.output_path(identity, index)
        return FileResponse(path, filename=f'dola-{identity[:8]}-{index+1}{path.suffix}' if download else None,
                            headers={'Cache-Control': 'private, max-age=3600'})

    @app.post('/api/admin/image-studio/jobs/{identity}/library/{index}')
    async def library(identity: str, index: int, x_admin_key: str | None = Header(default=None)):
        auth(x_admin_key)
        path = studio.output_path(identity, index)
        return studio.library.save(path.read_bytes(), f'dola-{identity[:8]}-{index+1}{path.suffix}', 40*1024*1024)
