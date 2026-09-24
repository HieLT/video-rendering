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
import re
import shutil
import tempfile
import time
import traceback
import uuid
from collections import defaultdict
from pathlib import Path

from fastapi import FastAPI, File, Header, HTTPException, UploadFile
from fastapi.staticfiles import StaticFiles
from patchright.async_api import async_playwright
from pydantic import BaseModel, Field
from PIL import Image

import config
from account_import import parse_netscape, imported_account_flow, cookie_identity
from add_account import add_account_flow
from browser_pool import AllAccountsLimitedError, AllAccountsQuotaBlockedError, BrowserPool
from media import download_reference_images, validate_reference_urls
from store import PendingTaskLimitExceeded, TaskQuotaExceeded, TaskStore

Path(config.DOWNLOAD_DIR).mkdir(parents=True, exist_ok=True)
Path("web").mkdir(parents=True, exist_ok=True)

app = FastAPI(title="Video Rendering", version="0.4.0")

store = TaskStore(config.DB_PATH)
pool = BrowserPool(max_concurrency=config.MAX_CONCURRENCY)

app.mount("/videos", StaticFiles(directory=config.DOWNLOAD_DIR), name="videos")

# Background jobs (add/verify), in-memory
JOBS: dict[str, dict] = {}
WEB_SESSIONS: dict[str, dict] = {}
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
    name: str | None = Field(None, max_length=200)
    model: str = "seedance-2.0"
    prompt: str = Field(..., min_length=1)
    size: str | None = None
    ratio: str | None = None
    duration: int | None = Field(None, ge=10, le=30)
    # Accepts durations: 10, 15, 30 seconds.
    reference_images: list[str] = Field(default_factory=list)
    start_end: bool = False


class TaskResponse(BaseModel):
    id: str
    name: str | None = None
    status: str
    model: str | None = None
    prompt: str | None = None
    video_url: str | None = None
    error: str | None = None


@app.post("/api/admin/reference-images", status_code=201)
async def upload_reference_images(
    files: list[UploadFile] = File(...),
    x_admin_key: str | None = Header(default=None),
):
    """Stores local reference images briefly for the next generation task."""
    _admin_auth(x_admin_key)
    if not files or len(files) > config.REFERENCE_IMAGE_MAX_COUNT:
        raise HTTPException(422, f"Maximum of {config.REFERENCE_IMAGE_MAX_COUNT} images allowed")
    root = Path(tempfile.mkdtemp(prefix="dola_upload_"))
    paths = []
    try:
        for index, upload in enumerate(files):
            data = await upload.read()
            if len(data) > config.REFERENCE_IMAGE_MAX_BYTES:
                raise ValueError("Reference image exceeds single file size limit")
            from io import BytesIO
            with Image.open(BytesIO(data)) as image:
                image.verify()
                image_format = image.format
            suffix = {"JPEG": ".jpg", "PNG": ".png", "WEBP": ".webp"}.get(image_format)
            if not suffix:
                raise ValueError("Reference image only supports JPEG, PNG, WEBP")
            path = root / f"image_{index}{suffix}"
            path.write_bytes(data)
            paths.append(str(path))
    except Exception as exc:
        shutil.rmtree(root, ignore_errors=True)
        raise HTTPException(422, str(exc)) from exc
    token = "uploaded://" + uuid.uuid4().hex
    UPLOADED_REFERENCES[token] = (root, paths)
    return {"reference_images": [token], "count": len(paths)}


def _resolve_ratio(size, ratio):
    if size and size in SIZE_TO_RATIO:
        return SIZE_TO_RATIO[size]
    return ratio


TASK_RUNNERS = {}


async def _run_task(task_id, model, prompt, ratio, duration, reference_images, client):
    TASK_RUNNERS[task_id] = asyncio.current_task()
    api_key_hash = client.get("api_key_hash")
    acquired = False
    reference_roots = []
    try:
        await key_limiter.acquire(api_key_hash, client.get("concurrency_limit", 0))
        acquired = True
        store.update(task_id, status="processing", started_at=time.time())

        def on_conversation_id(account, conversation_id, deadline_at):
            store.update(task_id, status="processing", account=account,
                         conversation_id=conversation_id, deadline_at=deadline_at,
                         last_poll_at=time.time())

        def on_poll(now):
            store.update(task_id, last_poll_at=now)

        reference_paths = []
        # Resolve in submission order, including mixed uploaded and remote images.
        for reference in reference_images or []:
            uploaded = UPLOADED_REFERENCES.pop(reference, None)
            if uploaded:
                root, paths = uploaded
                reference_roots.append(root)
                reference_paths.extend(paths)
            else:
                reference_root, downloaded = await download_reference_images(
                    [reference], task_id)
                if reference_root:
                    reference_roots.append(reference_root)
                reference_paths.extend(downloaded)
        result = await pool.generate_video(
            prompt, ratio, duration, model,
            on_conversation_id=on_conversation_id, on_poll=on_poll,
            reference_image_paths=reference_paths)
        public_url = f"{config.PUBLIC_BASE}/videos/{Path(result['local_path']).name}"
        store.update(task_id, status="completed", video_url=public_url,
                     account=result.get("account"), last_poll_at=time.time(),
                     finished_at=time.time())
    except (AllAccountsLimitedError, AllAccountsQuotaBlockedError) as e:
        store.update(task_id, status="failed", error=str(e),
                     failure_code="429", finished_at=time.time())
    except Exception as e:
        print(f"[task:{task_id}] failed type={type(e).__name__}: "
              f"{str(e).encode('ascii', 'backslashreplace').decode('ascii')}", flush=True)
        print(traceback.format_exc().encode('ascii', 'backslashreplace').decode('ascii'), flush=True)
        row = store.get(task_id)
        status = "failed" if isinstance(e, GenerationRejectedError) else ("needs_recovery" if row.get("conversation_id") else "failed")
        store.update(task_id, status=status, error=str(e), finished_at=time.time())
    finally:
        if TASK_RUNNERS.get(task_id) is asyncio.current_task():
            TASK_RUNNERS.pop(task_id, None)
        for reference_root in reference_roots:
            shutil.rmtree(reference_root, ignore_errors=True)
        if acquired:
            await key_limiter.release(api_key_hash)


async def _resume_task(row: dict):
    task_id = row["id"]
    TASK_RUNNERS[task_id] = asyncio.current_task()
    deadline = row.get("deadline_at") or (
        time.time() + (1800 if row.get("duration") == 30 else config.VIDEO_TIMEOUT)
    )
    remaining = max(1, int(deadline - time.time()))
    api_key_hash = row.get("api_key_hash")
    acquired = False
    try:
        await key_limiter.acquire(
            api_key_hash, int(row.get("client_concurrency_limit") or 0)
        )
        acquired = True
        store.update(task_id, status="processing", last_poll_at=time.time(),
                     started_at=row.get("started_at") or time.time())

        def on_poll(now):
            store.update(task_id, last_poll_at=now)

        result = await pool.resume_video(
            row["account"], row["conversation_id"], remaining, on_poll=on_poll)
        public_url = f"{config.PUBLIC_BASE}/videos/{Path(result['local_path']).name}"
        store.update(task_id, status="completed", video_url=public_url,
                     account=result.get("account"), last_poll_at=time.time(),
                     finished_at=time.time())
    except Exception as e:
        store.update(task_id, status="failed" if isinstance(e, GenerationRejectedError) else "needs_recovery", error=str(e),
                     finished_at=time.time())
    finally:
        if TASK_RUNNERS.get(task_id) is asyncio.current_task():
            TASK_RUNNERS.pop(task_id, None)
        if acquired:
            await key_limiter.release(api_key_hash)


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
    """Recovers accepted sessions on startup and requeues pending tasks."""
    orphaned = store.fail_orphaned_processing_tasks()
    if orphaned:
        print(f"[startup] marked {orphaned} orphaned processing task(s) as failed", flush=True)
    for row in store.recoverable_tasks():
        asyncio.create_task(_resume_task(row))
    for row in store.recoverable_queued_tasks():
        ratio = row.get("ratio")
        if ratio == "default":
            ratio = None
        asyncio.create_task(_run_task(
            row["id"], row["model"], row["prompt"], ratio, row["duration"],
            _task_reference_images(row.get("reference_images")), _task_client(row),
        ))


@app.post("/v1/videos/generations", response_model=TaskResponse)
async def create_video(req: VideoGenRequest, authorization: str | None = Header(default=None)):
    client = _auth(authorization)
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
        for reference in req.reference_images:
            if reference.startswith("uploaded://"):
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
        if len(set(reference_images)) != len(reference_images):
            raise ValueError("Duplicate reference entries are not supported")
        if req.start_end and image_count != 2:
            raise ValueError("Start / End requires exactly two images: start first, end second")
    except ValueError as exc:
        raise HTTPException(422, str(exc)) from exc
    # Queue task when accounts are busy; reject only when pool is fully exhausted.
    if not pool.available and pool.all_accounts_limited:
        raise HTTPException(429, "Rate limited: All accounts reached Dola daily video limit, please try again tomorrow")
    if not pool.available and pool.all_accounts_quota_blocked:
        raise HTTPException(429, "Insufficient credits: All accounts lack points, waiting for refresh")
    if not pool.accounts:
        raise HTTPException(503, "no account in pool")
    task_id = "video_" + uuid.uuid4().hex
    ratio = _resolve_ratio(req.size, req.ratio)
    prompt = req.prompt
    if req.start_end:
        prompt += ("\n\nUse the first uploaded image as the opening frame and the second "
                   "uploaded image as the ending frame. Create continuous motion between "
                   "these two frames, preserving their composition and subjects.")
    try:
        store.create(
            task_id,
            req.model,
            prompt,
            ratio or "default",
            duration,
            reference_images=json.dumps(reference_images, ensure_ascii=False),
            start_end=req.start_end,
            api_key_hash=client["api_key_hash"],
            api_key_name=client["api_key_name"],
            daily_limit=client["daily_limit"],
            concurrency_limit=client["concurrency_limit"],
            max_pending=config.MAX_PENDING_TASKS,
            name=req.name,
        )
    except TaskQuotaExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    except PendingTaskLimitExceeded as exc:
        raise HTTPException(429, str(exc)) from exc
    asyncio.create_task(_run_task(
        task_id, req.model, prompt, ratio, duration, reference_images, client
    ))
    return TaskResponse(id=task_id, name=(req.name or "").strip(), status="queued", model=req.model, prompt=prompt)


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
    name: str
    email: str = ""
    password: str = ""
    totp: str = ""
    method: str = "google"
    account_type: str = "unknown"
    display_name: str = ""
    cookie_text: str = Field(default="", max_length=1_000_000)


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
    _admin_auth(x_admin_key)
    return {"accounts": pool.list_accounts()}


@app.patch("/api/admin/accounts/{name}")
async def admin_account_patch(name: str, body: AccountPatch,
                              x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
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


@app.post("/api/admin/accounts/{name}/verify")
async def admin_account_verify(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
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


async def _run_open_web(name: str):
    session = WEB_SESSIONS[name]
    lock = pool._locks.setdefault(name, asyncio.Lock())
    acquired = False
    try:
        if lock.locked():
            raise RuntimeError("Account is busy")
        await lock.acquire()
        acquired = True
        pool._activities[name] = "browser_open"
        from browser import launch_account_context

        async with async_playwright() as playwright:
            context = await launch_account_context(
                playwright, name, headless=False,
                use_extension=config.EXTENSION_ENABLED)
            session["status"] = "open"
            session["started_at"] = time.time()
            page = context.pages[0] if context.pages else await context.new_page()
            await page.goto("https://www.dola.com/chat", timeout=60000,
                            wait_until="domcontentloaded")
            while context.pages:
                await asyncio.sleep(1)
    except Exception as exc:
        session["status"] = "failed"
        session["error"] = f"{type(exc).__name__}: {exc}"[:500]
        print(f"[open-web:{name}] {session['error']}", flush=True)
    finally:
        if acquired:
            pool._activities.pop(name, None)
            lock.release()
        WEB_SESSIONS.pop(name, None)


@app.post("/api/admin/accounts/{name}/open-web", status_code=202)
async def admin_account_open_web(name: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
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
    WEB_SESSIONS[name] = {"status": "starting", "started_at": time.time()}
    asyncio.create_task(_run_open_web(name))
    return {"ok": True, "status": "starting"}


class Try30Request(BaseModel):
    start_end: bool = False
    prompt: str = Field(min_length=1)
    model: str = "seedance-2.0"
    ratio: str = "16:9"
    reference_images: list[str] = Field(default_factory=list)


async def _run_try_30s(name, body, uploads, lock):
    job = JOBS[name]
    try:
        from try_30s import run_preview
        prompt = body.prompt
        if body.start_end:
            prompt += ("\n\nUse the first uploaded image as the opening frame and the second "
                       "uploaded image as the ending frame. Create continuous motion between "
                       "these two frames, preserving their composition and subjects.")
        await run_preview(name, prompt, body.model, body.ratio,
                          [path for _, paths in uploads for path in paths], job)
    except Exception as exc:
        job["result"] = "failed"
        job["error"] = str(exc)[:1000]
    finally:
        job["status"] = job.get("result", "failed")
        job["browser_closed"] = True
        pool._activities.pop(name, None)
        WEB_SESSIONS.pop(name, None)
        lock.release()
        for root, _ in uploads:
            shutil.rmtree(root, ignore_errors=True)


@app.post("/api/admin/accounts/{name}/try-30s", status_code=202)
async def admin_try_30s(name: str, body: Try30Request,
                       x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if name not in pool.accounts:
        raise HTTPException(404, "account not found")
    if not body.prompt.strip():
        raise HTTPException(422, "Please enter a prompt")
    if body.model not in ("seedance-2.0", "seedance-2.5") or body.ratio not in SIZE_TO_RATIO.values():
        raise HTTPException(422, "Unsupported model or ratio")
    if not config.EXTENSION_ENABLED:
        raise HTTPException(409, "Enable the Dola extension before trying 30s")
    lock = pool._locks.setdefault(name, asyncio.Lock())
    if lock.locked() or name in WEB_SESSIONS or JOBS.get(name, {}).get("status") == "running":
        raise HTTPException(409, "Account is busy. Close its browser or choose another account.")
    tokens = list(dict.fromkeys(body.reference_images))
    if any(token not in UPLOADED_REFERENCES for token in tokens):
        raise HTTPException(422, "Reference images expired; please try again")
    if body.start_end and sum(len(UPLOADED_REFERENCES[token][1]) for token in tokens) != 2:
        raise HTTPException(422, "Start / End requires exactly two images: start first, end second")
    await lock.acquire()
    uploads = [UPLOADED_REFERENCES.pop(token) for token in tokens]
    pool._activities[name] = "try_30s"
    JOBS[name] = {"kind": "try_30s", "status": "running", "result": None,
                  "message": "Opening Chrome...", "error": "", "browser_closed": False}
    WEB_SESSIONS[name] = {"status": "starting"}
    WEB_SESSIONS[name]["task"] = asyncio.create_task(_run_try_30s(name, body, uploads, lock))
    return {"ok": True, "account": name}


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
    if not NAME_RE.match(body.name):
        raise HTTPException(400, "invalid account name")
    if body.name in pool.accounts:
        raise HTTPException(409, "account exists")
    if JOBS.get(body.name, {}).get("status") == "running":
        raise HTTPException(409, "add job running")
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
    JOBS[body.name] = {"kind": "add", "status": "running", "error": "", "started_at": time.time()}
    asyncio.create_task(_run_add_job(body.name, body.email, body.password, body.totp, body.method, cookies))
    return {"ok": True, "job": "running"}


@app.get("/api/admin/jobs")
async def admin_jobs(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"jobs": JOBS}


@app.get("/api/admin/tasks")
async def admin_tasks(x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    return {"tasks": store.recent_tasks(limit=-1)}


TASK_ACTION_LOCKS = {}


@app.delete("/api/admin/tasks/{task_id}")
async def admin_task_delete(task_id: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    async with TASK_ACTION_LOCKS.setdefault(task_id, asyncio.Lock()):
        row = store.get(task_id)
        if not row or row.get("deleted_at") is not None:
            raise HTTPException(404, "Task not found")
        runner = TASK_RUNNERS.get(task_id)
        if row["status"] in ("queued", "processing") or (runner and not runner.done()):
            raise HTTPException(409, "Stop monitoring this task before deleting its record")
        if not store.delete_task(task_id):
            raise HTTPException(409, "Task cannot be deleted in its current state")
        return {"ok": True}


@app.post("/api/admin/tasks/{task_id}/{action}")
async def admin_task_action(task_id: str, action: str, x_admin_key: str | None = Header(default=None)):
    _admin_auth(x_admin_key)
    if action not in ("open", "resume", "stop"):
        raise HTTPException(404, "Unknown task action")
    async with TASK_ACTION_LOCKS.setdefault(task_id, asyncio.Lock()):
        row = store.get(task_id)
        if not row or row.get("deleted_at") is not None:
            raise HTTPException(404, "Task not found")
        if not row.get("account") or not row.get("conversation_id"):
            raise HTTPException(409, "Task has no saved conversation")
        if row["status"] == "completed":
            raise HTTPException(409, "Task already completed")
        runner = TASK_RUNNERS.get(task_id)
        if action == "stop":
            if runner and not runner.done():
                runner.cancel()
                try:
                    await runner
                except asyncio.CancelledError:
                    pass
            store.update(task_id, status="stopped", error="Monitoring stopped by user; Dola generation is not cancelled",
                         finished_at=time.time())
            return {"ok": True, "status": "stopped"}
        if runner and not runner.done():
            if action == "open":
                from browser import focus_account_context
                if await focus_account_context(row["account"]):
                    return {"ok": True, "status": "processing"}
                raise HTTPException(409, "Browser is recovering; retry shortly")
            raise HTTPException(409, "Task is already being monitored")
        for other_id, other_runner in TASK_RUNNERS.items():
            if other_id != task_id and not other_runner.done():
                other = store.get(other_id)
                if other and other.get("account") == row["account"]:
                    raise HTTPException(409, "Another task is using this account")
        lock = pool._locks.get(row["account"])
        if (lock and lock.locked()) or row["account"] in WEB_SESSIONS:
            raise HTTPException(409, "Account is busy in another browser session")
        # Only reopen the saved conversation. Never repeat generation submission.
        store.update(task_id, status="processing", error=None, finished_at=None,
                     deadline_at=time.time() + max(1800, config.VIDEO_TIMEOUT))
        TASK_RUNNERS[task_id] = asyncio.create_task(_resume_task(store.get(task_id)))
        logging.getLogger("uvicorn.error").info(
            "[task-recovery] task=%s action=%s conversation=%s", task_id, action, row["conversation_id"])
        return {"ok": True, "status": "processing"}


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
    st["per_account"] = [{**a, "completed_total": totals.get(a["name"], 0)} for a in accs]
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
app.mount("/", StaticFiles(directory="web", html=True), name="web")

