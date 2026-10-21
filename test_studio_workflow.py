import asyncio
import tempfile
import unittest
from pathlib import Path

from studio_workflow import Workflow, Conflict
from studio_runtime import StudioRuntime, AdmissionRejected, choose_retry_account, should_rotate_image_account


class WorkflowTests(unittest.TestCase):
    def test_retry_distributes_accounts_and_prefers_idle(self):
        accounts = [{'name': 'a'}, {'name': 'b'}, {'name': 'c'}, {'name': 'd', 'busy': True}]
        self.assertEqual(choose_retry_account(accounts, 'a', []), 'b')
        self.assertEqual(choose_retry_account(accounts, 'b', [{'account': 'a'}]), 'c')
        self.assertEqual(choose_retry_account(accounts, 'c', [{'account': 'a'}, {'account': 'b'}]), 'a')
        self.assertIsNone(choose_retry_account([{'name': 'a'}], 'a', []))

    def test_image_retry_keeps_account_for_three_failures(self):
        class Studio:
            def limited_accounts(self): return []
        job = {'error': 'temporary Dola error'}
        self.assertFalse(should_rotate_image_account(Studio(), 'a', job, []))
        self.assertFalse(should_rotate_image_account(Studio(), 'a', job, [{'account': 'a'}, {'account': 'a'}]))
        self.assertTrue(should_rotate_image_account(Studio(), 'a', job,
                                                    [{'account': 'a'}, {'account': 'a'}, {'account': 'a'}]))

    def test_image_daily_limit_rotates_immediately(self):
        class Studio:
            def limited_accounts(self): return []
        self.assertTrue(should_rotate_image_account(Studio(), 'a',
                                                    {'error': 'daily image limit reached'}, []))

    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.addCleanup(self.temp.cleanup)
        self.path = Path(self.temp.name) / 'studio.db'
        self.flow = Workflow(self.path)
        self.run = self.flow.create('Test film', 'A enters. B leaves.', 'request1', 'https://chatgpt.com/g/g-p-test/project')['id']

    def task(self, kind, output, clip=None):
        if kind == 'reference_plan':
            output = {**output, 'references': [{**{'reference_type': 'other'}, **ref}
                                                for ref in output['references']]}
        run = self.flow.get(self.run)
        task = next(t for t in run['tasks'] if t['kind'] == kind and t['status'] == 'pending' and (clip is None or t['clip'] == clip))
        self.flow.submit(self.run, task['id'], output)
        return task['id']

    def test_image_studio_job_is_reused_and_result_advances_to_qa(self):
        self.task('script_review', {'verdict': 'PASS', 'summary': 'Reviewed', 'issues': []})
        run = self.flow.get(self.run)
        self.flow.approve(self.run, 'screenplay', run['screenplay'], run['revision'])
        self.task('production_plan', {'clips': [dict(number=1, chapter=1, source_excerpt='A enters.',
            action='A enters.', dialogue='', color='warm amber', start_state='outside',
            end_state='inside', depends_on=[])], 'coverage_notes': 'Test coverage'})
        self.task('reference_plan', {'references': [{'id': 'A', 'description': 'A',
            'image_prompt': 'A warm amber scene'}], 'clip_references': {'1': ['A']}})
        file = self.path.parent / 'result.png'
        file.write_bytes(b'image content')
        class FakeStudio:
            def __init__(self): self.creates = []; self.status = 'running'
            def create(self, req):
                self.creates.append(req.request_id)
                return {'id': 'job1', 'status': self.status}
            def get(self, job): return {'id': job, 'status': self.status}
            def output_path(self, job, index): return file
        class FakeLibrary:
            def save(self, data, name, limit): return {'id': 'a'*64}
        studio = FakeStudio()
        runtime = StudioRuntime(self.flow, lambda _: None, lambda _: None, studio, FakeLibrary())
        runtime.image_candidate_count = 1
        asyncio.run(runtime.tick())
        asyncio.run(runtime.tick())
        self.assertEqual(len(studio.creates), 1)
        studio.status = 'completed'
        asyncio.run(runtime.tick())
        run = self.flow.get(self.run)
        self.assertEqual(run['status'], 'reference_qa')
        self.assertEqual(len([a for a in run['artifacts'] if a['kind'] == 'image_upload']), 1)
        self.assertTrue(any(t['kind'] == 'reference_qa' and t['status'] == 'pending' for t in run['tasks']))

    def test_image_studio_submits_independent_jobs_without_waiting(self):
        self.task('script_review', {'verdict': 'PASS', 'summary': 'Reviewed', 'issues': []})
        run = self.flow.get(self.run)
        self.flow.approve(self.run, 'screenplay', run['screenplay'], run['revision'])
        self.task('production_plan', {'clips': [dict(number=1, chapter=1, source_excerpt='A enters.',
            action='A enters.', dialogue='', color='warm amber', start_state='outside',
            end_state='inside', depends_on=[])], 'coverage_notes': 'Test coverage'})
        self.task('reference_plan', {'references': [
            {'id': name, 'description': name, 'image_prompt': 'Reference ' + name}
            for name in ('A', 'B', 'C')], 'clip_references': {'1': ['A', 'B', 'C']}})
        class FakeStudio:
            def __init__(self): self.created = []
            def create(self, req):
                self.created.append(req.request_id)
                return {'id': req.request_id, 'status': 'running'}
            def get(self, job): return {'id': job, 'status': 'running'}
        studio = FakeStudio()
        runtime = StudioRuntime(self.flow, lambda _: None, lambda _: None, studio)
        runtime.image_candidate_count = 1
        asyncio.run(runtime.tick())
        asyncio.run(runtime.tick())
        self.assertEqual(len(studio.created), 3)
        pending = [t for t in self.flow.get(self.run)['tasks'] if t['kind'] == 'image_create']
        self.assertEqual(len({t['image_job_id'] for t in pending}), 3)

    def test_failed_image_retries_on_another_account_with_stable_request(self):
        self.task('script_review', {'verdict': 'PASS', 'summary': 'Reviewed', 'issues': []})
        run = self.flow.get(self.run)
        self.flow.approve(self.run, 'screenplay', run['screenplay'], run['revision'])
        self.task('production_plan', {'clips': [dict(number=1, chapter=1, source_excerpt='A enters.',
            action='A enters.', dialogue='', color='warm amber', start_state='outside',
            end_state='inside', depends_on=[])], 'coverage_notes': 'Test coverage'})
        self.task('reference_plan', {'references': [{'id': 'A', 'description': 'A',
            'image_prompt': 'Reference A'}], 'clip_references': {'1': ['A']}})
        class FakeStudio:
            def __init__(self): self.created = []; self.other_available = False
            def accounts(self): return [{'name': 'old'}] + ([{'name': 'new'}] if self.other_available else [])
            def create(self, req):
                self.created.append((req.request_id, req.account))
                return {'id': 'job1' if len(self.created) == 1 else 'job2'}
            def get(self, job):
                return {'id': job, 'status': 'failed' if job == 'job1' else 'running',
                        'account': 'old', 'requested_account': '', 'error': 'failure',
                        'prompt': 'Reference A', 'model': 'Seedream 5.0 Pro',
                        'ratio': '16:9', 'style': 'auto', 'references': []}
        studio = FakeStudio()
        runtime = StudioRuntime(self.flow, lambda _: None, lambda _: None, studio)
        runtime.image_candidate_count = 1
        asyncio.run(runtime.tick())
        self.assertEqual(len(studio.created), 1)
        task = next(t for t in self.flow.get(self.run)['tasks'] if t['kind'] == 'image_create')
        self.assertEqual(task['image_job_history'][0]['job_id'], 'job1')
        self.assertEqual(task['image_retry_intent']['account'], 'old')
        studio.other_available = True
        with self.flow.edit(self.run) as run:
            next(t for t in run['tasks'] if t['kind'] == 'image_create')['image_retry_at'] = 0
        asyncio.run(runtime.tick())
        asyncio.run(runtime.tick())
        self.assertEqual(studio.created[1][1], 'old')
        task = next(t for t in self.flow.get(self.run)['tasks'] if t['kind'] == 'image_create')
        self.assertEqual(task['image_job_id'], 'job2')
        self.assertEqual(task['image_job_history'][0]['job_id'], 'job1')

    def test_five_candidates_are_selected_by_agent_before_reference_qa(self):
        self.task('script_review', {'verdict': 'PASS', 'summary': 'Reviewed', 'issues': []})
        run = self.flow.get(self.run)
        self.flow.approve(self.run, 'screenplay', run['screenplay'], run['revision'])
        self.task('production_plan', {'clips': [dict(number=1, chapter=1, source_excerpt='A enters.',
            action='A enters.', dialogue='', color='warm amber', start_state='outside',
            end_state='inside', depends_on=[])], 'coverage_notes': 'Test coverage'})
        self.task('reference_plan', {'references': [{'id': 'A', 'description': 'A',
            'image_prompt': 'Reference A', 'reference_type': 'character'}], 'clip_references': {'1': ['A']}})
        class FakeStudio:
            def __init__(self): self.ids = []
            def create(self, req):
                self_last_prompts.append(req.prompt)
                job = str(len(self.ids)); self.ids.append(job); return {'id': job}
            def get(self, job): return {'id': job, 'status': 'completed'}
            def output_path(self, job, index):
                path = self_dir / (job + '.png'); path.write_bytes(job.encode()); return path
        class FakeLibrary:
            def save(self, data, name, limit): return {'id': str(int(data)) * 64}
        self_dir = self.path.parent
        self_last_prompts = []
        studio = FakeStudio()
        runtime = StudioRuntime(self.flow, lambda _: None, lambda _: None, studio, FakeLibrary())
        asyncio.run(runtime.tick())
        asyncio.run(runtime.tick())
        self.assertEqual(len(studio.ids), 5)
        self.assertTrue(all('multi-panel character reference sheet' in p for p in self_last_prompts))
        run = self.flow.get(self.run)
        self.assertEqual(run['status'], 'creating_images')
        select = next(t for t in run['tasks'] if t['kind'] == 'reference_candidate_qa')
        with self.assertRaises(ValueError):
            self.flow.submit(self.run, select['id'], {'verdict': 'PASS', 'image_inspected': False,
                'selected_candidate': 3, 'evidence': 'not seen'})
        with self.assertRaises(ValueError):
            self.flow.submit(self.run, select['id'], {'verdict': 'FAIL', 'image_inspected': False,
                'selected_candidate': None, 'evidence': 'not seen'})
        previous_inputs = select['inputs'][:]
        self.flow.submit(self.run, select['id'], {'verdict': 'NOT_INSPECTED', 'image_inspected': False,
            'selected_candidate': None, 'evidence': 'Images unavailable', 'repair': 'Provide images'})
        asyncio.run(runtime.tick())
        self.assertEqual(len(studio.ids), 5)
        run = self.flow.get(self.run)
        select = next(t for t in run['tasks'] if t['kind'] == 'reference_candidate_qa' and t['status'] == 'pending')
        self.assertEqual(select['inputs'], previous_inputs)
        self.assertGreater(select['next_retry_at'], 0)
        self.flow.submit(self.run, select['id'], {'verdict': 'FAIL', 'image_inspected': True,
            'selected_candidate': None, 'evidence': 'No candidate fits', 'repair': 'Fix appearance'})
        asyncio.run(runtime.tick())
        run = self.flow.get(self.run)
        self.assertEqual(len(studio.ids), 10)
        self.assertTrue(all('Fix appearance' in p for p in self_last_prompts[5:]))
        select = next(t for t in run['tasks'] if t['kind'] == 'reference_candidate_qa' and t['status'] == 'pending')
        checks = {'full_body_front_side_back': True, 'face_front_three_quarter_side_back': True,
                  'consistent_identity_and_costume': True, 'distinctive_details_and_props': True}
        self.flow.submit(self.run, select['id'], {'verdict': 'PASS', 'image_inspected': True,
            'selected_candidate': 3, 'evidence': 'Inspected the five labelled images', 'character_sheet_checks': checks})
        run = self.flow.get(self.run)
        selected_qa = next(t for t in run['tasks'] if t['kind'] == 'reference_selected_qa' and t['status'] == 'pending')
        self.flow.submit(self.run, selected_qa['id'], {'reference_id': 'A', 'verdict': 'PASS',
            'image_inspected': True, 'evidence': 'Independent inspection of candidate 3',
            'character_sheet_checks': checks})
        run = self.flow.get(self.run)
        self.assertEqual(self.flow.content(run, run['images']['A'])['image_id'], '7' * 64)
        self.assertEqual(run['status'], 'reference_qa')
        qa = next(t for t in run['tasks'] if t['kind'] == 'reference_qa' and t['status'] == 'pending')
        with self.assertRaises(ValueError):
            self.flow.submit(self.run, qa['id'], {'references': [{'reference_id': 'A', 'verdict': 'PASS',
                'image_inspected': True, 'evidence': 'Seen'}]})
        self.flow.submit(self.run, qa['id'], {'references': [{'reference_id': 'A', 'verdict': 'PASS',
            'image_inspected': True, 'evidence': 'Seen', 'character_sheet_checks': checks}]})

    def ready(self, count=1, dialogue='', dependent=False):
        self.task('script_review', {'verdict': 'PASS', 'summary': 'Reviewed', 'issues': []})
        run = self.flow.get(self.run)
        self.flow.approve(self.run, 'screenplay', run['screenplay'], run['revision'])
        clips = [dict(number=n, chapter=1, source_excerpt='A enters.', action='A enters.', dialogue=dialogue,
                      color='warm amber', start_state='outside', end_state='inside', depends_on=[n-1] if dependent and n > 1 else []) for n in range(1, count+1)]
        self.task('production_plan', {'clips': clips, 'coverage_notes': 'Test coverage'})
        self.task('reference_plan', {'references': [{'id': 'A', 'description': 'A', 'image_prompt': 'A warm amber'}],
                                     'clip_references': {str(n): ['A'] for n in range(1, count+1)}})
        self.task('image_create', {'image_id': 'a'*64})
        self.task('reference_qa', {'references': [{'reference_id': 'A', 'verdict': 'PASS', 'image_inspected': True, 'evidence': 'Actual image seen', 'repair': ''}]})
        run = self.flow.get(self.run)
        self.flow.approve(self.run, 'references', run['reference_qa'], run['revision'])
        for n in range(1, count+1):
            if not dependent or n == 1:
                self.task('prompt', self.prompt(), n)

    def prompt(self, text='A enters in warm amber light.'):
        return {'prompt': text, 'reference_ids': ['A'], 'story_preserved': True, 'color_preserved': True, 'skill_sources': {},
                'reference_audit': 'A confirmed', 'shot_plan': 'Static medium shot', 'production_review': 'One action',
                'final_qa': 'Contract preserved', 'next_video_handoff': 'A indoors'}

    def render(self):
        reservation = self.flow.reserve_render(self.run)
        self.assertIsNotNone(reservation)
        self.flow.observe_render(self.run, reservation['number'], {'id': reservation['task_id'], 'batch_id': reservation['batch_id'],
                                                                 'status': 'completed', 'video_url': '/videos/test.mp4'})
        return reservation

    def qa(self, passed=False, clip=1):
        return self.task('video_qa', {'verdict': 'PASS' if passed else 'FAIL', 'video_inspected': True,
                                    'audio_inspected': True, 'screenplay_match': passed, 'color_match': True,
                                    'evidence': ['00:04 A enters' if passed else '00:04 A missing'], 'repair': 'Keep A visible'}, clip)

    def test_cap_ten_same_batch_and_repair_after_five(self):
        self.ready()
        batches, prompts = [], []
        for n in range(10):
            item = self.render(); batches.append(item['batch_id']); prompts.append(item['prompt'])
            self.qa()
            if n == 4:
                self.assertIsNone(self.flow.reserve_render(self.run))
                self.task('prompt_repair', self.prompt('A enters visibly, warm amber light.'))
                self.flow = Workflow(self.path)  # budget survives restart
        run = self.flow.get(self.run)
        self.assertEqual(len(set(batches)), 1)
        self.assertEqual(len(set(prompts[:5])), 1)
        self.assertEqual(len(set(prompts[5:])), 1)
        self.assertNotEqual(prompts[0], prompts[-1])
        self.assertEqual(run['clips'][0]['status'], 'exhausted')
        self.assertEqual(sum(a['generated'] for a in run['clips'][0]['attempts']), 10)
        self.assertIsNone(self.flow.reserve_render(self.run))

    def test_stop_on_pass_and_duplicate_result_idempotent(self):
        self.ready(); reservation = self.render(); task_id = self.qa(True)
        self.assertIsNone(self.flow.reserve_render(self.run))
        run = self.flow.get(self.run)
        self.assertEqual(run['status'], 'completed')
        self.assertEqual(run['clips'][0]['selected_task_id'], reservation['task_id'])
        task = next(t for t in run['tasks'] if t['id'] == task_id)
        self.flow.submit(self.run, task_id, self.flow.content(run, task['output']))
        self.assertEqual(self.flow.get(self.run)['revision'], run['revision'])

    def test_approval_version_and_single_active(self):
        with self.assertRaises(Conflict):
            self.flow.create('Another', 'text', 'other', '')
        first = self.flow.get(self.run)
        self.task('script_review', {'verdict': 'PASS', 'summary': 'Review', 'issues': []})
        with self.assertRaises(Conflict):
            self.flow.approve(self.run, 'screenplay', first['screenplay'], first['revision'])
        self.assertEqual(self.flow.create('Test film', 'A enters. B leaves.', 'request1', 'https://chatgpt.com/g/g-p-test/project')['id'], self.run)

    def test_missing_audio_cannot_pass_or_trigger_new_render(self):
        self.ready(dialogue='Hello.'); self.render()
        with self.assertRaises(ValueError):
            self.task('video_qa', {'verdict': 'PASS', 'video_inspected': True, 'audio_inspected': False,
                                 'screenplay_match': True, 'color_match': True, 'evidence': ['frames only'], 'repair': ''})
        self.task('video_qa', {'verdict': 'NOT_INSPECTED', 'video_inspected': False, 'audio_inspected': False,
                             'screenplay_match': None, 'color_match': None, 'evidence': ['No video access'], 'repair': ''})
        self.assertIsNone(self.flow.reserve_render(self.run))
        self.flow.retry_inspection(self.run, 1)
        self.qa(True)
        self.assertEqual(len(self.flow.get(self.run)['clips'][0]['attempts']), 1)

    def test_agent_mode_retries_uninspected_video_without_new_render(self):
        self.ready(dialogue='Hello.')
        self.flow.set_approval_mode(self.run, 'agent')
        self.render()
        self.task('video_qa', {'verdict': 'NOT_INSPECTED', 'video_inspected': False,
                              'audio_inspected': False, 'screenplay_match': None,
                              'color_match': None, 'evidence': ['Temporary media access failure'],
                              'repair': ''})
        run = self.flow.get(self.run)
        self.assertEqual(run['clips'][0]['status'], 'qa')
        self.assertEqual(len(run['clips'][0]['attempts']), 1)
        self.assertTrue(any(t['kind'] == 'video_qa' and t['status'] == 'pending'
                            for t in run['tasks']))

    def test_agent_mode_reference_qa_retries_without_three_cycle_stop(self):
        self.task('script_review', {'verdict': 'PASS', 'summary': 'Reviewed', 'issues': []})
        run = self.flow.get(self.run)
        self.flow.approve(self.run, 'screenplay', run['screenplay'], run['revision'])
        self.task('production_plan', {'clips': [dict(number=1, chapter=1, source_excerpt='A enters.',
                  action='A enters.', dialogue='', color='warm amber', start_state='outside',
                  end_state='inside', depends_on=[])], 'coverage_notes': 'Complete'})
        self.task('reference_plan', {'references': [{'id': 'A', 'description': 'A', 'image_prompt': 'A'}],
                                     'clip_references': {'1': ['A']}})
        self.task('image_create', {'image_id': 'a' * 64})
        self.flow.set_approval_mode(self.run, 'agent')
        with self.flow.edit(self.run) as current:
            current['image_cycles'] = 3
        self.task('reference_qa', {'references': [{'reference_id': 'A', 'verdict': 'FAIL',
                  'image_inspected': True, 'evidence': 'Wrong identity', 'repair': 'Regenerate'}]})
        run = self.flow.get(self.run)
        self.assertEqual(run['status'], 'creating_images')
        self.assertEqual(run['image_cycles'], 4)
        self.assertTrue(any(t['kind'] == 'image_create' and t['status'] == 'pending'
                            for t in run['tasks']))

    def test_queue_tail_and_dependencies(self):
        self.ready(2)
        for _ in range(5):
            one = self.render(); self.assertEqual(one['number'], 1); self.qa(False, 1)
        self.task('prompt_repair', self.prompt('Repaired prompt'), 1)
        two = self.render(); self.assertEqual(two['number'], 2); self.qa(True, 2)
        self.assertEqual(self.render()['number'], 1)

    def test_pause_and_uncertain_submission_no_duplicate(self):
        self.ready(); self.flow.control(self.run, 'pause')
        self.assertIsNone(self.flow.reserve_render(self.run))
        self.flow.control(self.run, 'resume')
        item = self.flow.reserve_render(self.run)
        self.flow.render_submission_error(self.run, item['number'], 'connection interrupted')
        self.flow = Workflow(self.path)
        self.assertIsNone(self.flow.reserve_render(self.run))
        self.flow.observe_render(self.run, 1, {'id': item['task_id'], 'batch_id': item['batch_id'], 'status': 'completed', 'video_url': '/videos/test.mp4'})
        self.qa(True)
        self.assertEqual(len(self.flow.get(self.run)['clips'][0]['attempts']), 1)

    def test_idle_tick_does_not_invalidate_approval(self):
        self.task('script_review', {'verdict': 'PASS', 'summary': 'Review', 'issues': []})
        before = self.flow.get(self.run)['revision']
        self.assertIsNone(self.flow.reserve_render(self.run))
        self.assertEqual(self.flow.get(self.run)['revision'], before)

    def test_script_editor_cannot_replace_full_screenplay_with_proposal(self):
        source = '\n'.join(f'**{n}. SCENE {n}**\nAction and dialogue for scene {n}.' for n in range(1, 5))
        path = Path(self.temp.name) / 'complete.db'
        flow = Workflow(path)
        run_id = flow.create('Complete film', source, 'complete', 'https://chatgpt.com/project', 'agent')['id']
        run = flow.get(run_id)
        review = next(t for t in run['tasks'] if t['kind'] == 'script_review')
        flow.submit(run_id, review['id'], {
            'verdict': 'NEEDS_REVIEW', 'summary': 'One issue',
            'issues': [{'severity': 'MAJOR', 'evidence': 'Scene 1', 'description': 'Clarify a fact', 'fix': 'Clarify it'}]})
        run = flow.get(run_id)
        edit = next(t for t in run['tasks'] if t['kind'] == 'script_edit' and t['status'] == 'pending')
        with self.assertRaises(ValueError):
            flow.submit(run_id, edit['id'], {'screenplay': 'Revision proposal only.', 'changes': ['Proposed clarification']})
        self.assertEqual(flow.get(run_id)['screenplay_source'], run['screenplay_source'])

    def test_minor_only_review_advances_in_agent_mode(self):
        path = Path(self.temp.name) / 'minor.db'
        flow = Workflow(path)
        run_id = flow.create('Agent film', 'A complete short screenplay.', 'agent', 'https://chatgpt.com/project', 'agent')['id']
        run = flow.get(run_id)
        review = next(t for t in run['tasks'] if t['kind'] == 'script_review')
        flow.submit(run_id, review['id'], {
            'verdict': 'NEEDS_REVIEW', 'summary': 'Only a production note',
            'issues': [{'severity': 'MINOR', 'evidence': 'Scene 1', 'description': 'Continuity note', 'fix': 'Track in planning'}]})
        run = flow.get(run_id)
        self.assertEqual(run['status'], 'production_planning')
        self.assertTrue(any(t['kind'] == 'production_plan' and t['status'] == 'pending' for t in run['tasks']))

    def test_restore_screenplay_source_cancels_stale_task_and_uses_full_source(self):
        full = '\n'.join(f'**{n}. SCENE {n}**\nFull scene content.' for n in range(1, 4)) * 20
        path = Path(self.temp.name) / 'restore.db'
        flow = Workflow(path)
        run_id = flow.create('Restore film', full, 'restore', 'https://chatgpt.com/project', 'agent')['id']
        run = flow.get(run_id)
        source_id = run['screenplay']
        flow.control(run_id, 'pause')
        task_id = flow.restore_screenplay_source(run_id, source_id, 'Clarify one sentence and return the full screenplay.')
        run = flow.get(run_id)
        task = next(t for t in run['tasks'] if t['id'] == task_id)
        self.assertEqual(task['inputs'][0], source_id)
        self.assertEqual(run['screenplay_source'], source_id)
        self.assertEqual(run['status'], 'script_revision')

    def test_exact_approved_patch_preserves_the_rest_of_the_screenplay(self):
        source = ('Header\nThe ambiguous sentence.\n' + ('Full screenplay body.\n' * 80))
        path = Path(self.temp.name) / 'patch.db'
        flow = Workflow(path)
        run_id = flow.create('Patch film', source, 'patch', 'https://chatgpt.com/project', 'agent')['id']
        run = flow.get(run_id)
        source_id = run['screenplay']
        flow.control(run_id, 'pause')
        task_id = flow.apply_approved_screenplay_patch(
            run_id, source_id,
            [('The ambiguous sentence.', 'The clarified sentence.')],
            'Clarify one sentence.')
        run = flow.get(run_id)
        patched = flow.content(run, run['screenplay'])
        self.assertEqual(patched, source.strip().replace('The ambiguous sentence.', 'The clarified sentence.'))
        self.assertEqual(run['screenplay_source'], run['screenplay'])
        self.assertEqual(next(t for t in run['tasks'] if t['id'] == task_id)['kind'], 'script_review')

    def test_image_task_envelope_contains_only_its_reference_spec(self):
        self.task('script_review', {'verdict': 'PASS', 'summary': 'Reviewed', 'issues': []})
        run = self.flow.get(self.run)
        self.flow.approve(self.run, 'screenplay', run['screenplay'], run['revision'])
        self.task('production_plan', {'clips': [dict(number=1, chapter=1, source_excerpt='A enters.',
                  action='A enters.', dialogue='', color='warm amber', start_state='outside',
                  end_state='inside', depends_on=[])], 'coverage_notes': 'Complete'})
        self.task('reference_plan', {
            'references': [
                {'id': 'A', 'description': 'A character', 'image_prompt': 'Character A'},
                {'id': 'B', 'description': 'Unused prop', 'image_prompt': 'Prop B'}],
            'clip_references': {'1': ['A']}})
        run = self.flow.get(self.run)
        task = next(t for t in run['tasks'] if t['kind'] == 'image_create' and t['ref'] == 'A')
        envelope = self.flow.envelope(self.run, task['id'])
        self.assertEqual(len(envelope['inputs']), 1)
        self.assertEqual(envelope['inputs'][0]['kind'], 'reference_spec')
        self.assertEqual(envelope['inputs'][0]['content']['id'], 'A')
        self.assertEqual(envelope['inputs'][0]['content']['used_by_clips'], [1])
        self.assertEqual(envelope['inputs'][0]['content']['locked_colors'], ['warm amber'])

    def test_reference_qa_envelope_labels_each_actual_image(self):
        self.task('script_review', {'verdict': 'PASS', 'summary': 'Reviewed', 'issues': []})
        run = self.flow.get(self.run)
        self.flow.approve(self.run, 'screenplay', run['screenplay'], run['revision'])
        self.task('production_plan', {'clips': [dict(number=1, chapter=1, source_excerpt='A enters.',
                  action='A enters.', dialogue='', color='warm', start_state='out', end_state='in',
                  depends_on=[])], 'coverage_notes': 'Complete'})
        self.task('reference_plan', {'references': [{'id': 'A', 'description': 'A', 'image_prompt': 'A'},
                                                    {'id': 'B', 'description': 'B', 'image_prompt': 'B'}],
                                     'clip_references': {'1': ['A', 'B']}})
        self.task('image_create', {'image_id': 'a' * 64})
        self.task('image_create', {'image_id': 'b' * 64})
        run = self.flow.get(self.run)
        task = next(t for t in run['tasks'] if t['kind'] == 'reference_qa' and t['status'] == 'pending')
        envelope = self.flow.envelope(self.run, task['id'])
        labelled = [item['content'] for item in envelope['inputs'] if item['kind'] == 'reference_image']
        self.assertEqual(labelled, [{'reference_id': 'A', 'image_id': 'a' * 64},
                                    {'reference_id': 'B', 'image_id': 'b' * 64}])

    def test_replacing_rejected_pending_task_uses_new_marker(self):
        run = self.flow.get(self.run)
        old = next(t for t in run['tasks'] if t['status'] == 'pending')
        self.flow.control(self.run, 'pause')
        new_id = self.flow.replace_pending_task(self.run, old['id'], 'Composer rejected oversized payload.')
        run = self.flow.get(self.run)
        self.assertEqual(next(t for t in run['tasks'] if t['id'] == old['id'])['status'], 'cancelled')
        replacement = next(t for t in run['tasks'] if t['id'] == new_id)
        self.assertEqual(replacement['kind'], old['kind'])
        self.assertEqual(replacement['inputs'], old['inputs'])

    def test_rescheduling_candidate_selector_moves_parent_pointer(self):
        with self.flow.edit(self.run) as run:
            parent = self.flow.task(run, 'image_create', [], ref='A')
            bundle = self.flow.artifact(run, 'image_candidates', {
                'parent_task_id': parent['id'], 'reference_id': 'A',
                'candidates': [{'image_id': 'a' * 64, 'job_id': 'job-a'}]}, [])
            selector = self.flow.task(run, 'reference_candidate_qa', [bundle], ref='A')
            parent['candidate_qa_id'] = selector['id']
            selector['delivery'] = 'sent'
        replacement_id = self.flow.reschedule_stalled_task(self.run, selector['id'], 'Timed out.')
        run = self.flow.get(self.run)
        parent = next(t for t in run['tasks'] if t['id'] == parent['id'])
        replacement = next(t for t in run['tasks'] if t['id'] == replacement_id)
        self.assertEqual(parent['candidate_qa_id'], replacement_id)
        self.assertEqual(replacement['replaces_task_id'], selector['id'])

    def test_duplicate_reference_image_is_rejected_and_legacy_mapping_requeued(self):
        self.task('script_review', {'verdict': 'PASS', 'summary': 'Reviewed', 'issues': []})
        run = self.flow.get(self.run)
        self.flow.approve(self.run, 'screenplay', run['screenplay'], run['revision'])
        self.task('production_plan', {'clips': [dict(number=1, chapter=1, source_excerpt='A enters.',
                  action='A enters.', dialogue='', color='warm', start_state='out', end_state='in',
                  depends_on=[])], 'coverage_notes': 'Complete'})
        self.task('reference_plan', {
            'references': [{'id': 'A', 'description': 'A', 'image_prompt': 'A'},
                           {'id': 'B', 'description': 'B', 'image_prompt': 'B'}],
            'clip_references': {'1': ['A', 'B']}})
        self.task('image_create', {'image_id': 'a' * 64})
        with self.assertRaises(ValueError):
            self.task('image_create', {'image_id': 'a' * 64})
        self.task('image_create', {'image_id': 'b' * 64})
        with self.flow.edit(self.run) as current:
            current['images']['B'] = current['images']['A']
        repaired = self.flow.repair_duplicate_images(self.run)
        run = self.flow.get(self.run)
        self.assertEqual(repaired[0]['reference_id'], 'B')
        self.assertNotIn('B', run['images'])
        self.assertTrue(any(t['kind'] == 'image_create' and t['ref'] == 'B'
                            and t['status'] == 'pending' for t in run['tasks']))

    def test_render_runtime_recovers_existing_task_without_resubmit(self):
        self.ready(); item = self.flow.reserve_render(self.run)
        calls = []
        async def submit(payload): calls.append(payload)
        def get(task_id): return {'id': task_id, 'batch_id': item['batch_id'], 'status': 'completed', 'video_url': '/videos/test.mp4'}
        runtime = StudioRuntime(Workflow(self.path), submit, get)
        asyncio.run(runtime.tick())
        self.assertEqual(calls, [])
        self.assertEqual(self.flow.get(self.run)['clips'][0]['status'], 'qa')

    def test_dependency_prompt_waits_for_selected_take(self):
        self.ready(2, dependent=True)
        self.assertFalse(any(t['clip'] == 2 for t in self.flow.get(self.run)['tasks']))
        first = self.render(); self.qa(True, 1)
        run = self.flow.get(self.run)
        second = next(t for t in run['tasks'] if t['kind'] == 'prompt' and t['clip'] == 2)
        envelope = self.flow.envelope(self.run, second['id'])
        self.assertEqual(envelope['previous_selected_takes'][0]['selected_task_id'], first['task_id'])
        self.assertFalse(any(a.get('kind') == 'screenplay' for a in envelope['inputs']))

    def test_exhausted_dependency_finishes_with_issues(self):
        self.ready(2, dependent=True)
        for i in range(10):
            self.render(); self.qa()
            if i == 4: self.task('prompt_repair', self.prompt('Repaired'))
        run = self.flow.get(self.run)
        self.assertEqual(run['status'], 'completed_with_issues')
        self.assertEqual(run['clips'][1]['status'], 'blocked_dependency')
        self.flow.create('Next film', 'New screenplay', 'next', '')

    def test_quota_wait_reuses_reservation_without_consuming_video_budget(self):
        self.ready(); sent = []
        async def submit(payload):
            sent.append(payload['task_id'])
            raise AdmissionRejected(429, 'No credits')
        runtime = StudioRuntime(self.flow, submit, lambda _: None)
        asyncio.run(runtime.tick()); asyncio.run(runtime.tick())
        run = self.flow.get(self.run)
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(run['clips'][0]['attempts']), 1)
        self.assertEqual(run['clips'][0]['status'], 'admission_wait')
        self.assertFalse(run['clips'][0]['attempts'][0]['generated'])

    def test_selected_take_annotation_keeps_other_batches_unchanged(self):
        self.ready(); selected = self.render(); self.qa(True)
        rows = self.flow.annotate_tasks([{'id': selected['task_id'], 'batch_id': selected['batch_id']},
                                         {'id': 'unrelated', 'batch_id': 'ordinary'}])
        self.assertTrue(rows[0]['studio_selected'])
        self.assertNotIn('studio_status', rows[1])


if __name__ == '__main__':
    unittest.main()

