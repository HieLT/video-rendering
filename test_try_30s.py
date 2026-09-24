import asyncio
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

import try_30s


class PreviewLifecycleTests(unittest.IsolatedAsyncioTestCase):
    async def test_draft_uploads_and_fills_without_submitting(self):
        page = MagicMock()
        page.goto = AsyncMock()
        duration = MagicMock()
        duration.wait_for = AsyncMock()
        duration.inner_text = AsyncMock(side_effect=["10s", "30s"])
        duration.click = AsyncMock()
        control = MagicMock()
        control.click = AsyncMock()
        control.first = duration
        control.last = control
        page.get_by_text.return_value = control
        root = MagicMock()
        root.get_by_text.return_value = control
        editor = MagicMock()
        with patch.object(try_30s, "_prepare_video_composer", AsyncMock()), \
             patch.object(try_30s, "_composer", AsyncMock(return_value=(editor, root))), \
             patch.object(try_30s, "attach_reference_images", AsyncMock()) as upload, \
             patch.object(try_30s, "_fill_video_prompt", AsyncMock(return_value=editor)) as fill:
            await try_30s.prepare_draft(page, "acc1", "my prompt", "seedance-2.5",
                                       "16:9", ["first.png", "second.png"], MagicMock())
            upload.assert_awaited_once_with(page, ["first.png", "second.png"])
            fill.assert_awaited_once_with(page, "my prompt", "acc1")
            editor.press.assert_not_called()
            page.keyboard.press.assert_not_called()
            page.get_by_text.assert_any_call("30s", exact=True)
            self.assertEqual([c.args[0] for c in page.get_by_text.call_args_list],
                             ["Dreamina Seedance 2.5", "16:9", "30s"])

    async def check_browser_lifetime(self, failure=None):
        callbacks = {}
        context = MagicMock()
        context.pages = [MagicMock()]
        context.on.side_effect = lambda event, callback: callbacks.update({event: callback})
        manager = MagicMock()
        manager.__aenter__ = AsyncMock(return_value=MagicMock())
        manager.__aexit__ = AsyncMock()
        prepared = asyncio.Event()

        async def prepare(*args):
            args[-1]("Ready")
            prepared.set()
            if failure:
                raise failure

        job = {}
        with patch.object(try_30s, "async_playwright", return_value=manager), \
             patch.object(try_30s, "launch_account_context", AsyncMock(return_value=context)), \
             patch.object(try_30s, "prepare_draft", side_effect=prepare):
            task = asyncio.create_task(try_30s.run_preview(
                "acc1", "test prompt", "seedance-2.0", "16:9", [], job))
            await asyncio.wait_for(prepared.wait(), 1)
            await asyncio.sleep(0)
            self.assertFalse(task.done(), "Browser must stay open after the attempt")
            manager.__aexit__.assert_not_awaited()
            context.close.assert_not_called()
            self.assertEqual(job["result"], "failed" if failure else "success")
            callbacks["close"]()
            await asyncio.wait_for(task, 1)
            manager.__aexit__.assert_awaited_once()

    async def test_success_waits_for_user_to_close(self):
        await self.check_browser_lifetime()

    async def test_selection_error_keeps_browser_open(self):
        await self.check_browser_lifetime(TimeoutError("30s option missing"))

    async def test_navigation_error_keeps_browser_open(self):
        await self.check_browser_lifetime(RuntimeError("Navigation failed"))


if __name__ == "__main__":
    unittest.main()
