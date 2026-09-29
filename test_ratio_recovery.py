"""Regression for bounded ratio recovery; no live accounts or generation."""
import asyncio
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch
import video_worker_ui as worker

async def main():
    for state in ('minimized', 'normal'):
        session = SimpleNamespace(send=AsyncMock(return_value={'windowId': 7, 'bounds': {'windowState': state}}), detach=AsyncMock())
        page = SimpleNamespace(context=SimpleNamespace(new_cdp_session=AsyncMock(return_value=session)), bring_to_front=AsyncMock(), keyboard=SimpleNamespace(press=AsyncMock()))
        once = AsyncMock(side_effect=[worker.PlaywrightTimeoutError('not stable'), None])
        with patch.object(worker, '_select_video_ratio_once', once):
            await worker._select_video_ratio(page, '16:9', 'test', 5)
        assert once.await_count == 2
        assert all(c.args == (page, '16:9', 'test', 5) for c in once.await_args_list)
        assert session.send.await_count == (2 if state == 'minimized' else 1)
        session.detach.assert_awaited_once()
        page.bring_to_front.assert_awaited_once()
        page.keyboard.press.assert_awaited_once_with('Escape')
    once=AsyncMock(side_effect=worker.PlaywrightTimeoutError('still unstable'))
    with patch.object(worker, '_select_video_ratio_once', once):
        try: await worker._select_video_ratio(page, '16:9', 'test', 5)
        except worker.PlaywrightTimeoutError: pass
        else: raise AssertionError('Second timeout must propagate')
    assert once.await_count == 2
    print('PASS: minimized restore, normal focus, image count preserved, bounded retry')

if __name__ == '__main__': asyncio.run(main())
