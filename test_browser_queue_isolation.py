"""Offline queue isolation; never launches a browser."""
import asyncio
import tempfile
import unittest
from pathlib import Path
from contextlib import asynccontextmanager
from unittest.mock import patch
import browser_queue as module

class IsolationTests(unittest.IsolatedAsyncioTestCase):
    async def test_account_opens_while_video_full_and_vice_versa(self):
        with tempfile.TemporaryDirectory() as folder:
            video=module.BrowserQueue(Path(folder)/'video',limit=1)
            account=module.BrowserQueue(Path(folder)/'account',limit=1)
            @asynccontextmanager
            async def driver():
                yield 'fake'
            with patch.object(module,'QUEUE',video),patch.object(module,'ACCOUNT_QUEUE',account),patch.object(module,'_playwright',driver),patch.object(module,'START_INTERVAL',0),patch.object(module,'VIDEO_START_INTERVAL',0):
                async def open_session(is_video):
                    async with module.async_playwright(video=is_video) as value:
                        self.assertEqual(value,'fake')
                async with video.slot(video=True):
                    await asyncio.wait_for(open_session(False),1)
                async with account.slot():
                    await asyncio.wait_for(open_session(True),1)
            for queue in (video,account):
                db=queue.connect()
                self.assertEqual(db.execute('SELECT COUNT(*) FROM tickets').fetchone()[0],0)
                db.close()
