from video_worker import GenerationRejectedError
"""Video Rendering: OpenAI-compatible Video API (FastAPI) and Admin Dashboard.

Endpoints (Asynchronous 2-stage):
POST /v1/videos/generations -> Create task (status=queued)
GET  /v1/videos/<id>         -> Query task status (queued/processing/completed/failed)
GET  /videos/<file>          -> Static video download server

Admin Dashboard: GET / -> web/index.html; Admin API /api/admin/*
"""
import asyncio
import hashlib
import json
import logging
import math
import re
import shutil
import sqlite3
import tempfile
import time
import traceback
import uuid
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from fastapi.responses import FileResponse
from image_library import ImageLibrary
from image_studio import ImageStudio, register_image_studio
from browser_queue import async_playwright
from patchright.async_api import async_playwright as manual_async_playwright
from video_schedule import VideoScheduler
from browser_queue import lock_file, unlock_file, ROOT as RUNTIME_ROOT
from pydantic import BaseModel, Field
from PIL import Image

import config
import account_connections
from asset_api import register_asset_routes
from scene_generation import task_reference_paths
from project_generation import submit_project_generation
from account_import import parse_netscape, imported_account_flow, cookie_identity
from add_account import add_account_flow
from browser_pool import AllAccountsLimitedError, AllAccountsQuotaBlockedError, NoUsableAccountsError, BrowserPool
from media import download_reference_images, validate_reference_urls
from reference_aliases import resolve_reference_aliases
from store import PendingTaskLimitExceeded, TaskQuotaExceeded, TaskSubmissionConflict, TaskStore

Path(config.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
Path("web").mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Video Rendering", version="0.4.0")

store = TaskStore(config.DB_PATH)
pool = BrowserPool(max_concurrency=config.MAX_CONCURRENCY)
store.bind_account_uuids(pool.list_accounts())
scheduler = VideoScheduler(store, pool)
SERVER_GUARD = None

app.mount("/videos", StaticFiles(directory=config.DOWNLOAD_DIR), name="videos")

# Background jobs (add/verify), in-memory
JOBS: dict[str, dict] = {}
WEB_SESSIONS: dict[str, dict] = {}
DOMAIN_WEB_QUEUE = None
UPLOADED_REFERENCES: dict[str, tuple[Path, list[str]]] = {}

SIZE_TO_RATIO = {
    "1280x720": "16:9", "1920x1080": "16:9",
    "720x1280": "9:16", "1080x1920": "9:16",
    "1024x1024": "1:1", "1440x1080": "4:3", "1080x1440": "3:4",
}
SUPPORTED_DURATIONS = (10, 15, 30)
NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")


class KeyConcurrencyLimiter:
    """Concurrency limits per API Key; 0 = unlimited."""

    def __init__(self):
        self._condition = asyncio.Condition()
        self._active: defaultdict[str, int] = defaultdict(int)

    async def acquire(self, api_key_hash: str | None, limit: int):
        if not api_key_hash or limit <= 0:
            return
        async with self._condition:
            while self._active[api_key_hash] >= limit:
                await self._condition.wait()
            self._active[api_key_hash] += 1

    async def release(self, api_key_hash: str | None):
        if not api_key_hash:
            return
        async with self._condition:
            if self._active[api_key_hash] > 0:
                self._active[api_key_hash] -= 1
            if self._active[api_key_hash] == 0:
                self._active.pop(api_key_hash, None)
            self._condition.notify_all()



key_limiter = KeyConcurrencyLimiter()


# ===== Authentication =====


def _hash_key(key: str) -> str:
    return hashlib.sha256(key.encode("utf-8")).hexdigest()


def _anonymous_client() -> dict:
    return {
        "api_key_hash": None,
        "api_key_name": "Anonymous",
        "daily_limit": 0,
        "concurrency_limit": 0,
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _env_client(key: str) -> dict:
    return {
        "api_key_hash": _hash_key(key),
        "api_key_name": f"Env Key ({key[:8]}…)",
        "daily_limit": 0,
        "concurrency_limit": 0,
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _auth(authorization):
    """Returns client policy for caller; empty key enables dev mode."""
    if not config.API_KEYS and not store.has_enabled_keys():
        return _anonymous_client()
    if not authorization or not authorization.startswith("Bearer "):
        raise HTTPException(401, "missing bearer token")
    key = authorization[7:].strip()
    if not key:
        raise HTTPException(401, "missing bearer token")
    if key in config.API_KEYS:
        return _env_client(key)
    record = store.get_key(key)
    if not record or not store.is_key_valid(key):
        raise HTTPException(401, "invalid api key")
    store.touch_key(key)
    return {
        "api_key_hash": _hash_key(key),
        "api_key_name": record["name"] or "Unnamed Client",
        "daily_limit": record["daily_limit"],
        "concurrency_limit": record["concurrency_limit"],
        "allowed_durations": record["allowed_durations"],
    }


def _admin_auth(x_admin_key: str | None):
    if not config.ADMIN_KEY:
        return
    if x_admin_key != config.ADMIN_KEY:
        raise HTTPException(401, "invalid admin key")


def _normalize_allowed_durations(values) -> list[int]:
    if values is None:
        return list(SUPPORTED_DURATIONS)
    try:
        normalized = sorted({int(value) for value in values})
    except (TypeError, ValueError):
        raise HTTPException(422, "allowed_durations must be an array of 10, 15, or 30")
    if not normalized or any(value not in SUPPORTED_DURATIONS for value in normalized):
        raise HTTPException(422, "allowed_durations must contain at least one of 10, 15, 30")
    return normalized


# ===== Client API =====


class VideoGenRequest(BaseModel):
    batch_id: str | None = Field(None, min_length=1, max_length=200)
    name: str | None = Field(None, max_length=200)
    model: str = "seedance-2.0"
    chat_mode: str = "fast"
    prompt: str = Field(..., min_length=1)
    size: str | None = None
    ratio: str | None = None
    duration: int | None = Field(None, ge=10, le=30)
    # Accepts durations: 10, 15, 30 seconds.
    reference_images: list[str] = Field(default_factory=list)
    reference_aliases: list[str] = Field(default_factory=list)
    start_end: bool = False
    has_end_frame: bool | None = None
    count: int = Field(1, ge=1, le=10, strict=True)


class TaskResponse(BaseModel):
    id: str
    name: str | None = None
    status: str
    model: str | None = None
    prompt: str | None = None
    video_url: str | None = None
    error: str | None = None


class BatchTaskResponse(BaseModel):
    batch_id: str
    tasks: list[TaskResponse]



image_library = ImageLibrary(Path(__file__).resolve().parent / ".image_library")
image_studio = ImageStudio(Path(__file__).resolve().parent / '.runtime' / 'image_studio', pool, image_library,
                           max_concurrency=config.IMAGE_MAX_CONCURRENCY,
                           unavailable=lambda name: JOBS.get(name, {}).get('status') == 'running'
                           or name in WEB_SESSIONS)
register_image_studio(app, image_studio, _admin_auth)


@app.get("/api/admin/image-library")
async def list_library_images(offset: int = 0, limit: int = 60, search: str = "", x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return image_library.list(max(0, offset), max(1, min(60, limit)), search[:255])


@app.post("/api/admin/image-library", status_code=201)
async def save_library_images(files: list[UploadFile] = File(...), x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not files or len(files) > config.REFERENCE_IMAGE_MAX_COUNT:
        raise HTTPException(422, f"Maximum of {config.REFERENCE_IMAGE_MAX_COUNT} images allowed")
    images = []
    for upload in files:
        data = await upload.read(config.REFERENCE_IMAGE_MAX_BYTES + 1)
        try:
            images.append(image_library.save(data, upload.filename, config.REFERENCE_IMAGE_MAX_BYTES))
        except Exception as exc:
            raise HTTPException(422, "Invalid image or file exceeds size limit") from exc
    return {"images": images}


@app.get("/api/admin/image-library/{identity}")
async def library_image(identity: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        path = image_library.path(identity)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return FileResponse(path)


@app.delete("/api/admin/image-library/{identity}")
async def delete_library_image(identity: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        # Synchronous check/delete: no task creation can interleave on the event loop.
        count = image_library.usage(store._conn, identity)
        if count:
            raise HTTPException(409, f"Ảnh đang được {count} job sử dụng. Xóa các job đó khỏi lịch sử trước khi xóa ảnh.")
        image_library.delete(identity)
    except ValueError as exc:
        raise HTTPException(404, str(exc)) from exc
    return {"ok": True}


@app.post("/api/admin/reference-images", status_code=201)
async def upload_reference_images(
    files: list[UploadFile] = File(...),
    x_admin_key: str | None = Header(default=None),
):
    """Compatibility upload endpoint; returns durable shared image references."""
    result = await save_library_images(files, x_admin_key)
    return {"reference_images": ["library://" + item["id"] for item in result["images"]],
            "count": len(result["images"])}



def _resolve_ratio(size, ratio):
    if size and size in SIZE_TO_RATIO:
        return SIZE_TO_RATIO[size]
    return ratio


TASK_RUNNERS = {}


async def _run_task(task_id, model=None, prompt=None, ratio=None, duration=None, reference_images=None, client=None):
    TASK_RUNNERS[task_id] = asyncio.current_task()
    row = store.get(task_id)
    client = client or _task_client(row)
    api_key_hash = client.get("api_key_hash")
    acquired = False
    try:
        await key_limiter.acquire(api_key_hash, client.get("concurrency_limit", 0))
        acquired = True
        current = store.get(task_id)
        if not current or current.get("deleted_at") is not None or current["status"] == "stopped":
            raise asyncio.CancelledError()
        await scheduler.run(task_id, UPLOADED_REFERENCES)
    except asyncio.CancelledError:
        # Shutdown preserves the durable checkpoint. Explicit stop updates it in the action handler.
        raise
    except Exception as exc:
        scheduler.review(task_id, f"{type(exc).__name__}: {exc}")
        logging.getLogger("uvicorn.error").exception("Scheduled task failed: %s", task_id)
    finally:
        if TASK_RUNNERS.get(task_id) is asyncio.current_task():
            TASK_RUNNERS.pop(task_id, None)
        if acquired:
            await key_limiter.release(api_key_hash)


async def _resume_task(row):
    return await _run_task(row["id"])


def _task_client(row: dict) -> dict:
    """Restores client context from task snapshot."""
    return {
        "api_key_hash": row.get("api_key_hash"),
        "api_key_name": row.get("api_key_name") or "Historical Task",
        "daily_limit": 0,
        "concurrency_limit": int(row.get("client_concurrency_limit") or 0),
        "allowed_durations": list(SUPPORTED_DURATIONS),
    }


def _task_reference_images(raw) -> list[str]:
    try:
        values = json.loads(raw or "[]")
    except (TypeError, json.JSONDecodeError):
        return []
    return values if isinstance(values, list) else []


@app.on_event("startup")
async def resume_incomplete_tasks():
    global SERVER_GUARD
    SERVER_GUARD = lock_file(RUNTIME_ROOT / "scheduler.lock")
    if SERVER_GUARD is None:
        raise RuntimeError("Only one scheduler server may run per workspace; use one uvicorn worker")
    scheduler.recover_reservations()
    image_studio.recover()
    rows = store._conn.execute(
        "SELECT * FROM tasks WHERE (status IN ('queued','processing') OR (status='needs_recovery' AND dispatch_uncertain=1)) AND deleted_at IS NULL ORDER BY created_at"
    ).fetchall()
    for raw in rows:
        row = dict(raw)
        if row["status"] == "needs_recovery" and row.get("dispatch_uncertain"):
            if scheduler.reserved.get(row.get("account")) != row["id"]:
                continue
            store.update(row["id"],status="queued",phase="dispatching")
        # Legacy records predate the persisted dispatch checkpoint.
        if row.get("conversation_id") and row.get("phase") == "ready":
            store.update(row["id"], phase="checking")
        elif row["status"] == "processing" and not row.get("conversation_id") and row.get("phase") == "ready":
            scheduler.review(row["id"], "Legacy interrupted job has no dispatch checkpoint; inspect before resubmitting")
            continue
        TASK_RUNNERS[row["id"]] = asyncio.create_task(_run_task(row["id"]))


@app.on_event("shutdown")
async def stop_task_runners():
    global SERVER_GUARD
    if DOMAIN_WEB_QUEUE:
        DOMAIN_WEB_QUEUE.stop()
    await image_studio.stop()
    runners = list(TASK_RUNNERS.values())
    for runner in runners:
        runner.cancel()
    if runners:
        await asyncio.gather(*runners, return_exceptions=True)
    unlock_file(SERVER_GUARD)
    SERVER_GUARD = None


@app.post("/v1/videos/generations", response_model=TaskResponse | BatchTaskResponse)
async def create_video(req: VideoGenRequest, authorization: str | None = Header(default=None)):
    return await _submit_video(req, _auth(authorization))


async def _submit_video(req, client, *, scene_id=None, reference_snapshot=None, prepare_only=False, project_id=None, scene_updated_at=None, generation_request_id=None, candidates_per_scene=1, studio_task_id=None):
    """Shared admission, task snapshots and scheduling for legacy and scene inputs."""
    chat_mode = (req.chat_mode or "fast").strip().lower()
    if chat_mode not in ("fast", "pro"):
        raise HTTPException(422, "chat_mode must be fast or pro")
    if chat_mode == "pro":
        model_key = req.model.lower().replace("-", "_")
        if model_key not in ("seedance_2.0", "seedance_v2.0", "seedance_20", "seedance_v20") or (req.duration or 10) != 15:
            raise HTTPException(422, "Pro chat mode currently requires Seedance 2.0 at 15 seconds")
        if project_id is not None or scene_id is not None or prepare_only:
            raise HTTPException(422, "Pro chat mode is currently supported for standalone video tasks only")
    if project_id is not None:
        return await submit_project_generation(project_id, client, store, VideoGenRequest, _submit_video, _run_task, config.MAX_PENDING_TASKS, candidates_per_scene, generation_request_id)
    if (req.batch_id or '').startswith('studio_') and not studio_task_id:
        raise HTTPException(409, 'Film Studio manages retries and the 10-video limit for this batch')
    if studio_task_id:
        if req.count != 1 or not (req.batch_id or '').startswith('studio_') or scene_id is not None:
            raise HTTPException(422, 'Invalid internal Film Studio submission')
        previous = store.get(studio_task_id)
        if previous:
            if previous.get('batch_id') != req.batch_id:
                raise HTTPException(409, 'Film Studio task belongs to a different batch')
            return BatchTaskResponse(batch_id=req.batch_id, tasks=[TaskResponse(id=studio_task_id, status=previous['status'])])
    duration = req.duration or 10
    if duration not in SUPPORTED_DURATIONS:
        raise HTTPException(422, "Currently supports durations of 10s, 15s, and 30s")
    if duration not in client["allowed_durations"]:
        raise HTTPException(422, f"Current API Key is not allowed to generate {duration}s videos")
    model_key = req.model.lower().replace("-", "_")
    if model_key not in (
        "seedance_2.0", "seedance_2.5", "seedance_v2.0", "seedance_v2.5",
        "seedance_20", "seedance_25", "seedance_v20", "seedance_v25",
    ):
        raise HTTPException(422, "Supported models are seedance-2.0 and seedance-2.5")
    try:
        reference_images = []
        image_count = 0
        if reference_snapshot is not None:
            # Validate stable paths before accepting the task; no uploaded:// tokens.
            persistent_paths = task_reference_paths({"reference_snapshot": reference_snapshot})
            image_count = len(persistent_paths)
            reference_images = ["asset://" + ref["asset_id"] for ref in reference_snapshot]
        else:
            for reference in req.reference_images:
                if reference.startswith("library://"):
                    image_library.path(reference[10:])
                    image_count += 1
                    reference_images.append(reference)
                elif reference.startswith("uploaded://"):
                    uploaded = UPLOADED_REFERENCES.get(reference)
                    if not uploaded:
                        raise ValueError("Uploaded images expired; please upload them again")
                    image_count += len(uploaded[1])
                    reference_images.append(reference)
                else:
                    reference_images.extend(await validate_reference_urls([reference]))
                    image_count += 1
        if image_count > config.REFERENCE_IMAGE_MAX_COUNT:
            raise ValueError("Too many reference images")
        resolved_prompt = resolve_reference_aliases(req.prompt, req.reference_aliases, image_count)
        has_end_frame = req.has_end_frame if req.has_end_frame is not None else image_count >= 2
        if req.start_end and image_count < 1:
            raise ValueError("Start / End requires a start image")
        if req.start_end and has_end_frame and image_count < 2:
            raise ValueError("An end frame was specified but fewer than two images were provided")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Queue task when accounts are busy; reject only when pool is fully exhausted.
    if not pool.available and pool.all_accounts_limited:
        raise HTTPException(429, "Rate limited: All accounts reached Dola daily video limit, please try again tomorrow")
    if not pool.available and pool.all_accounts_quota_blocked:
        raise HTTPException(429, "Insufficient credits: All accounts lack points, waiting for refresh")
    if not pool.accounts:
        raise HTTPException(503, "no account in pool")
    task_ids = [studio_task_id] if studio_task_id else ["video_" + uuid.uuid4().hex for _ in range(req.count)]
    batch_id = store.submission_batch_id("scene",scene_id,generation_request_id,client["api_key_hash"]) if scene_id and generation_request_id else (req.batch_id or "batch_" + uuid.uuid4().hex)
    ratio = _resolve_ratio(req.size, req.ratio)
    prompt = resolved_prompt
    if req.start_end and not has_end_frame:
        prompt += '\n\nUse the first uploaded image as the opening frame. Continue the motion naturally from this frame, preserving its composition and subjects. No ending frame is specified.'
        if image_count > 1:
            prompt += ' Use all remaining uploaded images only as environment and scene references for background, lighting, and scene details; do not treat them as an ending frame.'
    elif req.start_end:
        prompt += ("\n\nUse the first uploaded image as the opening frame and the second "
                   "uploaded image as the ending frame. Create continuous motion between "
                   "these two frames, preserving their composition and subjects.")
        if image_count > 2:
            prompt += (" Use uploaded images from the third onward only as additional environment "
                       "and scene references. Use them to guide the background, lighting, and scene "
                       "details without replacing the opening or ending frame or overriding their subjects.")
    if prepare_only:
        if scene_id is None or reference_snapshot is None:
            raise ValueError("Project batch preparation requires persistent scene references")
        return dict(scene_id=scene_id, model=req.model, prompt=prompt, ratio=ratio or "default",
                    duration=duration, chat_mode=chat_mode, start_end=req.start_end, name=req.name,
                    reference_images=reference_images, reference_snapshot=reference_snapshot)
    # Resolve again after URL validation awaits, then persist without yielding.
    references = []
    try:
        for reference in reference_images:
            if reference.startswith("library://"):
                image_library.path(reference[10:])
                references.append(reference)
            elif reference.startswith("uploaded://"):
                for path in UPLOADED_REFERENCES[reference][1]:
                    references.append("library://" + image_library.import_path(path)["id"])
            else:
                references.append(reference)
    except (ValueError, KeyError, OSError) as exc:
        raise HTTPException(422, "Reference image is no longer available; please select it again") from exc
    task_references = {task_id: references for task_id in task_ids}
    try:
        store.create_batch(
            task_ids, req.model, prompt, ratio or "default", duration,
            chat_mode=chat_mode,
            reference_images={key: json.dumps(value, ensure_ascii=False) for key, value in task_references.items()},
            batch_id=batch_id, start_end=req.start_end,
            append_batch=(bool(req.batch_id) and scene_id is None and
                          (not studio_task_id or store._conn.execute('SELECT 1 FROM tasks WHERE batch_id=?', (req.batch_id,)).fetchone() is not None)),
            api_key_hash=client["api_key_hash"], api_key_name=client["api_key_name"],
            daily_limit=client["daily_limit"], concurrency_limit=client["concurrency_limit"],
            max_pending=config.MAX_PENDING_TASKS,
            name=req.name,
            scene_id=scene_id,
            reference_snapshot=reference_snapshot,
            guard_scene=scene_id is not None, scene_updated_at=scene_updated_at,
        )
    except TaskSubmissionConflict as exc:
        raise HTTPException(409, str(exc)) from exc
    except sqlite3.IntegrityError as exc:
        raise HTTPException(409, 'Task batch conflict; no new candidates were queued') from exc
    except sqlite3.DatabaseError as exc:
        raise HTTPException(500, 'Task batch creation failed; the batch was rolled back') from exc
    except TaskQuotaExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    except PendingTaskLimitExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    if studio_task_id:
        store.rename_batch(batch_id, req.name or batch_id)
    for reference in set(reference_images):
        uploaded = UPLOADED_REFERENCES.pop(reference, None)
        if uploaded:
            shutil.rmtree(uploaded[0], ignore_errors=True)
    for task_id in task_ids:
        asyncio.create_task(_run_task(
            task_id, req.model, prompt, ratio, duration, task_references[task_id], client
        ))
    tasks = [TaskResponse(id=task_id, name=store.get(task_id).get("name", ""), status="queued", model=req.model, prompt=prompt)
             for task_id in task_ids]
    return BatchTaskResponse(batch_id=batch_id, tasks=tasks) if req.count > 1 or req.batch_id or (scene_id and generation_request_id) else tasks[0]


@app.get("/v1/videos/{task_id}", response_model=TaskResponse)
async def get_video(task_id: str, authorization: str | None = Header(default=None)):
    client = _auth(authorization)
    row = store.get_for_client(task_id, client["api_key_hash"])
    if not row:
        raise HTTPException(404, "task not found")
    return TaskResponse(
        id=row["id"], name=row.get("name") or "", status=row["status"], model=row["model"],
        prompt=row["prompt"], video_url=row["video_url"], error=row["error"],
    )


@app.get("/health")
async def health():
    return {
        "ok": True,
        "accounts": pool.account_status(),
        "available": pool.available,
        "pending_tasks": store.pending_task_count(),
        "max_pending_tasks": config.MAX_PENDING_TASKS,
    }


# ===== Admin Dashboard API =====


class AdminLogin(BaseModel):
    key: str


class AccountPatch(BaseModel):
    scheduling: bool | None = None
    note: str | None = None
    email: str | None = None


class AccountAdd(BaseModel):
    name: str = ""
    email: str = ""
    password: str = ""
    totp: str = ""
    method: str = "google"
    account_type: str = "unknown"
    display_name: str = ""
    cookie_text: str = Field(default="", max_length=1_000_000)
    connection_id: str | None = None


class KeyCreate(BaseModel):
    name: str = ""
    daily_limit: int = Field(0, ge=0, le=1_000_000)
    concurrency_limit: int = Field(0, ge=0, le=1_000)
    allowed_durations: list[int] = Field(default_factory=lambda: list(SUPPORTED_DURATIONS))
    expires_at: float | None = Field(None, ge=0)


class KeyPatch(BaseModel):
    name: str | None = None
    enabled: bool | None = None
    daily_limit: int | None = Field(None, ge=0, le=1_000_000)
    concurrency_limit: int | None = Field(None, ge=0, le=1_000)
    allowed_durations: list[int] | None = None
    expires_at: float | None = Field(None, ge=0)


@app.post("/api/admin/login")
async def admin_login(body: AdminLogin):
    if not config.ADMIN_KEY:
        return {"ok": True, "auth_required": False}
    if body.key == config.ADMIN_KEY:
        return {"ok": True, "auth_required": True}
    raise HTTPException(401, "wrong admin key")


@app.get("/api/admin/accounts")
async def admin_accounts(x_admin_key: str | None = Header(default=None)):
    import account_connections
    _admin_auth(x_admin_key)
    routing = account_connections.routing_snapshot()
    accounts = []
    for account in pool.list_accounts():
        public = pool.public_account(account)
        from dola_account_lifecycle import read_checkpoint
        public["dola_delete_phase"] = read_checkpoint(account["name"]).get("phase", "")
        public["ready_to_generate"] = bool(
            scheduler.eligible(account)
            and JOBS.get(account["name"], {}).get("status") != "running"
        )
        public["active_requests"] = len(scheduler.reservation_ids(account['name']))
        public['connection_id'] = routing['accounts'].get(account['name'], 'direct')
        accounts.append(public)
    return {"accounts": accounts, "dual_requests": scheduler.dual_requests,
            "connections": routing['connections'],
            "web_queue": DOMAIN_WEB_QUEUE.snapshot() if DOMAIN_WEB_QUEUE else None}


class AccountConnectionAssignment(BaseModel):
    accounts: list[str] = Field(min_length=1, max_length=500)
    connection_id: str


@app.post('/api/admin/account-connections/distribute')
async def distribute_account_connections(preview: bool = False,
                                         x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    accounts = pool.list_accounts()
    busy = set()
    for account in accounts:
        name = account['name']
        lock = pool._locks.get(name)
        if (account.get('busy') or name in WEB_SESSIONS or (lock and lock.locked())
                or scheduler.reservation_ids(name) or JOBS.get(name, {}).get('status') == 'running'):
            busy.add(name)
    try:
        return account_connections.distribute_proxies(accounts, busy, preview=preview)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


class ProxyImport(BaseModel):
    text: str = Field(repr=False)


@app.get('/api/admin/proxies')
async def admin_proxies(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {'proxies': account_connections.proxy_records(pool.list_accounts())}


@app.post('/api/admin/proxies/import')
async def admin_proxy_import(body: ProxyImport, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        return account_connections.import_proxies(body.text)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc


@app.delete('/api/admin/proxies/{identifier}')
async def admin_proxy_delete(identifier: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        account_connections.remove_proxy(identifier, set(pool.accounts))
    except KeyError as exc:
        raise HTTPException(404, 'Proxy không tồn tại') from exc
    except ValueError as exc:
        raise HTTPException(409, str(exc)) from exc
    return {'ok': True}


@app.patch('/api/admin/account-connections')
async def assign_account_connections(body: AccountConnectionAssignment,
                                     x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        account_connections.validate(body.connection_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    names = list(dict.fromkeys(pool.resolve_account(identifier) for identifier in body.accounts))
    existing = set(pool.accounts)
    for name in names:
        if name not in existing:
            raise HTTPException(404, 'Account not found')
        lock = pool._locks.get(name)
        if (name in WEB_SESSIONS or (lock and lock.locked())
                or scheduler.reservation_ids(name)
                or JOBS.get(name, {}).get('status') == 'running'):
            raise HTTPException(409, 'Close the account browser and finish or stop its video tasks before changing connection')
    account_connections.assign(names, body.connection_id)
    return {'ok': True, 'count': len(names), 'connection_id': body.connection_id}


class AccountRequestMode(BaseModel):
    enabled: bool = Field(strict=True)


@app.patch("/api/admin/account-request-mode")
async def admin_account_request_mode(body: AccountRequestMode, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    scheduler.set_dual_requests(body.enabled)
    return {"dual_requests": scheduler.dual_requests}


class AccountGroupDispatch(BaseModel):
    enabled: bool
    domain: str | None = None


@app.patch("/api/admin/account-groups/{group}/dispatch")
async def admin_group_dispatch(group: str, body: AccountGroupDispatch,
                               x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if group not in ('gmail', 'other'):
        raise HTTPException(422, 'Unknown account group')
    count = pool.set_group_scheduling(group, body.enabled, body.domain)
    return {'ok': True, 'group': group, 'enabled': body.enabled, 'count': count}


@app.patch("/api/admin/accounts/{name}")
async def admin_account_patch(name: str, body: AccountPatch,
                              x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    name = pool.resolve_account(name)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    if body.scheduling is not None:
        pool.set_scheduling(name, body.scheduling)
    if body.note is not None:
        pool.set_note(name, body.note)
    if body.email is not None:
        pool.set_email(name, body.email)
    return {"ok": True}


@app.delete("/api/admin/accounts/{name}")
async def admin_account_delete(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    name = pool.resolve_account(name)
    logger = logging.getLogger("uvicorn.error")
    if JOBS.get(name, {}).get("status") == "running":
        raise HTTPException(409, "Account login job is running")
    session = WEB_SESSIONS.get(name)
    lock = pool._locks.get(name)
    logger.info("[account-delete] account=%r stage=request web_session=%s busy=%s",
                name, session.get("status") if session else None, bool(lock and lock.locked()))
    if name not in pool.accounts:
        logger.warning("[account-delete] account=%r stage=not_found", name)
        raise HTTPException(404, "account not found")
    if session:
        logger.warning("[account-delete] account=%r stage=blocked_open_web", name)
        raise HTTPException(409, "Account browser is open. Close its Open Web window, then retry deletion.")
    try:
        pool.delete_account(name)
    except PermissionError as e:
        logger.exception("[account-delete] account=%r stage=permission_denied file=%r errno=%s winerror=%s",
                         name, e.filename, e.errno, getattr(e, "winerror", None))
        raise HTTPException(409, "Cannot delete account profile: a file is in use or access is denied. "
                            "Close Chrome windows for this account, then retry. See server.err.log for details.") from e
    except RuntimeError as e:
        logger.warning("[account-delete] account=%r stage=blocked reason=%s", name, e)
        raise HTTPException(409, str(e)) from e
    except Exception as e:
        logger.exception("[account-delete] account=%r stage=failed type=%s", name, type(e).__name__)
        raise HTTPException(500, "Account deletion failed. See server.err.log for details.") from e
    logger.info("[account-delete] account=%r stage=completed", name)
    return {"ok": True}



async def _run_dola_delete(name: str, resume: bool = False):
    from dola_account_lifecycle import delete_and_relogin, checkpoint, read_checkpoint
    recovering_uncertain = resume and read_checkpoint(name).get("phase") == "submitting"
    def report(stage, message):
        if JOBS[name].get("stage") != stage:
            logging.getLogger("uvicorn.error").info("[delete-dola] account=%s stage=%s", name, stage)
        JOBS[name].update(stage=stage, message=message)
    try:
        async with pool.account_activity(name, "delete_dola"):
            # Keep failed/interrupted flows out of dispatch, including after restart.
            pool.auth_result(name, "expired", "Dola deletion / Google re-login in progress")
            await delete_and_relogin(name, pool._meta(name)["email"], report, resume=resume)
            report("verifying", "Verifying the new Dola session")
            from browser import inspect_account_session
            result = await inspect_account_session(name)
            if result["state"] != "active":
                raise RuntimeError("Google re-login could not be verified")
            state = pool.auth_result(name, "active", identity=result)
            if state != "active":
                raise RuntimeError("Re-login identity duplicates another account")
            pool.reset_renewed_account_usage(name)
            checkpoint(name, "complete")
            JOBS[name].update(status="success", stage="complete", message=("Google login verified; usage reset to 0/2; previous deletion result was not confirmed" if recovering_uncertain else "Account renewed; Google login verified; usage reset to 0/2"), error="")
    except Exception as exc:
        # Never copy browser exception URLs (OAuth tokens) into logs / dashboard.
        import traceback
        locations = " > ".join(
            f"{frame.name}:{frame.lineno}" for frame in traceback.extract_tb(exc.__traceback__)
            if frame.filename.endswith(("dola_account_lifecycle.py", "server.py", "browser.py")))
        stage = JOBS[name].get("stage", "starting")
        error = f"Dola recovery stopped at {stage} ({type(exc).__name__}). Resume login to continue."
        pool.auth_result(name, "expired", error)
        JOBS[name].update(status="failed", error=error)
        logging.getLogger("uvicorn.error").warning("[delete-dola] account=%s stage=%s failure=%s location=%s", name, stage, type(exc).__name__, locations)


@app.post("/api/admin/accounts/{name}/delete-dola", status_code=202)
async def admin_delete_dola(name: str, resume: bool = False,
                            x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    name = pool.resolve_account(name)
    if name not in pool.accounts:
        raise HTTPException(404, "Account not found")
    meta = pool._meta(name)
    if meta["account_type"] != "google":
        raise HTTPException(400, "Only Google accounts are supported; Facebook will be added later")
    if not meta["email"]:
        raise HTTPException(400, "Account email is required to select the correct Google identity")
    if scheduler.reservation_ids(name):
        raise HTTPException(409, "Account has unfinished video tasks; finish or stop them first")
    from browser import ACTIVE_CONTEXTS
    lock = pool._locks.get(name)
    if name in WEB_SESSIONS or name in ACTIVE_CONTEXTS or (lock and lock.locked()) or JOBS.get(name, {}).get("status") == "running":
        raise HTTPException(409, "Account is busy; close its browser first")
    from dola_account_lifecycle import read_checkpoint
    phase = read_checkpoint(name).get("phase")
    if resume:
        if phase not in ("deleted", "submitting"):
            raise HTTPException(409, "No interrupted deletion awaiting login")
    else:
        if phase in ("submitting", "deleted"):
            raise HTTPException(409, "Previous deletion requires recovery; do not delete again")
    pool.auth_result(name, "expired", "Dola deletion / Google re-login in progress")
    JOBS[name] = {"kind":"delete_dola", "status":"running", "stage":"starting", "error":"", "started_at":time.time()}
    asyncio.create_task(_run_dola_delete(name, resume))
    return {"ok": True}


@app.post("/api/admin/accounts/{name}/reset-quota")
async def admin_account_reset_quota(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    name = pool.resolve_account(name) or name
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    pool.reset_quota(name)
    return {"ok": True}

@app.post("/api/admin/accounts/{name}/verify")
async def admin_account_verify(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    name = pool.resolve_account(name)
    if name in WEB_SESSIONS or JOBS.get(name, {}).get("status") == "running":
        raise HTTPException(409, "Account browser or login job is active")
    try:
        ok = await pool.verify_account(name)
    except RuntimeError as e:
        raise HTTPException(409, str(e))
    except FileNotFoundError as e:
        raise HTTPException(404, str(e))
    if ok and name in JOBS:
        JOBS[name] = {**JOBS[name], "status":"success", "error":""}
    return {"ok": ok}


async def _run_open_web(name: str, conversation_id=None):
    session = WEB_SESSIONS[name]
    lock = pool._locks.setdefault(name, asyncio.Lock())
    acquired = False
    context = None
    try:
        if lock.locked():
            raise RuntimeError("Account is busy")
        await lock.acquire()
        acquired = True
        pool._activities[name] = "browser_starting"
        from browser import launch_account_context

        # Interactive windows launch immediately, outside worker browser admission.
        async with manual_async_playwright() as playwright:
            context = await launch_account_context(
                playwright, name, headless=False,
                use_extension=config.EXTENSION_ENABLED)
            page = context.pages[0] if context.pages else await context.new_page()
            await page.bring_to_front()
            session["status"] = "open"
            session["started_at"] = time.time()
            pool._activities[name] = "browser_open"
            try:
                await page.goto("https://www.dola.com/chat" + ("/" + conversation_id if conversation_id else ""), timeout=60000,
                                wait_until="domcontentloaded")
            except Exception as exc:
                # Manual browsers must remain available for inspection and reload.
                session["error"] = f"{type(exc).__name__}: {exc}"[:500]
                print(f"[open-web:{name}] Navigation failed; keeping browser open: {session['error']}", flush=True)
            while context.pages:
                await asyncio.sleep(1)
    except Exception as exc:
        session["status"] = "failed"
        session["error"] = f"{type(exc).__name__}: {exc}"[:500]
        print(f"[open-web:{name}] {session['error']}", flush=True)
    finally:
        if context is not None:
            try:
                await context.close()
            except Exception:
                pass
            from browser import ACTIVE_CONTEXTS
            entry = ACTIVE_CONTEXTS.get(name)
            if entry and entry[0] is context:
                ACTIVE_CONTEXTS.pop(name, None)
        if acquired:
            pool._activities.pop(name, None)
            lock.release()
        WEB_SESSIONS.pop(name, None)


@app.post('/api/admin/image-studio/jobs/{identity}/open', status_code=202)
async def open_image_conversation(identity: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    row = image_studio.get(identity)
    account, conversation = row.get('account') or row.get('requested_account'), row.get('conversation_id')
    if not account:
        raise HTTPException(409, 'Tác vụ chưa được gán tài khoản Dola.')
    if account not in pool.accounts:
        raise HTTPException(404, 'Tài khoản của tác vụ không còn tồn tại.')
    if JOBS.get(account, {}).get('status') == 'running':
        raise HTTPException(409, 'Tài khoản đang đăng nhập hoặc kiểm tra. Hãy thử lại sau.')
    from browser import focus_task_conversation, focus_account_context
    try:
        focused = await focus_task_conversation(account, conversation) if conversation else await focus_account_context(account)
        if focused:
            return {'ok': True, 'status': 'open'}
    except Exception as exc:
        raise HTTPException(409, 'Trình duyệt tài khoản đang bận. Hãy thử lại khi tác vụ hoàn tất.') from exc
    lock = pool._locks.get(account)
    if account in WEB_SESSIONS or (lock and lock.locked()):
        raise HTTPException(409, 'Tài khoản đang bận hoặc trình duyệt đang mở. Hãy thử lại sau.')
    if len(WEB_SESSIONS) >= 10:
        raise HTTPException(409, 'Đã mở 10 cửa sổ Dola. Đóng bớt cửa sổ trước khi mở thêm.')
    WEB_SESSIONS[account] = {'status': 'starting', 'started_at': time.time()}
    asyncio.create_task(_run_open_web(account, conversation))
    return {'ok': True, 'status': 'starting'}


@app.post("/api/admin/accounts/{name}/open-web", status_code=202)
async def admin_account_open_web(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    name = pool.resolve_account(name)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    if JOBS.get(name, {}).get("status") == "running":
        raise HTTPException(409, "Account login window is already open")
    from browser import focus_account_context
    try:
        if await focus_account_context(name):
            logging.getLogger("uvicorn.error").info(
                "[open-web] account=%r focused existing browser", name)
            return {"ok": True, "status": "open", "reused": True}
    except Exception as exc:
        logging.getLogger("uvicorn.error").exception(
            "[open-web] account=%r failed to focus existing browser", name)
        raise HTTPException(409, "Existing browser could not be displayed; please retry shortly.") from exc
    lock = pool._locks.get(name)
    if lock and lock.locked():
        raise HTTPException(409, "Account browser is starting or closing; please retry shortly")
    if name in WEB_SESSIONS:
        raise HTTPException(409, "Account web session is already open")
    if len(WEB_SESSIONS) >= 10:
        raise HTTPException(409, "Đã mở 10 cửa sổ Open Web; đóng một cửa sổ trước khi mở thêm")
    WEB_SESSIONS[name] = {"status": "starting", "started_at": time.time()}
    asyncio.create_task(_run_open_web(name))
    return {"ok": True, "status": "starting"}


class DomainOpenWeb(BaseModel):
    domain: str = Field(max_length=253)


@app.post('/api/admin/accounts/open-web-domain', status_code=202)
async def open_web_domain(body: DomainOpenWeb, x_admin_key: str | None = Header(default=None)):
    global DOMAIN_WEB_QUEUE
    _admin_auth(x_admin_key)
    domain = body.domain.strip().lower().lstrip('@')
    accounts = []
    for account in pool.list_accounts():
        match = re.fullmatch(r'[^@\s]+@([^@\s]+)', (account.get('email') or '').strip().lower())
        if (match.group(1) if match else '') == domain:
            accounts.append(account)
    if not accounts:
        raise HTTPException(404, 'Không có account thuộc domain này')
    if DOMAIN_WEB_QUEUE is None:
        from account_web_queue import AccountWebQueue
        async def open_account(name):
            return await admin_account_open_web(name, x_admin_key=config.ADMIN_KEY)
        DOMAIN_WEB_QUEUE = AccountWebQueue(open_account, lambda: len(WEB_SESSIONS))
    names = [a['name'] for a in accounts if a['name'] not in WEB_SESSIONS]
    return DOMAIN_WEB_QUEUE.enqueue(names)


@app.post('/api/admin/accounts/open-web-domain/stop')
async def stop_domain_open_web(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if DOMAIN_WEB_QUEUE:
        DOMAIN_WEB_QUEUE.stop()
    return {'ok': True}


def find_duplicate_account(name, identity_hash):
    if not identity_hash:
        return None
    for other in pool.accounts:
        if other == name:
            continue
        meta = pool._meta(other)
        other_hash = meta["identity_hash"] if meta else ""
        file = pool.accounts_dir / other / "gateway_identity.json"
        if file.exists():
            other_hash = json.loads(file.read_text(encoding="utf-8")).get("identity_hash", other_hash)
        if other_hash == identity_hash:
            return other
    return None


def find_duplicate_login(name, email, account_type):
    normalized = (email or "").strip().casefold()
    if not normalized:
        return None
    for account in pool.list_accounts():
        if (account["name"] != name
                and (account.get("email") or "").strip().casefold() == normalized
                and (account.get("account_type") or "unknown") == account_type):
            return account["name"]
    return None


async def _run_add_job(name: str, email: str, password: str, totp: str, method="google", cookies=None):
    lock = pool._locks.setdefault(name, asyncio.Lock())
    try:
        async with pool.account_activity(name, "adding"):
            pool.auth_result(name, "adding")
            if method == "google":
                await add_account_flow(name, email, password, totp)
            else:
                await imported_account_flow(name, method, cookies, username=email, password=password)
            from browser import inspect_account_session
            result = await inspect_account_session(name)
            state = pool.auth_result(name, result["state"], result.get("error", ""), result)
            if state != "active":
                raise RuntimeError("Login not activated: " + state)
            JOBS[name] = {**JOBS[name], "status":"success", "error":""}
    except Exception as exc:
        state = pool._meta(name)["auth_state"]
        error = "Login incomplete; use Verify if login succeeded in browser, or Retry"
        if state not in ("duplicate", "expired"):
            pool.auth_result(name, "failed", error)
        JOBS[name] = {**JOBS[name], "status":"failed", "error":pool._meta(name)["auth_error"]}
        logging.getLogger("uvicorn.error").warning("[account-add] account=%s failed type=%s", name, type(exc).__name__)


@app.post("/api/admin/accounts/{name}/retry", status_code=202)
async def retry_account(name: str, body: AccountAdd, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    name = pool.resolve_account(name)
    if name not in pool.accounts:
        raise HTTPException(404, "Account not found")
    lock = pool._locks.get(name)
    if name in WEB_SESSIONS or (lock and lock.locked()) or JOBS.get(name, {}).get("status") == "running":
        raise HTTPException(409, "Account is busy")
    if pool._meta(name)["auth_state"] == "active":
        raise HTTPException(409, "Account is already active")
    if body.method not in ("google", "facebook", "cookies"):
        raise HTTPException(400, "Invalid login method")
    cookies = None
    if body.method == "cookies":
        try:
            cookies = parse_netscape(body.cookie_text)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
        duplicate = find_duplicate_account(name, cookie_identity(cookies))
        if duplicate:
            raise HTTPException(409, f"Duplicate account: {duplicate}")
    elif not body.email or not body.password:
        raise HTTPException(400, "Account and password required")
    if body.account_type not in ("unknown", "google", "facebook", "cookies"):
        raise HTTPException(400, "Invalid account type")
    account_type = body.method
    effective_email = body.email.strip() or pool._meta(name)["email"]
    duplicate = find_duplicate_login(name, effective_email, account_type)
    if duplicate:
        raise HTTPException(409, f"Login account already registered for {account_type}: {duplicate}")
    if body.connection_id is not None:
        try:
            account_connections.validate(body.connection_id)
        except ValueError as exc:
            raise HTTPException(422, str(exc)) from exc
        if scheduler.reservation_ids(name):
            raise HTTPException(409, 'Account has unfinished video tasks')
        account_connections.assign([name], body.connection_id)
    if body.email.strip():
        pool.set_email(name, body.email.strip())
    pool._conn.execute("UPDATE accounts_meta SET account_type=?, display_name=CASE WHEN ?<>'' THEN ? ELSE display_name END WHERE name=?",
                       (body.method, body.display_name.strip(), body.display_name.strip(), name))
    pool._conn.commit()
    JOBS[name] = {"kind":"add", "status":"running", "error":"", "started_at":time.time()}
    asyncio.create_task(_run_add_job(name, body.email, body.password, body.totp, body.method, cookies))
    return {"ok":True}


@app.post("/api/admin/accounts", status_code=202)
async def admin_account_add(body: AccountAdd, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    connection_id = body.connection_id or 'direct'
    try:
        account_connections.validate(connection_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    body.name = str(uuid.uuid4())
    if body.method not in ("google", "facebook", "cookies"):
        raise HTTPException(400, "Unsupported login method")
    if body.method in ("google", "facebook") and (not body.email or not body.password):
        raise HTTPException(400, "Login account and password are required")
    cookies = None
    if body.method == "cookies":
        try:
            cookies = parse_netscape(body.cookie_text)
        except ValueError as exc:
            raise HTTPException(400, str(exc)) from exc
    if body.account_type not in ("unknown", "google", "facebook", "cookies"):
        raise HTTPException(400, "Invalid account type")
    if body.method == "cookies":
        unknown = [a for a in pool.list_accounts() if a.get("login_ok") == 1
                   and not pool._meta(a["name"])["identity_hash"]
                   and not (pool.accounts_dir / a["name"] / "gateway_identity.json").exists()]
        if unknown:
            raise HTTPException(409, "Verify existing accounts first to check duplicate identities: " + ", ".join(a["name"] for a in unknown))
    duplicate = find_duplicate_account(body.name, cookie_identity(cookies or []))
    if duplicate:
        raise HTTPException(409, f"Duplicate Dola account: already registered as {duplicate}")
    account_type = body.method
    duplicate = find_duplicate_login(body.name, body.email, account_type)
    if duplicate:
        raise HTTPException(409, f"Login account already registered for {account_type}: {duplicate}")
    (pool.accounts_dir / body.name).mkdir(parents=True, exist_ok=False)
    pool._ensure_meta(body.name)
    pool.set_scheduling(body.name, False, manual=False)
    pool._conn.execute("UPDATE accounts_meta SET account_type=?, display_name=?, email=?, identity_hash=? WHERE name=?",
                       (body.method,
                        body.display_name.strip(), body.email.strip(), cookie_identity(cookies or []), body.name))
    pool._conn.commit()
    account_connections.assign([body.name], connection_id)
    JOBS[body.name] = {"kind": "add", "status": "running", "error": "", "started_at": time.time()}
    asyncio.create_task(_run_add_job(body.name, body.email, body.password, body.totp, body.method, cookies))
    return {"ok": True, "job": "running", "uuid": body.name, "name": body.name}


from bulk_accounts import parse_accounts, run_import

ACCOUNT_IMPORTS = {}
ACCOUNT_IMPORT_TASKS = set()


class GoogleBulkAdd(BaseModel):
    text: str = Field(max_length=200_000, repr=False)
    concurrency: int = Field(default=10, ge=1, le=10, strict=True)
    connection_id: str = 'direct'


@app.post("/api/admin/accounts/google-bulk", status_code=202)
async def google_bulk_add(body: GoogleBulkAdd, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    try:
        account_connections.validate(body.connection_id)
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    if any(p['status'] == 'running' for p in ACCOUNT_IMPORTS.values()):
        raise HTTPException(409, 'A Google import is already running')
    try:
        accounts = parse_accounts(body.text)
    except ValueError as exc:
        raise HTTPException(400, str(exc)) from exc
    identifier = str(uuid.uuid4())
    progress = {'status': 'running', 'rows': [{'email': email, 'status': 'queued'} for email, _ in accounts]}
    ACCOUNT_IMPORTS.clear()
    ACCOUNT_IMPORTS[identifier] = progress

    async def submit(email, password):
        result = await admin_account_add(AccountAdd(email=email, password=password,
                                                   connection_id=body.connection_id), x_admin_key)
        return result['uuid']

    task = asyncio.create_task(run_import(accounts, body.concurrency, progress, submit,
        lambda name: JOBS.get(name, {}).get('status')))
    ACCOUNT_IMPORT_TASKS.add(task)
    task.add_done_callback(ACCOUNT_IMPORT_TASKS.discard)
    return {'id': identifier}


@app.get("/api/admin/accounts/google-bulk/progress")
async def google_bulk_progress(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {'imports': ACCOUNT_IMPORTS}


@app.get("/api/admin/jobs")
async def admin_jobs(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"jobs": {pool.account_uuid(name): job for name, job in JOBS.items()}}


class BatchRenameRequest(BaseModel):
    name: str = Field(..., min_length=1, max_length=200)


@app.get("/api/admin/batches")
async def admin_batches(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"batches": store.list_batches()}


@app.patch("/api/admin/batches/{batch_id}")
async def admin_rename_batch(batch_id: str, body: BatchRenameRequest, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    name = body.name.strip()
    if not name:
        raise HTTPException(422, 'Batch name cannot be empty')
    try:
        store.rename_batch(batch_id, name)
    except KeyError:
        raise HTTPException(404, 'Batch not found')
    return {"ok": True}


@app.get("/api/admin/tasks")
async def admin_tasks(limit: int = -1, x_admin_key: str | None = Header(default=None), task_id: str = "",
                      query: str = "", search_in: str = "all", status: str = "",
                      account: str = "", duration: int = 0, edit_selected: bool | None = None,
                      page: int = 1, video_filter: str = "", group_by_batch: bool = False,
                      created_from: float | None = None, created_before: float | None = None):
    _admin_auth(x_admin_key)
    if any(value is not None and not math.isfinite(value) for value in (created_from, created_before)):
        raise HTTPException(422, "Invalid creation time")
    if created_from is not None and created_before is not None and created_from >= created_before:
        raise HTTPException(422, "Creation time range must start before it ends")
    if video_filter not in ("", "available", "missing"):
        raise HTTPException(422, "Unknown video filter")
    result = store.recent_tasks(limit, task_id=task_id, query=query, search_in=search_in,
        status=status, account=account, duration=duration, edit_selected=edit_selected,
        page=page, video_filter=video_filter, created_from=created_from, created_before=created_before,
        group_by_batch=group_by_batch)
    result["tasks"] = [{**t, "account": t.get("account_uuid") or
        (pool.account_uuid(t["account"]) if t.get("account") else None)} for t in result["tasks"]]
    studio_runtime = getattr(app.state, 'film_studio_runtime', None)
    if studio_runtime:
        result['tasks'] = studio_runtime.workflow.annotate_tasks(result['tasks'])
    return result


TASK_ACTION_LOCKS = {}


class EditSelectionRequest(BaseModel):
    selected: bool


@app.patch("/api/admin/tasks/{task_id}/edit-selection")
async def admin_edit_selection(task_id: str, body: EditSelectionRequest,
                               x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    async with TASK_ACTION_LOCKS.setdefault(task_id, asyncio.Lock()):
        row = store.get(task_id)
        if not row or row.get('deleted_at') is not None:
            raise HTTPException(404, 'Task not found')
        from video_tags import set_edit_selection
        try:
            # Copy off the event loop; keep the lock until the operation finishes.
            operation = asyncio.create_task(asyncio.to_thread(
                set_edit_selection, store, row, body.selected, config.DOWNLOAD_DIR,
                Path(__file__).resolve().parent / 'tag'))
            try:
                filename = await asyncio.shield(operation)
            except asyncio.CancelledError:
                await operation
                raise
        except FileNotFoundError as exc:
            raise HTTPException(404, str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        except OSError as exc:
            logging.getLogger('uvicorn.error').exception('Could not update tag copy for %s', task_id)
            raise HTTPException(500, 'Could not update tag copy. Check disk space and file permissions.') from exc
        return {'ok': True, 'edit_selected': body.selected, 'tag_filename': filename}


@app.delete("/api/admin/tasks/{task_id}")
async def admin_task_delete(task_id: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    async with TASK_ACTION_LOCKS.setdefault(task_id, asyncio.Lock()):
        row = store.get(task_id)
        if not row or row.get("deleted_at") is not None:
            raise HTTPException(404, "Task not found")
        if (row.get('batch_id') or '').startswith('studio_'):
            raise HTTPException(409, 'Film Studio retains its render history and selected takes')
        if row.get('edit_selected'):
            raise HTTPException(409, 'Unselect this video for editing before deleting its record')
        runner = TASK_RUNNERS.get(task_id)
        if row["status"] in ("queued", "processing", "needs_recovery") or (runner and not runner.done()):
            raise HTTPException(409, "Stop monitoring this task before deleting its record")
        try:
            deleted = store.delete_task(task_id)
        except ValueError as exc:
            raise HTTPException(409, str(exc)) from exc
        if not deleted:
            raise HTTPException(409, "Task cannot be deleted in its current state")
        return {"ok": True}



class SwitchTaskAccount(BaseModel):
    account_uuid: str


def _switch_account_candidates(row):
    from browser import ACTIVE_CONTEXTS
    return [
        {**pool.public_account(account), "number": index + 1}
        for index, account in enumerate(pool.list_accounts())
        if account["name"] != row.get("account")
        and pool._schedulable(account) and not account["busy"]
        and account["name"] not in WEB_SESSIONS
        and account["name"] not in ACTIVE_CONTEXTS
        and JOBS.get(account["name"], {}).get("status") != "running"
    ]


def _review_task(task_id):
    row = store.get(task_id)
    if not row or row.get("deleted_at") is not None:
        raise HTTPException(404, "Task not found")
    if row["status"] not in ("needs_recovery", "stopped"):
        raise HTTPException(409, "Only stopped tasks or tasks needing review can switch accounts")
    return row


@app.get("/api/admin/tasks/{task_id}/switch-account")
async def task_switch_candidates(task_id: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"accounts": _switch_account_candidates(_review_task(task_id))}


@app.post("/api/admin/tasks/{task_id}/switch-account")
async def task_switch_account(task_id: str, body: SwitchTaskAccount,
                              x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    async with TASK_ACTION_LOCKS.setdefault(task_id, asyncio.Lock()):
        row = _review_task(task_id)
        runner = TASK_RUNNERS.get(task_id)
        if runner and not runner.done():
            raise HTTPException(409, "Task is still running; try again shortly")
        target = next((a for a in _switch_account_candidates(row)
                       if a["uuid"] == body.account_uuid), None)
        if target is None:
            raise HTTPException(409, "Selected account is no longer available; choose another account")
        try:
            paths = json.loads(row.get("reference_paths") or "[]")
            sources = json.loads(row.get("reference_images") or "[]")
            if not isinstance(paths, list) or not isinstance(sources, list):
                raise ValueError()
            if sources and all(isinstance(s, str) and s.startswith("library://") for s in sources):
                paths = [str(image_library.path(s[10:])) for s in sources]
            if not paths and sources:
                if not all(isinstance(s, str) and s.startswith("local://") for s in sources):
                    raise ValueError()
                paths = [s[8:] for s in sources]
            if sources and not any(str(s).startswith("uploaded://") for s in sources) and len(paths) != len(sources):
                raise ValueError()
            if not all(isinstance(p, str) and Path(p).is_file() for p in paths):
                raise ValueError()
        except (TypeError, ValueError, OSError):
            raise HTTPException(409, "Saved reference images are missing or invalid; account was not changed")
        name = pool.resolve_account(target["uuid"])
        # No awaits between checking availability and reserving the target.
        # Preserve all input fields and reset only the previous execution state.
        if row.get("account") in pool.accounts:
            pool.set_scheduling(row["account"], False)
        store.update(task_id, account=name, account_uuid=target["uuid"],
                     status="queued", phase="ready", conversation_id=None,
                     deadline_at=None, last_poll_at=None, failure_code=None,
                     next_check_at=None, accepted_at=None, eta_seconds=None, eta_raw=None,
                     check_round=0, retry_count=0, after_index=0, dispatch_uncertain=0,
                     submitted_at=None, result_url=None, video_url=None, error=None,
                     finished_at=None)
        scheduler.release(row)
        scheduler.reserved[name] = task_id
        logging.getLogger("uvicorn.error").info(
            "Task account switch task=%s old=%s new=%s previous_conversation=%s",
            task_id, row.get("account"), name, row.get("conversation_id"))
        TASK_RUNNERS[task_id] = asyncio.create_task(_run_task(task_id))
        return {"ok": True, "status": "queued", "account_uuid": target["uuid"]}


@app.post("/api/admin/tasks/{task_id}/{action}")
async def admin_task_action(task_id: str, action: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if action not in ("open", "resume", "check", "stop", "retry"):
        raise HTTPException(404, "Unknown task action")
    async with TASK_ACTION_LOCKS.setdefault(task_id, asyncio.Lock()):
        row = store.get(task_id)
        if not row or row.get("deleted_at") is not None:
            raise HTTPException(404, "Task not found")
        if (row.get('batch_id') or '').startswith('studio_') and action == 'retry':
            raise HTTPException(409, 'Retry this video through Film Studio to preserve attempt accounting')
        if row["status"] == "completed" and action != "open":
            raise HTTPException(409, "Task already completed")
        if action == "open":
            if not row.get("account") or not row.get("conversation_id"):
                raise HTTPException(409, "This task has no saved conversation yet")
            from browser import focus_task_conversation
            if await focus_task_conversation(row["account"], row["conversation_id"]):
                return {"ok": True, "status": "open", "conversation_id": row["conversation_id"]}
            lock = pool._locks.get(row["account"])
            if row["account"] in WEB_SESSIONS or (lock and lock.locked()):
                raise HTTPException(409, "Account browser is starting or closing; try again shortly")
            WEB_SESSIONS[row["account"]] = {"status":"starting", "started_at":time.time()}
            asyncio.create_task(_run_open_web(row["account"], row["conversation_id"]))
            return {"ok": True, "status": "queued", "conversation_id": row["conversation_id"]}
        runner = TASK_RUNNERS.get(task_id)
        if runner and not runner.done():
            if action != "stop" and row.get("phase") != "waiting":
                raise HTTPException(409, "Task already running or queued")
            runner.cancel()
            try:
                await runner
            except asyncio.CancelledError:
                pass
        if action == "retry":
            try:
                if row.get('reference_snapshot') is not None:
                    task_reference_paths(row)
                store.retry_task(task_id, max_pending=config.MAX_PENDING_TASKS)
            except (ValueError, FileNotFoundError) as exc:
                raise HTTPException(409, str(exc)) from exc
            scheduler.release(row)
            TASK_RUNNERS[task_id] = asyncio.create_task(_run_task(task_id))
            return {"ok": True, "status": "queued", "task_id": task_id}
        if action == "stop":
            store.update(task_id, status="stopped", phase="stopped", next_check_at=None,
                         error="Monitoring stopped by user; Dola generation is not cancelled", finished_at=time.time())
            scheduler.release(row)
            return {"ok": True, "status": "stopped"}
        if not row.get("account"):
            raise HTTPException(409, "Task has no assigned account")
        if not scheduler.can_resume(row["account"], task_id):
            raise HTTPException(409, "Account is reserved by another task")
        if not row.get("conversation_id") and not row.get("result_url"):
            raise HTTPException(409, "Uncertain submission: use Open chat to inspect history before resubmitting")
        scheduler.reserved.setdefault(row["account"], task_id)
        store.update(task_id, status="queued", phase="downloading" if row.get("result_url") else "checking",
                     error=None, finished_at=None, next_check_at=None, check_round=0)
        TASK_RUNNERS[task_id] = asyncio.create_task(_run_task(task_id))
        return {"ok":True, "status":"queued"}


@app.get("/api/admin/stats")
async def admin_stats(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    st = store.stats()
    accs = pool.list_accounts()
    sched = [a for a in accs if pool._schedulable(a)]
    st["total_accounts"] = len(accs)
    st["available_accounts"] = sum(1 for a in sched if not a["busy"])
    st["total_remaining"] = sum(a["remaining"] for a in sched)
    totals = st.pop("per_account_total", {})
    st["per_account"] = [{**pool.public_account(a), "completed_total": totals.get(a["uuid"], 0)} for a in accs]
    return st


@app.get("/api/admin/keys")
async def admin_keys(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    keys = []
    for key in store.list_keys():
        usage = store.key_usage(store.hash_api_key(key["key"]))
        keys.append({**key, **{
            "today_total": usage["total"],
            "today_completed": usage["completed"],
            "today_failed": usage["failed"],
            "today_active": usage["active"],
            "today_queued": usage["queued"],
        }})
    return {"keys": keys, "env_keys": len(config.API_KEYS)}


@app.post("/api/admin/keys")
async def admin_key_create(body: KeyCreate, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    allowed = _normalize_allowed_durations(body.allowed_durations)
    return {"created": store.create_key(
        body.name,
        daily_limit=body.daily_limit,
        concurrency_limit=body.concurrency_limit,
        allowed_durations=allowed,
        expires_at=body.expires_at,
    )}


@app.patch("/api/admin/keys/{key}")
async def admin_key_patch(key: str, body: KeyPatch,
                          x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not store.get_key(key):
        raise HTTPException(404, "api key not found")
    fields = {}
    if body.name is not None:
        fields["name"] = body.name
    if body.enabled is not None:
        fields["enabled"] = 1 if body.enabled else 0
    if body.daily_limit is not None:
        fields["daily_limit"] = body.daily_limit
    if body.concurrency_limit is not None:
        fields["concurrency_limit"] = body.concurrency_limit
    if body.allowed_durations is not None:
        fields["allowed_durations"] = _normalize_allowed_durations(body.allowed_durations)
    if body.expires_at is not None:
        fields["expires_at"] = body.expires_at
    store.update_key(key, **fields)
    return {"ok": True}


@app.delete("/api/admin/keys/{key}")
async def admin_key_delete(key: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if not store.get_key(key):
        raise HTTPException(404, "api key not found")
    store.delete_key(key)
    return {"ok": True}


# Dashboard single-file frontend
# Register API routes before the dashboard catch-all mount.
register_asset_routes(app, store, _admin_auth, _auth, VideoGenRequest, _submit_video)

from task_references import register_task_reference_routes
register_task_reference_routes(app, store, _admin_auth, image_library, Path(__file__).resolve().parent)

from film_studio import register_film_studio
film_registry = register_film_studio(app, _admin_auth, Path(config.DB_PATH).with_name('film_studio.db'))
from studio_api import register_studio_workflow


async def _studio_submit_video(payload):
    request = VideoGenRequest(batch_id=payload['batch_id'], name=payload['name'],
                              model='seedance-2.5', ratio='16:9', duration=30, count=1,
                              prompt=payload['prompt'], reference_images=payload['reference_images'],
                              reference_aliases=payload.get('reference_aliases', []))
    client = {**_anonymous_client(), 'api_key_name': 'Film Studio', 'concurrency_limit': 1}
    try:
        return await _submit_video(request, client, studio_task_id=payload['task_id'])
    except HTTPException as exc:
        if store.get(payload['task_id']) is None:
            from studio_runtime import AdmissionRejected
            raise AdmissionRejected(exc.status_code, exc.detail) from exc
        raise


register_studio_workflow(app, _admin_auth, film_registry, image_library, _studio_submit_video, store.get,
                         config.REFERENCE_IMAGE_MAX_COUNT, image_studio=image_studio)


FILM_STUDIO_SUPERVISOR = None


async def _film_studio_supervisor_loop():
    """Keep the selected Account List context and ChatGPT worker alive.

    The durable workflow remains authoritative.  This supervisor only restores
    transport while a nonterminal, unpaused Film Studio run exists, and never
    opens a second browser for an already-live account context.
    """
    from browser import ACTIVE_CONTEXTS
    from studio_workflow import TERMINAL
    while True:
        try:
            runtime = app.state.film_studio_runtime
            active = [item for item in runtime.workflow.list()
                      if item['status'] not in TERMINAL and not item.get('paused')]
            if active:
                settings = film_registry.read()
                worker = app.state.film_studio_chatgpt
                if settings.account_id:
                    account = pool.resolve_account(settings.account_id)
                    if (account not in ACTIVE_CONTEXTS and account not in WEB_SESSIONS
                            and not (pool._locks.get(account) and pool._locks[account].locked())):
                        WEB_SESSIONS[account] = {'status': 'starting', 'started_at': time.time(),
                                                 'owner': 'film_studio_supervisor'}
                        asyncio.create_task(_run_open_web(account))
                    if account in ACTIVE_CONTEXTS and not worker.enabled:
                        await worker.connect_profile()
                elif (settings.profile_id or settings.chatgpt_profile_id) and not worker.enabled:
                    await worker.connect_profile()
                # Connection alone does not advance a durable Film Studio
                # task.  The worker owns one serialized operation per tick
                # and journals delivery before Send, so invoking it here lets
                # an unpaused run recover and continue after restart without
                # requiring a manual API request.
                if worker.enabled:
                    await worker.tick()
        except asyncio.CancelledError:
            raise
        except Exception as exc:
            logging.getLogger('uvicorn.error').warning(
                'Film Studio transport supervisor retrying: %s', str(exc)[:500])
        await asyncio.sleep(5)


@app.on_event('startup')
async def start_film_studio_supervisor():
    global FILM_STUDIO_SUPERVISOR
    if FILM_STUDIO_SUPERVISOR is None or FILM_STUDIO_SUPERVISOR.done():
        FILM_STUDIO_SUPERVISOR = asyncio.create_task(_film_studio_supervisor_loop())


@app.on_event('shutdown')
async def stop_film_studio_supervisor():
    global FILM_STUDIO_SUPERVISOR
    if FILM_STUDIO_SUPERVISOR:
        FILM_STUDIO_SUPERVISOR.cancel()
        await asyncio.gather(FILM_STUDIO_SUPERVISOR, return_exceptions=True)
        FILM_STUDIO_SUPERVISOR = None

app.mount("/", StaticFiles(directory="web", html=True), name="web")


