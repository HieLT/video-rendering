"""Dashboard edit selection test with fully intercepted APIs, no live data."""
import asyncio
from pathlib import Path
from urllib.parse import urlsplit, parse_qs
from patchright.async_api import async_playwright


async def main():
    task = dict(id='video_test', name='scene 2', prompt='Fixture', status='completed',
                video_url='http://tags.test/videos/fixture.mp4', edit_selected=0)
    changes, errors = [], []
    async with async_playwright() as p:
        browser = await p.chromium.launch(channel='chrome', headless=True)
        page = await browser.new_page(viewport={'width':1440, 'height':1000})
        page.on('pageerror', lambda error: errors.append(str(error)))
        async def route(r):
            url = urlsplit(r.request.url)
            if url.path == '/':
                return await r.fulfill(content_type='text/html', body=Path('web/index.html').read_text(encoding='utf-8'))
            if url.path.endswith('/edit-selection'):
                selected = r.request.post_data_json['selected']
                changes.append(selected)
                task['edit_selected'] = int(selected)
                return await r.fulfill(json={'ok':True, 'edit_selected':selected})
            if url.path == '/api/admin/tasks':
                selection = parse_qs(url.query).get('edit_selected', [''])[0]
                matches = not selection or (selection == 'true') == bool(task['edit_selected'])
                return await r.fulfill(json={'tasks':[task] if matches else []})
            if url.path == '/api/admin/accounts':
                return await r.fulfill(json={'accounts':[]})
            return await r.fulfill(json={'per_day':[], 'per_account':[], 'jobs':{}})
        await page.route('**/*', route)
        await page.goto('http://tags.test/')
        await page.add_script_tag(content="clearInterval(timer);switchTab('tasks');")
        await page.get_by_role('button', name='☆ Chọn để edit', exact=True).click()
        await page.get_by_role('button', name='⭐ Đã chọn để edit', exact=True).wait_for()
        assert changes == [True]
        assert await page.locator('#tasksTable').get_by_role('button', name='Delete', exact=True).count() == 0
        await page.select_option('#taskEditSelected', 'false')
        await page.wait_for_function("document.querySelector('#taskSearchSummary').textContent.includes('No tasks')")
        await page.select_option('#taskEditSelected', 'true')
        await page.get_by_role('button', name='⭐ Đã chọn để edit', exact=True).click()
        await page.wait_for_function("document.querySelector('#taskSearchSummary').textContent.includes('No tasks')")
        assert changes == [True, False]
        await page.get_by_role('button', name='Clear filters').click()
        await page.get_by_role('button', name='☆ Chọn để edit', exact=True).wait_for()
        assert await page.locator('#tasksTable').get_by_role('button', name='Delete', exact=True).count() == 1
        assert not errors, errors
        await browser.close()
    print('PASS tag button, selected/unselected filters, clear filters, delete protection; mocked APIs only')


if __name__ == '__main__':
    asyncio.run(main())
