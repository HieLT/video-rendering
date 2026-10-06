"""Persistent requirements, atomic scene import and computed production readiness."""
import sqlite3
import time
import uuid

from asset_storage import resolve_asset_path
from scene_import import (asset_name_index, asset_name_key, configuration_errors,
                          find_asset_name_collisions, AssetNameCollisionError,
                          ImportValidationError, SceneNotReadyError, validate_import_payload)


class SceneWorkflowMixin:
    def _check_asset_name_collisions(self):
        if not self._conn.execute("SELECT 1 FROM sqlite_master WHERE type='table' AND name='assets'").fetchone():
            return
        rows = [dict(row) for row in self._conn.execute('SELECT id,project_id,name FROM assets')]
        collisions = find_asset_name_collisions(rows)
        if collisions:
            raise AssetNameCollisionError(collisions)

    def _migrate_v4(self):
        if self._conn.execute('PRAGMA user_version').fetchone()[0] >= 4:
            return
        self._check_asset_name_collisions()
        self._conn.execute('CREATE UNIQUE INDEX IF NOT EXISTS assets_project_name_nocase ON assets(project_id, name COLLATE NOCASE)')
        self._conn.execute("""CREATE TABLE IF NOT EXISTS scene_reference_requirements (
            scene_id TEXT NOT NULL REFERENCES scenes(id) ON DELETE CASCADE,
            position INTEGER NOT NULL CHECK(position >= 1),
            name TEXT NOT NULL CHECK(length(trim(name)) > 0),
            reference_alias TEXT NOT NULL COLLATE NOCASE CHECK(length(trim(reference_alias)) > 0),
            asset_id TEXT REFERENCES assets(id) ON DELETE RESTRICT,
            PRIMARY KEY(scene_id, position), UNIQUE(scene_id, name COLLATE NOCASE),
            UNIQUE(scene_id, reference_alias), UNIQUE(scene_id, asset_id))""")
        self._conn.execute('CREATE INDEX IF NOT EXISTS scene_requirements_asset ON scene_reference_requirements(asset_id)')
        self._conn.execute("""INSERT INTO scene_reference_requirements(scene_id,position,name,reference_alias,asset_id)
            SELECT sa.scene_id,sa.position,a.name,sa.reference_alias,sa.asset_id
            FROM scene_assets sa JOIN assets a ON a.id=sa.asset_id
            WHERE NOT EXISTS (SELECT 1 FROM scene_reference_requirements r
                              WHERE r.scene_id=sa.scene_id AND r.position=sa.position)""")
        if self._conn.execute('PRAGMA foreign_key_check').fetchall():
            raise sqlite3.IntegrityError('Scene requirement migration failed foreign_key_check')
        self._conn.execute('PRAGMA user_version=4')

    def _ordered_scene_requirements(self, scene_id):
        return [dict(row) for row in self._conn.execute("""SELECT r.scene_id,r.position,r.name,
            r.reference_alias AS alias,r.asset_id,a.file_path,a.project_id AS asset_project_id
            FROM scene_reference_requirements r LEFT JOIN assets a ON a.id=r.asset_id
            WHERE r.scene_id=? ORDER BY r.position""", (scene_id,))]

    def _scene_details(self, scene_id):
        scene = self._require('scenes', scene_id)
        rows = self._ordered_scene_requirements(scene_id)
        references = [{key:row[key] for key in ('name','alias','position','asset_id')} for row in rows]
        missing = [ref for ref in references if ref['asset_id'] is None]
        errors = configuration_errors(scene, references)
        for row in rows:
            if row['asset_id'] is not None:
                if row['asset_project_id'] != scene['project_id']:
                    errors.append('Reference asset belongs to another project')
                try:
                    if not resolve_asset_path(row['file_path']).is_file():
                        errors.append(f"Reference source file is missing: {row['name']}")
                except (ValueError, OSError) as exc:
                    errors.append(str(exc))
        linked = self._ordered_scene_assets(scene_id)
        resolved = [row for row in rows if row['asset_id'] is not None]
        if [(r['asset_id'],r['position'],r['alias']) for r in resolved] != [(r['asset_id'],r['position'],r['reference_alias']) for r in linked]:
            errors.append('Resolved reference links are inconsistent with their requirements')
        ready = not missing and not errors
        return {**scene, 'references':references, 'ready':ready,
                'readiness':'READY' if ready else ('MISSING_REFERENCES' if missing else 'INVALID_CONFIGURATION'),
                'missing_references':missing, 'validation_errors':errors}

    def scene_details(self, scene_id):
        with self._lock, self._conn:
            self._conn.execute('BEGIN')
            return self._scene_details(scene_id)

    def project_scene_details(self, project_id):
        with self._lock, self._conn:
            self._conn.execute('BEGIN')
            self._require('projects', project_id)
            identifiers = [row[0] for row in self._conn.execute('SELECT id FROM scenes WHERE project_id=? ORDER BY scene_number,id', (project_id,))]
            return [self._scene_details(identifier) for identifier in identifiers]

    def list_scene_requirements(self, scene_id):
        with self._lock:
            self._require('scenes', scene_id)
            return [{key:row[key] for key in ('name','alias','position','asset_id')}
                    for row in self._ordered_scene_requirements(scene_id)]

    def _import_plan(self, project_id, payload):
        self._require('projects', project_id)
        assets = [dict(row) for row in self._conn.execute('SELECT * FROM assets WHERE project_id=? ORDER BY id', (project_id,))]
        existing = [dict(row) for row in self._conn.execute('SELECT id,scene_number FROM scenes WHERE project_id=?', (project_id,))]
        return validate_import_payload(payload, assets, existing)

    def validate_scene_import(self, project_id, payload):
        with self._lock, self._conn:
            self._conn.execute('BEGIN')
            return self._import_plan(project_id, payload)

    def import_scenes(self, project_id, payload):
        with self._lock, self._conn:
            self._conn.execute('BEGIN IMMEDIATE')
            report = self._import_plan(project_id, payload)
            if not report['valid']:
                raise ImportValidationError(report)
            now = time.time()
            identifiers = []
            for item in sorted(report['scenes'], key=lambda row:row['scene_number']):
                identifier = uuid.uuid4().hex
                self._conn.execute("""INSERT INTO scenes
                    (id,project_id,scene_number,scene_name,prompt,model,ratio,duration,start_end,created_at,updated_at)
                    VALUES (?,?,?,?,?,?,?,?,?,?,?)""", (identifier,project_id,item['scene_number'],item['scene_name'],
                    item['prompt'],item['model'],item['ratio'],item['duration'],int(item['start_end']),now,now))
                for ref in item['references']:
                    self._conn.execute('INSERT INTO scene_reference_requirements VALUES (?,?,?,?,?)',
                                       (identifier,ref['position'],ref['name'],ref['alias'],ref['asset_id']))
                    if ref['asset_id'] is not None:
                        self._conn.execute('INSERT INTO scene_assets VALUES (?,?,?,?)',
                                           (identifier,ref['asset_id'],ref['position'],ref['alias']))
                identifiers.append(identifier)
            return {**report, 'imported_count':len(identifiers), 'scenes':[self._scene_details(i) for i in identifiers]}

    def resolve_scene_references(self, scene_id):
        with self._lock, self._conn:
            self._conn.execute('BEGIN IMMEDIATE')
            scene = self._require('scenes', scene_id)
            assets = [dict(row) for row in self._conn.execute('SELECT * FROM assets WHERE project_id=?', (scene['project_id'],))]
            lookup = asset_name_index(assets)
            resolved_count = 0
            for ref in self._ordered_scene_requirements(scene_id):
                if ref['asset_id'] is None:
                    asset = lookup.get(asset_name_key(ref['name']))
                    if asset:
                        self._conn.execute('UPDATE scene_reference_requirements SET asset_id=? WHERE scene_id=? AND position=?',
                                           (asset['id'],scene_id,ref['position']))
                        self._conn.execute('INSERT INTO scene_assets VALUES (?,?,?,?)',
                                           (scene_id,asset['id'],ref['position'],ref['alias']))
                        resolved_count += 1
            if resolved_count:
                self._conn.execute('UPDATE scenes SET updated_at=? WHERE id=?', (time.time(),scene_id))
            return {'resolved_count':resolved_count, 'scene':self._scene_details(scene_id)}
