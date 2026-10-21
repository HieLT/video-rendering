"""Offline lifecycle regressions: recover once and fail without leaking a browser."""
import unittest
from types import SimpleNamespace
from unittest.mock import AsyncMock, Mock, patch
import browser

class StartupTests(unittest.IsolatedAsyncioTestCase):
    async def test_ready_worker_uses_manifest_entrypoint(self):
        worker = SimpleNamespace(
            url='chrome-extension://test/service-worker-v2.js',
            evaluate=AsyncMock(return_value=True),
        )
        context = SimpleNamespace(service_workers=[worker])
        with patch.object(browser.config, 'EXTENSION_DIR', 'extensions/dola30'):
            self.assertIs(await browser._ready_extension(context), worker)
        worker.evaluate.assert_awaited_once()
        self.assertIn('upper_bound:30', worker.evaluate.await_args.args[0])
        self.assertIn('upper_bound === 15', worker.evaluate.await_args.args[0])

    async def test_lifecycle(self):
        for mode in ('healthy', 'recover', 'unavailable'):
            with self.subTest(mode=mode):
                first = SimpleNamespace(close=AsyncMock(), on=Mock())
                launch = AsyncMock(return_value=first)
                p = SimpleNamespace(chromium=SimpleNamespace(launch_persistent_context=launch))
                worker = object()
                outcomes = [worker] if mode == 'healthy' else [RuntimeError('not running'), worker if mode == 'recover' else RuntimeError('still unavailable')]
                with patch('browser.Path.exists', return_value=True), patch.object(browser.config, 'EXTENSION_ENABLED', True), patch('browser._ready_extension', new=AsyncMock(side_effect=outcomes)), patch('browser._reload_extension', new=AsyncMock()) as reload, patch('browser._attach_extension_before_navigation', new=AsyncMock()) as attach:
                    if mode == 'unavailable':
                        with self.assertRaisesRegex(RuntimeError, 'Dola30 extension unavailable'):
                            await browser.launch_account_context(p, 'test', use_extension=True)
                        first.close.assert_awaited_once()
                        self.assertNotIn('test', browser.ACTIVE_CONTEXTS)
                    else:
                        result = await browser.launch_account_context(p, 'test', use_extension=True)
                        self.assertIs(result, first)
                        attach.assert_awaited_once_with(result, worker)
                        browser.ACTIVE_CONTEXTS.pop('test')
                    self.assertEqual(launch.await_count, 1)
                    self.assertEqual(reload.await_count, 0 if mode == 'healthy' else 1)
                    self.assertEqual(first.close.await_count, 1 if mode == 'unavailable' else 0)

if __name__ == '__main__':
    unittest.main()
