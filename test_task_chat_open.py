import asyncio
import unittest
from unittest.mock import AsyncMock, patch
import browser

class Page:
    def __init__(self, url):
        self.url=url
        self.bring_to_front=AsyncMock()
        self.goto=AsyncMock()
        self.close=AsyncMock()
    def is_closed(self): return False

class ChatOpenTests(unittest.IsolatedAsyncioTestCase):
    async def test_two_tasks_same_account_open_exact_tabs(self):
        a=Page("https://www.dola.com/chat/111")
        b=Page("https://www.dola.com/chat/222?view=1")
        class Context:
            pages=[a,b]
            new_page=AsyncMock()
        with patch.dict(browser.ACTIVE_CONTEXTS, {"account":(Context(),False)},clear=True):
            self.assertTrue(await browser.focus_task_conversation("account","222"))
            b.bring_to_front.assert_awaited_once()
            a.bring_to_front.assert_not_awaited()
            a.goto.assert_not_awaited()
            Context.new_page.assert_not_awaited()

    async def test_new_tab_without_navigating_worker_and_no_duplicates(self):
        worker=Page("https://www.dola.com/chat/111")
        new=Page("about:blank")
        class Context:
            def __init__(self): self.pages=[worker]
            async def new_page(self):
                self.pages.append(new)
                return new
        context=Context()
        async def goto(url, **kwargs): new.url=url
        new.goto.side_effect=goto
        with patch.dict(browser.ACTIVE_CONTEXTS,{"account":(context,False)},clear=True), patch.dict(browser.CHAT_OPEN_LOCKS,{},clear=True):
            await asyncio.gather(browser.focus_task_conversation("account","222"),browser.focus_task_conversation("account","222"))
        self.assertEqual(len(context.pages),2)
        worker.goto.assert_not_awaited()
        new.goto.assert_awaited_once()
        self.assertEqual(new.url,"https://www.dola.com/chat/222")

    async def test_no_context_returns_false(self):
        with patch.dict(browser.ACTIVE_CONTEXTS,{},clear=True):
            self.assertFalse(await browser.focus_task_conversation("account","222"))

class SessionTests(unittest.IsolatedAsyncioTestCase):
    async def test_login_without_session_expires_but_cookie_or_hidden_login_does_not(self):
        class Locator:
            async def all(self): return [self]
            is_visible=AsyncMock(return_value=True)
        class LoginPage:
            url="https://www.dola.com/chat"
            def get_by_text(self, pattern): return locator
        class Context:
            cookies=AsyncMock(return_value=[])
        locator=Locator()
        with self.assertRaises(browser.AccountSessionExpiredError):
            await browser.ensure_account_session(LoginPage(),Context())
        Context.cookies.return_value=[{"name":"sessionid","value":"fixture"}]
        await browser.ensure_account_session(LoginPage(),Context())
        Context.cookies.return_value=[]
        locator.is_visible.return_value=False
        await browser.ensure_account_session(LoginPage(),Context())
