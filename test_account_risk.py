import asyncio,json,sqlite3,tempfile,unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import patch
from account_risk import contains_shark_block,quarantine,check_risk,install_risk_monitor,AccountRiskBlockedError
from browser_pool import BrowserPool, EnvironmentChangedError

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
    self.assertEqual(rows['bad']['auth_state'],'active')
    self.assertTrue(rows['peer']['scheduling'])
    self.assertTrue(rows['other']['scheduling']);self.assertTrue(rows['sub']['scheduling'])
    pool.auth_result('peer','active',identity={'identity_hash':'peer'})
    self.assertTrue(next(r for r in pool.list_accounts() if r['name']=='peer')['scheduling'])
   finally:pool._conn.close()
 async def test_monitor_and_terminal_error(self):
  class Context:
   def __init__(self):self.handlers={};self.routes={}
   def on(self,event,callback):self.handlers[event]=callback
   async def route(self,pattern,callback):self.routes[pattern]=callback
  class Response:
   url='https://www.dola.com/chat/completion'
   async def text(self):return 'event: STREAM_ERROR\ndata: {"error_code":710022002}\n'
  c=Context();await install_risk_monitor(c,'fixture')
  with patch('account_risk.quarantine') as block:
   c.handlers['response'](Response());await asyncio.sleep(.01)
   block.assert_not_called()
   with self.assertRaises(AccountRiskBlockedError):check_risk(c)

 async def test_logout_is_blocked_only_after_exact_shark_error(self):
  class Context:
   def __init__(self):self.handlers={};self.routes={}
   def on(self,event,callback):self.handlers[event]=callback
   async def route(self,pattern,callback):self.routes[pattern]=callback
  class Route:
   def __init__(self):
    from unittest.mock import AsyncMock
    self.abort=AsyncMock();self.continue_=AsyncMock()
  context=Context();await install_risk_monitor(context,'fixture')
  route=Route();handler=context.routes['**/passport/web/logout/**']
  await handler(route);route.continue_.assert_awaited_once();route.abort.assert_not_awaited()
  context._dola_risk_error='SHARK_BLOCKED'
  route=Route();await handler(route);route.abort.assert_awaited_once();route.continue_.assert_not_awaited()

 def test_strict_egress_change_sets_cooldown_before_dola(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);accounts=root/'accounts';(accounts/'a').mkdir(parents=True)
   pool=BrowserPool(str(accounts),str(root/'pool.db'))
   try:
    pool.observe_egress('a','203.0.113.10')
    with patch('browser_pool.time.time',return_value=1000), patch('browser_pool.config.EGRESS_CHANGE_COOLDOWN_SECONDS',1800):
     with self.assertRaises(EnvironmentChangedError):pool.observe_egress('a','203.0.113.11')
    row=pool._meta('a');self.assertEqual(row['egress_ip'],'203.0.113.11');self.assertEqual(row['cooldown_until'],2800)
   finally:pool._conn.close()

 def test_shark_backoff_increases_for_account_and_egress(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);accounts=root/'accounts';(accounts/'a').mkdir(parents=True)
   pool=BrowserPool(str(accounts),str(root/'pool.db'))
   try:
    pool.observe_egress('a','203.0.113.10')
    with patch('browser_pool.time.time',return_value=1000):self.assertEqual(pool._mark_throttled('a'),1800)
    with patch('browser_pool.time.time',return_value=1100):self.assertEqual(pool._mark_throttled('a'),3600)
    row=pool._meta('a');self.assertEqual(row['throttle_streak'],2);self.assertEqual(row['cooldown_until'],4700)
    egress=pool._conn.execute('SELECT * FROM egress_throttle WHERE egress_ip=?',('203.0.113.10',)).fetchone()
    self.assertEqual(egress['streak'],2);self.assertEqual(egress['blocked_until'],1340)
    pool._clear_throttle_success('a')
    self.assertEqual(pool._meta('a')['throttle_streak'],0)
    self.assertEqual(pool._conn.execute('SELECT streak FROM egress_throttle WHERE egress_ip=?',('203.0.113.10',)).fetchone()['streak'],0)
   finally:pool._conn.close()

 async def test_submit_pacing_uses_account_and_egress_timestamp(self):
  with tempfile.TemporaryDirectory() as tmp:
   root=Path(tmp);accounts=root/'accounts';(accounts/'a').mkdir(parents=True)
   pool=BrowserPool(str(accounts),str(root/'pool.db'))
   try:
    pool.observe_egress('a','203.0.113.10')
    clock=[100.0]
    def now():return clock[0]
    async def advance(seconds):clock[0]+=seconds
    with patch('browser_pool.time.time',side_effect=now), patch('browser_pool.asyncio.sleep',side_effect=advance), patch('browser_pool.config.MIN_SUBMIT_INTERVAL_SECONDS',15):
     await pool._pace_submit('a')
     clock[0]=105.0
     await pool._pace_submit('a')
    self.assertEqual(clock[0],115.0)
    self.assertEqual(pool._meta('a')['last_submit_at'],115.0)
   finally:pool._conn.close()

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
