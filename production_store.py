"""Film production metadata on the TaskStore connection; no generation execution."""
from contextlib import closing
from pathlib import Path
import json
import os
import sqlite3
import tempfile
import time
import uuid

from asset_storage import validate_asset_path
from reference_aliases import resolve_reference_aliases
from scene_workflow import SceneWorkflowMixin
from review_store import ReviewStoreMixin
from scene_info import SceneInfoMixin
from scene_import import asset_name_key, normalize_reference_alias, validate_aliases, SceneNotReadyError

SCHEMA_VERSION = 9
ASSET_TYPES = {"character", "environment", "prop", "special_state"}


class RecordNotFoundError(ValueError):
    pass


class AssetInUseError(ValueError):
    pass


class ProductionStoreMixin(SceneWorkflowMixin, ReviewStoreMixin, SceneInfoMixin):
    def _migrate_v7(self):
        columns = {row[1] for row in self._conn.execute('PRAGMA table_info(projects)')}
        if 'parent_project_id' not in columns:
            self._conn.execute('ALTER TABLE projects ADD COLUMN parent_project_id TEXT REFERENCES projects(id) ON DELETE RESTRICT')
        self._conn.execute('CREATE INDEX IF NOT EXISTS projects_parent ON projects(parent_project_id)')
        self._conn.execute('PRAGMA user_version=7')
        self._migrate_v8()

    def _migrate_v8(self):
        self._conn.execute("""CREATE TABLE IF NOT EXISTS project_account_allowlist (
            project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE CASCADE,
            account_uuid TEXT NOT NULL, PRIMARY KEY(project_id,account_uuid))""")
        self._conn.execute('PRAGMA user_version=8')
        self._conn.execute('CREATE TABLE IF NOT EXISTS project_account_overrides (project_id TEXT PRIMARY KEY REFERENCES projects(id) ON DELETE CASCADE)')
        self._conn.execute('PRAGMA user_version=9')

    def _account_owner(self, project_id):
        project=self._require('projects',project_id)
        override=self._conn.execute('SELECT 1 FROM project_account_overrides WHERE project_id=?',(project_id,)).fetchone()
        return project_id if override or not project.get('parent_project_id') else project['parent_project_id']

    def project_accounts(self, project_id):
        with self._lock:
            owner=self._account_owner(project_id)
            ids=[row[0] for row in self._conn.execute(
                'SELECT account_uuid FROM project_account_allowlist WHERE project_id=? ORDER BY account_uuid',(owner,))]
            return {'project_id':owner, 'account_uuids':ids, 'all_accounts':not ids, 'inherited':owner!=project_id}

    def set_project_accounts(self, project_id, account_uuids, inherit=False):
        if not isinstance(account_uuids,list) or any(not isinstance(x,str) or not x.strip() for x in account_uuids):
            raise ValueError('Expected a list of account UUIDs')
        ids=sorted(set(x.strip() for x in account_uuids))
        with self._lock, self._conn:
            project=self._require('projects',project_id)
            owner=project_id
            if inherit:
                if not project.get('parent_project_id'):
                    raise ValueError('Only chapters can inherit account settings')
                self._conn.execute('DELETE FROM project_account_overrides WHERE project_id=?',(project_id,))
                ids=[]
            elif project.get('parent_project_id'):
                self._conn.execute('INSERT OR IGNORE INTO project_account_overrides VALUES (?)',(project_id,))
            self._conn.execute('DELETE FROM project_account_allowlist WHERE project_id=?',(owner,))
            self._conn.executemany('INSERT INTO project_account_allowlist VALUES (?,?)',[(owner,x) for x in ids])
        return self.project_accounts(project_id)

    def task_allowed_accounts(self, row):
        if not row.get('scene_id'):
            return None
        with self._lock:
            scene=self._require('scenes',row['scene_id'])
            owner=self._account_owner(scene['project_id'])
            ids={r[0] for r in self._conn.execute(
                'SELECT account_uuid FROM project_account_allowlist WHERE project_id=?',(owner,))}
            return ids or None

    def _library_project_id(self, project_id):
        project = self._require('projects', project_id)
        return project.get('parent_project_id') or project_id

    def library_project_id(self, project_id):
        with self._lock:
            return self._library_project_id(project_id)

    def create_chapter(self, project_id, name):
        return self.create_project(name, parent_project_id=project_id)

    def list_chapters(self, project_id):
        with self._lock:
            self._require('projects', project_id)
            return [dict(row) for row in self._conn.execute(
                'SELECT * FROM projects WHERE parent_project_id=? ORDER BY created_at,id', (project_id,))]

    def _backup_before_migration(self):
        version = self._conn.execute("PRAGMA user_version").fetchone()[0]
        if version > SCHEMA_VERSION:
            raise RuntimeError(f"Unsupported database schema version: {version}")
        task_columns = {row[1] for row in self._conn.execute('PRAGMA table_info(tasks)')}
        queue_upgrade = bool(task_columns and 'phase' not in task_columns)
        if version == SCHEMA_VERSION and not queue_upgrade:
            return
        self._check_asset_name_collisions()
        filename = self._conn.execute("PRAGMA database_list").fetchone()[2]
        if not filename:  # In-memory tests have no persistent database to back up.
            return
        suffix = '.before_queue.bak' if version == SCHEMA_VERSION and queue_upgrade else f".before_v{2 if version < 2 else version + 1}.bak"
        backup = Path(filename + suffix)
        if backup.exists():
            return  # Keep the original pre-migration snapshot on retries.
        descriptor, temporary_name = tempfile.mkstemp(prefix=backup.name + ".", dir=backup.parent)
        os.close(descriptor)
        temporary = Path(temporary_name)
        try:
            with closing(sqlite3.connect(temporary)) as target:
                self._conn.backup(target)
            # Publish only a complete backup, atomically, without overwriting an
            # original snapshot if another initializer published one first.
            try:
                os.link(temporary, backup)
            except FileExistsError:
                pass
        finally:
            temporary.unlink(missing_ok=True)

    def _migrate_v2(self):
        if self._conn.execute("PRAGMA user_version").fetchone()[0] >= 2:
            self._migrate_v3()
            return
        statements = [
            """CREATE TABLE IF NOT EXISTS projects (
                id TEXT PRIMARY KEY NOT NULL, name TEXT NOT NULL,
                created_at REAL NOT NULL, updated_at REAL NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS scenes (
                id TEXT PRIMARY KEY NOT NULL,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                scene_number INTEGER NOT NULL CHECK(scene_number >= 1),
                scene_name TEXT NOT NULL DEFAULT '', prompt TEXT NOT NULL DEFAULT '',
                model TEXT NOT NULL, ratio TEXT NOT NULL,
                duration INTEGER NOT NULL CHECK(duration IN (10,15,30)),
                start_end INTEGER NOT NULL DEFAULT 0 CHECK(start_end IN (0,1)),
                selected_task_id TEXT REFERENCES tasks(id) ON DELETE RESTRICT,
                created_at REAL NOT NULL, updated_at REAL NOT NULL,
                UNIQUE(project_id, scene_number))""",
            """CREATE TABLE IF NOT EXISTS assets (
                id TEXT PRIMARY KEY NOT NULL,
                project_id TEXT NOT NULL REFERENCES projects(id) ON DELETE RESTRICT,
                name TEXT NOT NULL,
                type TEXT NOT NULL CHECK(type IN ('character','environment','prop','special_state')),
                file_path TEXT NOT NULL, created_at REAL NOT NULL)""",
            """CREATE TABLE IF NOT EXISTS scene_assets (
                scene_id TEXT NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
                asset_id TEXT NOT NULL REFERENCES assets(id) ON DELETE RESTRICT,
                position INTEGER NOT NULL CHECK(position >= 1),
                reference_alias TEXT NOT NULL COLLATE NOCASE CHECK(length(trim(reference_alias)) > 0),
                PRIMARY KEY(scene_id, asset_id),
                UNIQUE(scene_id, position), UNIQUE(scene_id, reference_alias))""",
        ]
        for statement in statements:
            self._conn.execute(statement)
        columns = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)")}
        if "scene_id" not in columns:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN scene_id TEXT REFERENCES scenes(id) ON DELETE RESTRICT")
        foreign_keys = self._conn.execute("PRAGMA foreign_key_list(tasks)").fetchall()
        if not any(r[2] == "scenes" and r[3] == "scene_id" and r[4] == "id" and r[6] == "RESTRICT" for r in foreign_keys):
            raise sqlite3.IntegrityError("Existing tasks.scene_id is missing the required scenes foreign key; migration will not rebuild tasks")
        for statement in (
            "CREATE INDEX IF NOT EXISTS tasks_scene_created ON tasks(scene_id, created_at, id)",
            "CREATE INDEX IF NOT EXISTS assets_project ON assets(project_id)",
            "CREATE INDEX IF NOT EXISTS scene_assets_asset ON scene_assets(asset_id)",
            "CREATE INDEX IF NOT EXISTS scenes_selected_task ON scenes(selected_task_id)",
        ):
            self._conn.execute(statement)
        violations = self._conn.execute("PRAGMA foreign_key_check").fetchall()
        if violations:
            raise sqlite3.IntegrityError("V2 migration failed foreign_key_check")
        self._conn.execute("PRAGMA user_version=2")
        self._migrate_v3()

    def _migrate_v3(self):
        if self._conn.execute("PRAGMA user_version").fetchone()[0] >= 3:
            self._migrate_v4()
            return
        columns = {r[1] for r in self._conn.execute("PRAGMA table_info(tasks)")}
        if "reference_snapshot" not in columns:
            self._conn.execute("ALTER TABLE tasks ADD COLUMN reference_snapshot TEXT")
        if self._conn.execute("PRAGMA foreign_key_check").fetchall():
            raise sqlite3.IntegrityError("Reference snapshot migration failed foreign_key_check")
        self._conn.execute("PRAGMA user_version=3")
        self._migrate_v4()

    def _migrate_v5(self):
        if self._conn.execute('PRAGMA user_version').fetchone()[0] >= 5:
            self._migrate_v6()
            return
        columns = {row[1] for row in self._conn.execute('PRAGMA table_info(assets)')}
        if 'retired_at' not in columns:
            self._conn.execute('ALTER TABLE assets ADD COLUMN retired_at REAL')
        self._conn.execute('DROP INDEX IF EXISTS assets_project_name_nocase')
        self._conn.execute('CREATE UNIQUE INDEX assets_project_name_nocase ON assets(project_id,name COLLATE NOCASE) WHERE retired_at IS NULL')
        if self._conn.execute('PRAGMA foreign_key_check').fetchall():
            raise sqlite3.IntegrityError('Asset retirement migration failed foreign_key_check')
        self._conn.execute('PRAGMA user_version=5')
        self._migrate_v6()

    def asset_has_history(self, asset_id):
        with self._lock:
            for row in self._conn.execute('SELECT reference_snapshot FROM tasks WHERE reference_snapshot IS NOT NULL'):
                if any(ref['asset_id'] == asset_id for ref in json.loads(row[0])):
                    return True
            return False

    def _retire_asset_bindings(self, old, replacement_id=None):
        if old['retired_at'] is not None:
            raise AssetInUseError('Asset has already been removed or replaced; refresh the library')
        scenes = {row[0] for row in self._conn.execute('SELECT scene_id FROM scene_reference_requirements WHERE asset_id=?', (old['id'],))}
        scenes.update(row[0] for row in self._conn.execute('SELECT scene_id FROM scene_assets WHERE asset_id=?', (old['id'],)))
        now = time.time()
        self._conn.execute('UPDATE assets SET retired_at=? WHERE id=?', (now,old['id']))
        self._conn.execute('UPDATE scene_reference_requirements SET asset_id=? WHERE asset_id=?', (replacement_id,old['id']))
        if replacement_id:
            self._conn.execute('UPDATE scene_assets SET asset_id=? WHERE asset_id=?', (replacement_id,old['id']))
        else:
            self._conn.execute('DELETE FROM scene_assets WHERE asset_id=?', (old['id'],))
        self._conn.executemany('UPDATE scenes SET updated_at=? WHERE id=?', [(now,scene) for scene in scenes])
        return len(scenes)

    def replace_asset(self, asset_id, new_id, file_path):
        with self._lock, self._conn:
            self._conn.execute('BEGIN IMMEDIATE')
            old = self._require('assets',asset_id)
            if old['retired_at'] is not None:
                raise AssetInUseError('Asset has already been removed or replaced; refresh the library')
            validate_asset_path(old['project_id'],new_id,file_path)
            # Free the current name and insert the new backing identity in this transaction.
            self._conn.execute('UPDATE assets SET retired_at=? WHERE id=?',(time.time(),asset_id))
            self._conn.execute('INSERT INTO assets(id,project_id,name,type,file_path,created_at) VALUES (?,?,?,?,?,?)',
                (new_id,old['project_id'],old['name'],old['type'],file_path,time.time()))
            # The helper checks the captured pre-retirement row, not mutable metadata.
            count = self._retire_asset_bindings(old,new_id)
            return dict(asset=self._require('assets',new_id),retired_asset_id=asset_id,affected_scenes=count)

    def remove_current_asset(self, asset_id):
        with self._lock, self._conn:
            self._conn.execute('BEGIN IMMEDIATE')
            old = self._require('assets',asset_id)
            count = self._retire_asset_bindings(old)
            return dict(retired_asset_id=asset_id,affected_scenes=count)

    def _require(self, table, identifier):
        row = self._conn.execute(f"SELECT * FROM {table} WHERE id=?", (identifier,)).fetchone()
        if row is None:
            raise RecordNotFoundError(f"{table} record not found: {identifier}")
        return dict(row)

    @staticmethod
    def _text(value, field, allow_empty=False):
        if not isinstance(value, str) or (not allow_empty and not value.strip()):
            raise ValueError(f"{field} must be a {'string' if allow_empty else 'non-empty string'}")
        return value

    @staticmethod
    def _positive_int(value, field):
        if type(value) is not int or value < 1:
            raise ValueError(f"{field} must be a positive integer")
        return value

    @classmethod
    def _scene_fields(cls, fields):
        allowed = {"scene_number", "scene_name", "prompt", "model", "ratio", "duration", "start_end"}
        if set(fields) - allowed:
            raise ValueError("Only editable scene configuration can be updated")
        fields = dict(fields)
        for key in ("scene_name", "prompt", "model", "ratio"):
            if key in fields:
                cls._text(fields[key], key, allow_empty=key in {"scene_name", "prompt"})
        if "scene_number" in fields:
            cls._positive_int(fields["scene_number"], "scene_number")
        if "duration" in fields and (type(fields["duration"]) is not int or fields["duration"] not in (10, 15, 30)):
            raise ValueError("duration must be 10, 15, or 30")
        if "start_end" in fields:
            if type(fields["start_end"]) not in (int, bool) or fields["start_end"] not in (0, 1):
                raise ValueError("start_end must be a boolean")
            fields["start_end"] = int(fields["start_end"])
        return fields

    def create_project(self, name, *, project_id=None, parent_project_id=None):
        name = self._text(name, "name").strip()
        identifier = project_id or uuid.uuid4().hex
        self._text(identifier, "project_id")
        now = time.time()
        with self._lock, self._conn:
            if parent_project_id:
                parent = self._require('projects', parent_project_id)
                if parent.get('parent_project_id'):
                    raise ValueError('Chapters must belong directly to a project')
                self._conn.execute("INSERT INTO projects(id,name,created_at,updated_at,parent_project_id) VALUES (?,?,?,?,?)",
                                   (identifier, name, now, now, parent_project_id))
            else:
                self._conn.execute("INSERT INTO projects(id,name,created_at,updated_at) VALUES (?,?,?,?)",
                                   (identifier, name, now, now))
            return self._require("projects", identifier)

    def get_project(self, project_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM projects WHERE id=?", (project_id,)).fetchone()
            return dict(row) if row else None

    def list_projects(self):
        with self._lock:
            return [dict(r) for r in self._conn.execute("SELECT * FROM projects ORDER BY created_at, id")]

    def update_project(self, project_id, *, name):
        name = self._text(name, "name").strip()
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            self._require("projects", project_id)
            self._conn.execute("UPDATE projects SET name=?, updated_at=? WHERE id=?", (name, time.time(), project_id))
            return self._require("projects", project_id)

    def create_scene(self, project_id, scene_number, scene_name="", prompt="", model="seedance-2.0",
                     ratio="16:9", duration=10, start_end=False, *, scene_id=None):
        fields = self._scene_fields(dict(scene_number=scene_number, scene_name=scene_name, prompt=prompt,
                                         model=model, ratio=ratio, duration=duration, start_end=start_end))
        identifier = scene_id or uuid.uuid4().hex
        self._text(identifier, "scene_id")
        now = time.time()
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            self._require("projects", project_id)
            self._conn.execute("""INSERT INTO scenes
                (id,project_id,scene_number,scene_name,prompt,model,ratio,duration,start_end,created_at,updated_at)
                VALUES (?,?,?,?,?,?,?,?,?,?,?)""",
                (identifier, project_id, fields["scene_number"], fields["scene_name"], fields["prompt"],
                 fields["model"], fields["ratio"], fields["duration"], fields["start_end"], now, now))
            return self._require("scenes", identifier)

    def get_scene(self, scene_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM scenes WHERE id=?", (scene_id,)).fetchone()
            return dict(row) if row else None

    def list_project_scenes(self, project_id):
        with self._lock:
            self._require("projects", project_id)
            return [dict(r) for r in self._conn.execute("SELECT * FROM scenes WHERE project_id=? ORDER BY scene_number, id", (project_id,))]

    def update_scene(self, scene_id, **fields):
        fields = self._scene_fields(fields)
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            self._require("scenes", scene_id)
            if fields:
                fields["updated_at"] = time.time()
                columns = ", ".join(f"{key}=?" for key in fields)
                self._conn.execute(f"UPDATE scenes SET {columns} WHERE id=?", (*fields.values(), scene_id))
            return self._require("scenes", scene_id)

    def create_asset(self, project_id, name, asset_type, file_path, *, asset_id=None):
        """Register immutable metadata; caller persists the image separately (no upload here)."""
        name = self._text(name, "name").strip()
        if asset_type not in ASSET_TYPES:
            raise ValueError("Unsupported asset type")
        self._text(file_path, "file_path")
        identifier = asset_id or Path(file_path).stem
        project_id = self.library_project_id(project_id)
        file_path = validate_asset_path(project_id, identifier, file_path)
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            self._require("projects", project_id)
            existing_names = self._conn.execute("SELECT name FROM assets WHERE project_id=? AND retired_at IS NULL", (project_id,)).fetchall()
            if any(asset_name_key(row[0]) == asset_name_key(name) for row in existing_names):
                raise sqlite3.IntegrityError("Asset name already exists in this project (case-insensitive)")
            self._conn.execute("INSERT INTO assets (id,project_id,name,type,file_path,created_at) VALUES (?,?,?,?,?,?)",
                               (identifier, project_id, name, asset_type, file_path, time.time()))
            return self._require("assets", identifier)

    def get_asset(self, asset_id):
        with self._lock:
            row = self._conn.execute("SELECT * FROM assets WHERE id=?", (asset_id,)).fetchone()
            return dict(row) if row else None

    def list_project_assets(self, project_id):
        project_id = self.library_project_id(project_id)
        with self._lock:
            self._require("projects", project_id)
            return [dict(r) for r in self._conn.execute("SELECT * FROM assets WHERE project_id=? AND retired_at IS NULL ORDER BY created_at, id", (project_id,))]

    def _ordered_scene_assets(self, scene_id):
        return [dict(r) for r in self._conn.execute("""SELECT sa.scene_id, sa.asset_id, sa.position,
            sa.reference_alias, a.project_id, a.name, a.type, a.file_path, a.created_at
            FROM scene_assets sa JOIN assets a ON a.id=sa.asset_id
            WHERE sa.scene_id=? ORDER BY sa.position""", (scene_id,))]

    @staticmethod
    def _normalize_alias(reference_alias):
        return normalize_reference_alias(reference_alias)

    def attach_asset_to_scene(self, scene_id, asset_id, position, reference_alias):
        self._positive_int(position, "position")
        alias = self._normalize_alias(reference_alias)
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            scene = self._require("scenes", scene_id)
            asset = self._require("assets", asset_id)
            if asset.get("retired_at") is not None:
                raise AssetInUseError("Asset has been retired from the current project")
            if asset["project_id"] != self._library_project_id(scene["project_id"]):
                raise ValueError("Asset and scene must belong to the same project")
            requirements = self._ordered_scene_requirements(scene_id)
            if any(ref["asset_id"] == asset_id for ref in requirements):
                raise sqlite3.IntegrityError("Asset is already attached to this scene")
            slot = next((ref for ref in requirements if ref["position"] == position), None)
            if slot and slot["asset_id"] is not None:
                raise sqlite3.IntegrityError("Reference position is already occupied")
            if slot and asset_name_key(slot["name"]) != asset_name_key(asset["name"]):
                raise ValueError("Asset name does not match the missing requirement at this position")
            if any(ref["position"] != position and asset_name_key(ref["name"]) == asset_name_key(asset["name"]) for ref in requirements):
                raise ValueError("This asset is already required at another position")
            validate_aliases([ref["alias"] for ref in requirements if ref["position"] != position] + [alias])
            if slot:
                self._conn.execute("UPDATE scene_reference_requirements SET asset_id=?,reference_alias=? WHERE scene_id=? AND position=?", (asset_id,alias,scene_id,position))
            else:
                self._conn.execute("INSERT INTO scene_reference_requirements VALUES (?,?,?,?,?)", (scene_id,position,asset["name"],alias,asset_id))
            self._conn.execute("INSERT INTO scene_assets VALUES (?,?,?,?)", (scene_id, asset_id, position, alias))
            self._conn.execute("UPDATE scenes SET updated_at=? WHERE id=?", (time.time(), scene_id))
            return self._ordered_scene_assets(scene_id)

    def detach_asset_from_scene(self, scene_id, asset_id):
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            self._require("scenes", scene_id)
            cur = self._conn.execute("DELETE FROM scene_assets WHERE scene_id=? AND asset_id=?", (scene_id, asset_id))
            if cur.rowcount:
                # Explicit detach removes the requirement as well as its resolved link.
                self._conn.execute("DELETE FROM scene_reference_requirements WHERE scene_id=? AND asset_id=?", (scene_id,asset_id))
                self._conn.execute("UPDATE scenes SET updated_at=? WHERE id=?", (time.time(), scene_id))
            return cur.rowcount == 1

    def list_scene_assets(self, scene_id):
        with self._lock:
            self._require("scenes", scene_id)
            return self._ordered_scene_assets(scene_id)

    def reorder_scene_assets(self, scene_id, asset_ids):
        """asset_ids must be the complete attachment list in its new order."""
        asset_ids = list(asset_ids)
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            self._require("scenes", scene_id)
            if any(ref["asset_id"] is None for ref in self._ordered_scene_requirements(scene_id)):
                raise ValueError("Resolve missing references before reordering by asset IDs")
            existing = self._ordered_scene_assets(scene_id)
            if len(set(asset_ids)) != len(asset_ids) or set(asset_ids) != {r["asset_id"] for r in existing}:
                raise ValueError("Reorder must contain each attached asset exactly once")
            offset = max((r["position"] for r in existing), default=0) + len(existing) + 1
            # Move outside the existing range before assigning final positions.
            self._conn.execute("UPDATE scene_assets SET position=position+? WHERE scene_id=?", (offset, scene_id))
            self._conn.execute("UPDATE scene_reference_requirements SET position=position+? WHERE scene_id=?", (offset,scene_id))
            self._conn.executemany("UPDATE scene_assets SET position=? WHERE scene_id=? AND asset_id=?",
                                   [(index, scene_id, asset_id) for index, asset_id in enumerate(asset_ids, 1)])
            self._conn.executemany("UPDATE scene_reference_requirements SET position=? WHERE scene_id=? AND asset_id=?",
                                   [(index, scene_id, asset_id) for index, asset_id in enumerate(asset_ids, 1)])
            self._conn.execute("UPDATE scenes SET updated_at=? WHERE id=?", (time.time(), scene_id))
            return self._ordered_scene_assets(scene_id)

    def select_scene_task(self, scene_id, task_id):
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            self._require("scenes", scene_id)
            task = self._require("tasks", task_id)
            if task["scene_id"] != scene_id:
                raise ValueError("Selected task must belong to this scene")
            if task["status"] != "completed" or task["deleted_at"] is not None or not (task["video_url"] or "").strip():
                raise ValueError("Selected task must be completed, not deleted, and have a video_url")
            self._conn.execute("UPDATE scenes SET selected_task_id=?, updated_at=? WHERE id=?", (task_id, time.time(), scene_id))
            return self._require("scenes", scene_id)

    def clear_scene_selection(self, scene_id):
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            self._require("scenes", scene_id)
            self._conn.execute("UPDATE scenes SET selected_task_id=NULL, updated_at=? WHERE id=?", (time.time(), scene_id))
            return self._require("scenes", scene_id)

    def update_scene_asset_alias(self, scene_id, asset_id, reference_alias):
        alias = self._normalize_alias(reference_alias)
        with self._lock, self._conn:
            self._conn.execute("BEGIN IMMEDIATE")
            self._require("scenes", scene_id)
            references = self._ordered_scene_requirements(scene_id)
            if asset_id not in {r["asset_id"] for r in references}:
                raise RecordNotFoundError("Asset is not attached to this scene")
            aliases = [alias if r["asset_id"] == asset_id else r["alias"] for r in references]
            validate_aliases(aliases)
            self._conn.execute("UPDATE scene_assets SET reference_alias=? WHERE scene_id=? AND asset_id=?",
                               (alias, scene_id, asset_id))
            self._conn.execute("UPDATE scene_reference_requirements SET reference_alias=? WHERE scene_id=? AND asset_id=?", (alias,scene_id,asset_id))
            self._conn.execute("UPDATE scenes SET updated_at=? WHERE id=?", (time.time(), scene_id))
            return self._ordered_scene_assets(scene_id)

    def scene_generation_input(self, scene_id):
        """Capture editable configuration and ordered references from one read transaction."""
        with self._lock, self._conn:
            self._conn.execute("BEGIN")
            scene = self._scene_details(scene_id)
            if not scene["ready"]:
                raise SceneNotReadyError(scene)
            scene["project_name"] = self._require("projects", scene["project_id"])["name"]
            return scene, self._ordered_scene_assets(scene_id)

    def _validate_reference_snapshot(self, scene_id, references):
        if scene_id is None or not isinstance(references, list):
            raise ValueError("Reference snapshot requires a scene and an ordered list")
        scene = self._require("scenes", scene_id)
        result = []
        seen = set()
        previous_position = 0
        for ref in references:
            if not isinstance(ref, dict) or set(ref) != {"asset_id", "reference_alias", "position", "file_path"}:
                raise ValueError("Invalid reference snapshot entry")
            asset = self._require("assets", ref["asset_id"])
            position = self._positive_int(ref["position"], "position")
            if asset.get("retired_at") is not None:
                raise ValueError("New attempts cannot use a retired asset")
            if asset["project_id"] != self._library_project_id(scene["project_id"]) or asset["file_path"] != ref["file_path"]:
                raise ValueError("Snapshot asset must belong to the scene project and match its immutable path")
            if position <= previous_position or asset["id"] in seen:
                raise ValueError("Snapshot must have unique assets in position order")
            result.append({"asset_id": asset["id"], "reference_alias": self._normalize_alias(ref["reference_alias"]),
                           "position": position, "file_path": asset["file_path"]})
            seen.add(asset["id"])
            previous_position = position
        resolve_reference_aliases("", [r["reference_alias"] for r in result], len(result))
        return json.dumps(result, ensure_ascii=False)
