"""Browser toggle acceptance using temporary stores, with no live Dola requests."""
import asyncio
from pathlib import Path
from urllib.parse import urlsplit
from patchright.async_api import async_playwright
from test_account_request_mode import RequestModeTests


async def main():
    fixture = RequestModeTests()
    fixture.setUp()
    client = fixture.api_client()
    errors = []
    try:
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True, channel='chromium')
            try:
                context = await browser.new_context()
                await context.add_init_script("localStorage.setItem('dola_admin_key','fixture-admin')")
                page = await context.new_page()
                page.on('pageerror', lambda error: errors.append(str(error)))
                async def route(request):
                    path = urlsplit(request.request.url).path
                    if path == '/':
                        return await request.fulfill(content_type='text/html', body=Path('web/index.html').read_text(encoding='utf-8'))
                    if path == '/api/admin/stats':
                        return await request.fulfill(json=dict(per_day=[], per_account=[], today_completed=0, today_failed=0, total_accounts=1, available_accounts=1, total_remaining=2))
                    fixtures = {'/api/admin/jobs': {'jobs': {}}, '/api/admin/accounts/google-bulk/progress': {'imports': {}}, '/api/admin/keys': {'keys': []}, '/api/admin/tasks': {'tasks': []}}
                    if path in fixtures:
                        return await request.fulfill(json=fixtures[path])
                    response = client.request(request.request.method, path, content=request.request.post_data_buffer, headers=dict(request.request.headers))
                    await request.fulfill(status=response.status_code, content_type='application/json', body=response.content)
                await page.route('**/*', route)
                await page.goto('http://mode.test/')
                await page.wait_for_selector('#loginMask', state='hidden')
                await page.get_by_role('button', name='Accounts', exact=True).click()
                toggle = page.locator('#dualRequests')
                await toggle.wait_for(state='visible')
                assert not await toggle.is_checked()
                await toggle.check()
                await page.wait_for_function("document.querySelector('#dualRequests').checked && !document.querySelector('#dualRequests').disabled")
                assert fixture.scheduler.dual_requests
                await page.reload()
                await page.get_by_role('button', name='Accounts', exact=True).click()
                await page.wait_for_function("document.querySelector('#dualRequests').checked")
                await toggle.uncheck()
                await page.wait_for_function("!document.querySelector('#dualRequests').checked && !document.querySelector('#dualRequests').disabled")
                assert not fixture.scheduler.dual_requests
                assert not errors, errors
                print('PASS account request mode UI: default off, enable, reload, disable; no live Dola generation')
            finally:
                await browser.close()
    finally:
        fixture.tearDown()


if __name__ == '__main__':
    asyncio.run(main())
