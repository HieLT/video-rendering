"""Local authenticated proxy fixtures; no external network or real credentials."""
import asyncio
import base64
import unittest
from urllib.parse import urlsplit
from proxy_bridge import ProxyBridge, prepare_browser_proxy


class BridgeTests(unittest.IsolatedAsyncioTestCase):
    async def setup_bridge(self, status=200):
        self.headers = []
        async def upstream(reader, writer):
            try:
                header = await reader.readuntil(b'\r\n\r\n')
                self.headers.append(header)
                writer.write(f'HTTP/1.1 {status} Fixture\r\n\r\n'.encode())
                await writer.drain()
                if status == 200:
                    data = await reader.read(4)
                    writer.write(data.upper())
                    await writer.drain()
            finally:
                writer.close()
                await writer.wait_closed()
        server = await asyncio.start_server(upstream, '127.0.0.1', 0)
        self.addCleanup(server.close)
        port = server.sockets[0].getsockname()[1]
        options, bridge = await prepare_browser_proxy({'server': f'http://127.0.0.1:{port}', 'username': 'fixture', 'password': 'secret'})
        self.addAsyncCleanup(bridge.aclose)
        self.assertNotIn('password', options)
        self.assertNotIn('username', options)
        target = urlsplit(options['server'])
        return bridge, target

    async def test_connect_auth_and_bidirectional_tunnel(self):
        bridge, target = await self.setup_bridge()
        reader, writer = await asyncio.open_connection(target.hostname, target.port)
        try:
            writer.write(b'CONNECT fixture.test:443 HTTP/1.1\r\nHost: fixture.test:443\r\nProxy-Authorization: wrong\r\n\r\n')
            await writer.drain()
            self.assertIn(b'200', await reader.readuntil(b'\r\n\r\n'))
            writer.write(b'ping')
            await writer.drain()
            self.assertEqual(await reader.readexactly(4), b'PING')
            expected = base64.b64encode(b'fixture:secret')
            self.assertIn(b'Proxy-Authorization: Basic ' + expected, self.headers[0])
            self.assertNotIn(b'wrong', self.headers[0])
        finally:
            writer.close()
            await writer.wait_closed()
        bridge.close()
        await bridge.server.wait_closed()
        with self.assertRaises(OSError):
            await asyncio.open_connection(target.hostname, target.port)

    async def test_upstream_auth_failure_is_forwarded_without_fallback(self):
        _, target = await self.setup_bridge(407)
        reader, writer = await asyncio.open_connection(target.hostname, target.port)
        try:
            writer.write(b'CONNECT fixture.test:443 HTTP/1.1\r\n\r\n')
            await writer.drain()
            self.assertIn(b'407', await reader.readuntil(b'\r\n\r\n'))
            self.assertEqual(await reader.read(), b'')
        finally:
            writer.close()
            await writer.wait_closed()

    async def test_direct_and_unauthenticated_routes_have_no_relay(self):
        self.assertEqual(await prepare_browser_proxy(None), (None, None))
        proxy = {'server': 'http://127.0.0.1:9999'}
        self.assertEqual(await prepare_browser_proxy(proxy), (proxy, None))


if __name__ == '__main__':
    unittest.main()
