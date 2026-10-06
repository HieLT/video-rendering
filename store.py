"""Task state persistence (SQLite) + API Key management with asyncio locks."""
import datetime
import hashlib
import json
import secrets
import sqlite3
import threading
import time

from production_store import ProductionStoreMixin
from review_store import production_status

_LOCK = threading.Lock()
SUPPORTED_DURATIONS = (10, 15, 30)
DEFAULT_ALLOWED_DURATIONS = list(SUPPORTED_DURATIONS)


class TaskSubmissionConflict(ValueError):
    pass


class TaskQuotaExceeded(RuntimeError):
    """API Key reached daily task quota."""


class PendingTaskLimitExceeded(RuntimeError):
    """Server pending task queue is full."""


class TaskStore(ProductionStoreMixin):
    def __init__(self, db_path: str):
        self.db_path = db_path
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.execute("PRAGMA busy_timeout=5000")
        self._lock = _LOCK
        self._conn.execute("PRAGMA foreign_keys=ON")
        try:
            self._init()
        except BaseException:
            self._conn.close()
            raise

    def _init(self):
        with _LOCK:
            self._backup_before_migration()
            with self._conn:
                self._conn.execute("BEGIN IMMEDIATE")
                self._conn.execute(
                    "CREATE TABLE IF NOT EXISTS generated_reset_tasks (task_id TEXT PRIMARY KEY)"
                )
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS tasks (
                        id TEXT PRIMARY KEY,
                        model TEXT,
                        prompt TEXT,
                        ratio TEXT,
                        duration INTEGER,
                        status TEXT,
                        video_url TEXT,
                        error TEXT,
                        created_at REAL,
                        updated_at REAL,
                        conversation_id TEXT,
                        deadline_at REAL,
                        last_poll_at REAL,
                        failure_code TEXT,
                        reference_images TEXT,
                        api_key_hash TEXT,
                        api_key_name TEXT,
                        started_at REAL,
                        finished_at REAL,
                        client_concurrency_limit INTEGER DEFAULT 0
                    )
                    """
                )
                # Legacy migration: add missing columns for task recovery, client usage, and timing stats.
                for column, definition in (
                    ("name", "TEXT DEFAULT ''"),
                    ("edit_selected", "INTEGER NOT NULL DEFAULT 0"),
                    ("tag_filename", "TEXT"),
                    ("account", "TEXT"),
                    ("account_uuid", "TEXT"),
                    ("deleted_at", "REAL"),
                    ("start_end", "INTEGER DEFAULT 0"),
                    ("batch_id", "TEXT"),
                    ("batch_index", "INTEGER"),
                    ("batch_count", "INTEGER"),
                    ("conversation_id", "TEXT"),
                    ("deadline_at", "REAL"),
                    ("last_poll_at", "REAL"),
                    ("failure_code", "TEXT"),
                    ("reference_images", "TEXT"),
                    ("api_key_hash", "TEXT"),
                    ("api_key_name", "TEXT"),
                    ("started_at", "REAL"),
                    ("finished_at", "REAL"),
                    ("client_concurrency_limit", "INTEGER DEFAULT 0"),
                ):
                    columns = {row[1] for row in self._conn.execute("PRAGMA table_info(tasks)")}
                    if column not in columns:
                        self._conn.execute(f"ALTER TABLE tasks ADD COLUMN {column} {definition}")
                self._conn.execute(
                    """
                    CREATE TABLE IF NOT EXISTS api_keys (
                        key TEXT PRIMARY KEY,
                        name TEXT,
                        enabled INTEGER DEFAULT 1,
                        created_at REAL,
                        last_used_at REAL DEFAULT 0,
                        daily_limit INTEGER DEFAULT 0,
                        concurrency_limit INTEGER DEFAULT 0,
                        allowed_durations TEXT DEFAULT '[10, 15, 30]',
                        expires_at REAL DEFAULT 0
                    )
                    """
                )
                # Legacy migration: add client-level policy columns to existing API Keys.
                for column, definition in (
                    ("daily_limit", "INTEGER DEFAULT 0"),
                    ("concurrency_limit", "INTEGER DEFAULT 0"),
                    ("allowed_durations", "TEXT DEFAULT '[10, 15, 30]'"),
                    ("expires_at", "REAL DEFAULT 0"),
                ):
                    columns = {row[1] for row in self._conn.execute("PRAGMA table_info(api_keys)")}
                    if column not in columns:
                        self._conn.execute(f"ALTER TABLE api_keys ADD COLUMN {column} {definition}")
                self._migrate_v2()


    @staticmethod
    def hash_api_key(key: str) -> str:
        return hashlib.sha256(key.encode("utf-8")).hexdigest()

    @staticmethod
    def _parse_allowed_durations(raw) -> list[int]:
        if isinstance(raw, list):
            values = raw
        else:
            try:
                values = json.loads(raw or "[]")
            except (TypeError, json.JSONDecodeError):
                values = []
        result = set()
        for value in values if isinstance(values, list) else []:
            try:
                value = int(value)
            except (TypeError, ValueError):
                continue
            if value in SUPPORTED_DURATIONS:
                result.add(value)
        return sorted(result) or list(DEFAULT_ALLOWED_DURATIONS)

    @classmethod
    def _key_row(cls, row):
        if not row:
            return None
        data = dict(row)
        data["enabled"] = bool(data.get("enabled"))
        data["allowed_durations"] = cls._parse_allowed_durations(data.get("allowed_durations"))
        data["daily_limit"] = max(0, int(data.get("daily_limit") or 0))
        data["concurrency_limit"] = max(0, int(data.get("concurrency_limit") or 0))
        data["expires_at"] = float(data.get("expires_at") or 0)
        return data

    # ===== tasks =====

    def create(self, task_id, *args, **kwargs):
        return self.create_batch([task_id], *args, **kwargs)

    def create_batch(
        self,
        task_ids,
        model,
        prompt,
        ratio,
        duration,
        account=None,
        reference_images=None,
        api_key_hash=None,
        api_key_name=None,
        daily_limit=0,
        concurrency_limit=0,
        max_pending=0,
        start_end=False,
        batch_id=None,
        name=None,
        *,
        scene_id=None,
        reference_snapshot=None,
        guard_scene=False,
        scene_updated_at=None,
    ):
        if not 1 <= len(task_ids) <= 5 or len(set(task_ids)) != len(task_ids):
            raise ValueError("Expected 1 to 5 unique task IDs")
        now = time.time()
        with _LOCK, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            if scene_id is not None:
                scene = self._require("scenes", scene_id)
                if guard_scene:
                    if batch_id and self._conn.execute('SELECT 1 FROM tasks WHERE batch_id=?',(batch_id,)).fetchone():
                        raise TaskSubmissionConflict('This generation action has already been submitted')
                    if self._conn.execute("SELECT 1 FROM tasks WHERE scene_id=? AND status IN ('queued','processing') AND deleted_at IS NULL",(scene_id,)).fetchone():
                        raise TaskSubmissionConflict('This Scene already has active candidates; review them or wait before regenerating')
                    if scene_updated_at is not None and scene['updated_at'] != scene_updated_at:
                        raise TaskSubmissionConflict('Scene references/configuration changed during submission; refresh and retry')
            snapshot_json = (self._validate_reference_snapshot(scene_id, reference_snapshot)
                             if reference_snapshot is not None else None)
            if max_pending > 0:
                pending = self._conn.execute(
                    "SELECT COUNT(*) FROM tasks WHERE status IN ('queued','processing')"
                ).fetchone()[0]
                if pending + len(task_ids) > max_pending:
                    raise PendingTaskLimitExceeded(
                        f"Pending task queue has reached server limit ({max_pending})"
                    )
            if daily_limit > 0 and api_key_hash:
                day = datetime.date.today().isoformat()
                used = self._conn.execute(
                    "SELECT COUNT(*) FROM tasks "
                    "WHERE api_key_hash=? AND date(created_at,'unixepoch','localtime')=?",
                    (api_key_hash, day),
                ).fetchone()[0]
                if used + len(task_ids) > daily_limit:
                    raise TaskQuotaExceeded(
                        f"API Key daily quota exceeded ({daily_limit} tasks)"
                    )
            self._insert_task_rows([(
                    task_id,
                    model,
                    prompt,
                    ratio,
                    duration,
                    account,
                    now,
                    now,
                    (reference_images.get(task_id, "[]") if isinstance(reference_images, dict) else reference_images) or "[]",
                    api_key_hash,
                    api_key_name,
                    None,
                    None,
                    max(0, int(concurrency_limit or 0)),
                    int(start_end),
                    batch_id,
                    index,
                    len(task_ids),
                    ((name or "").strip() + (f" ({index}/{len(task_ids)})" if len(task_ids) > 1 and name else "")),
                    scene_id,
                    snapshot_json,
                ) for index, task_id in enumerate(task_ids, 1)])
            self._conn.commit()

    @staticmethod
    def submission_batch_id(scope, identifier, request_id, client_hash):
        return 'batch_' + hashlib.sha256(json.dumps([scope,identifier,request_id,client_hash]).encode()).hexdigest()

    def _insert_task_rows(self, rows):
        self._conn.executemany("INSERT INTO tasks (id,model,prompt,ratio,duration,status,account,created_at,updated_at,conversation_id,deadline_at,last_poll_at,failure_code,reference_images,api_key_hash,api_key_name,started_at,finished_at,client_concurrency_limit,start_end,batch_id,batch_index,batch_count,name,scene_id,reference_snapshot) VALUES (?,?,?,?,?,'queued',?,?,?,NULL,NULL,0,NULL,?,?,?,?,?,?,?,?,?,?,?,?,?)", rows)

    def _project_generation_status(self, project_id):
        self._require('projects', project_id)
        ids = [r[0] for r in self._conn.execute('SELECT id FROM scenes WHERE project_id=? ORDER BY scene_number,id', (project_id,))]
        result = []
        for scene_id in ids:
            scene = self._scene_details(scene_id)
            attempts = [dict(r) for r in self._conn.execute("SELECT * FROM tasks WHERE scene_id=? AND deleted_at IS NULL ORDER BY created_at DESC,batch_index DESC,id DESC", (scene_id,))]
            active = next((r for r in attempts if r['status']=='processing'),None) or next((r for r in attempts if r['status']=='queued'),None)
            counts = {key:sum(r['status']==key for r in attempts) for key in ('completed','processing','queued','failed','stopped','needs_recovery')}
            counts['total'] = len(attempts)
            revision = hashlib.sha256(json.dumps([[r['id'],r['status'],r['updated_at'],r['video_url'],r['error']] for r in attempts]).encode()).hexdigest()
            selected = next((r for r in attempts if r['id']==scene['selected_task_id'] and r['status']=='completed' and (r['video_url'] or '').strip()), None)
            latest = attempts[0] if attempts else None
            reason = 'ACTIVE_GENERATION_EXISTS' if active else ('ALREADY_SELECTED' if selected else (scene['readiness'] if not scene['ready'] else None))
            status = active['status'].upper() if active else ('SELECTED' if selected else (scene['readiness'] if not scene['ready'] else (latest['status'].upper() if latest else 'READY')))
            result.append({**scene, 'generation_status':status, 'latest_task':latest,
                           'active_task':active, 'selected_task':selected, 'skip_reason':reason,
                           'production_status':production_status(scene,attempts,active,selected),
                           'candidate_counts':counts,'candidate_revision':revision,
                           'completed_attempt_count':sum(row['status']=='completed' for row in attempts)})
        return result

    def project_generation_status(self, project_id):
        with self._lock, self._conn:
            self._conn.execute('BEGIN')
            return self._project_generation_status(project_id)

    def create_project_batch(self, project_id, plans, client, max_pending=0, candidates_per_scene=1, request_id=None):
        """One creation transaction; recheck active/selected before admitting attempts."""
        import uuid
        if type(candidates_per_scene) is not int or not 1 <= candidates_per_scene <= 5:
            raise ValueError('Candidates per Scene must be an integer from 1 to 5')
        plans = {p['scene_id']:p for p in plans}
        with self._lock, self._conn:
            self._conn.execute('BEGIN IMMEDIATE')
            scenes = self._project_generation_status(project_id)
            requested_batch = self.submission_batch_id('project',project_id,request_id,client['api_key_hash']) if request_id else None
            if requested_batch and self._conn.execute('SELECT 1 FROM tasks WHERE batch_id=?',(requested_batch,)).fetchone():
                raise TaskSubmissionConflict('This generation action has already been submitted')
            skipped, accepted = [], []
            for scene in scenes:
                reason = scene['skip_reason']
                plan = plans.get(scene['id'])
                if not reason and plan is None:
                    reason = 'NOT_IN_REQUEST'
                if reason:
                    skipped.append(dict(scene_id=scene['id'], scene_number=scene['scene_number'], reason=reason))
                    continue
                if scene['updated_at'] != plan['scene_updated_at']:
                    raise sqlite3.IntegrityError('Scene changed while preparing the batch; retry Generate All')
                snapshot_json = self._validate_reference_snapshot(scene['id'], plan['reference_snapshot'])
                for candidate in range(1,candidates_per_scene+1):
                    accepted.append({**plan, 'scene_number':scene['scene_number'], 'snapshot_json':snapshot_json,
                                     'task_id':'video_'+uuid.uuid4().hex,'candidate_index':candidate})
            count = len(accepted)
            if max_pending > 0:
                pending = self._conn.execute("SELECT count(*) FROM tasks WHERE status IN ('queued','processing')").fetchone()[0]
                if count and pending + count > max_pending:
                    raise PendingTaskLimitExceeded(f'Pending task queue has reached server limit ({max_pending})')
            daily_limit = client['daily_limit']
            if count and daily_limit > 0 and client['api_key_hash']:
                day = datetime.date.today().isoformat()
                used = self._conn.execute("SELECT count(*) FROM tasks WHERE api_key_hash=? AND date(created_at,'unixepoch','localtime')=?", (client['api_key_hash'],day)).fetchone()[0]
                if used + count > daily_limit:
                    raise TaskQuotaExceeded(f'API Key daily quota exceeded ({daily_limit} tasks)')
            now = time.time()
            batch_id = (requested_batch or 'batch_'+uuid.uuid4().hex) if count else None
            self._insert_task_rows([(
                p['task_id'],p['model'],p['prompt'],p['ratio'],p['duration'],None,now,now,
                json.dumps(p['reference_images'],ensure_ascii=False),client['api_key_hash'],client['api_key_name'],
                None,None,max(0,int(client['concurrency_limit'] or 0)),int(p['start_end']),
                batch_id,index,count,(p['name'] or '').strip() + (f" ({p['candidate_index']}/{candidates_per_scene})" if candidates_per_scene>1 and p['name'] else ''),p['scene_id'],p['snapshot_json']
            ) for index,p in enumerate(accepted,1)])
            result = dict(project_id=project_id, requested=len(scenes), created=count, skipped=len(skipped),
                          batch_id=batch_id, candidates_per_scene=candidates_per_scene, created_scenes=len({p['scene_id'] for p in accepted}), tasks=[dict(scene_id=p['scene_id'],scene_number=p['scene_number'],task_id=p['task_id']) for p in accepted], skipped_scenes=skipped)
            return result, accepted

    def update(self, task_id, **fields):
        if not fields:
            return
        if {"scene_id", "reference_snapshot"} & set(fields):
            raise ValueError("Task scene_id and reference_snapshot are immutable; set them when creating the task")
        fields["updated_at"] = time.time()
        cols = ", ".join(f"{k}=?" for k in fields)
        vals = list(fields.values()) + [task_id]
        with _LOCK, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            columns = {row[1] for row in self._conn.execute("PRAGMA table_info(tasks)")}
            if set(fields) - columns or "id" in fields:
                raise ValueError("Invalid task update fields")
            selected = self._conn.execute("SELECT 1 FROM scenes WHERE selected_task_id=?", (task_id,)).fetchone()
            if selected:
                row = self._require("tasks", task_id)
                merged = {**row, **fields}
                if merged["deleted_at"] is not None or merged["status"] != "completed" or not (merged["video_url"] or "").strip():
                    raise ValueError("Clear scene selection before invalidating its selected task")
            self._conn.execute(f"UPDATE tasks SET {cols} WHERE id=?", vals)

    def get(self, task_id):
        with _LOCK:
            row = self._conn.execute("SELECT * FROM tasks WHERE id=?", (task_id,)).fetchone()
        return dict(row) if row else None

    def get_for_client(self, task_id, api_key_hash: str | None):
        """Returns tasks belonging to current API Key; anonymous mode isolated by NULL hash."""
        with _LOCK:
            if api_key_hash:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE id=? AND api_key_hash=?",
                    (task_id, api_key_hash),
                ).fetchone()
            else:
                row = self._conn.execute(
                    "SELECT * FROM tasks WHERE id=? AND api_key_hash IS NULL",
                    (task_id,),
                ).fetchone()
        return dict(row) if row else None

    def recoverable_tasks(self) -> list:
        """Recovers tasks with existing conversation_id after restart without re-submitting prompt."""
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status IN ('queued','processing') "
                "AND conversation_id IS NOT NULL AND account IS NOT NULL "
                "ORDER BY created_at"
            ).fetchall()
        return [dict(r) for r in rows]

    def recoverable_queued_tasks(self) -> list:
        """Recovers queued tasks without conversation_id after restart."""
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE status='queued' "
                "AND conversation_id IS NULL ORDER BY created_at,batch_index,id"
            ).fetchall()
        return [dict(r) for r in rows]

    def fail_orphaned_processing_tasks(self) -> int:
        """Marks processing tasks without an assigned account/session as failed."""
        with _LOCK:
            cur = self._conn.execute(
                "UPDATE tasks SET status='failed', error=?, finished_at=?, updated_at=? "
                "WHERE status='processing' AND (account IS NULL OR conversation_id IS NULL)",
                ("Worker stopped before assigning an account/conversation", time.time(), time.time()),
            )
            self._conn.commit()
            return cur.rowcount

    def pending_task_count(self) -> int:
        with _LOCK:
            return self._conn.execute(
                "SELECT COUNT(*) FROM tasks WHERE status IN ('queued','processing')"
            ).fetchone()[0]

    def recent_tasks(self, limit: int = 50, api_key_hash: str | None = None,
                     task_id: str = "", query: str = "", search_in: str = "all",
                     status: str = "", account: str = "", duration: int = 0,
                     edit_selected: bool | None = None) -> list:
        fields = {"name": ["name"], "id": ["id"], "prompt": ["prompt"], "client": ["api_key_name"],
                  "batch": ["batch_id"], "error": ["error"]}
        clauses, params = [], []
        if api_key_hash:
            clauses.append("api_key_hash=?")
            params.append(api_key_hash)
        else:
            clauses.append("deleted_at IS NULL")
        if task_id.strip():
            clauses.append("instr(lower(id),lower(?))>0")
            params.append(task_id.strip())
        if query.strip():
            columns = fields.get(search_in, ["name", "id", "prompt", "api_key_name", "batch_id", "error", "account", "account_uuid", "status", "model"])
            clauses.append("(" + " OR ".join(f"instr(lower(COALESCE({column},'')),lower(?))>0" for column in columns) + ")")
            params.extend([query.strip()] * len(columns))
        if status:
            clauses.append("status=?")
            params.append(status)
        if account:
            clauses.append("(account_uuid=? OR account=?)")
            params.extend([account, account])
        if duration:
            clauses.append("duration=?")
            params.append(duration)
        if edit_selected is not None:
            clauses.append("COALESCE(edit_selected,0)=?")
            params.append(int(edit_selected))
        params.append(limit)
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM tasks WHERE " + " AND ".join(clauses) +
                " ORDER BY created_at DESC, id DESC LIMIT ?", params).fetchall()
        return [dict(r) for r in rows]

    def delete_task(self, task_id: str) -> bool:
        """Hide a finished record while preserving usage accounting and media."""
        with _LOCK, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            if self._conn.execute("SELECT 1 FROM scenes WHERE selected_task_id=?", (task_id,)).fetchone():
                raise ValueError("Clear scene selection before deleting its selected task")
            cur = self._conn.execute(
                "UPDATE tasks SET deleted_at=?, updated_at=? WHERE id=? "
                "AND deleted_at IS NULL AND status IN ('completed','failed','stopped','needs_recovery')",
                (time.time(), time.time(), task_id),
            )
            self._conn.commit()
            return cur.rowcount == 1

    def key_usage(self, api_key_hash: str, day: str | None = None) -> dict:
        day = day or datetime.date.today().isoformat()
        with _LOCK:
            row = self._conn.execute(
                "SELECT COUNT(*) AS total, "
                "SUM(status='completed') AS completed, "
                "SUM(status='failed') AS failed, "
                "SUM(status='processing') AS active, "
                "SUM(status='queued') AS queued "
                "FROM tasks WHERE api_key_hash=? "
                "AND date(created_at,'unixepoch','localtime')=?",
                (api_key_hash, day),
            ).fetchone()
        return {
            "day": day,
            "total": row["total"] or 0,
            "completed": row["completed"] or 0,
            "failed": row["failed"] or 0,
            "active": row["active"] or 0,
            "queued": row["queued"] or 0,
        }

    def bind_account_uuids(self, accounts):
        """Pin historical task ownership once, including hidden/deleted task records."""
        with _LOCK, self._conn:
            for account in accounts:
                self._conn.execute(
                    "UPDATE tasks SET account_uuid=? WHERE account_uuid IS NULL AND account IN (?, ?)",
                    (account["uuid"], account["name"], account["uuid"]),
                )

    def stats(self) -> dict:
        """Daily completed/failed stats, success rate, 7-day trend, and total generated per account."""
        days = [(datetime.date.today() - datetime.timedelta(days=i)).isoformat()
                for i in range(6, -1, -1)]
        with _LOCK:
            per_day = []
            for d in days:
                row = self._conn.execute(
                    "SELECT sum(status='completed'), sum(status='failed') FROM tasks "
                    "WHERE date(created_at,'unixepoch','localtime')=?", (d,),
                ).fetchone()
                per_day.append({"day": d[5:], "completed": row[0] or 0, "failed": row[1] or 0})
            t = per_day[-1]
            per_account = self._conn.execute(
                "SELECT account_uuid, sum(status='completed') FROM tasks "
                "WHERE account_uuid IS NOT NULL "
                "AND id NOT IN (SELECT task_id FROM generated_reset_tasks) GROUP BY account_uuid"
            ).fetchall()
        completed, failed = t["completed"], t["failed"]
        total = completed + failed
        return {
            "today_completed": completed,
            "today_failed": failed,
            "success_rate": round(completed / total, 3) if total else None,
            "per_day": per_day,
            "per_account_total": {r[0]: r[1] for r in per_account},
        }

    # ===== api keys =====

    def list_keys(self) -> list:
        with _LOCK:
            rows = self._conn.execute(
                "SELECT * FROM api_keys ORDER BY created_at DESC"
            ).fetchall()
        return [self._key_row(r) for r in rows]

    def get_key(self, key: str) -> dict | None:
        with _LOCK:
            row = self._conn.execute(
                "SELECT * FROM api_keys WHERE key=?", (key,)
            ).fetchone()
        return self._key_row(row)

    def create_key(
        self,
        name: str,
        daily_limit: int = 0,
        concurrency_limit: int = 0,
        allowed_durations: list[int] | None = None,
        expires_at: float | None = None,
    ) -> dict:
        key = "sk-" + secrets.token_hex(16)
        now = time.time()
        allowed = self._parse_allowed_durations(allowed_durations or DEFAULT_ALLOWED_DURATIONS)
        expires = float(expires_at or 0)
        with _LOCK:
            self._conn.execute(
                "INSERT INTO api_keys (key,name,enabled,created_at,last_used_at,"
                "daily_limit,concurrency_limit,allowed_durations,expires_at) "
                "VALUES (?,?,1,?,0,?,?,?,?)",
                (
                    key,
                    name or "",
                    now,
                    max(0, int(daily_limit or 0)),
                    max(0, int(concurrency_limit or 0)),
                    json.dumps(allowed, ensure_ascii=False),
                    expires,
                ),
            )
            self._conn.commit()
        data = self.get_key(key)
        data["last_used_at"] = 0
        data["key"] = key
        return data

    def update_key(self, key: str, **fields):
        if not fields:
            return
        fields = dict(fields)
        if "allowed_durations" in fields:
            fields["allowed_durations"] = json.dumps(
                self._parse_allowed_durations(fields["allowed_durations"]),
                ensure_ascii=False,
            )
        cols = ", ".join(f"{k}=?" for k in fields)
        with _LOCK:
            self._conn.execute(
                f"UPDATE api_keys SET {cols} WHERE key=?",
                list(fields.values()) + [key],
            )
            self._conn.commit()

    def delete_key(self, key: str):
        with _LOCK:
            self._conn.execute("DELETE FROM api_keys WHERE key=?", (key,))
            self._conn.commit()

    def has_enabled_keys(self) -> bool:
        with _LOCK:
            row = self._conn.execute(
                "SELECT 1 FROM api_keys WHERE enabled=1 LIMIT 1"
            ).fetchone()
        return bool(row)

    def is_key_valid(self, key: str) -> bool:
        with _LOCK:
            row = self._conn.execute(
                "SELECT enabled, expires_at FROM api_keys WHERE key=?", (key,)
            ).fetchone()
        if not row or not row["enabled"]:
            return False
        return not row["expires_at"] or row["expires_at"] > time.time()

    def touch_key(self, key: str, min_interval: float = 60):
        """Throttled update of last_used_at."""
        now = time.time()
        with _LOCK:
            row = self._conn.execute(
                "SELECT last_used_at FROM api_keys WHERE key=?", (key,)
            ).fetchone()
            if row and now - (row[0] or 0) >= min_interval:
                self._conn.execute(
                    "UPDATE api_keys SET last_used_at=? WHERE key=?", (now, key)
                )
                self._conn.commit()
