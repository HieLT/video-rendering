"""Transactional, resumable Film Studio workflow; external effects are journalled first.

Run documents are atomic snapshots. Artifacts and completed tasks are append-only.
The model never directly chooses state transitions, approval status or batch IDs.
"""
import hashlib
import json
import re
import sqlite3
import time
import uuid
from contextlib import contextmanager

from studio_contracts import OUTPUTS, ROLE_BY_KIND, INSTRUCTIONS, PROTOCOL_VERSION, MESSAGE_TYPE, DELIVERY_STATES


class Conflict(ValueError):
    pass


def identity(prefix):
    return prefix + '_' + uuid.uuid4().hex


def digest(value):
    return hashlib.sha256(json.dumps(value, ensure_ascii=False, sort_keys=True).encode()).hexdigest()


TERMINAL = ('completed', 'completed_with_issues', 'cancelled')


class Workflow:
    def __init__(self, database, reference_limit=30):
        self.database = str(database)
        self.reference_limit = reference_limit
        with self.connection() as db:
            db.execute('CREATE TABLE IF NOT EXISTS studio_runs (id TEXT PRIMARY KEY, active INTEGER NOT NULL, revision INTEGER NOT NULL, document TEXT NOT NULL)')
            db.execute('CREATE UNIQUE INDEX IF NOT EXISTS studio_one_active_run ON studio_runs(active) WHERE active=1')

    @contextmanager
    def connection(self):
        db = sqlite3.connect(self.database, timeout=20)
        try:
            db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except Exception:
            db.rollback()
            raise
        finally:
            db.close()

    def _load(self, db, run_id):
        row = db.execute('SELECT document FROM studio_runs WHERE id=?', (run_id,)).fetchone()
        if not row:
            raise KeyError(run_id)
        return json.loads(row[0])

    @contextmanager
    def edit(self, run_id, revision=None):
        with self.connection() as db:
            run = self._load(db, run_id)
            before = digest(run)
            if revision is not None and revision != run['revision']:
                raise Conflict('Dữ liệu đã thay đổi. Tải lại trước khi duyệt.')
            yield run
            if digest(run) != before:
                run['revision'] += 1
                run['updated_at'] = time.time()
                db.execute('UPDATE studio_runs SET revision=?, active=?, document=? WHERE id=?',
                           (run['revision'], int(run['status'] not in TERMINAL),
                            json.dumps(run, ensure_ascii=False), run_id))

    def get(self, run_id):
        with self.connection() as db:
            return self._load(db, run_id)

    def list(self):
        with self.connection() as db:
            rows = [json.loads(r[0]) for r in db.execute('SELECT document FROM studio_runs ORDER BY rowid DESC')]
        return [{k: r[k] for k in ('id', 'title', 'status', 'revision', 'updated_at', 'paused')} for r in rows]

    def annotate_tasks(self, tasks):
        batches = {t.get('batch_id') for t in tasks if (t.get('batch_id') or '').startswith('studio_')}
        if not batches:
            return tasks
        metadata = {}
        with self.connection() as db:
            for row in db.execute('SELECT document FROM studio_runs'):
                run = json.loads(row[0])
                for clip in run['clips']:
                    if clip['batch_id'] in batches:
                        metadata[clip['batch_id']] = clip
        return [{**t, **({'studio_status': metadata[t['batch_id']]['status'],
                         'studio_selected': metadata[t['batch_id']]['selected_task_id'] == t['id'],
                         'studio_selected_task_id': metadata[t['batch_id']]['selected_task_id']}
                        if t.get('batch_id') in metadata else {})} for t in tasks]

    def event(self, run, kind, detail):
        run['events'].append({'at': time.time(), 'kind': kind, 'detail': detail})

    def artifact(self, run, kind, content, parents=()):
        item = {'id': identity('artifact'), 'kind': kind, 'content': content,
                'parents': list(parents), 'hash': digest(content), 'created_at': time.time()}
        run['artifacts'].append(item)
        return item['id']

    def content(self, run, artifact_id):
        return next(a['content'] for a in run['artifacts'] if a['id'] == artifact_id)

    def task(self, run, kind, inputs, clip=None, ref=None):
        item = {'id': identity('agent'), 'kind': kind, 'role': ROLE_BY_KIND[kind], 'status': 'pending',
                'inputs': inputs, 'clip': clip, 'ref': ref, 'created_at': time.time(), 'output': None,
                'delivery': 'not_sent', 'error': None, 'attempts': 0,
                'last_transition_at': time.time(), 'next_retry_at': 0}
        run['tasks'].append(item)
        return item

    def create(self, title, screenplay, request_id, project_url, approval_mode='manual'):
        title, screenplay = title.strip(), screenplay.strip()
        if approval_mode not in ('manual', 'agent'):
            raise ValueError('Approval mode khÃ´ng há»£p lá»‡.')
        if not title or len(title) > 120 or not screenplay or len(screenplay) > 500000:
            raise ValueError('Tên phim/kịch bản không hợp lệ.')
        key = digest([title, screenplay, project_url])
        with self.connection() as db:
            for row in db.execute('SELECT document FROM studio_runs'):
                old = json.loads(row[0])
                if old['request_id'] == request_id:
                    if old['request_hash'] != key:
                        raise Conflict('Request ID đã được dùng với dữ liệu khác.')
                    return old
            if db.execute('SELECT 1 FROM studio_runs WHERE active=1').fetchone():
                raise Conflict('Chỉ một dự án hoạt động. Hoàn tất hoặc hủy dự án hiện tại trước.')
            run = dict(id=identity('film'), title=title, project_url=project_url, request_id=request_id,
                       request_hash=key, approval_mode=approval_mode, revision=1, status='script_review', paused=False, updated_at=time.time(),
                       artifacts=[], tasks=[], clips=[], events=[], approvals=[], images={}, image_cycles=0,
                       queue_counter=0, screenplay=None, screenplay_source=None, plan=None, references=None, reference_qa=None)
            run['screenplay'] = self.artifact(run, 'screenplay', screenplay)
            run['screenplay_source'] = run['screenplay']
            self.task(run, 'script_review', [run['screenplay']])
            self.event(run, 'created', 'Kịch bản đầu vào đã lưu; chờ phản biện và duyệt.')
            db.execute('INSERT INTO studio_runs VALUES (?,1,1,?)', (run['id'], json.dumps(run, ensure_ascii=False)))
            return run

    def envelope(self, run_id, task_id):
        run = self.get(run_id)
        task = next(t for t in run['tasks'] if t['id'] == task_id)
        inputs = [a for a in run['artifacts'] if a['id'] in task['inputs']]
        if task['kind'] == 'reference_candidate_qa':
            by_id = {a['id']: a for a in inputs}
            inputs = [by_id[i] for i in task['inputs'] if i in by_id]
        if task['kind'] == 'reference_selected_qa':
            by_id = {a['id']: a for a in inputs}
            inputs = [by_id[i] for i in task['inputs'] if i in by_id]
        handoffs = []
        if task['kind'] == 'image_create' and run.get('references'):
            # Image generation needs one reference specification, not the
            # complete screenplay and every clip contract. Sending the full
            # project exceeds ChatGPT's composer limit on feature-length runs.
            reference_plan = self.content(run, run['references'])
            spec = next(r for r in reference_plan['references'] if r['id'] == task['ref'])
            used_by = [int(number) for number, ids in reference_plan['clip_references'].items()
                       if task['ref'] in ids]
            colors = []
            if run.get('plan'):
                contracts = self.content(run, run['plan'])['clips']
                colors = list(dict.fromkeys(c['color'] for c in contracts if c['number'] in used_by))
            projected = [{
                'source_artifact': run['references'],
                'source_hash': next(a['hash'] for a in run['artifacts'] if a['id'] == run['references']),
                'kind': 'reference_spec',
                'content': {**spec, 'used_by_clips': used_by, 'locked_colors': colors},
            }]
            # Preserve actual prior-image and QA artifacts on repair cycles.
            projected.extend(a for a in inputs if a['id'] not in
                             {run.get('screenplay'), run.get('plan'), run.get('references')})
            inputs = projected
        if task['kind'] == 'reference_qa' and run.get('references'):
            # Give QA an explicit reference ID -> actual image mapping. The
            # browser transport can then build labelled contact sheets instead
            # of exceeding ChatGPT's per-message attachment count.
            image_by_artifact = {artifact_id: ref for ref, artifact_id in run['images'].items()}
            projected = []
            for artifact in inputs:
                ref = image_by_artifact.get(artifact['id'])
                if ref:
                    projected.append({'source_artifact': artifact['id'],
                                      'source_hash': artifact['hash'],
                                      'kind': 'reference_image',
                                      'content': {'reference_id': ref,
                                                  'image_id': artifact['content']['image_id']}})
                else:
                    projected.append(artifact)
            inputs = projected
        if task['clip'] is not None:
            clip = self.clip(run, task['clip'])
            mapping = self.content(run, run['references'])['clip_references'][str(clip['number'])]
            image_artifacts = set(run['images'].values())
            needed_images = {run['images'][ref] for ref in mapping}
            projected = []
            for a in inputs:
                if a['id'] in image_artifacts and a['id'] not in needed_images:
                    continue
                if a['id'] == run['screenplay']:
                    projected.append({'source_artifact': a['id'], 'source_hash': a['hash'], 'kind': 'screenplay_excerpt',
                                      'content': clip['contract']['source_excerpt']})
                elif a['id'] == run['plan']:
                    projected.append({'source_artifact': a['id'], 'source_hash': a['hash'], 'kind': 'clip_contract',
                                      'content': clip['contract']})
                elif a['id'] == run['references']:
                    refs = self.content(run, a['id'])
                    projected.append({'source_artifact': a['id'], 'source_hash': a['hash'], 'kind': 'clip_reference_mapping',
                                      'content': {'references': [r for r in refs['references'] if r['id'] in mapping], 'order': mapping}})
                else:
                    projected.append(a)
            inputs = projected
            for n in clip['contract']['depends_on']:
                previous = self.clip(run, n)
                handoffs.append({'video_number': n, 'selected_task_id': previous['selected_task_id'],
                                 'selected_take': next((a for a in previous['attempts'] if a['task_id'] == previous['selected_task_id']), None),
                                 'planned_end_state': previous['contract']['end_state'],
                                 'note': 'Planned state is not an observation; inspect selected take for actual continuity.'})
        return {'protocol_version': PROTOCOL_VERSION, 'message_type': MESSAGE_TYPE,
                'schema_version': 1, 'run_id': run_id, 'task_id': task_id,
                'sender': 'workflow_orchestrator', 'recipient': task['role'], 'role': task['role'],
                # Transport needs the concrete task kind when several tasks
                # share one role (reference_candidate_qa and the stricter
                # reference_selected_qa both use reference_qa).
                'task_kind': task['kind'],
                'instruction': INSTRUCTIONS[task['kind']], 'clip_number': task['clip'], 'reference_id': task['ref'],
                'inputs': inputs, 'previous_selected_takes': handoffs,
                'output_schema': OUTPUTS[task['kind']].model_json_schema(),
                'constraints': {'duration': 30, 'dialogue': 'English', 'audio': 'Dola', 'max_videos_per_batch': 10,
                                'max_references_per_clip': self.reference_limit,
                                'approval_mode': run.get('approval_mode', 'manual'),
                                'screenplay_approval_authority': ('workflow_agent' if run.get('approval_mode') == 'agent' else 'user'),
                                'reference_approval_authority': ('workflow_agent' if run.get('approval_mode') == 'agent' else 'user'),
                                'script_changes_require_approval': run.get('approval_mode') != 'agent',
                                'agent_mode_note': ('The user delegated screenplay and reference quality gates to the workflow agent for this run. '
                                                    'Do not report missing author approval as a defect.'
                                                    if run.get('approval_mode') == 'agent' else ''),
                                'references_require_approval': run.get('approval_mode') != 'agent'},
                'response_contract': {'type': 'object', 'required': ['task_id', 'output'],
                                      'additionalProperties': False,
                                      'task_id_must_equal': task_id,
                                      'output_must_match': 'output_schema'}}

    def bind_skills(self, run_id, task_id, sources):
        with self.edit(run_id) as run:
            task = next(t for t in run['tasks'] if t['id'] == task_id)
            if task['role'] not in ('prompt_engineer', 'reference_selector'):
                return {}
            if task.get('skill_artifact'):
                return self.content(run, task['skill_artifact'])
            if task['status'] != 'pending':
                return {}
            aid = self.artifact(run, 'skill_snapshot', sources)
            task['skill_artifact'] = aid
            task['inputs'].append(aid)
            return sources

    @staticmethod
    def without_skill_snapshots(run, inputs):
        """Keep retry envelopes bounded and bind a fresh immutable skill snapshot."""
        artifacts = {artifact['id']: artifact for artifact in run['artifacts']}
        return [artifact_id for artifact_id in inputs
                if artifacts.get(artifact_id, {}).get('kind') != 'skill_snapshot']

    def submit(self, run_id, task_id, result):
        with self.edit(run_id) as run:
            task = next((t for t in run['tasks'] if t['id'] == task_id), None)
            if not task:
                raise KeyError(task_id)
            parsed = OUTPUTS[task['kind']].model_validate(result).model_dump()
            if task['status'] == 'completed':
                if self.content(run, task['output']) != parsed:
                    raise Conflict('Nhiệm vụ đã hoàn thành với kết quả khác.')
                return
            if task['status'] != 'pending' or run['status'] == 'cancelled':
                raise Conflict('Nhiệm vụ không còn nhận kết quả.')
            self._validate(run, task, parsed)
            output = self.artifact(run, task['kind'], parsed, task['inputs'])
            task.update(status='completed', output=output, error=None)
            self._advance(run, task, parsed, output)
            self.event(run, 'agent_result', {'task': task_id, 'artifact': output})

    def _validate(self, run, task, value):
        kind = task['kind']
        if kind == 'image_create':
            duplicate_refs = []
            for ref, artifact_id in run['images'].items():
                if ref == task['ref']:
                    continue
                existing = self.content(run, artifact_id)
                if existing.get('image_id') == value['image_id']:
                    duplicate_refs.append(ref)
            if duplicate_refs:
                raise ValueError('Reference image duplicates existing reference(s): ' + ', '.join(duplicate_refs))
        if kind == 'script_edit':
            source_id = run.get('screenplay_source') or run['screenplay']
            source = self.content(run, source_id)
            candidate = value['screenplay']

            # A Script Editor result is a full replacement artifact. Refuse
            # summaries/proposals that would silently erase most of the film.
            # Numbered scene headings are used when the source provides them;
            # the length floor also protects screenplays with another format.
            def scene_numbers(text):
                return {int(n) for n in re.findall(r'(?m)^\s*(?:\*\*)?(\d+)\.\s+', text)}

            source_scenes = scene_numbers(source)
            candidate_scenes = scene_numbers(candidate)
            if len(candidate.strip()) < max(1, int(len(source.strip()) * 0.70)):
                raise ValueError('Script Editor must return the complete consolidated screenplay; the result is too short.')
            if len(source_scenes) >= 2 and not source_scenes.issubset(candidate_scenes):
                missing = sorted(source_scenes - candidate_scenes)
                raise ValueError(f'Script Editor removed numbered scenes: {missing}. Return the complete screenplay.')
        if kind == 'production_plan':
            script = self.content(run, run['screenplay'])
            def canonical_source(text):
                # Markdown emphasis/backticks and line wrapping are transport
                # formatting, not screenplay content.
                return re.sub(r'\s+', ' ', re.sub(r'[*_`]', '', text)).strip()
            canonical_script = canonical_source(script)
            if any(canonical_source(c['source_excerpt']) not in canonical_script for c in value['clips']):
                raise ValueError('Mỗi clip phải trích nguyên văn một đoạn trong kịch bản đã duyệt.')
        if kind == 'reference_plan':
            if set(value['clip_references']) != {str(c['number']) for c in run['clips']}:
                raise ValueError('Reference mapping phải bao phủ mọi clip.')
            if any(len(ids) > self.reference_limit for ids in value['clip_references'].values()):
                raise ValueError(f'Mỗi clip tối đa {self.reference_limit} reference theo cấu hình Dola.')
        if kind == 'reference_qa':
            verdicts = value['references']
            if len({v['reference_id'] for v in verdicts}) != len(verdicts) or {v['reference_id'] for v in verdicts} != set(run['images']):
                raise ValueError('QA phải đánh giá đầy đủ bộ ảnh hiện tại.')
            specs = {r['id']: r for r in self.content(run, run['references'])['references']}
            for verdict in verdicts:
                if verdict['verdict'] == 'PASS' and specs[verdict['reference_id']].get('reference_type') == 'character':
                    checks = verdict.get('character_sheet_checks')
                    if not checks or not all(checks.values()):
                        raise ValueError('Character sheet PASS requires all visual checks.')
        if kind == 'reference_candidate_qa':
            bundle = next(self.content(run, a) for a in task['inputs']
                          if next(x for x in run['artifacts'] if x['id'] == a)['kind'] == 'image_candidates')
            parent = next(t for t in run['tasks'] if t['id'] == bundle['parent_task_id'])
            if parent['status'] != 'pending' or parent.get('candidate_qa_id') != task['id']:
                raise Conflict('Candidate selection is stale.')
            if value['verdict'] == 'PASS':
                chosen = bundle['candidates'][value['selected_candidate'] - 1]
                # Candidate selection is deliberately a ranking pass. The
                # independent reference_selected_qa task owns the strict
                # character-sheet approval checks at original resolution.
                self._validate(run, parent, {'image_id': chosen['image_id']})
        if kind == 'reference_selected_qa':
            if value['reference_id'] != task['ref']:
                raise ValueError('Independent Reference QA returned the wrong reference ID.')
            spec = next(self.content(run, a) for a in task['inputs']
                        if next(x for x in run['artifacts'] if x['id'] == a)['kind'] == 'reference_spec')
            if value['verdict'] == 'PASS' and spec.get('reference_type') == 'character':
                checks = value.get('character_sheet_checks')
                if not checks or not all(checks.values()):
                    raise ValueError('Character sheet PASS requires all visual checks.')
        if kind in ('prompt', 'prompt_repair'):
            clip = self.clip(run, task['clip'])
            expected = self.content(run, run['references'])['clip_references'][str(clip['number'])]
            if value['reference_ids'] != expected:
                raise ValueError('Prompt phải giữ đúng reference và thứ tự đã duyệt.')
        if kind == 'video_qa':
            clip = self.clip(run, task['clip'])
            if value['verdict'] == 'PASS' and clip['contract']['dialogue'] and not value['audio_inspected']:
                raise ValueError('Clip có thoại phải được nghe trước khi PASS.')

    def clip(self, run, number):
        return next(c for c in run['clips'] if c['number'] == number)

    def enqueue(self, run, clip):
        run['queue_counter'] += 1
        clip.update(status='queued', queue_position=run['queue_counter'])

    def settle(self, run):
        if not run['clips'] or run['status'] != 'production':
            return
        for clip in run['clips']:
            if clip['status'] in ('waiting_dependency', 'prompt', 'queued') and any(
                    self.clip(run, n)['status'] in ('exhausted', 'blocked_dependency') for n in clip['contract']['depends_on']):
                clip['status'] = 'blocked_dependency'
                for task in run['tasks']:
                    if task['clip'] == clip['number'] and task['status'] == 'pending':
                        task['status'] = 'cancelled'
            elif clip['status'] == 'waiting_dependency' and all(
                    self.clip(run, n)['status'] == 'passed' for n in clip['contract']['depends_on']):
                self.prompt_task(run, clip)
        if all(c['status'] == 'passed' for c in run['clips']):
            run['status'] = 'completed'
        elif all(c['status'] in ('passed', 'exhausted', 'blocked_dependency') for c in run['clips']):
            run['status'] = 'completed_with_issues'

    def prompt_task(self, run, clip):
        clip['status'] = 'prompt'
        self.task(run, 'prompt', [run['screenplay'], run['plan'], run['references'], *run['images'].values()], clip=clip['number'])

    def _advance(self, run, task, value, output):
        kind = task['kind']
        if kind == 'script_review':
            # A review verdict is a quality gate even in agent mode. In
            # automatic mode, NEEDS_REVIEW/NOT_INSPECTED starts an edit-review
            # loop instead of being treated as approval. It continues until
            # the reviewer returns PASS.
            blocking = [issue for issue in value.get('issues', [])
                        if issue.get('severity') in ('CRITICAL', 'MAJOR')]
            verdict = value.get('verdict')
            if verdict == 'NEEDS_REVIEW' and not blocking:
                verdict = 'PASS'
                self.event(run, 'nonblocking_review_normalized',
                           {'task_id': task['id'], 'reason': 'Only MINOR/OPTIONAL findings were reported.'})
            if verdict in ('NEEDS_REVIEW', 'NOT_INSPECTED'):
                edits = sum(t['kind'] == 'script_edit' for t in run['tasks'])
                notes = '\n'.join(
                    f"[{issue.get('severity', 'MAJOR')}] {issue.get('description', '')}\nFix: {issue.get('fix', '')}"
                    for issue in blocking)
                note = self.artifact(run, 'agent_revision_request', notes or 'Resolve all review findings and preserve the locked story intent.')
                source_id = run.get('screenplay_source') or run['screenplay']
                self.task(run, 'script_edit', [source_id, note])
                run['status'] = 'script_revision'
                self.event(run, 'agent_revision_requested', {'artifact': note, 'cycle': edits + 1})
                return
            if run.get('approval_mode', 'manual') == 'agent':
                # Explicit agent mode delegates this gate to the workflow.
                # Preserve the review artifact and record who approved it.
                run['approvals'].append({'gate': 'screenplay', 'artifact': run['screenplay'],
                                         'actor': 'agent', 'at': time.time(), 'image_versions': {}})
                self.event(run, 'agent_approved', {'gate': 'screenplay', 'artifact': run['screenplay']})
                run['status'] = 'production_planning'
                self.task(run, 'production_plan', [run['screenplay']])
            else:
                run['status'] = 'awaiting_screenplay_approval'
        elif kind == 'script_edit':
            run['screenplay'] = self.artifact(run, 'screenplay', value['screenplay'], [output])
            run['screenplay_source'] = run['screenplay']
            self.task(run, 'script_review', [run['screenplay']])
            run['status'] = 'script_review'
        elif kind == 'production_plan':
            run['plan'] = output
            run['clips'] = [dict(number=c['number'], chapter=c['chapter'], contract=c,
                                 batch_id='studio_' + run['id'] + f"_{c['number']:04d}",
                                 batch_name=f"Video {c['number']:03d} _ Chương {c['chapter']:02d} _ {run['title']}",
                                 status='planning', attempts=[], prompts=[], selected_task_id=None,
                                 queue_position=0) for c in value['clips']]
            self.task(run, 'reference_plan', [run['screenplay'], output])
            run['status'] = 'reference_planning'
        elif kind == 'reference_plan':
            run['references'] = output
            run['status'] = 'creating_images'
            for ref in value['references']:
                self.task(run, 'image_create', [run['screenplay'], run['plan'], output], ref=ref['id'])
        elif kind == 'reference_candidate_qa':
            bundle = next(self.content(run, a) for a in task['inputs']
                          if next(x for x in run['artifacts'] if x['id'] == a)['kind'] == 'image_candidates')
            parent = next(t for t in run['tasks'] if t['id'] == bundle['parent_task_id'])
            if value['verdict'] == 'PASS':
                chosen = bundle['candidates'][value['selected_candidate'] - 1]
                selected = self.artifact(run, 'image_create', {'image_id': chosen['image_id']},
                                         [*parent['inputs'], output])
                spec_id = next(a for a in task['inputs']
                               if next(x for x in run['artifacts'] if x['id'] == a)['kind'] == 'reference_spec')
                base_refs = [a for a in task['inputs'] if a != spec_id and a != next(
                    x for x in task['inputs'] if next(y for y in run['artifacts'] if y['id'] == x)['kind'] == 'image_candidates')]
                review = self.task(run, 'reference_selected_qa', [spec_id, selected, *base_refs, output], ref=parent['ref'])
                review['parent_image_task_id'] = parent['id']
                review['selection_task_id'] = task['id']
                review['candidate_bundle_id'] = next(x for x in task['inputs']
                                                     if next(y for y in run['artifacts'] if y['id'] == x)['kind'] == 'image_candidates')
                review['selected_candidate'] = value['selected_candidate']
                review['selected_candidate_job_id'] = chosen['job_id']
                parent['selected_review_id'] = review['id']
                self.event(run, 'image_candidate_selected_for_independent_qa',
                           {'task': parent['id'], 'qa_task': review['id'], 'candidate': value['selected_candidate'],
                            'job_id': chosen['job_id']})
            elif value['verdict'] == 'FAIL':
                parent.update(candidate_slots=[], candidate_qa_id=None,
                              candidate_round=parent.get('candidate_round', 0) + 1,
                              candidate_repair=value.get('repair') or value.get('evidence'), error=None)
                self.event(run, 'image_candidates_rejected', {'task': parent['id'], 'verdict': value['verdict']})
            else:
                # No visual conclusion is not evidence that all candidates
                # failed. Keep the exact candidate artifacts and re-inspect.
                retry = self.task(run, 'reference_candidate_qa',
                                  self.without_skill_snapshots(run, task['inputs']), ref=parent['ref'])
                retry['next_retry_at'] = time.time() + 30
                parent['candidate_qa_id'] = retry['id']
                self.event(run, 'image_candidates_reinspection_requested',
                           {'task': parent['id'], 'qa_task': retry['id'], 'verdict': value['verdict']})
        elif kind == 'reference_selected_qa':
            parent = next(t for t in run['tasks'] if t['id'] == task['parent_image_task_id'])
            selected = task['selected_candidate']
            if value['verdict'] == 'PASS':
                bundle = self.content(run, task['candidate_bundle_id'])
                chosen = bundle['candidates'][selected - 1]
                self.artifact(run, 'image_upload', {'task_id': parent['id'], 'image_id': chosen['image_id'],
                              'image_studio_job_id': chosen['job_id'], 'candidate_selection': task['inputs'][-1],
                              'independent_reference_qa': output}, parent['inputs'])
                result = {'image_id': chosen['image_id']}
                image = self.artifact(run, 'image_create', result, [*parent['inputs'], output])
                parent.update(status='completed', output=image, error=None, candidate_qa_id=None)
                self._advance(run, parent, result, image)
                self.event(run, 'image_candidate_independently_approved',
                           {'task': parent['id'], 'candidate': selected, 'qa_task': task['id']})
            elif value['verdict'] == 'FAIL':
                rejection = self.artifact(run, 'candidate_rejection',
                                          {'candidate': selected, 'evidence': value['evidence'], 'repair': value.get('repair', '')},
                                          [output])
                selector = next(t for t in run['tasks'] if t['id'] == task['selection_task_id'])
                retry = self.task(run, 'reference_candidate_qa',
                                  [*self.without_skill_snapshots(run, selector['inputs']), rejection], ref=parent['ref'])
                retry['next_retry_at'] = time.time() + 5
                parent['candidate_qa_id'] = retry['id']
                parent['selected_review_id'] = None
                self.event(run, 'selected_candidate_rejected_for_reselection',
                           {'task': parent['id'], 'candidate': selected, 'qa_task': task['id'], 'reselection_task': retry['id']})
            else:
                retry = self.task(run, 'reference_selected_qa', list(task['inputs']), ref=parent['ref'])
                retry.update(parent_image_task_id=parent['id'], selected_candidate=selected,
                             selected_candidate_job_id=task['selected_candidate_job_id'], candidate_bundle_id=task['candidate_bundle_id'],
                             next_retry_at=time.time() + 30)
                parent['selected_review_id'] = retry['id']
                self.event(run, 'selected_candidate_reinspection_requested',
                           {'task': parent['id'], 'qa_task': retry['id'], 'verdict': value['verdict']})
        elif kind == 'image_create':
            run['images'][task['ref']] = output
            ids = {r['id'] for r in self.content(run, run['references'])['references']}
            if set(run['images']) == ids and not any(t['kind'] == 'image_create' and t['status'] == 'pending' for t in run['tasks']):
                self.task(run, 'reference_qa', [run['references'], *run['images'].values()])
                run['status'] = 'reference_qa'
        elif kind == 'reference_qa':
            run['reference_qa'] = output
            failed = [v for v in value['references'] if v['verdict'] == 'FAIL']
            uncertain = [v for v in value['references']
                         if v['verdict'] in ('NEEDS_REVIEW', 'NOT_INSPECTED')]
            # An incomplete inspection must never discard an image. Only
            # confirmed failures are eligible for a new generation round.
            repairs = failed
            if repairs and (run.get('approval_mode', 'manual') == 'agent' or run['image_cycles'] < 3):
                run['image_cycles'] += 1
                for v in repairs:
                    self.task(run, 'image_create', [run['references'], run['images'][v['reference_id']], output], ref=v['reference_id'])
                run['status'] = 'creating_images'
                self.event(run, 'agent_reference_repairs_requested',
                           {'references': [v['reference_id'] for v in repairs],
                            'cycle': run['image_cycles']})
            elif uncertain:
                retry = self.task(run, 'reference_qa', list(task['inputs']))
                retry['next_retry_at'] = time.time() + 30
                run['status'] = 'reference_qa'
                self.event(run, 'image_set_reinspection_requested',
                           {'qa_task': retry['id'], 'references': [v['reference_id'] for v in uncertain]})
            elif failed:
                run['status'] = 'reference_revision_limit'
            else:
                if run.get('approval_mode', 'manual') == 'agent':
                    run['status'] = 'production'
                    run['approvals'].append({'gate': 'references', 'artifact': run['reference_qa'],
                                             'actor': 'agent', 'at': time.time(),
                                             'image_versions': dict(run['images'])})
                    self.event(run, 'agent_approved', {'gate': 'references', 'artifact': run['reference_qa']})
                    for clip in run['clips']:
                        if clip['contract']['depends_on']:
                            clip['status'] = 'waiting_dependency'
                        else:
                            self.prompt_task(run, clip)
                else:
                    run['status'] = 'awaiting_reference_approval'
        elif kind in ('prompt', 'prompt_repair'):
            clip = self.clip(run, task['clip'])
            clip['prompts'].append(output)
            self.enqueue(run, clip)
        elif kind == 'video_qa':
            clip = self.clip(run, task['clip'])
            attempt = clip['attempts'][-1]
            attempt['qa'] = output
            if value['verdict'] == 'PASS':
                clip.update(status='passed', selected_task_id=attempt['task_id'])
            elif value['verdict'] in ('NOT_INSPECTED', 'NEEDS_REVIEW'):
                if run.get('approval_mode', 'manual') == 'agent':
                    self.task(run, 'video_qa', list(task['inputs']), clip=clip['number'])
                    clip['status'] = 'qa'
                    self.event(run, 'agent_reinspection_requested',
                               {'gate': 'video', 'clip': clip['number'], 'task_id': attempt['task_id']})
                else:
                    clip['status'] = 'inspection_required'
            else:
                count = sum(a['generated'] for a in clip['attempts'])
                if count >= 10:
                    clip['status'] = 'exhausted'
                elif count == 5:
                    clip['status'] = 'prompt_repair'
                    inputs = [run['screenplay'], run['plan'], run['references'], *run['images'].values(),
                              clip['prompts'][-1], *[a['qa'] for a in clip['attempts'] if a.get('qa')]]
                    self.task(run, 'prompt_repair', inputs, clip=clip['number'])
                else:
                    clip['status'] = 'queued'
            self.settle(run)

    def approve(self, run_id, gate, artifact_id, revision):
        with self.edit(run_id, revision) as run:
            if gate == 'screenplay':
                if run['status'] != 'awaiting_screenplay_approval' or run['screenplay'] != artifact_id:
                    raise Conflict('Kịch bản không ở đúng phiên bản chờ duyệt.')
                run['status'] = 'production_planning'
                self.task(run, 'production_plan', [artifact_id])
            elif gate == 'references':
                if run['status'] != 'awaiting_reference_approval' or run['reference_qa'] != artifact_id:
                    raise Conflict('Bộ ảnh không ở đúng phiên bản chờ duyệt.')
                run['status'] = 'production'
                for clip in run['clips']:
                    if clip['contract']['depends_on']:
                        clip['status'] = 'waiting_dependency'
                    else:
                        self.prompt_task(run, clip)
            else:
                raise ValueError('Approval gate không hợp lệ.')
            run['approvals'].append({'gate': gate, 'artifact': artifact_id, 'at': time.time(),
                                     'image_versions': dict(run['images']) if gate == 'references' else {}})
            self.event(run, 'approved', {'gate': gate, 'artifact': artifact_id})

    def set_approval_mode(self, run_id, mode):
        if mode not in ('manual', 'agent'):
            raise ValueError('Approval mode khÃ´ng há»£p lá»‡.')
        with self.edit(run_id) as run:
            run['approval_mode'] = mode
            self.event(run, 'approval_mode_changed', {'mode': mode})
            # Switching to agent mode at the screenplay gate can advance an
            # already completed review without fabricating a new response.
            if mode == 'agent' and run['status'] == 'awaiting_screenplay_approval':
                review = next((t for t in run['tasks'] if t['kind'] == 'script_review' and t['status'] == 'completed'), None)
                if review:
                    run['approvals'].append({'gate': 'screenplay', 'artifact': run['screenplay'],
                                             'actor': 'agent', 'at': time.time(), 'image_versions': {}})
                    self.event(run, 'agent_approved', {'gate': 'screenplay', 'artifact': run['screenplay']})
                    run['status'] = 'production_planning'
                    self.task(run, 'production_plan', [run['screenplay']])
            elif mode == 'agent' and run['status'] == 'awaiting_reference_approval':
                run['approvals'].append({'gate': 'references', 'artifact': run['reference_qa'],
                                         'actor': 'agent', 'at': time.time(),
                                         'image_versions': dict(run['images'])})
                self.event(run, 'agent_approved', {'gate': 'references', 'artifact': run['reference_qa']})
                run['status'] = 'production'
                for clip in run['clips']:
                    if clip['contract']['depends_on']:
                        clip['status'] = 'waiting_dependency'
                    else:
                        self.prompt_task(run, clip)

    def rewind_to_script_review(self, run_id):
        """Discard downstream pending work while preserving all artifact history."""
        with self.edit(run_id) as run:
            for task in run['tasks']:
                if task['status'] == 'pending':
                    task['status'] = 'cancelled'
                    task['error'] = 'Cancelled when rewinding to screenplay review.'
            run['plan'] = None
            run['references'] = None
            run['reference_qa'] = None
            run['images'] = {}
            run['clips'] = []
            run['status'] = 'script_review'
            task = self.task(run, 'script_review', [run['screenplay']])
            self.event(run, 'rewound_to_script_review', {'task_id': task['id'], 'screenplay': run['screenplay']})
            return task['id']

    def restore_screenplay_source(self, run_id, artifact_id, notes):
        """Restore a known complete screenplay and restart the edit/review loop.

        This is intentionally explicit and append-only: the damaged artifacts
        remain in history, while pending replies are cancelled so they cannot
        overwrite the restored source later.
        """
        with self.edit(run_id) as run:
            if not run['paused']:
                raise Conflict('Pause the run before restoring its screenplay source.')
            artifact = next((a for a in run['artifacts'] if a['id'] == artifact_id), None)
            if not artifact or artifact['kind'] != 'screenplay' or not isinstance(artifact['content'], str):
                raise ValueError('The restore artifact must be an existing screenplay.')
            if len(artifact['content'].strip()) < 1000:
                raise ValueError('The restore artifact is not a complete screenplay.')
            for pending in run['tasks']:
                if pending['status'] == 'pending' and pending['kind'] in ('script_edit', 'script_review'):
                    pending['status'] = 'cancelled'
                    pending['error'] = 'Cancelled when restoring the canonical screenplay source.'
            run['screenplay'] = artifact_id
            run['screenplay_source'] = artifact_id
            note = self.artifact(run, 'agent_revision_request', notes, [artifact_id])
            task = self.task(run, 'script_edit', [artifact_id, note])
            run['status'] = 'script_revision'
            self.event(run, 'screenplay_source_restored',
                       {'artifact': artifact_id, 'revision_request': note, 'task_id': task['id'],
                        'actor': 'user_authorized_repair'})
            return task['id']

    def apply_approved_screenplay_patch(self, run_id, source_id, replacements, summary):
        """Apply small, exact, user-authorized edits without model truncation.

        Each old string must occur exactly once. This keeps the unchanged
        screenplay byte-for-byte identical and records a new artifact instead
        of asking a chat model to reproduce a long source document.
        """
        with self.edit(run_id) as run:
            if not run['paused']:
                raise Conflict('Pause the run before applying an approved screenplay patch.')
            source = next((a for a in run['artifacts'] if a['id'] == source_id), None)
            if not source or source['kind'] != 'screenplay' or not isinstance(source['content'], str):
                raise ValueError('The patch source must be an existing screenplay.')
            content = source['content']
            for old, new in replacements:
                if not old or old == new or content.count(old) != 1:
                    raise ValueError('Each screenplay patch must match one exact source passage.')
                content = content.replace(old, new, 1)
            if len(content) < int(len(source['content']) * 0.95):
                raise ValueError('The screenplay patch removed too much source content.')
            for pending in run['tasks']:
                if pending['status'] == 'pending' and pending['kind'] in ('script_edit', 'script_review'):
                    pending['status'] = 'cancelled'
                    pending['error'] = 'Cancelled after an exact approved screenplay patch was applied.'
            patched = self.artifact(run, 'screenplay', content, [source_id])
            run['screenplay'] = patched
            run['screenplay_source'] = patched
            task = self.task(run, 'script_review', [patched])
            run['status'] = 'script_review'
            self.event(run, 'approved_screenplay_patch_applied',
                       {'source': source_id, 'artifact': patched, 'summary': summary,
                        'replacement_count': len(replacements), 'task_id': task['id'],
                        'actor': 'user_authorized_repair'})
            return task['id']

    def retry_task(self, run_id, task_id):
        with self.edit(run_id) as run:
            task = next(t for t in run['tasks'] if t['id'] == task_id)
            # A browser navigation failure can leave a task journalled as
            # `sent` even though no response was inspected. It is safe to
            # reconcile that exact marker after reconnecting the worker.
            retryable = task['delivery'] in ('invalid_result', 'uncertain') or (
                task['delivery'] == 'sent' and 'navigation redirected' in (task.get('error') or '').lower())
            if task['status'] != 'pending' or not retryable:
                raise Conflict('Task is not retryable.')
            task.update(delivery='not_sent', error=None, raw_reply=None,
                        reply_hash=None, reply_seen_at=None, sent_at=None,
                        stale_since=None, uncertain_since=None,
                        last_transition_at=time.time())
            self.event(run, 'task_retry_requested', {'task_id': task_id})

    def replace_pending_task(self, run_id, task_id, reason):
        """Replace a pending delivery with a fresh task identity.

        Use this when ChatGPT rejected a user turn before generation (for
        example, an oversized composer payload). A new task ID prevents the
        rejected marker in the stable chat from being mistaken for a reply.
        """
        with self.edit(run_id) as run:
            if not run['paused']:
                raise Conflict('Pause the run before replacing a pending task.')
            old = next(t for t in run['tasks'] if t['id'] == task_id)
            if old['status'] != 'pending':
                raise Conflict('Only a pending task can be replaced.')
            old['status'] = 'cancelled'
            old['error'] = reason
            replacement = self.task(run, old['kind'], self.without_skill_snapshots(run, old['inputs']),
                                    clip=old['clip'], ref=old['ref'])
            self.event(run, 'pending_task_replaced',
                       {'old_task_id': task_id, 'new_task_id': replacement['id'], 'reason': reason})
            return replacement['id']

    def repair_duplicate_images(self, run_id):
        """Remove duplicate active mappings and queue fresh reference images.

        Older transports could download the newest page-wide image for more
        than one reference. Keep the first mapping, retain every old artifact
        for audit, and regenerate later duplicate references exactly once.
        """
        repaired = []
        with self.edit(run_id) as run:
            seen = {}
            for ref, artifact_id in list(run.get('images', {}).items()):
                image_id = self.content(run, artifact_id).get('image_id')
                if not image_id or image_id not in seen:
                    seen[image_id] = ref
                    continue
                if any(t['kind'] == 'image_create' and t['ref'] == ref and t['status'] == 'pending'
                       for t in run['tasks']):
                    run['images'].pop(ref, None)
                    continue
                source = next((t for t in reversed(run['tasks'])
                               if t['kind'] == 'image_create' and t['ref'] == ref
                               and t['status'] == 'completed'), None)
                if not source:
                    continue
                run['images'].pop(ref, None)
                replacement = self.task(run, 'image_create', list(source['inputs']), ref=ref)
                replacement['replaces_duplicate_artifact'] = artifact_id
                repaired.append({'reference_id': ref, 'duplicate_of': seen[image_id],
                                 'old_artifact': artifact_id, 'task_id': replacement['id']})
            if repaired:
                run['status'] = 'creating_images'
                self.event(run, 'duplicate_reference_images_requeued', repaired)
        return repaired

    def reschedule_stalled_task(self, run_id, task_id, reason):
        """Move a delivered task that produced no response to the queue tail.

        The old marker remains an immutable delivery receipt.  A fresh task ID
        prevents a delayed response for that old turn from being attributed to
        the replacement.  This transport recovery is automatic and therefore
        must not require the run to be paused.
        """
        with self.edit(run_id) as run:
            old = next(t for t in run['tasks'] if t['id'] == task_id)
            if old['status'] != 'pending' or old['delivery'] not in ('sent', 'uncertain'):
                raise Conflict('Only a pending delivered task can be rescheduled.')
            old['status'] = 'cancelled'
            old['delivery'] = 'stalled'
            old['error'] = reason
            replacement = self.task(run, old['kind'], list(old['inputs']), clip=old['clip'], ref=old['ref'])
            replacement['transport_retries'] = old.get('transport_retries', 0) + 1
            replacement['replaces_task_id'] = old['id']
            # These two review tasks are owned by an image-create parent. A
            # transport retry gets a new task ID, so its parent pointer must
            # move with it. Without this, the eventual response is rejected as
            # stale, which itself used to create another replacement forever.
            if old['kind'] == 'reference_candidate_qa':
                bundle_id = next((artifact_id for artifact_id in old['inputs']
                                  if next((a for a in run['artifacts'] if a['id'] == artifact_id), {})
                                  .get('kind') == 'image_candidates'), None)
                if bundle_id:
                    bundle = self.content(run, bundle_id)
                    parent = next((t for t in run['tasks'] if t['id'] == bundle.get('parent_task_id')), None)
                    if parent and parent.get('candidate_qa_id') == old['id']:
                        parent['candidate_qa_id'] = replacement['id']
            elif old['kind'] == 'reference_selected_qa':
                for field in ('parent_image_task_id', 'selection_task_id', 'candidate_bundle_id',
                              'selected_candidate', 'selected_candidate_job_id'):
                    if field in old:
                        replacement[field] = old[field]
                parent = next((t for t in run['tasks']
                               if t['id'] == old.get('parent_image_task_id')), None)
                if parent and parent.get('selected_review_id') == old['id']:
                    parent['selected_review_id'] = replacement['id']
            self.event(run, 'stalled_task_rescheduled',
                       {'old_task_id': task_id, 'new_task_id': replacement['id'],
                        'transport_retry': replacement['transport_retries'], 'reason': reason})
            return replacement['id']

    def request_changes(self, run_id, gate, notes, revision):
        with self.edit(run_id, revision) as run:
            if not notes.strip():
                raise ValueError('Cần nội dung yêu cầu sửa.')
            note = self.artifact(run, 'user_revision_request', notes)
            if gate == 'screenplay' and run['status'] == 'awaiting_screenplay_approval':
                source_id = run.get('screenplay_source') or run['screenplay']
                self.task(run, 'script_edit', [source_id, note])
                run['status'] = 'script_revision'
            elif gate == 'references' and run['status'] in ('awaiting_reference_approval', 'reference_revision_limit'):
                for ref, image in run['images'].items():
                    self.task(run, 'image_create', [run['references'], image, note], ref=ref)
                run['image_cycles'] = 0
                run['status'] = 'creating_images'
            else:
                raise Conflict('Chỉ sửa tại cổng duyệt tương ứng; không đổi artifact đã khóa.')
            self.event(run, 'changes_requested', {'gate': gate, 'notes': notes})

    def control(self, run_id, action):
        with self.edit(run_id) as run:
            if action == 'pause':
                run['paused'] = True
            elif action == 'resume' and run['status'] not in TERMINAL:
                run['paused'] = False
            elif action == 'cancel':
                if any(c['status'] in ('rendering', 'render_uncertain') for c in run['clips']):
                    raise Conflict('Chờ render đang chạy hoàn tất trước khi hủy để không mất đối soát.')
                run['status'] = 'cancelled'
                for t in run['tasks']:
                    if t['status'] == 'pending':
                        t['status'] = 'cancelled'
            else:
                raise Conflict('Không thể áp dụng thao tác này.')
            self.event(run, action, '')

    def reserve_render(self, run_id):
        with self.edit(run_id) as run:
            if run['paused'] or run['status'] != 'production':
                return None
            if any(c['status'] == 'rendering' for c in run['clips']):
                return None
            for clip in sorted(run['clips'], key=lambda c: c['queue_position']):
                if clip['status'] != 'queued':
                    continue
                if any(self.clip(run, n)['status'] != 'passed' for n in clip['contract']['depends_on']):
                    continue
                if sum(a['generated'] for a in clip['attempts']) >= 10:
                    raise Conflict('Đã đạt giới hạn 10 video.')
                prompt_id = clip['prompts'][-1]
                prompt = self.content(run, prompt_id)
                image_ids = [self.content(run, run['images'][r])['image_id'] for r in prompt['reference_ids']]
                attempt = {'task_id': identity('video'), 'generated': False, 'prompt': prompt_id,
                           'images': image_ids, 'status': 'reserved', 'qa': None, 'error': None,
                           'created_at': time.time()}
                clip['attempts'].append(attempt)
                clip['status'] = 'rendering'
                self.event(run, 'render_reserved', attempt['task_id'])
                return {'task_id': attempt['task_id'], 'batch_id': clip['batch_id'], 'name': clip['batch_name'],
                        'prompt': prompt['prompt'], 'reference_images': ['library://' + i for i in image_ids],
                        'number': clip['number'], 'first': len(clip['attempts']) == 1}
            return None

    def render_submission_error(self, run_id, number, error):
        with self.edit(run_id) as run:
            clip = self.clip(run, number)
            clip['status'] = 'render_uncertain'
            clip['attempts'][-1]['error'] = str(error)[:1000]
            self.event(run, 'render_uncertain', {'clip': number, 'error': str(error)[:1000]})

    def observe_render(self, run_id, number, actual):
        with self.edit(run_id) as run:
            clip = self.clip(run, number)
            attempt = clip['attempts'][-1]
            if actual['id'] != attempt['task_id'] or actual.get('batch_id') != clip['batch_id']:
                raise Conflict('Render không thuộc task/batch đang chờ.')
            if attempt['status'] in ('completed', 'technical_failure'):
                return
            if actual['status'] == 'completed' and actual.get('video_url'):
                attempt.update(status='completed', generated=True, video_url=actual['video_url'])
                media = self.artifact(run, 'rendered_video', dict(attempt), [attempt['prompt']])
                clip['status'] = 'qa'
                self.task(run, 'video_qa', [run['screenplay'], run['plan'], media, *run['images'].values()], clip=number)
                self.event(run, 'render_completed', attempt['task_id'])
            elif actual['status'] in ('failed', 'cancelled', 'needs_recovery', 'stopped'):
                # Failure may follow upstream generation; automatic retry would risk the cap.
                attempt.update(status='uncertain', error=actual.get('error') or actual['status'])
                if actual.get('result_url') or actual.get('video_url'):
                    attempt['generated'] = True
                clip['status'] = 'render_uncertain'
            else:
                attempt['status'] = actual['status']
                clip['status'] = 'rendering'

    def retry_inspection(self, run_id, number=None):
        with self.edit(run_id) as run:
            if number is None and run['status'] == 'reference_inspection_required':
                self.task(run, 'reference_qa', [run['references'], *run['images'].values()])
                run['status'] = 'reference_qa'
            elif number is not None:
                clip = self.clip(run, number)
                if clip['status'] != 'inspection_required':
                    raise Conflict('Clip không chờ kiểm tra lại.')
                old = next(t for t in reversed(run['tasks']) if t['kind'] == 'video_qa' and t['clip'] == number)
                self.task(run, 'video_qa', old['inputs'], clip=number)
                clip['status'] = 'qa'
            else:
                raise Conflict('Không có media chờ kiểm tra lại.')

    def retry_admission(self, run_id, number):
        with self.edit(run_id) as run:
            clip = self.clip(run, number)
            if clip['status'] != 'admission_blocked' or run['status'] != 'production':
                raise Conflict('Chỉ thử lại khi gateway đã xác nhận chưa nhận video.')
            clip['status'] = 'admission_wait'
            clip['attempts'][-1]['retry_at'] = 0
            self.event(run, 'admission_retry', number)
