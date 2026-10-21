import ast
import asyncio
import tempfile
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch, AsyncMock

from fastapi import Header, HTTPException
from browser_queue import async_playwright
import dola_account_lifecycle as lifecycle


class LifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_overlay_race_retries_without_aborting(self):
        button = SimpleNamespace(click=AsyncMock(side_effect=[
            lifecycle.BrowserTimeout('dialog-overlay intercepts pointer events'), None]))
        self.assertFalse(await lifecycle.login_click(button))
        self.assertTrue(await lifecycle.login_click(button))

    async def test_logout_navigation_race_and_modal_only(self):
        login = SimpleNamespace(is_visible=AsyncMock(side_effect=[
            lifecycle.BrowserError('Execution context was destroyed, most likely because of a navigation'), False]))
        modal = SimpleNamespace(is_visible=AsyncMock(return_value=True))
        page = SimpleNamespace(is_closed=lambda: False, url='https://www.dola.com/chat/',
                               get_by_test_id=lambda tid: login if tid == 'to_login_button' else modal)
        context = SimpleNamespace(pages=[page])
        self.assertIs(await lifecycle.wait_for_logged_out_page(context, timeout=2), page)

    async def test_logout_unexpected_error_is_not_suppressed(self):
        page = SimpleNamespace(is_closed=lambda: False, url='https://www.dola.com/chat/',
            get_by_test_id=lambda tid: SimpleNamespace(is_visible=AsyncMock(side_effect=lifecycle.BrowserError('unexpected failure'))))
        with self.assertRaises(lifecycle.BrowserError):
            await lifecycle.wait_for_logged_out_page(SimpleNamespace(pages=[page]), timeout=1)

    async def test_age_onboarding_is_scoped_to_callback(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                html = '''<div role="dialog" data-state="open">
                <p data-slot="dialog-description">Confirm you are 18 or older</p>
                <div data-slot="dialog-footer"><button>No</button>
                <button class="bg-dbx-fill-highlight" onclick="document.body.dataset.accepted='yes'">OK</button></div></div>'''
                await page.route('**/*', lambda route: route.fulfill(body=html))
                for url in ('https://example.com/auth/callback', 'https://www.dola.com/chat'):
                    await page.goto(url)
                    self.assertFalse(await lifecycle.confirm_adult_onboarding(page))
                await page.goto('https://www.dola.com/auth/callback')
                self.assertTrue(await lifecycle.confirm_adult_onboarding(page))
                self.assertEqual(await page.locator('body').get_attribute('data-accepted'), 'yes')
                await page.locator('[data-slot="dialog-description"]').evaluate('(el)=>el.textContent="Unrelated confirmation"')
                self.assertFalse(await lifecycle.confirm_adult_onboarding(page))
            finally:
                await browser.close()

    async def test_relogin_follows_new_callback_tab(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                context = await browser.new_context()
                await context.add_cookies([dict(name='sessionid', value='fixture', domain='www.dola.com', path='/')])
                async def route(request):
                    callback = '/auth/callback' in request.request.url
                    body = ('<div data-testid="sidebar_footer_setting_entry">Account</div>' if callback else
                            '''<div data-testid="login_content"><button data-dbx-name="button"
                            onclick="window.open('/auth/callback')">Google</button></div>''')
                    await request.fulfill(body=body)
                await context.route('**/*', route)
                page = await context.new_page()
                await lifecycle.relogin(context, page, 'test@example.com', lambda *a: None, timeout=10)
                self.assertEqual(len(context.pages), 2)
            finally:
                await browser.close()

    async def test_endpoint_guards(self):
        tree = ast.parse(Path("server.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "admin_delete_dola")
        node.decorator_list = []
        meta = {"account_type":"google", "email":"test@example.com"}
        pool = SimpleNamespace(resolve_account=lambda n:n, accounts=["test"],
                               _meta=lambda n:meta, _locks={}, used_today=lambda n:2, auth_result=lambda *a:None)
        queued = []
        env = dict(pool=pool, scheduler=SimpleNamespace(reservation_ids=lambda n:set()), JOBS={}, WEB_SESSIONS={}, _admin_auth=lambda k:None,
                   Header=Header, HTTPException=HTTPException,
                   asyncio=SimpleNamespace(create_task=queued.append),
                   _run_dola_delete=lambda *args:args, time=SimpleNamespace(time=lambda:0))
        exec(compile(ast.Module(body=[node],type_ignores=[]), "server.py", "exec"),env)
        endpoint=env["admin_delete_dola"]
        with patch.object(lifecycle,"read_checkpoint",return_value={}), patch("browser.ACTIVE_CONTEXTS",{}):
            for provider, used, expected in (("facebook",2,400),):
                meta["account_type"]=provider; pool.used_today=lambda n:used
                with self.assertRaises(HTTPException) as caught: await endpoint("test")
                self.assertEqual(caught.exception.status_code,expected)
            meta["account_type"]="google"
            for used in (0, 1, 2):
                env["JOBS"].clear()
                pool.used_today=lambda n:used
                await endpoint("test")
            self.assertEqual(queued,[("test",False)] * 3)
            with self.assertRaises(HTTPException) as caught: await endpoint("test")
            self.assertEqual(caught.exception.status_code,409)
        env["JOBS"].clear(); pool.used_today=lambda n:0
        with patch.object(lifecycle,"read_checkpoint",return_value={"phase":"submitting"}), patch("browser.ACTIVE_CONTEXTS",{}):
            with self.assertRaises(HTTPException): await endpoint("test")
            await endpoint("test",resume=True)
            self.assertEqual(queued[-1],("test",True))

        env["JOBS"].clear()
        env["scheduler"].reservation_ids = lambda n: {"unfinished-task"}
        with self.assertRaises(HTTPException) as caught:
            await endpoint("test", resume=True)
        self.assertEqual(caught.exception.status_code, 409)

    async def test_verified_renewal_resets_usage_and_blocks(self):
        from browser_pool import BrowserPool
        with tempfile.TemporaryDirectory() as directory:
            root = Path(directory)
            (root / "accounts" / "fixture").mkdir(parents=True)
            pool = BrowserPool(str(root / "accounts"), str(root / "pool.db"))
            try:
                with self.assertRaises(RuntimeError):
                    pool.reset_renewed_account_usage("fixture")
                pool._claim("fixture")
                with pool._conn:
                    pool._conn.execute("UPDATE accounts_meta SET auth_state='active', login_ok=1, rate_limited_until=9999999999, quota_blocked_until=9999999999, cooldown_until=9999999999, credit_balance=0 WHERE name='fixture'")
                pool.reset_renewed_account_usage("fixture")
                self.assertEqual(pool.used_today("fixture"), 0)
                meta = pool._meta("fixture")
                for field in ("rate_limited_until", "quota_blocked_until", "cooldown_until"):
                    self.assertEqual(meta[field], 0)
                self.assertIsNone(meta["credit_balance"])
            finally:
                pool._conn.close()

    async def test_worker_resets_only_after_verified_login(self):
        import logging
        from contextlib import asynccontextmanager
        from unittest.mock import Mock
        tree = ast.parse(Path("server.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "_run_dola_delete")
        @asynccontextmanager
        async def activity(*args):
            yield
        for state in ("active", "expired"):
            pool = SimpleNamespace(account_activity=activity, _meta=lambda n: {"email":"test@example.com"},
                                   auth_result=Mock(return_value="active"), reset_renewed_account_usage=Mock())
            env = dict(pool=pool, JOBS={"test":{}}, logging=logging)
            exec(compile(ast.Module(body=[node], type_ignores=[]), "server.py", "exec"), env)
            with patch.object(lifecycle, "read_checkpoint", return_value={}), patch.object(lifecycle, "checkpoint") as saved, patch.object(lifecycle, "delete_and_relogin", new_callable=AsyncMock), patch("browser.inspect_account_session", new=AsyncMock(return_value={"state":state})):
                await env["_run_dola_delete"]("test")
                if state == "active":
                    pool.reset_renewed_account_usage.assert_called_once_with("test")
                    saved.assert_called_once_with("test", "complete")
                    self.assertEqual(env["JOBS"]["test"]["status"], "success")
                else:
                    pool.reset_renewed_account_usage.assert_not_called()
                    saved.assert_not_called()
                    self.assertEqual(env["JOBS"]["test"]["status"], "failed")

    async def test_dashboard_menu_and_confirmation(self):
        html = Path("web/index.html").read_text(encoding="utf-8")
        menu = html[html.index('function closeAccountActions()'):html.index("document.addEventListener('click',event=>")]
        action = html[html.index('async function deleteDolaAcc('):html.index('async function resetQuota(')]
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            try:
                page = await browser.new_page()
                await page.set_content('<button id="trigger">Actions</button>')
                await page.evaluate('''() => {
                    window.q=s=>document.querySelector(s); window.esc=s=>s;
                    window.accountMenuTrigger=null;
                    window.accountSnapshot=[{name:'test',account_type:'google'}];
                    window.accountJobs={}; window.calls=[];
                    window.api=async (...args)=>calls.push(args); window.refresh=()=>{};
                }''')
                await page.evaluate("source => { window.eval(source); }", menu+action)
                await page.evaluate("openAccountActions('test',q('#trigger'))")
                self.assertEqual(await page.get_by_role('menuitem', name='Delete Dola & re-login').count(), 1)
                await page.evaluate("window.confirm=()=>false; deleteDolaAcc('test')")
                self.assertEqual(await page.evaluate('calls.length'), 0)
                await page.evaluate("window.confirm=()=>true; deleteDolaAcc('test')")
                self.assertEqual(await page.evaluate('calls[0][0]'), '/api/admin/accounts/test/delete-dola')
                await page.evaluate("closeAccountActions(); accountSnapshot[0].dola_delete_phase='submitting'; openAccountActions('test',q('#trigger'))")
                self.assertEqual(await page.get_by_role('menuitem', name='Resume Google login').count(), 1)
                await page.evaluate("closeAccountActions(); accountSnapshot[0].active_requests=1; openAccountActions('test',q('#trigger'))")
                self.assertTrue(await page.get_by_role('menuitem', name='Resume Google login').is_disabled())
            finally:
                await browser.close()

    async def test_logout_on_other_tab_with_stale_cookie(self):
        closed = SimpleNamespace(is_closed=lambda:True)
        old = SimpleNamespace(is_closed=lambda:False, url="https://www.dola.com/delete-account",
                              get_by_test_id=lambda _:SimpleNamespace(is_visible=AsyncMock(return_value=False)))
        logged_out = SimpleNamespace(is_closed=lambda:False, url="https://dola.com/chat",
                              get_by_test_id=lambda _:SimpleNamespace(is_visible=AsyncMock(return_value=True)))
        context = SimpleNamespace(pages=[closed,old,logged_out],
                                  cookies=AsyncMock(return_value=[{"name":"sessionid","value":"stale"}]))
        self.assertIs(await lifecycle.wait_for_logged_out_page(context,timeout=1),logged_out)
        context.cookies.assert_not_called()

    async def test_resume_never_repeats_deletion(self):
        from contextlib import asynccontextmanager
        @asynccontextmanager
        async def playwright():
            yield object()
        page = SimpleNamespace(goto=AsyncMock())
        context = SimpleNamespace(pages=[page], close=AsyncMock())
        for phase in ("submitting", "deleted"):
            with patch.object(lifecycle, "read_checkpoint", return_value={"phase":phase}), patch.object(lifecycle, "async_playwright", playwright), patch.object(lifecycle, "launch_account_context", new=AsyncMock(return_value=context)), patch.object(lifecycle, "unique_click", new_callable=AsyncMock) as click, patch.object(lifecycle, "relogin", new_callable=AsyncMock) as login:
                await lifecycle.delete_and_relogin("test", "test@example.com", lambda *args:None, resume=True)
                click.assert_not_called()
                login.assert_awaited_once()
                with self.assertRaises(RuntimeError):
                    await lifecycle.delete_and_relogin("test", "test@example.com", lambda *args:None)

    async def test_translated_dom_and_google_choice(self):
        async with async_playwright() as p:
            browser=await p.chromium.launch(headless=True)
            page=await browser.new_page()
            try:
                await page.set_content('''<div role="dialog" data-state="open" aria-hidden="true"><button class="bg-dbx-function-danger">hidden</button></div>
                <div role="dialog" data-state="open"><div data-slot="dialog-title" status="warning"></div>
                <div data-slot="dialog-footer"><button data-dbx-name="button">Cancel</button>
                <button data-dbx-name="button" class="bg-dbx-function-danger" onclick="document.body.dataset.confirmed=1"><font>Translated</font></button></div></div>''')
                await lifecycle.unique_click(page.locator(lifecycle.CONFIRM))
                self.assertEqual(await page.locator("body").get_attribute("data-confirmed"),"1")
                await page.locator('[role="dialog"]:not([aria-hidden]) [data-slot="dialog-footer"]').evaluate('(el)=>el.appendChild(el.lastElementChild.cloneNode(true))')
                with self.assertRaises(Exception): await lifecycle.unique_click(page.locator(lifecycle.CONFIRM))
                await page.route("**/*",lambda route:route.fulfill(body='''<div data-identifier="other@example.com" onclick="document.body.dataset.chosen='wrong'">Same translated label</div><div data-identifier="test@example.com" onclick="document.body.dataset.chosen='right'">Same translated label</div>'''))
                await page.goto("https://accounts.google.com/test")
                await lifecycle.google_step(page.context,"TEST@example.com",lambda *a:None)
                self.assertEqual(await page.locator("body").get_attribute("data-chosen"),"right")
            finally:
                await browser.close()

if __name__ == "__main__":
    unittest.main()
