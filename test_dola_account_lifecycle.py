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
    async def test_endpoint_guards(self):
        tree = ast.parse(Path("server.py").read_text(encoding="utf-8"))
        node = next(n for n in tree.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "admin_delete_dola")
        node.decorator_list = []
        meta = {"account_type":"google", "email":"test@example.com"}
        pool = SimpleNamespace(resolve_account=lambda n:n, accounts=["test"],
                               _meta=lambda n:meta, _locks={}, used_today=lambda n:2)
        queued = []
        env = dict(pool=pool, JOBS={}, WEB_SESSIONS={}, _admin_auth=lambda k:None,
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

    async def test_age_confirmation_only_on_dola_callback(self):
        async with async_playwright() as p:
            browser = await p.chromium.launch(headless=True)
            page = await browser.new_page()
            try:
                html = '''<div role="dialog" data-state="open"><h2 data-slot="dialog-title">Translated</h2>
                <button data-slot="dialog-close"></button><div data-slot="dialog-footer">
                <button data-dbx-name="button">Translated</button>
                <button data-dbx-name="button" class="bg-dbx-fill-highlight" onclick="document.body.dataset.accepted=1">Translated</button></div></div>'''
                await page.route("**/*", lambda route:route.fulfill(body=html))
                for url, expected in (("https://www.dola.com/chat",None),
                                      ("https://example.com/auth/callback",None),
                                      ("https://www.dola.com/auth/callback","1")):
                    await page.goto(url)
                    await lifecycle.confirm_age(page.context,lambda *a:None)
                    self.assertEqual(await page.locator("body").get_attribute("data-accepted"),expected)
            finally:
                await browser.close()

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
