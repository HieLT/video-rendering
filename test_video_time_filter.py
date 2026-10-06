"""Time-filter regressions: real store queries and browser interactions, no live mutations."""
import asyncio
import json
import tempfile
import unittest
from datetime import datetime
from pathlib import Path
from urllib.parse import parse_qs, urlsplit
from patchright.async_api import async_playwright
from store import TaskStore

ROOT = Path(__file__).resolve().parent
START = datetime.fromisoformat('2026-10-06T10:00:00+07:00').timestamp()

class TimeFilterTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.store = TaskStore(str(Path(self.tmp.name) / 'tasks.db'))
        for i, offset in enumerate((-1, 0, 30, 59.9, 60, 3600)):
            self.store.create('video_'+str(i), 'seedance-2.5', 'Fixture', '16:9', 30)
            self.store.update('video_'+str(i), created_at=START+offset,
                              status='completed' if i % 2 else 'processing')

    def tearDown(self):
        self.store._conn.close()
        self.tmp.cleanup()

    def test_inclusive_start_exclusive_end_and_fractional_seconds(self):
        result = self.store.recent_tasks(page=1, created_from=START, created_before=START+60)
        self.assertEqual({r['id'] for r in result['tasks']}, {'video_1','video_2','video_3'})

    def test_one_sided_and_empty_ranges(self):
        self.assertEqual(self.store.recent_tasks(page=1, created_from=START)['total'], 5)
        self.assertEqual(self.store.recent_tasks(page=1, created_before=START)['total'], 1)
        self.assertEqual(self.store.recent_tasks(page=1, created_from=START+7200)['total'], 0)

    def test_combined_filters_and_pagination(self):
        args = dict(created_from=START, created_before=START+60, status='completed', limit=1)
        first = self.store.recent_tasks(page=1, **args)
        second = self.store.recent_tasks(page=2, **args)
        self.assertEqual((first['total'],first['pages']), (2,2))
        self.assertNotEqual(first['tasks'][0]['id'], second['tasks'][0]['id'])
        self.assertEqual(self.store.recent_tasks(page=1,query='video_1',**args)['total'],1)

    async def test_browser_visibility_selection_timezone_validation_and_clear(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                # Explicit UTC+7 filter must work even when the browser uses another zone.
                page = await browser.new_page(viewport={'width':1280,'height':900}, timezone_id='America/Los_Angeles')
                errors, requests = [], []
                page.on('pageerror', lambda e: errors.append(str(e)))
                async def route(r):
                    u = urlsplit(r.request.url)
                    if u.path == '/':
                        return await r.fulfill(body=(ROOT/'web/index.html').read_text(encoding='utf-8'),content_type='text/html')
                    if u.path == '/api/admin/accounts':
                        return await r.fulfill(json={'accounts':[]})
                    if u.path == '/api/admin/tasks':
                        q = {k:v[0] for k,v in parse_qs(u.query).items()}
                        requests.append(q)
                        args = {k:float(q[k]) for k in ('created_from','created_before') if k in q}
                        result = self.store.recent_tasks(page=int(q.get('page',1)),limit=int(q.get('limit',25)),status=q.get('status',''),**args)
                        return await r.fulfill(json=result)
                    return await r.fulfill(json={})
                await page.route('**/*',route)
                await page.goto('http://fixture/')
                await page.locator('#loginMask').wait_for(state='hidden')
                await page.get_by_role('button',name='Video Tasks',exact=True).click()
                start, end = page.locator('#taskCreatedFrom'), page.locator('#taskCreatedTo')
                self.assertTrue(await start.is_visible())
                self.assertTrue(await end.is_visible())
                self.assertEqual(await start.get_attribute('type'),'datetime-local')
                await start.fill('2026-10-06T10:00')
                await end.fill('2026-10-06T10:00')
                await end.press('Tab')
                await page.wait_for_function("document.querySelector('#taskSearchSummary').textContent.includes('of 3 tasks')")
                self.assertEqual(float(requests[-1]['created_from']),START)
                self.assertEqual(float(requests[-1]['created_before']),START+60)
                await page.locator('#taskStatus').select_option('completed')
                await page.wait_for_function("document.querySelector('#taskSearchSummary').textContent.includes('of 2 tasks')")
                await end.fill('2026-10-06T09:00')
                await end.press('Tab')
                await page.wait_for_function("document.querySelector('#taskSearchSummary').textContent.includes('Invalid time range')")
                self.assertFalse(await end.evaluate('(e)=>e.validity.valid'))
                await page.get_by_role('button',name='Clear filters',exact=True).click()
                await page.wait_for_function("document.querySelector('#taskSearchSummary').textContent.includes('of 6 tasks')")
                self.assertEqual(await start.input_value(),'')
                self.assertEqual(await end.input_value(),'')
                self.assertNotIn('created_from',requests[-1])
                self.assertNotIn('created_before',requests[-1])
                self.assertTrue(await end.evaluate('(e)=>e.validity.valid'))
                await page.set_viewport_size({'width':390,'height':844})
                for control in (start,end):
                    await control.scroll_into_view_if_needed()
                    self.assertTrue(await control.is_visible())
                    box = await control.bounding_box()
                    self.assertGreaterEqual(box['x'],0)
                    self.assertLessEqual(box['x']+box['width'],391)
                await start.fill('2026-10-06T10:00')
                await start.press('Tab')
                await page.wait_for_function("document.querySelector('#taskSearchSummary').textContent.includes('of 5 tasks')")
                self.assertNotIn('created_before',requests[-1])
                self.assertEqual(errors,[])
            finally:
                await browser.close()

if __name__ == '__main__':
    unittest.main(verbosity=2)
