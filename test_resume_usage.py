"""Recovery must not charge an existing generation again."""
import ast
import asyncio
from contextlib import asynccontextmanager
from pathlib import Path
from types import SimpleNamespace
import unittest
from unittest.mock import AsyncMock, Mock


class ResumeUsageTest(unittest.IsolatedAsyncioTestCase):
    async def test_repeated_recovery_preserves_usage_and_deadline(self):
        tree = ast.parse(Path("browser_pool.py").read_text(encoding="utf-8"))
        cls = next(n for n in tree.body if isinstance(n, ast.ClassDef) and n.name == "BrowserPool")
        method = next(n for n in cls.body if isinstance(n, ast.AsyncFunctionDef) and n.name == "resume_video")
        worker = AsyncMock(side_effect=[TimeoutError(), TimeoutError(), {"local_path": "video.mp4"}])
        scope = {"asyncio": asyncio, "resume_video": worker}
        exec(compile(ast.Module(body=[method], type_ignores=[]), "browser_pool.py", "exec"), scope)

        @asynccontextmanager
        async def activity(account, kind):
            yield

        pool = SimpleNamespace(semaphore=asyncio.Semaphore(1), _locks={},
                               account_activity=activity, _claim=Mock(),
                               _conn=Mock(), _set_credit_balance=Mock())
        run_resume = scope["resume_video"]
        scope["resume_video"] = worker
        poll = Mock()
        for _ in range(2):
            with self.assertRaises(TimeoutError):
                await run_resume(pool, "acc1", "conversation1", 60, on_poll=poll)
        result = await run_resume(pool, "acc1", "conversation1", 60, on_poll=poll)
        self.assertEqual(result, {"local_path": "video.mp4"})
        self.assertEqual(worker.await_count, 3)
        pool._claim.assert_not_called()
        pool._conn.execute.assert_not_called()
        pool._conn.commit.assert_not_called()
        self.assertIs(worker.call_args.kwargs["on_poll"], poll)
        worker.call_args.kwargs["on_balance"](4, "recovery")
        pool._set_credit_balance.assert_called_once_with("acc1", 4, "recovery")


if __name__ == "__main__":
    unittest.main()
