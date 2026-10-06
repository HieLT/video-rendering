"""Persistent image files and metadata with rollback for ordinary IO/DB failures."""
from pathlib import Path
import json
import sqlite3
import re
import uuid

from asset_storage import asset_root, asset_relative_path, resolve_asset_path, SUPPORTED_ASSET_EXTENSIONS
from media import validate_reference_image
from production_store import ASSET_TYPES, AssetInUseError, RecordNotFoundError


class AssetLibrary:
    def __init__(self, store):
        self.store = store

    def upload(self, project_id, name, asset_type, filename, data):
        if self.store.get_project(project_id) is None:
            raise RecordNotFoundError("Project not found")
        self.store._text(name, "name")
        if asset_type not in ASSET_TYPES:
            raise ValueError("Unsupported asset type")
        if not filename or Path(filename).suffix.lower() not in SUPPORTED_ASSET_EXTENSIONS:
            raise ValueError("Asset filename must use .jpg, .jpeg, .png, or .webp")
        return self._persist(project_id, filename, data, lambda identifier,relative: self.store.create_asset(project_id, name, asset_type, relative, asset_id=identifier))

    def _persist(self, project_id, filename, data, register):
        if not filename or Path(filename).suffix.lower() not in SUPPORTED_ASSET_EXTENSIONS:
            raise ValueError("Asset filename must use .jpg, .jpeg, .png, or .webp")
        suffix = validate_reference_image(data)
        identifier = uuid.uuid4().hex
        relative = asset_relative_path(project_id, identifier, suffix)
        path = resolve_asset_path(relative)
        path.parent.mkdir(parents=True, exist_ok=True)
        owns_file = False
        try:
            with path.open("xb") as image:
                owns_file = True
                image.write(data)
            return register(identifier, relative)
        except BaseException:
            if owns_file:
                path.unlink(missing_ok=True)
            raise

    def _cleanup_retired(self, result):
        identifier = result['retired_asset_id']
        result['historical_backing_retained'] = True
        try:
            if not self.store.asset_has_history(identifier):
                self.delete(identifier)  # Existing guarded, staged physical deletion.
                result['historical_backing_retained'] = False
        except (OSError, ValueError, sqlite3.DatabaseError):
            # Current bindings already committed; leave the retired backing intact.
            result['cleanup_warning'] = 'Current reference updated; unused retired backing was retained for safe cleanup'
        return result

    def replace(self, asset_id, filename, data):
        old = self.store.get_asset(asset_id)
        if old is None:
            raise RecordNotFoundError('Asset not found')
        result = self._persist(old['project_id'],filename,data,
            lambda identifier,relative: self.store.replace_asset(asset_id,identifier,relative))
        return self._cleanup_retired(result)

    def remove(self, asset_id):
        return self._cleanup_retired(self.store.remove_current_asset(asset_id))

    def delete(self, asset_id):
        """Stage a file, commit metadata deletion, then remove it; restore on failure."""
        store = self.store
        staged = None
        original = None
        row = None
        committed = False
        with store._lock:
            try:
                with store._conn:
                    store._conn.execute("BEGIN IMMEDIATE")
                    row = store._require("assets", asset_id)
                    if store._conn.execute("SELECT 1 FROM scene_assets WHERE asset_id=?", (asset_id,)).fetchone():
                        raise AssetInUseError("Asset is attached to a scene; detach it before deletion")
                    for task in store._conn.execute("SELECT reference_snapshot FROM tasks WHERE reference_snapshot IS NOT NULL"):
                        if any(ref["asset_id"] == asset_id for ref in json.loads(task[0])):
                            raise AssetInUseError("Asset is retained by a generation attempt snapshot")
                    original = resolve_asset_path(row["file_path"])
                    if not original.is_file():
                        raise FileNotFoundError("Asset source file is missing; metadata was preserved")
                    staged = original.with_name(f".{original.name}.deleting-{uuid.uuid4().hex}")
                    original.rename(staged)
                    store._conn.execute("DELETE FROM assets WHERE id=?", (asset_id,))
                committed = True
                staged.unlink()
            except BaseException:
                if staged is not None and staged.exists():
                    if committed:
                        with store._conn:
                            store._conn.execute("BEGIN IMMEDIATE")
                            store._conn.execute("INSERT INTO assets (id,project_id,name,type,file_path,created_at) VALUES (?,?,?,?,?,?)",
                                               tuple(row[key] for key in ("id","project_id","name","type","file_path","created_at")))
                            store._conn.execute('UPDATE assets SET retired_at=? WHERE id=?',(row.get('retired_at'),row['id']))
                            staged.rename(original)
                    else:
                        staged.rename(original)
                raise
        return True

    def recover_deletions(self):
        """Reconcile recognized staged deletes after an interrupted process."""
        root = asset_root()
        recovered = 0
        with self.store._lock:
            for staged in root.glob("*/.*.deleting-*"):
                match = re.fullmatch(r"\.([A-Za-z0-9_-]+\.(?:jpg|jpeg|png|webp))\.deleting-[a-f0-9]{32}", staged.name)
                if not match:
                    continue
                relative = f"{staged.parent.name}/{match.group(1)}"
                original = resolve_asset_path(relative)
                if staged.is_symlink() or not staged.resolve().is_relative_to(root):
                    raise ValueError("Staged deletion escapes asset storage")
                identifier = Path(match.group(1)).stem
                row = self.store._conn.execute("SELECT file_path FROM assets WHERE id=?", (identifier,)).fetchone()
                if row:
                    if row[0] != relative or original.exists():
                        raise RuntimeError("Ambiguous staged asset deletion; files were preserved")
                    staged.rename(original)  # DB transaction was rolled back.
                else:
                    staged.unlink()  # Metadata deletion had already committed.
                recovered += 1
        return recovered
