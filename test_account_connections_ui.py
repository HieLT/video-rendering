"""Offline browser acceptance for selecting and persisting account connections."""
import asyncio
from unittest.mock import patch
from pathlib import Path
from urllib.parse import urlsplit

from patchright.async_api import async_playwright
from test_account_connections import ConnectionTests


async def main():
    fixture = ConnectionTests()
    fixture.setUp()
    fixture.pool.set_email('one', 'one@gmail.com')
    fixture.pool.set_email('two', 'two@gmail.com')
    errors = []
    imports = []
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
                        return await request.fulfill(json=dict(per_day=[], per_account=[], today_completed=0, today_failed=0, total_accounts=2, available_accounts=0, total_remaining=4))
                    fixtures = {'/api/admin/jobs': {'jobs': {fixture.pool.account_uuid(name): value for name,value in fixture.jobs.items()}}, '/api/admin/accounts/google-bulk/progress': {'imports': {}}, '/api/admin/keys': {'keys': []}, '/api/admin/tasks': {'tasks': []}}
                    if path in fixtures:
                        return await request.fulfill(json=fixtures[path])
                    if path == '/api/admin/accounts/google-bulk':
                        imports.append(request.request.post_data_json)
                        return await request.fulfill(status=202, json={'id': 'fixture-import'})
                    response = fixture.http.request(request.request.method, path, content=request.request.post_data_buffer, headers=dict(request.request.headers))
                    await request.fulfill(status=response.status_code, content_type='application/json', body=response.content)
                await page.route('**/*', route)
                await page.goto('http://connections.test/')
                await page.wait_for_selector('#loginMask', state='hidden')
                await page.get_by_role('button', name='Accounts', exact=True).click()
                await page.wait_for_selector('#selectAllaccounts')
                await page.locator('#selectAllaccounts').check()
                await page.locator('#assignSelectedConnection').click()
                await page.locator('#f_assign_connection').select_option('fixture')
                await page.locator('#f_assign_save').click()
                await page.wait_for_function("document.querySelectorAll('#accountsTable tbody tr').length===2 && document.querySelector('#accountsTable').textContent.includes('Fixture proxy')")
                assert fixture.path.exists()
                assert fixture.pool.accounts
                from account_connections import account_connection
                assert account_connection('one') == account_connection('two') == 'fixture'
                await page.reload()
                await page.get_by_role('button', name='Accounts', exact=True).click()
                await page.wait_for_function("document.querySelector('#accountsTable').textContent.includes('Fixture proxy')")
                first_id=fixture.pool.account_uuid('one')
                selector=f'[data-account-connection="{first_id}"]'
                await page.locator(selector).select_option('direct')
                await page.wait_for_function("id => {const select=document.querySelector('[data-account-connection=\"'+id+'\"]');return select.value==='direct'&&!select.disabled;}", arg=first_id)
                assert sorted([account_connection('one'), account_connection('two')]) == ['direct', 'fixture']
                await page.reload()
                await page.get_by_role('button', name='Accounts', exact=True).click()
                await page.wait_for_selector(selector)
                assert await page.locator(selector).input_value() == 'direct'
                fixture.jobs['one']={'status':'running'}
                await page.reload()
                await page.get_by_role('button', name='Accounts', exact=True).click()
                await page.wait_for_selector(selector)
                assert await page.locator(selector).is_disabled()
                fixture.jobs.clear()
                await page.get_by_role('button', name='＋ Add Account', exact=True).click()
                await page.locator('#f_connection').select_option('fixture')
                await page.locator('#f_method').select_option('google_bulk')
                await page.locator('#f_bulk_text').fill('third@example.com|fixture')
                assert await page.locator('#f_bulk_concurrency').input_value() == '10'
                await page.locator('#f_submit').click()
                await page.wait_for_selector('#dlg', state='hidden')
                assert imports[0]['connection_id'] == 'fixture'
                assert imports[0]['concurrency'] == 10
                assert not errors, errors
                assert 'fixture-secret' not in await page.content()
                print('PASS connection UI: batch assignment, reload persistence, separate routes, Google bulk selection, no credentials in page; no live login/generation')
            finally:
                await browser.close()
    finally:
        fixture.http.close()
        fixture.pool._conn.close()
        patch.stopall()
        fixture.tmp.cleanup()


if __name__ == '__main__':
    asyncio.run(main())
