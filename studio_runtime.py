"""Effect runner. ChatGPT transport is deliberately explicit, never simulated live."""
import asyncio
from collections import Counter
import logging
import time
from studio_workflow import TERMINAL

log = logging.getLogger(__name__)


def choose_retry_account(accounts, previous, history):
    """Rotate away from the failed account and distribute retries across accounts."""
    eligible = [account for account in accounts if account['name'] != previous]
    counts = Counter(item.get('account') for item in history if item.get('account'))
    if previous:
        counts[previous] += 1
    eligible.sort(key=lambda account: (bool(account.get('busy')), counts[account['name']], account['name']))
    return eligible[0]['name'] if eligible else None


def should_rotate_image_account(image_studio, previous, job, history):
    """Apply the image retry policy.

    A confirmed daily image-limit is account-specific and rotates immediately.
    Every other failure stays on the same account for three retries; only the
    fourth failure is allowed to select another account.  This prevents
    transient Dola/UI/content errors from needlessly burning through the pool.
    """
    error = str((job or {}).get('error') or '')
    daily_limit = any(token in error.lower() for token in (
        'daily image limit', 'hết lượt tạo ảnh', 'image daily limit',
        '本日分の画像の上限数',
    ))
    if not daily_limit and previous and image_studio is not None:
        try:
            daily_limit = any(row.get('account') == previous
                              for row in image_studio.limited_accounts())
        except Exception:
            daily_limit = False
    if daily_limit:
        return True
    # history contains completed failures before the current one.  Keep the
    # account for failures 1..3, rotate starting with failure 4.
    return len(history) >= 3

CHARACTER_SHEET_PROMPT = ('Create a clearly separated multi-panel character reference sheet of the SAME individual. '
    'Show full body from front, left profile, back, right profile and three-quarter view; '
    'include large close-ups of the head/face from front, both three-quarter views, both side profiles and back. '
    'Keep face or mask, hair, body proportions, costume and colors consistent across every panel. '
    'Include close-up details of distinctive costume features and any accessories or weapons specified for this character. '
    'Do not invent weapons or remove masks; preserve the supplied character design and screenplay. '
    'Use neutral lighting and background so all panels are legible, no decorative text.')


def image_prompt_for_spec(spec):
    prompt = spec['image_prompt']
    if spec.get('reference_type') == 'character':
        prompt = prompt[:10000] + '\n' + CHARACTER_SHEET_PROMPT
    return prompt


class AdmissionRejected(Exception):
    """Local gateway confirms no render task was accepted or dispatched."""
    def __init__(self, status, message):
        super().__init__(str(message))
        self.status = status


class StudioRuntime:
    def __init__(self, workflow, submit_video, get_video, image_studio=None, image_library=None, export_references=None):
        self.workflow = workflow
        self.submit_video = submit_video
        self.get_video = get_video
        self.image_studio = image_studio
        self.image_library = image_library
        self.export_references = export_references
        self.job = None
        self.lock = asyncio.Lock()
        self.last_error = None
        self.image_candidate_count = 5

    async def start(self):
        if self.job is None or self.job.done():
            self.job = asyncio.create_task(self.loop())

    async def stop(self):
        if self.job:
            self.job.cancel()
            await asyncio.gather(self.job, return_exceptions=True)
            self.job = None

    async def loop(self):
        while True:
            try:
                await self.tick()
                self.last_error = None
            except asyncio.CancelledError:
                raise
            except Exception:
                self.last_error = 'Film Studio worker gặp lỗi; kiểm tra server log.'
                log.exception('Film Studio worker failed')
            await asyncio.sleep(3)

    async def tick(self):
        async with self.lock:
            for summary in self.workflow.list():
                if summary['status'] in TERMINAL:
                    continue
                run_id = summary['id']
                run = self.workflow.get(run_id)
                if self.image_studio and not run['paused']:
                    await self._image_tick(run)
                    run = self.workflow.get(run_id)
                for clip in run['clips']:
                    if clip['status'] not in ('rendering', 'render_uncertain', 'admission_wait'):
                        continue
                    attempt = clip['attempts'][-1]
                    actual = self.get_video(attempt['task_id'])
                    if actual:
                        if attempt['status'] != actual['status'] or clip['status'] == 'render_uncertain' and actual['status'] == 'completed':
                            self.workflow.observe_render(run_id, clip['number'], actual)
                    elif clip['status'] in ('rendering', 'admission_wait'):
                        # Recover only an unsubmitted local reservation, using its exact ID.
                        prompt = self.workflow.content(run, attempt['prompt'])
                        payload = dict(task_id=attempt['task_id'], batch_id=clip['batch_id'], name=clip['batch_name'],
                                       prompt=prompt['prompt'], reference_images=['library://' + i for i in attempt['images']],
                                       number=clip['number'], first=len(clip['attempts']) == 1,
                                       reference_aliases=prompt['reference_ids'])
                        other_rendering = any(c['number'] != clip['number'] and c['status'] == 'rendering' for c in run['clips'])
                        if not run['paused'] and not other_rendering and attempt.get('retry_at', 0) <= time.time():
                            await self._send(run_id, payload)
                payload = self.workflow.reserve_render(run_id)
                if payload:
                    fresh = self.workflow.get(run_id)
                    clip = self.workflow.clip(fresh, payload['number'])
                    payload['reference_aliases'] = self.workflow.content(fresh, clip['prompts'][-1])['reference_ids']
                    await self._send(run_id, payload)

    async def _image_tick(self, run):
        """Admit and reconcile all independent, unsent Image Studio tasks."""
        tasks = [t['id'] for t in run['tasks'] if t['kind'] == 'image_create'
                 and t['status'] == 'pending' and t['delivery'] == 'not_sent']
        for task_id in tasks:
            current = self.workflow.get(run['id'])
            if current['paused']:
                break
            task = next((t for t in current['tasks'] if t['id'] == task_id), None)
            if not task or task['status'] != 'pending' or task['delivery'] != 'not_sent':
                continue
            try:
                if self.image_candidate_count == 1 or task.get('image_retry_intent'):
                    await self._image_task_tick(current, task)
                else:
                    await self._image_candidates_tick(current, task)
            except Exception:
                log.exception('Film Studio image task %s failed to reconcile', task_id)

    async def _image_candidates_tick(self, run, task):
        from image_studio import ImageRequest
        if task.get('candidate_qa_id'):
            return
        if not task.get('candidate_slots'):
            with self.workflow.edit(run['id']) as current:
                fresh = next(t for t in current['tasks'] if t['id'] == task['id'])
                if not fresh.get('candidate_slots'):
                    fresh['candidate_slots'] = [dict(index=i, job_id=(fresh.get('image_job_id') if i == 0
                        and fresh.get('candidate_round', 0) == 0 else None), attempt=0, account='', retry_at=0)
                        for i in range(self.image_candidate_count)]
        for index in range(self.image_candidate_count):
            current = self.workflow.get(run['id'])
            fresh = next(t for t in current['tasks'] if t['id'] == task['id'])
            if current['paused'] or fresh['status'] != 'pending' or fresh.get('candidate_qa_id'):
                return
            slot = fresh['candidate_slots'][index]
            if slot.get('retry_at', 0) > time.time():
                continue
            job_id = slot.get('job_id')
            if not job_id and slot.get('account'):
                # A slot may retain the account that failed admission while
                # the operator enables a different account. Rebind it before
                # creating a new job, but only to currently available accounts.
                available = self.image_studio.accounts()
                if not any(a.get('name') == slot.get('account') for a in available):
                    rebound = choose_retry_account(available, slot.get('account'), slot.get('history', []))
                    if rebound:
                        with self.workflow.edit(run['id']) as state:
                            target = next(t for t in state['tasks'] if t['id'] == task['id'])['candidate_slots'][index]
                            target.update(account=rebound, retry_at=0, error=None)
                        slot['account'] = rebound
            if not job_id:
                envelope = self.workflow.envelope(run['id'], task['id'])
                spec = next(item['content'] for item in envelope['inputs'] if item['kind'] == 'reference_spec')
                refs = [item['content']['image_id'] for item in envelope['inputs']
                        if item['kind'] == 'image_create' and item['content'].get('image_id')][:6]
                prompt = image_prompt_for_spec(spec)
                if refs:
                    prompt += ' Use the supplied image only for visual continuity. Correct differences from the requested specification.'
                for item in envelope['inputs']:
                    if item['kind'] == 'reference_qa':
                        verdict = next((v for v in item['content']['references'] if v['reference_id'] == task['ref']), None)
                        if verdict and verdict.get('repair'):
                            prompt += '\nCorrection requested: ' + verdict['repair']
                if fresh.get('candidate_repair'):
                    prompt += '\nSelection review: ' + fresh['candidate_repair']
                request = ImageRequest(prompt=prompt[:12000], model='Seedream 5.0 Pro', ratio='16:9', style='auto',
                    account=slot.get('account', ''), references=refs,
                    request_id=f"filmstudio-{task['id']}-r{fresh.get('candidate_round', 0)}-c{index}-a{slot.get('attempt', 0)}")
                try:
                    job = self.image_studio.create(request)
                except Exception as exc:
                    with self.workflow.edit(run['id']) as state:
                        target = next(t for t in state['tasks'] if t['id'] == task['id'])['candidate_slots'][index]
                        history = target.setdefault('history', [])
                        history.append({'job_id': None, 'account': target.get('account') or '',
                                        'error': ('Admission: ' + str(exc))[:600], 'at': time.time()})
                        previous = target.get('account') or ''
                        next_account = choose_retry_account(self.image_studio.accounts(), previous, history)
                        if next_account:
                            target.update(account=next_account, attempt=target.get('attempt', 0) + 1,
                                          error=None, retry_at=0)
                        else:
                            target.update(error='Admission: ' + str(exc)[:500], retry_at=time.time()+30)
                    continue
                job_id = job['id']
                with self.workflow.edit(run['id']) as state:
                    target = next(t for t in state['tasks'] if t['id'] == task['id'])['candidate_slots'][index]
                    target.update(job_id=job_id, error=None, retry_at=0)
                    self.workflow.event(state, 'image_candidate_submitted', {'task': task['id'], 'candidate': index+1, 'job_id': job_id})
            job = self.image_studio.get(job_id)
            if job['status'] == 'completed' and not slot.get('image_id'):
                path = self.image_studio.output_path(job_id, 0)
                image = self.image_library.save(path.read_bytes(), path.name, 20 * 1024 * 1024)
                with self.workflow.edit(run['id']) as state:
                    target = next(t for t in state['tasks'] if t['id'] == task['id'])['candidate_slots'][index]
                    target.update(image_id=image['id'], error=None)
            elif job['status'] in ('failed', 'interrupted'):
                previous = job.get('account') or job.get('requested_account')
                history = slot.get('history', [])
                rotate = should_rotate_image_account(self.image_studio, previous, job, history)
                with self.workflow.edit(run['id']) as state:
                    target = next(t for t in state['tasks'] if t['id'] == task['id'])['candidate_slots'][index]
                    history = target.setdefault('history', [])
                    history.append({'job_id': job_id, 'account': previous,
                                    'error': (job.get('error') or '')[:600]})
                    next_account = (choose_retry_account(self.image_studio.accounts(), previous, history)
                                    if rotate else previous)
                    if not next_account:
                        target.update(error='No other account available for retry.' if rotate
                                      else (job.get('error') or 'Image generation failed')[:600], retry_at=time.time()+30)
                    else:
                        target.update(job_id=None, account=next_account, attempt=target.get('attempt', 0)+1,
                                      error=None, retry_at=0)
        current = self.workflow.get(run['id'])
        fresh = next(t for t in current['tasks'] if t['id'] == task['id'])
        if len(fresh.get('candidate_slots', [])) == self.image_candidate_count and all(s.get('image_id') for s in fresh['candidate_slots']):
            envelope = self.workflow.envelope(run['id'], task['id'])
            spec = next(i['content'] for i in envelope['inputs'] if i['kind'] == 'reference_spec')
            repair_notes = [v.get('repair') for i in envelope['inputs'] if i['kind'] == 'reference_qa'
                            for v in i['content']['references'] if v['reference_id'] == task['ref'] and v.get('repair')]
            if fresh.get('candidate_repair'):
                repair_notes.append(fresh['candidate_repair'])
            spec = {**spec, 'repair_notes': repair_notes}
            with self.workflow.edit(run['id']) as state:
                parent = next(t for t in state['tasks'] if t['id'] == task['id'])
                if parent['status'] != 'pending' or parent.get('candidate_qa_id'):
                    return
                spec_id = self.workflow.artifact(state, 'reference_spec', spec, parent['inputs'])
                candidates_id = self.workflow.artifact(state, 'image_candidates', {'parent_task_id': parent['id'],
                    'reference_id': parent['ref'], 'candidates': [{'image_id': s['image_id'], 'job_id': s['job_id']}
                                                                for s in parent['candidate_slots']]}, parent['inputs'])
                # Explicitly named identity references are sent after the five
                # candidates, so their order remains unambiguous to the reviewer.
                spec_text = ' '.join(str(spec.get(k, '')) for k in ('description', 'image_prompt'))
                base_refs = [artifact for ref, artifact in state['images'].items()
                             if ref != parent['ref'] and ref in spec_text]
                qa = self.workflow.task(state, 'reference_candidate_qa', [spec_id, candidates_id, *base_refs], ref=parent['ref'])
                parent['candidate_qa_id'] = qa['id']
                self.workflow.event(state, 'image_candidates_ready', {'task': parent['id'], 'qa_task': qa['id']})

    async def _image_task_tick(self, run, task):
        """Journal each job separately so restarts do not duplicate submissions."""
        from image_studio import ImageRequest
        job_id = task.get('image_job_id')
        intent = task.get('image_retry_intent')
        if intent:
            if task.get('image_retry_at', 0) > time.time():
                return
            source = self.image_studio.get(intent['source_job'])
            request = ImageRequest(prompt=source['prompt'], model=source['model'], ratio=source['ratio'],
                                   style=source['style'], account=intent['account'],
                                   references=source['references'], request_id=intent['request_id'])
            try:
                job = self.image_studio.create(request)
            except Exception as exc:
                with self.workflow.edit(run['id']) as current:
                    fresh = next(t for t in current['tasks'] if t['id'] == task['id'])
                    fresh.update(error='Image Studio retry admission: ' + str(exc)[:600],
                                 image_retry_at=time.time()+30)
                return
            job_id = job['id']
            with self.workflow.edit(run['id']) as current:
                fresh = next(t for t in current['tasks'] if t['id'] == task['id'])
                fresh.update(image_job_id=job_id, image_retry_intent=None, image_retry_at=0, error=None)
                self.workflow.event(current, 'image_studio_retry_submitted',
                                    {'task': task['id'], 'job_id': job_id, 'account': intent['account'],
                                     'previous_job_id': intent['source_job']})
        if not job_id:
            if task.get('image_retry_at', 0) > time.time():
                return
            envelope = self.workflow.envelope(run['id'], task['id'])
            spec = next(item['content'] for item in envelope['inputs'] if item['kind'] == 'reference_spec')
            refs = [item['content']['image_id'] for item in envelope['inputs']
                    if item['kind'] == 'image_create' and item['content'].get('image_id')][:6]
            prompt = image_prompt_for_spec(spec)
            if refs:
                prompt += (' Use the supplied image only for visual continuity. '
                           'Correct any differences from the requested specification.')
            request = ImageRequest(prompt=prompt, model='Seedream 5.0 Pro', ratio='16:9',
                                   style='auto', account='', references=refs,
                                   request_id='filmstudio-' + task['id'])
            try:
                job = self.image_studio.create(request)
            except Exception as exc:
                with self.workflow.edit(run['id']) as current:
                    fresh = next(t for t in current['tasks'] if t['id'] == task['id'])
                    fresh.update(error='Image Studio admission: ' + str(exc)[:600],
                                 image_retry_at=time.time()+60)
                return
            job_id = job['id']
            with self.workflow.edit(run['id']) as current:
                fresh = next(t for t in current['tasks'] if t['id'] == task['id'])
                fresh.update(image_job_id=job_id, image_provider='image_studio', error=None)
                self.workflow.event(current, 'image_studio_submitted',
                                    {'task': task['id'], 'job_id': job_id, 'reference_id': task['ref']})
        job = self.image_studio.get(job_id)
        if job['status'] == 'completed':
            path = self.image_studio.output_path(job_id, 0)
            item = self.image_library.save(path.read_bytes(), path.name, 20 * 1024 * 1024)
            with self.workflow.edit(run['id']) as current:
                fresh = next(t for t in current['tasks'] if t['id'] == task['id'])
                if fresh['status'] != 'pending':
                    return
                receipts = [a for a in current['artifacts'] if a['kind'] == 'image_upload'
                            and a['content'].get('task_id') == task['id']]
                if not receipts:
                    self.workflow.artifact(current, 'image_upload', {
                        'task_id': task['id'], 'image_id': item['id'],
                        'image_studio_job_id': job_id}, fresh['inputs'])
            self.workflow.submit(run['id'], task['id'], {'image_id': item['id']})
            if self.export_references:
                self.export_references(run['id'])
        elif job['status'] in ('failed', 'interrupted'):
            if task.get('image_retry_at', 0) > time.time():
                return
            previous = job.get('account') or job.get('requested_account')
            history_before = task.get('image_job_history', [])
            rotate = should_rotate_image_account(self.image_studio, previous, job, history_before)
            with self.workflow.edit(run['id']) as current:
                fresh = next(t for t in current['tasks'] if t['id'] == task['id'])
                history = fresh.setdefault('image_job_history', [])
                history.append({'job_id': job_id, 'account': previous, 'status': job['status'],
                                'error': (job.get('error') or '')[:600], 'at': time.time()})
                next_account = (choose_retry_account(self.image_studio.accounts(), previous, history)
                                if rotate else previous)
                if not next_account:
                    fresh.update(error='No other account available for retry.' if rotate
                                  else (job.get('error') or 'Image generation failed')[:600],
                                  image_retry_at=time.time()+30)
                    return
                fresh['image_retry_intent'] = {'source_job': job_id, 'account': next_account,
                    'request_id': 'filmstudio-' + task['id'] + '-retry-' + str(len(history))}
                fresh.update(error=None, image_retry_at=0)
                self.workflow.event(current, 'image_studio_retry_planned',
                                    {'task': task['id'], 'job_id': job_id, 'account': next_account})
        elif job['status'] == 'cancelled':
            with self.workflow.edit(run['id']) as current:
                fresh = next(t for t in current['tasks'] if t['id'] == task['id'])
                fresh['error'] = 'Image Studio cancelled: ' + (job.get('error') or '')[:600]

    async def _send(self, run_id, payload):
        try:
            await self.submit_video(payload)
            with self.workflow.edit(run_id) as run:
                clip = self.workflow.clip(run, payload['number'])
                clip['status'] = 'rendering'
                clip['attempts'][-1]['retry_at'] = 0
                clip['attempts'][-1]['error'] = None
        except asyncio.CancelledError:
            raise  # Reservation stays durable; next process reconciles exact task ID.
        except AdmissionRejected as exc:
            with self.workflow.edit(run_id) as run:
                clip = self.workflow.clip(run, payload['number'])
                clip['status'] = 'admission_wait' if exc.status in (429, 503) else 'admission_blocked'
                clip['attempts'][-1].update(error=str(exc)[:1000], retry_at=time.time()+60)
                self.workflow.event(run, 'admission_rejected', {'clip': payload['number'], 'status': exc.status})
        except Exception as exc:
            self.workflow.render_submission_error(run_id, payload['number'], str(exc))

    def status(self):
        return {'render_worker_running': bool(self.job and not self.job.done()),
                'chatgpt_transport': 'not_connected', 'last_error': self.last_error,
                'blockers': ['Chưa có kết nối ChatGPT web được kiểm chứng.',
                             'Chưa kiểm chứng khả năng đọc video/âm thanh trong ChatGPT web.']}
