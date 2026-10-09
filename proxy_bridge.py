"""Loopback relay keeps upstream proxy auth independent of Chrome Fetch handlers."""
import asyncio
import base64
import ssl
from urllib.parse import urlsplit


class ProxyBridge:
    def __init__(self, proxy):
        self.upstream = urlsplit(proxy['server'])
        if self.upstream.scheme not in ('http', 'https') or not self.upstream.hostname:
            raise ValueError('Authenticated browser proxies must use HTTP or HTTPS')
        credentials = f"{proxy['username']}:{proxy.get('password', '')}"
        self.authorization = base64.b64encode(credentials.encode()).decode()
        self.server = None
        self.tasks = set()
        self.writers = set()
        self.closed = False

    async def start(self):
        self.server = await asyncio.start_server(self._serve, '127.0.0.1', 0, limit=65536)
        port = self.server.sockets[0].getsockname()[1]
        return {'server': f'http://127.0.0.1:{port}'}

    def close(self):
        if self.closed:
            return
        self.closed = True
        if self.server:
            self.server.close()
        for writer in list(self.writers):
            writer.close()
        for task in list(self.tasks):
            task.cancel()

    async def aclose(self):
        self.close()
        if self.server:
            await self.server.wait_closed()
        if self.tasks:
            await asyncio.gather(*list(self.tasks), return_exceptions=True)

    async def _serve(self, reader, writer):
        task = asyncio.current_task()
        self.tasks.add(task)
        self.writers.add(writer)
        upstream_writer = None
        pipes = []
        try:
            if self.closed:
                return
            header = await asyncio.wait_for(reader.readuntil(b'\r\n\r\n'), 30)
            lines = header.decode('iso-8859-1').split('\r\n')
            method = lines[0].split(' ', 1)[0]
            headers = [line for line in lines[1:] if line and
                       line.split(':', 1)[0].lower() not in ('proxy-authorization', 'proxy-connection', 'connection')]
            headers.append('Proxy-Authorization: Basic ' + self.authorization)
            if method != 'CONNECT':
                headers.extend(['Connection: close', 'Proxy-Connection: close'])
            forwarded = ('\r\n'.join([lines[0], *headers]) + '\r\n\r\n').encode('iso-8859-1')
            tls = ssl.create_default_context() if self.upstream.scheme == 'https' else None
            upstream_reader, upstream_writer = await asyncio.wait_for(asyncio.open_connection(
                self.upstream.hostname, self.upstream.port or (443 if tls else 80), ssl=tls), 30)
            self.writers.add(upstream_writer)
            upstream_writer.write(forwarded)
            await upstream_writer.drain()
            response = await asyncio.wait_for(upstream_reader.readuntil(b'\r\n\r\n'), 30)
            writer.write(response)
            await writer.drain()
            if method == 'CONNECT' and response.split(b' ', 2)[1] != b'200':
                return

            async def copy(source, target):
                while chunk := await source.read(65536):
                    target.write(chunk)
                    await target.drain()
            pipes = [asyncio.create_task(copy(reader, upstream_writer)),
                     asyncio.create_task(copy(upstream_reader, writer))]
            await asyncio.wait(pipes, return_when=asyncio.FIRST_COMPLETED)
        except (OSError, ValueError, asyncio.IncompleteReadError, asyncio.LimitOverrunError, asyncio.TimeoutError):
            try:
                writer.write(b'HTTP/1.1 502 Bad Gateway\r\nConnection: close\r\nContent-Length: 0\r\n\r\n')
                await writer.drain()
            except OSError:
                pass
        finally:
            for connection in (upstream_writer, writer):
                if connection:
                    connection.close()
            for pipe in pipes:
                pipe.cancel()
            if pipes:
                await asyncio.gather(*pipes, return_exceptions=True)
            for connection in (upstream_writer, writer):
                if connection:
                    try:
                        await asyncio.wait_for(connection.wait_closed(), 2)
                    except (OSError, asyncio.TimeoutError):
                        pass
                    self.writers.discard(connection)
            self.tasks.discard(task)


async def prepare_browser_proxy(proxy):
    if not proxy or not proxy.get('username'):
        return proxy, None
    bridge = ProxyBridge(proxy)
    try:
        return await bridge.start(), bridge
    except BaseException:
        bridge.close()
        raise
