"""Project-scoped production summaries, independent of generation configuration."""
import sqlite3
from scene_import import ImportValidationError

class SceneInfoMixin:
    def _migrate_v6(self):
        if self._conn.execute('PRAGMA user_version').fetchone()[0] >= 6:
            self._migrate_v7()
            return
        columns = {row[1] for row in self._conn.execute('PRAGMA table_info(scenes)')}
        if 'scene_summary' not in columns:
            self._conn.execute('ALTER TABLE scenes ADD COLUMN scene_summary TEXT')
        if self._conn.execute('PRAGMA foreign_key_check').fetchall():
            raise sqlite3.IntegrityError('Scene summary migration failed foreign_key_check')
        self._conn.execute('PRAGMA user_version=6')
        self._migrate_v7()

    def _scene_info_plan(self, project_id, payload):
        self._require('projects', project_id)
        errors, entries, seen = [], [], set()
        if not isinstance(payload, dict) or set(payload) != {'scenes'} or not isinstance(payload.get('scenes'), list) or not payload['scenes']:
            errors.append({'field':'scenes','message':'Expected exactly a non-empty scenes array'})
        else:
            for index, row in enumerate(payload['scenes']):
                field = f'scenes[{index}]'
                if not isinstance(row, dict) or set(row) != {'scene_number','summary'}:
                    errors.append({'field':field,'message':'Expected only scene_number and summary'})
                    continue
                number = row['scene_number']
                if type(number) is not int or number < 1:
                    errors.append({'field':field+'.scene_number','message':'Scene number must be a positive integer'})
                elif number in seen:
                    errors.append({'field':field+'.scene_number','message':f'Duplicate Scene {number}'})
                elif not isinstance(row['summary'], str):
                    errors.append({'field':field+'.summary','message':'Summary must be a string'})
                else:
                    entries.append(row)
                if type(number) is int:
                    seen.add(number)
        scenes = {row['scene_number']:row['id'] for row in self._conn.execute('SELECT id,scene_number FROM scenes WHERE project_id=?',(project_id,))}
        missing = [row['scene_number'] for row in entries if row['scene_number'] not in scenes]
        report = dict(valid=not errors and not missing, total_entries=len(payload['scenes']) if isinstance(payload,dict) and isinstance(payload.get('scenes'),list) else 0,
            matched_count=sum(row['scene_number'] in scenes for row in entries), missing_scene_numbers=missing,
            validation_errors=errors, conflicts=[{'scene_number':number,'message':f'Scene {number} not found in this Project'} for number in missing])
        return report, [(row['summary'],scenes[row['scene_number']]) for row in entries if row['scene_number'] in scenes]

    def validate_scene_info(self, project_id, payload):
        with self._lock:
            return self._scene_info_plan(project_id,payload)[0]

    def import_scene_info(self, project_id, payload):
        with self._lock, self._conn:
            self._conn.execute('BEGIN IMMEDIATE')
            report, updates = self._scene_info_plan(project_id,payload)
            if not report['valid']:
                raise ImportValidationError(report)
            # updated_at is the generation revision; metadata must not invalidate submissions.
            self._conn.executemany('UPDATE scenes SET scene_summary=? WHERE id=?',updates)
            return dict(report,updated_count=len(updates))

    def update_scene_summary(self, scene_id, payload):
        if not isinstance(payload,dict) or set(payload) != {'summary'} or (payload['summary'] is not None and not isinstance(payload['summary'],str)):
            raise ValueError('Expected only summary, containing a string or null')
        with self._lock, self._conn:
            self._conn.execute('BEGIN IMMEDIATE')
            self._require('scenes',scene_id)
            self._conn.execute('UPDATE scenes SET scene_summary=? WHERE id=?',(payload['summary'],scene_id))
            return self._require('scenes',scene_id)
