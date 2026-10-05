import asyncio
import unittest
from bulk_accounts import parse_accounts, run_import


class BulkTests(unittest.IsolatedAsyncioTestCase):
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
            self.assertTrue(all(b-a >= .49 for a,b in zip(starts, starts[1:])))
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
