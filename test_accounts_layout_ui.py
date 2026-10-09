"""Responsive account table acceptance with 22 temporary accounts; no live login."""
import asyncio
from pathlib import Path
from urllib.parse import urlsplit
from unittest.mock import patch

from patchright.async_api import async_playwright
from test_account_connections import ConnectionTests


async def main():
    fixture=ConnectionTests()
    fixture.setUp()
    errors=[]
    opened=[]
    try:
        for index in range(22):
            name=('one','two')[index] if index<2 else f'layout-{index:02d}'
            (fixture.root/'accounts'/name).mkdir(exist_ok=True)
            fixture.pool._ensure_meta(name)
            fixture.pool.set_email(name,f'long-account-name-{index:02d}-abcdefghijklmnopqrstuvw@mtnewtoy.us')
        with fixture.pool._conn:
            fixture.pool._conn.execute("UPDATE accounts_meta SET auth_state='active',login_ok=1,account_type='google',created_at=1791537600")
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True,channel='chromium')
            try:
                context=await browser.new_context(viewport={'width':1440,'height':1000})
                await context.add_init_script("localStorage.setItem('dola_admin_key','fixture-admin')")
                page=await context.new_page()
                page.on('pageerror',lambda error:errors.append(str(error)))
                async def route(request):
                    parsed=urlsplit(request.request.url);path=parsed.path
                    if path=='/':
                        return await request.fulfill(content_type='text/html',body=Path('web/index.html').read_text(encoding='utf-8'))
                    if path=='/api/admin/stats':
                        return await request.fulfill(json=dict(per_day=[],per_account=[],today_completed=0,today_failed=0,total_accounts=22,available_accounts=22,total_remaining=44))
                    fixtures={'/api/admin/jobs':{'jobs':{}},'/api/admin/accounts/google-bulk/progress':{'imports':{}},'/api/admin/keys':{'keys':[]},'/api/admin/tasks':{'tasks':[]}}
                    if path in fixtures:return await request.fulfill(json=fixtures[path])
                    if path.endswith('/open-web'):
                        opened.append(path);return await request.fulfill(status=202,json={'ok':True})
                    response=fixture.http.request(request.request.method,path+('?' + parsed.query if parsed.query else ''),content=request.request.post_data_buffer,headers=dict(request.request.headers))
                    await request.fulfill(status=response.status_code,content_type='application/json',body=response.content)
                await page.route('**/*',route)
                await page.goto('http://layout.test/')
                await page.wait_for_selector('#loginMask',state='hidden')
                await page.get_by_role('button',name='Accounts',exact=True).click()
                await page.locator('#accountGroupOther').click()
                await page.locator('[data-domain-action="filter"][data-domain="mtnewtoy.us"]').click()
                await page.wait_for_selector('#accountsTable tbody tr:nth-child(22)')
                domain_button=page.get_by_role('button',name='Open Web cả domain',exact=True)
                assert await domain_button.get_attribute('data-open-web-domain')=='mtnewtoy.us'
                await page.route('**/api/admin/accounts/open-web-domain',lambda route:route.fulfill(status=202,json={'queued':22}))
                async with page.expect_request('**/api/admin/accounts/open-web-domain') as request_info:
                    await domain_button.click()
                assert (await request_info.value).post_data_json=={'domain':'mtnewtoy.us'}
                assert await page.locator('[data-proxy-stat="direct"] .proxy-stat-count').inner_text()=='22 acc'
                selector=page.locator('#accountsTable select').first
                proxy_id=await selector.locator('option').evaluate_all("options=>options.find(o=>o.value!=='direct').value")
                await selector.select_option(proxy_id)
                await page.wait_for_function("document.querySelector('[data-proxy-stat=direct] .proxy-stat-count').textContent==='21 acc'")
                assert await page.locator(f'[data-proxy-stat="{proxy_id}"] .proxy-stat-count').inner_text()=='1 acc'
                await page.locator('#accountGroupGmail').click()
                assert await page.locator(f'[data-proxy-stat="{proxy_id}"] .proxy-stat-count').inner_text()=='1 acc'
                await page.locator('#accountGroupOther').click()
                await page.locator('[data-domain-action="filter"][data-domain="mtnewtoy.us"]').click()
                metrics=[]
                for width in (1440,1280,1024,900,820,390):
                    await page.set_viewport_size({'width':width,'height':1000})
                    result=await page.evaluate("""()=>{
                        const box=document.querySelector('#accountsTable');
                        const rows=[...box.querySelectorAll('tbody tr')];
                        return {width:innerWidth,pageWidth:document.documentElement.scrollWidth,
                            tableWidth:box.scrollWidth,available:box.clientWidth,
                            maxRowHeight:Math.max(...rows.map(r=>r.getBoundingClientRect().height)),
                            actionsFit:[...box.querySelectorAll('.account-actions')].every(a=>a.getBoundingClientRect().right<=innerWidth),
                            overflow:[...document.querySelectorAll('body *')].filter(e=>e.getBoundingClientRect().right>innerWidth+1&&getComputedStyle(e).position!=='fixed').slice(0,8).map(e=>({tag:e.tagName,id:e.id,cls:e.className,right:e.getBoundingClientRect().right})),
                            connectionFits:[...box.querySelectorAll('select')].every(s=>s.getBoundingClientRect().width<=s.parentElement.clientWidth)};
                    }""")
                    assert result['pageWidth']<=width+1,result
                    assert result['tableWidth']<=result['available']+1,result
                    assert result['actionsFit'] and result['connectionFits'],result
                    if width>860:assert result['maxRowHeight']<=66,result
                    metrics.append(result)
                await page.set_viewport_size({'width':1280,'height':1000})
                row=page.locator('#accountsTable tbody tr').first
                assert '@mtnewtoy.us' in await row.locator('.account-name').get_attribute('title')
                await row.get_by_role('button',name='Open Web',exact=True).click()
                assert opened
                await row.locator('[data-account-menu-trigger]').click()
                await page.get_by_role('menuitem',name='Retry login',exact=True).click()
                await page.wait_for_selector('#f_connection')
                await page.locator('#dlg').get_by_role('button',name='Cancel',exact=True).click()
                await row.locator('[data-account-menu-trigger]').click()
                await page.keyboard.press('Escape')
                assert await page.locator('#accountActionMenu').is_hidden()
                output=Path('diagnostics');output.mkdir(exist_ok=True)
                await page.screenshot(path=str(output/'accounts-layout-desktop.png'))
                await page.set_viewport_size({'width':390,'height':1000})
                await row.scroll_into_view_if_needed()
                await page.screenshot(path=str(output/'accounts-layout-mobile.png'))
                assert not errors,errors
                print('PASS 22-account layout, viewport widths 390–1440, compact desktop rows, no horizontal overflow, proxy selectors, Open Web, action menu and keyboard close')
                print(metrics)
            finally:await browser.close()
    finally:
        fixture.http.close();fixture.pool._conn.close();patch.stopall();fixture.tmp.cleanup()


if __name__=='__main__':asyncio.run(main())
