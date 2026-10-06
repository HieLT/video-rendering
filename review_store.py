"""Scene review read models derived from existing tasks and selected_task_id."""
import json
import time


def valid_output(task, scene_id):
    return bool(task and task['scene_id'] == scene_id and task['status'] == 'completed'
                and task['deleted_at'] is None and (task.get('video_url') or '').strip())


def production_status(scene, attempts, active, selected):
    # Selection is a production decision; a later regeneration does not undo it.
    if selected:
        return 'SELECTED'
    if active:
        return active['status'].upper()
    if any(task['status'] == 'completed' for task in attempts):
        return 'NEEDS_REVIEW'
    if not scene['ready']:
        return scene['readiness']
    return 'READY'


def generation_view(task, number, selected_id):
    keys = ('scene_id','created_at','status','model','ratio','duration','start_end',
            'video_url','error','failure_code','batch_id','batch_index','batch_count',
            'started_at','finished_at','prompt','deleted_at','account','conversation_id',
            'phase','next_check_at','retry_count','check_round','result_url')
    raw = task.get('reference_snapshot')
    try:
        snapshot = json.loads(raw) if isinstance(raw,str) else raw
    except ValueError:
        snapshot = raw  # Preserve malformed legacy data for inspection.
    return {**{key:task.get(key) for key in keys}, 'task_id':task['id'],
            'generation_number':number, 'reference_snapshot':snapshot,
            'selected':task['id']==selected_id and valid_output(task,task['scene_id'])}


class ReviewStoreMixin:
    def _scene_generations(self, scene_id):
        scene = self._require('scenes', scene_id)
        # Include soft-deleted rows for stable historical numbering.
        rows = [dict(row) for row in self._conn.execute(
            'SELECT * FROM tasks WHERE scene_id=? ORDER BY created_at,batch_index,id', (scene_id,))]
        generations = [generation_view(row,index,scene['selected_task_id']) for index,row in enumerate(rows,1)]
        return {'scene_id':scene_id, 'selected_task_id':scene['selected_task_id'],
                'generations':list(reversed(generations))}

    def scene_generations(self, scene_id):
        with self._lock, self._conn:
            self._conn.execute('BEGIN')
            return self._scene_generations(scene_id)

    def project_review_status(self, project_id):
        with self._lock, self._conn:
            self._conn.execute('BEGIN')
            scenes = self._project_generation_status(project_id)
            summary = {key:0 for key in ('selected','needs_review','processing','queued',
                                         'missing_references','invalid_configuration','ready')}
            summary['total_scenes'] = len(scenes)
            for scene in scenes:
                summary[scene['production_status'].lower()] += 1
            return {'project_id':project_id, 'scenes':scenes, 'summary':summary,
                    'production_complete':bool(scenes) and summary['selected']==len(scenes)}

    def project_selected_videos(self, project_id):
        with self._lock, self._conn:
            self._conn.execute('BEGIN')
            scenes = self._project_generation_status(project_id)
            videos = []
            for scene in scenes:
                task = scene['selected_task']
                if task:
                    videos.append(dict(scene_id=scene['id'],scene_number=scene['scene_number'],
                        scene_name=scene['scene_name'],task_id=task['id'],video_url=task['video_url'],
                        duration=task['duration'],model=task['model'],ratio=task['ratio']))
            return dict(project_id=project_id,total_scenes=len(scenes),selected_count=len(videos),
                        complete=bool(scenes) and len(videos)==len(scenes),videos=videos)

    def select_latest_completed(self, project_id):
        with self._lock, self._conn:
            self._conn.execute('BEGIN IMMEDIATE')
            scenes = self._project_generation_status(project_id)
            changed, skipped = [], []
            for scene in scenes:
                if scene['selected_task']:
                    skipped.append(dict(scene_id=scene['id'],reason='ALREADY_SELECTED'))
                    continue
                rows = [dict(row) for row in self._conn.execute(
                    "SELECT * FROM tasks WHERE scene_id=? AND status='completed' AND deleted_at IS NULL ORDER BY created_at DESC,batch_index DESC,id DESC", (scene['id'],))]
                task = next((row for row in rows if valid_output(row,scene['id'])), None)
                if not task:
                    skipped.append(dict(scene_id=scene['id'],reason='NO_COMPLETED_GENERATION'))
                    continue
                self._conn.execute('UPDATE scenes SET selected_task_id=?,updated_at=? WHERE id=?',
                                   (task['id'],time.time(),scene['id']))
                changed.append(dict(scene_id=scene['id'],task_id=task['id']))
            return dict(project_id=project_id,changed=len(changed),skipped_count=len(skipped),
                        selected_scenes=changed,skipped_scenes=skipped)
