"""Offline delivery/recovery tests; never contact ChatGPT or a live profile."""
import json
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import AsyncMock
from PIL import Image

from film_studio import StudioRegistry, StudioSettings
from studio_workflow import Workflow
from studio_chatgpt import ChatGPTWorker, local_endpoint, parse_reply, project_chat_url


class TransportTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root/'Profile 7').mkdir()
        (self.root/'Local State').write_text(json.dumps({'profile':{'info_cache':{'Profile 7':{'name':'Test'}}}}))
        self.registry = StudioRegistry(self.root/'test.db', self.root)
        self.registry.save(StudioSettings(profile_id='Profile 7', project_url='https://chatgpt.com/g/g-p-test/project',
                                         instructions_confirmed=True, sessions={'script_doctor':'https://chatgpt.com/g/g-p-test-film-studio/c/12345678-1234-1234-1234-123456789abc'}))
        self.flow = Workflow(self.root/'test.db')
        self.run = self.flow.create('Test', 'A enters.', 'test', self.registry.read().project_url)
        self.task_id = self.run['tasks'][0]['id']
        self.driver = AsyncMock()
        self.driver.inspect.return_value = {'sent':False,'reply':None}
        self.accepted = []
        self.worker = ChatGPTWorker(self.flow, self.registry, self.flow.envelope,
                                   lambda *args:self.accepted.append(args), None, lambda _:None, self.root,
                                   driver=self.driver)
        self.worker.enabled = True
        self.worker.connected_profile = 'Profile 7'

    def task(self):
        return self.flow.get(self.run['id'])['tasks'][0]

    def test_selector_candidates_are_compacted_to_one_labelled_attachment(self):
        files = {}
        for index in range(5):
            path = self.root / f'candidate-{index}.png'
            Image.new('RGB', (640, 360), (index * 30, 20, 40)).save(path)
            files[f'{index:x}' * 64] = path
        class Library:
            def path(_, image_id): return files[image_id]
        self.worker.image_library = Library()
        paths = self.worker.attachments({
            'task_id': 'agent_selector', 'role': 'reference_selector',
            'inputs': [{'kind': 'image_candidates', 'content': {'candidates': [
                {'image_id': f'{index:x}' * 64} for index in range(5)]}}]})
        self.assertEqual(len(paths), 1)
        self.assertTrue(paths[0].is_file())
        with Image.open(paths[0]) as sheet:
            self.assertEqual(sheet.size, (2048, 3600))

    def test_selected_reference_qa_compacts_original_pngs(self):
        files = {}
        for index in range(2):
            path = self.root / f'selected-{index}.png'
            Image.new('RGB', (2848, 1600), (index * 40, 30, 50)).save(path)
            files[f'{index:x}' * 64] = path
        class Library:
            def path(_, image_id): return files[image_id]
        self.worker.image_library = Library()
        paths = self.worker.attachments({
            'task_id': 'agent_selected_qa', 'role': 'reference_selected_qa',
            'inputs': [
                {'kind': 'image_create', 'source_artifact': 'selected',
                 'content': {'image_id': '0' * 64}},
                {'kind': 'image_create', 'source_artifact': 'identity',
                 'content': {'image_id': '1' * 64}},
            ]})
        self.assertEqual(len(paths), 1)
        self.assertLess(paths[0].stat().st_size, 2_000_000)
        with Image.open(paths[0]) as sheet:
            self.assertEqual(sheet.size, (2048, 1960))

    async def test_send_journal_precedes_click_and_restart_never_resends(self):
        async def fail(_):
            self.assertEqual(self.task()['delivery'], 'sending')
            raise RuntimeError('connection lost during click')
        self.driver.send.side_effect = fail
        await self.worker.tick()
        self.assertEqual(self.task()['delivery'], 'sending')
        await self.worker.tick()
        self.assertEqual(self.task()['delivery'], 'uncertain')
        self.assertEqual(self.driver.send.await_count, 1)

    async def test_existing_marker_reconciles_without_click(self):
        reply = json.dumps({'task_id':self.task_id,'output':{'test':'value'}})
        self.driver.inspect.return_value = {'sent':True,'reply':reply}
        await self.worker.tick()
        self.worker.update(self.run['id'],self.task_id,reply_seen_at=0)
        await self.worker.tick()
        self.assertEqual(self.task()['delivery'],'received')
        self.assertEqual(len(self.accepted),1)
        self.driver.send.assert_not_awaited()

    async def test_legacy_sent_task_without_timestamp_is_rescheduled(self):
        self.driver.inspect.return_value = {'sent':True, 'reply':None, 'busy':False}
        with self.flow.edit(self.run['id']) as run:
            task = run['tasks'][0]
            task.update(delivery='sent', sent_at=None, last_transition_at=time.time() - 200)
        await self.worker.tick()
        old = self.task()
        self.assertEqual(old['status'], 'cancelled')
        self.assertEqual(old['delivery'], 'stalled')
        replacement = self.flow.get(self.run['id'])['tasks'][-1]
        self.assertEqual(replacement['status'], 'pending')
        self.assertEqual(replacement['replaces_task_id'], self.task_id)
        self.assertEqual(replacement['transport_retries'], 1)
        self.driver.send.assert_not_awaited()

    async def test_missing_marker_is_requeued_after_uncertain_send(self):
        self.worker.update(self.run['id'], self.task_id, delivery='uncertain',
                           sent_at=time.time() - 60)
        await self.worker.tick()
        self.assertEqual(self.task()['delivery'], 'not_sent')
        self.driver.send.assert_not_awaited()

    async def test_active_generation_is_not_rescheduled_before_hard_timeout(self):
        self.driver.inspect.return_value = {'sent':True, 'reply':None, 'busy':True}
        self.worker.update(self.run['id'], self.task_id, delivery='sent',
                           sent_at=time.time() - 200)
        await self.worker.tick()
        self.assertEqual(self.task()['status'], 'pending')
        self.assertEqual(len(self.flow.get(self.run['id'])['tasks']), 1)

    async def test_wrong_task_reply_is_not_submitted(self):
        self.driver.inspect.return_value = {'sent':True,'reply':json.dumps({'task_id':'wrong','output':{}})}
        await self.worker.tick()
        self.worker.update(self.run['id'],self.task_id,reply_seen_at=0)
        await self.worker.tick()
        self.assertEqual(self.task()['delivery'],'invalid_result')
        self.assertFalse(self.accepted)

    async def test_agent_mode_wrong_task_reply_is_rescheduled(self):
        self.flow.set_approval_mode(self.run['id'], 'agent')
        self.driver.inspect.return_value = {
            'sent': True, 'reply': json.dumps({'task_id': 'wrong', 'output': {}}), 'busy': False}
        await self.worker.tick()
        self.worker.update(self.run['id'], self.task_id, reply_seen_at=0)
        await self.worker.tick()
        run = self.flow.get(self.run['id'])
        old = next(t for t in run['tasks'] if t['id'] == self.task_id)
        self.assertEqual(old['delivery'], 'stalled')
        self.assertTrue(any(t.get('replaces_task_id') == self.task_id and t['status'] == 'pending'
                            for t in run['tasks']))
        self.assertFalse(self.accepted)

    async def test_paused_run_does_not_touch_browser(self):
        self.flow.control(self.run['id'],'pause')
        await self.worker.tick()
        self.driver.page.assert_not_awaited()

    async def test_pause_during_preparation_prevents_send(self):
        async def pause(*args): self.flow.control(self.run['id'],'pause')
        self.driver.prepare.side_effect = pause
        await self.worker.tick()
        self.driver.send.assert_not_awaited()
        self.assertEqual(self.task()['delivery'],'not_sent')

    async def test_failed_unsent_task_waits_before_retry(self):
        self.driver.prepare.side_effect = RuntimeError('attachment failed')
        await self.worker.tick()
        self.assertGreater(self.task()['next_retry_at'], time.time())
        await self.worker.tick()
        self.assertEqual(self.driver.prepare.await_count, 1)

    async def test_profile_change_disables_worker(self):
        self.worker.connected_profile='different'
        with self.assertRaises(ValueError): await self.worker.tick()
        self.assertFalse(self.worker.enabled)
        self.driver.page.assert_not_awaited()

    async def test_uncertain_bootstrap_is_not_repeated(self):
        settings=self.registry.read(); settings.sessions={}; self.registry.save(settings)
        self.driver.send.side_effect=RuntimeError('uncertain')
        with self.assertRaises(RuntimeError): await self.worker.tick()
        with self.assertRaises(ValueError): await self.worker.tick()
        self.assertEqual(self.driver.send.await_count,1)

    def test_endpoint_and_reply_validation(self):
        settings = self.registry.read()
        settings.profile_id = 'film-studio-managed'
        self.registry.save(settings)
        self.assertEqual(self.registry.profile_path(self.registry.read().profile_id),
                         self.root / '.runtime' / 'film-studio-chrome' / 'Default')
        for url in ['http://evil.test:9222','https://localhost:9222','http://user@localhost:9222','http://localhost:9222/path']:
            with self.assertRaises(ValueError): local_endpoint(url)
        self.assertEqual(local_endpoint('http://127.0.0.1:9222/'),'http://127.0.0.1:9222')
        self.assertEqual(parse_reply('```json\n{"task_id":"a","output":{}}\n```','a'),{})
        with self.assertRaises(ValueError): parse_reply('{"task_id":"b","output":{}}','a')

    def test_only_settled_project_chat_is_reusable(self):
        project = 'https://chatgpt.com/g/g-p-test/project'
        self.assertTrue(project_chat_url('https://chatgpt.com/g/g-p-test-film-studio/c/12345678-1234-1234-1234-123456789abc', project))
        self.assertFalse(project_chat_url('https://chatgpt.com/g/g-p-test-film-studio/c/local-chatgpt%3A1234', project))
        self.assertFalse(project_chat_url('https://chatgpt.com/c/12345678-1234-1234-1234-123456789abc', project))
        self.assertFalse(project_chat_url('https://chatgpt.com/g/g-p-other/c/12345678-1234-1234-1234-123456789abc', project))

    async def test_legacy_placeholder_replaced_once_with_project_chat(self):
        settings = self.registry.read()
        settings.sessions['script_editor'] = 'https://chatgpt.com/g/g-p-test-film-studio/c/local-chatgpt%3Aold'
        self.registry.save(settings)
        with self.flow.connection() as db:
            db.execute('INSERT INTO studio_chat_bootstrap VALUES (?,?,?,?)',
                       (settings.project_url, 'script_editor', 'ready', settings.sessions['script_editor']))
        stable = 'https://chatgpt.com/g/g-p-test-film-studio/c/abcdef12-1234-1234-1234-123456789abc'
        self.driver.created_chat_url.return_value = stable
        first = await self.worker.bootstrap(settings, 'script_editor')
        second = await self.worker.bootstrap(self.registry.read(), 'script_editor')
        self.assertEqual((first, second), (stable, stable))
        self.assertEqual(self.registry.read().sessions['script_editor'], stable)
        self.assertEqual(self.driver.send.await_count, 1)

    def test_missing_video_never_replaced_with_text_evidence(self):
        with self.assertRaises(ValueError):
            self.worker.attachments({'inputs':[{'kind':'rendered_video','content':{'task_id':'missing'}}]})


if __name__ == '__main__': unittest.main()
