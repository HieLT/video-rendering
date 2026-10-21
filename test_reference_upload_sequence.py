"""Offline sequencing checks; no browser or Dola requests."""
import unittest
from unittest.mock import AsyncMock, Mock, patch
import video_worker_ui as worker

class SequenceTests(unittest.IsolatedAsyncioTestCase):
    def page(self, names):
        root=Mock()
        root.locator.return_value.evaluate_all=AsyncMock(side_effect=names)
        page=Mock()
        page.locator.return_value.filter.return_value.last=root
        page.is_closed.return_value=False
        return page

    async def test_one_image_per_call_and_order(self):
        page=self.page([['a.png'],['a.png','b.png'],['a.png','b.png']])
        with patch.object(worker,'_upload_reference_attempt',new_callable=AsyncMock) as upload, patch.object(worker,'_wait_for_manual_reference_upload',new_callable=AsyncMock) as manual:
            await worker.attach_reference_images(page,['a.png','b.png'])
            self.assertEqual([c.args[1:] for c in upload.await_args_list],[(['a.png'],1),(['b.png'],2)])
            manual.assert_not_awaited()

    async def test_failure_does_not_send_remaining_images(self):
        page=self.page([['a.png']])
        with patch.object(worker,'_upload_reference_attempt',new_callable=AsyncMock,side_effect=[None,RuntimeError('upload failed')]) as upload, patch.object(worker,'_wait_for_manual_reference_upload',new_callable=AsyncMock) as manual:
            await worker.attach_reference_images(page,['a.png','b.png','c.png'])
            self.assertEqual(upload.await_count,2)
            manual.assert_awaited_once_with(page,3)

    async def test_order_mismatch_stops_before_next_image(self):
        page=self.page([['wrong.png']])
        with patch.object(worker,'_upload_reference_attempt',new_callable=AsyncMock) as upload, patch.object(worker,'_wait_for_manual_reference_upload',new_callable=AsyncMock) as manual:
            await worker.attach_reference_images(page,['a.png','b.png'])
            self.assertEqual(upload.await_count,1)
            manual.assert_awaited_once()
