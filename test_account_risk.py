import asyncio,json,sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from account_risk import contains_shark_block,quarantine,check_risk,install_risk_monitor,AccountRiskBlockedError
from browser_pool import BrowserPool

class RiskTests(unittest.IsolatedAsyncioTestCase):
 def test_exact_code(self):
  self.assertTrue(contains_shark_block('event: STREAM_ERROR\ndata: {"error_code":710022002}\n\n'))
  self.assertTrue(contains_shark_block('{"data":{"code":"710022002"}}'))
  self.assertFalse(contains_shark_block('{"message":"high demand 710022002","code":500}'))
  self.assertFalse(contains_shark_block('broken stream'))
 def test_account_only_persistence(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp); accounts=root/'accounts';accounts.mkdir()
   for name in ('bad','peer','other','sub'): (accounts/name).mkdir()
   pool=BrowserPool(str(accounts),str(root/'pool.db'))
   try:
    for name,email in [('bad','a@mtnewtoy.us'),('peer','B@MTNEWTOY.US'),('other','x@gmail.com'),('sub','x@sub.mtnewtoy.us')]:
     pool._ensure_meta(name);pool.set_email(name,email)
     pool.auth_result(name,'active',identity={'identity_hash':name})
    quarantine('bad',root/'pool.db')
    rows={r['name']:r for r in pool.list_accounts()}
    self.assertEqual(rows['bad']['auth_state'],'limited')
    self.assertTrue(rows['peer']['scheduling'])
    self.assertTrue(rows['other']['scheduling']);self.assertTrue(rows['sub']['scheduling'])
    pool.auth_result('peer','active',identity={'identity_hash':'peer'})
    self.assertTrue(next(r for r in pool.list_accounts() if r['name']=='peer')['scheduling'])
   finally:pool._conn.close()
 async def test_monitor_and_terminal_error(self):
  class Context:
   def __init__(self):self.handlers={}
   def on(self,event,callback):self.handlers[event]=callback
  class Response:
   url='https://www.dola.com/chat/completion'
   async def text(self):return 'event: STREAM_ERROR\ndata: {"error_code":710022002}\n'
  c=Context();await install_risk_monitor(c,'fixture')
  with patch('account_risk.quarantine') as block:
   c.handlers['response'](Response());await asyncio.sleep(.01)
   block.assert_called_once_with('fixture')
   with self.assertRaises(AccountRiskBlockedError):check_risk(c)

class SchedulerRiskTests(unittest.IsolatedAsyncioTestCase):
 async def test_no_retry_or_rotation(self):
  from test_video_schedule import ScheduleTests
  from unittest.mock import AsyncMock
  fixture=ScheduleTests()
  fixture.setUp()
  try:
   submit=AsyncMock(side_effect=AccountRiskBlockedError('SHARK_BLOCKED 710022002'))
   with patch.object(fixture.scheduler,'retry') as retry:
    await fixture.drive(submit,AsyncMock())
    retry.assert_not_called()
   submit.assert_awaited_once()
   row=fixture.store.get('job')
   self.assertEqual(row['status'],'needs_recovery')
   self.assertEqual(row['phase'],'review')
  finally:fixture.tearDown()

if __name__=='__main__':unittest.main()
