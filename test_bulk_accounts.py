import asyncio
import ast
import unittest
from pathlib import Path
from pydantic import BaseModel, Field, ValidationError
from unittest.mock import patch, Mock
from bulk_accounts import parse_accounts, run_import


class BulkTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        interval = patch('bulk_accounts.START_INTERVAL', .02)
        interval.start()
        self.addCleanup(interval.stop)

    async def test_twenty_second_spacing_without_waiting_in_real_time(self):
        clock = Mock()
        now = 0
        starts = []
        clock.time.side_effect = lambda: now

        async def sleep(delay):
            nonlocal now
            now += delay

        async def submit(email, password):
            starts.append(now)
            return email

        accounts = [(f'{i}@example.com', 'fixture') for i in range(3)]
        progress = {'rows': [{'status': 'queued'} for _ in accounts]}
        with patch('bulk_accounts.START_INTERVAL', 20), patch('bulk_accounts.asyncio.get_running_loop', return_value=clock), patch('bulk_accounts.asyncio.sleep', side_effect=sleep):
            await run_import(accounts, 1, progress, submit, lambda _: 'success')
        self.assertEqual(starts, [0, 20, 40])

    def test_api_defaults_to_ten_and_rejects_more(self):
        tree = ast.parse(Path('server.py').read_text(encoding='utf-8'))
        model = next(node for node in tree.body if isinstance(node, ast.ClassDef) and node.name == 'GoogleBulkAdd')
        namespace = {'BaseModel': BaseModel, 'Field': Field}
        exec(compile(ast.Module(body=[model], type_ignores=[]), 'server.py', 'exec'), namespace)
        request = namespace['GoogleBulkAdd']
        self.assertEqual(request(text='a@example.com|fixture').concurrency, 10)
        self.assertEqual(request(text='a@example.com|fixture', concurrency=10).concurrency, 10)
        for count in (0, 11, 20, True, 1.5):
            with self.assertRaises(ValidationError):
                request(text='a@example.com|fixture', concurrency=count)

    async def test_slow_submit_does_not_serialize_workers_and_cap_is_ten(self):
        accounts = [(f'{i}@example.com', 'fixture') for i in range(12)]
        progress = {'rows': [{'status': 'queued'} for _ in accounts]}
        started = []
        release = asyncio.Event()
        five_started = asyncio.Event()
        async def submit(email, password):
            started.append(email)
            if len(started) == 10:
                five_started.set()
            await release.wait()
            return email
        task = asyncio.create_task(run_import(accounts, 20, progress, submit, lambda _: 'success'))
        try:
            await asyncio.wait_for(five_started.wait(), 4)
            self.assertEqual(len(started), 10)
            self.assertEqual(sum(r['status'] == 'queued' for r in progress['rows']), 2)
            release.set()
            await asyncio.wait_for(task, 4)
            self.assertEqual(len(started), 12)
        finally:
            release.set()
            if not task.done():
                task.cancel()
            await asyncio.gather(task, return_exceptions=True)

    async def test_ten_accounts_start_five_before_any_finishes(self):
        accounts = [(f'{i}@example.com', 'fixture') for i in range(10)]
        progress = {'rows': [{'status': 'queued'} for _ in accounts]}
        states = {}
        starts = []
        five_started = asyncio.Event()

        async def submit(email, password):
            starts.append(asyncio.get_running_loop().time())
            states[email] = 'running'
            if len(states) == 5:
                five_started.set()
            return email

        task = asyncio.create_task(run_import(accounts, 5, progress, submit, states.get))
        try:
            await asyncio.wait_for(five_started.wait(), 4)
            self.assertEqual(len(states), 5)
            self.assertTrue(all(b-a >= .019 for a,b in zip(starts, starts[1:])))
            self.assertEqual(sum(r['status'] == 'running' for r in progress['rows']), 5)
            self.assertEqual(sum(r['status'] == 'queued' for r in progress['rows']), 5)
            for email in list(states):
                states[email] = 'success'
            await asyncio.sleep(3)
            self.assertEqual(len(states), 10)
            for email in states:
                states[email] = 'success'
            await asyncio.wait_for(task, 2)
        finally:
            if not task.done():
                task.cancel()
                await asyncio.gather(task, return_exceptions=True)

    def test_parse(self):
        self.assertEqual(parse_accounts(' a@example.com|p|x\r\n\nA@example.com|other'), [('a@example.com', 'p|x')])
        with self.assertRaises(ValueError):
            parse_accounts('bad input')

    async def test_concurrency_and_failure(self):
        accounts = [(f'{i}@example.com', 'secret') for i in range(5)]
        progress = {'rows': [{'status': 'queued'} for _ in accounts]}
        running, peak, states = set(), 0, {}

        async def finish(email):
            await asyncio.sleep(.7)
            running.remove(email)
            states[email] = 'success'

        async def submit(email, password):
            nonlocal peak
            if email.startswith('2@'):
                raise RuntimeError('secret must not appear')
            running.add(email)
            peak = max(peak, len(running))
            states[email] = 'running'
            asyncio.create_task(finish(email))
            return email

        await run_import(accounts, 2, progress, submit, states.get)
        self.assertEqual(peak, 2)
        self.assertEqual(sum(r['status'] == 'success' for r in progress['rows']), 4)
        self.assertNotIn('secret', str(progress))
        self.assertEqual(progress['status'], 'finished')


if __name__ == '__main__':
    unittest.main()
