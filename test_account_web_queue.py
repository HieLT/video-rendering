"""Virtual-clock queue checks without browser launches or real accounts."""
import asyncio
import ast
import re
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest.mock import AsyncMock, patch

from account_web_queue import AccountWebQueue


class QueueTests(unittest.IsolatedAsyncioTestCase):
    async def test_domain_scope_auth_and_stop_api(self):
        from test_account_connections import ConnectionTests
        fixture=ConnectionTests()
        fixture.setUp()
        try:
            fixture.pool.set_email('one','one@Example.com')
            fixture.pool.set_email('two','two@other.com')
            fixture.ns.update(re=re,config=SimpleNamespace(ADMIN_KEY='fixture-admin'),
                              admin_account_open_web=AsyncMock(return_value={'status':'starting'}))
            names={'DomainOpenWeb','open_web_domain','stop_domain_open_web'}
            tree=ast.parse(Path('server.py').read_text(encoding='utf-8'))
            exec(compile(ast.Module(body=[n for n in tree.body if getattr(n,'name',None) in names],type_ignores=[]),'server.py','exec'),fixture.ns)
            body=fixture.ns['DomainOpenWeb'](domain='@EXAMPLE.COM')
            with self.assertRaises(fixture.ns['HTTPException']):
                await fixture.ns['open_web_domain'](body,x_admin_key='wrong')
            result=await fixture.ns['open_web_domain'](body,x_admin_key='fixture-admin')
            self.assertEqual(result['queued'],1)
            await fixture.ns['DOMAIN_WEB_QUEUE'].task
            fixture.ns['admin_account_open_web'].assert_awaited_once_with('one',x_admin_key='fixture-admin')
            await fixture.ns['stop_domain_open_web'](x_admin_key='fixture-admin')
            self.assertEqual(fixture.ns['DOMAIN_WEB_QUEUE'].pending,[])
        finally:
            fixture.http.close();fixture.pool._conn.close();patch.stopall();fixture.tmp.cleanup()

    async def test_spacing_capacity_and_release(self):
        clock = [0.0]
        active = {}
        starts = []
        maximum = [0]
        async def sleep(seconds):
            clock[0] += seconds
            for name, expiry in list(active.items()):
                if expiry <= clock[0]:
                    active.pop(name)
        async def open_account(name):
            active[name] = clock[0] + 100
            starts.append(clock[0])
            maximum[0] = max(maximum[0], len(active))
            return {'status': 'starting'}
        queue = AccountWebQueue(open_account, lambda: len(active), clock=lambda:clock[0], sleep=sleep)
        queue.enqueue([str(n) for n in range(23)])
        queue.enqueue(['0', '1'])
        await queue.task
        self.assertEqual(len(starts), 23)
        self.assertEqual(maximum[0], 10)
        self.assertTrue(all(b-a >= 5 for a,b in zip(starts,starts[1:])))
        self.assertGreaterEqual(starts[10],100)
        self.assertEqual(queue.snapshot()['queued'],0)

    async def test_failure_does_not_abort_and_stop_preserves_windows(self):
        active = {'existing': True}
        async def open_account(name):
            if name == 'busy':
                raise RuntimeError('busy')
            active[name] = True
            return {'status':'starting'}
        queue = AccountWebQueue(open_account,lambda:len(active),interval=0)
        queue.enqueue(['busy','ok'])
        await queue.task
        self.assertEqual(queue.skipped,1)
        self.assertEqual(queue.opened,1)
        queue.capacity=2
        queue.enqueue(['waiting'])
        await asyncio.sleep(0)
        queue.stop()
        await asyncio.gather(queue.task,return_exceptions=True)
        self.assertEqual(queue.pending,[])
        self.assertEqual(set(active),{'existing','ok'})


if __name__ == '__main__':
    unittest.main()
