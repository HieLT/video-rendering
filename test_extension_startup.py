"""Offline lifecycle regressions: recover once and fail without leaking a browser."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import browser

class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_lifecycle(self):
        for mode in ('healthy', 'recover', 'unavailable'):
            with self.subTest(mode=mode):
                first = SimpleNamespace(close=AsyncMock(), on=Mock())
                second = SimpleNamespace(close=AsyncMock(), on=Mock())
                launch = AsyncMock(side_effect=[first, second])
                p = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
                worker = object()
                outcomes = [worker] if mode == 'healthy' else [RuntimeError('not running'), worker if mode == 'recover' else RuntimeError('still unavailable')]
                with patch('browser.Path.exists', return_value=True), patch.object(browser.config, 'EXTENSION_ENABLED', True), patch('browser._ready_extension', new=AsyncMock(side_effect=outcomes)), patch('browser._reload_extension', new=AsyncMock()) as reload, patch('browser._attach_extension_before_navigation', new=AsyncMock()) as attach:
                    if mode == 'unavailable':
                        with self.assertRaisesRegex(RuntimeError, 'Dola30 extension unavailable'):
                            await browser.launch_account_context(p, 'test', use_extension=True)
                        second.close.assert_awaited_once()
                        self.assertNotIn('test', browser.ACTIVE_CONTEXTS)
                    else:
                        result = await browser.launch_account_context(p, 'test', use_extension=True)
                        self.assertIs(result, first if mode == 'healthy' else second)
                        attach.assert_awaited_once_with(result, worker)
                        browser.ACTIVE_CONTEXTS.pop('test')
                    self.assertEqual(launch.await_count, 1 if mode == 'healthy' else 2)
                    self.assertEqual(reload.await_count, 0 if mode == 'healthy' else 1)
                    self.assertEqual(first.close.await_count, 0 if mode == 'healthy' else 1)

if __name__ == '__main__':
    unittest.main()
