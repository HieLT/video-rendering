"""Offline launcher integration: real HTTP, temporary app and Chromium, no accounts."""
import asyncio
import os
from pathlib import Path
import socket
import subprocess
import sys
import tempfile
import time
import urllib.request
import json

import uvicorn


def main():
    if sys.platform != 'win32':
        print('Windows-only regression')
        return
    loop = uvicorn.Config('fixture:app', reload=True).get_loop_factory()()
    try:
        loop.run_until_complete(asyncio.create_subprocess_exec(sys.executable, '-c', 'pass'))
    except NotImplementedError:
        print('REPRODUCED: default Uvicorn reload loop cannot create subprocesses', flush=True)
    else:
        raise AssertionError('Expected original failure')
    finally:
        loop.close()
    with tempfile.TemporaryDirectory(prefix='dola_loop_test_') as folder:
        folder = Path(folder)
        fixture = folder / 'fixture.py'
        fixture.write_text('''import asyncio, os
from fastapi import FastAPI
from patchright.async_api import async_playwright
app = FastAPI()
@app.get('/probe')
async def probe():
    async with async_playwright() as p:
        browser = await p.chromium.launch(headless=True)
        try:
            page = await browser.new_page()
            await page.set_content('<title>Browser works</title>')
            return {'title': await page.title(), 'loop': type(asyncio.get_running_loop()).__name__, 'pid': os.getpid()}
        finally:
            await browser.close()
''', encoding='utf-8')
        with socket.socket() as sock:
            sock.bind(('127.0.0.1', 0))
            port = sock.getsockname()[1]
        env = os.environ.copy()
        env['PYTHONPATH'] = str(Path(__file__).resolve().parent) + os.pathsep + env.get('PYTHONPATH', '')
        with (folder / 'server.log').open('w+') as log:
            command = [sys.executable, '-c',
                "import sys, uvicorn, fixture, run_server; original=uvicorn.Config; "
                "run_server.uvicorn.Config=lambda *a, **kw: original(fixture.app, **kw); "
                "sys.argv=['run_server.py','--port','" + str(port) + "']; run_server.main()"]
            def start():
                return subprocess.Popen(command, cwd=folder, env=env, stdout=log, stderr=log,
                                        creationflags=subprocess.CREATE_NO_WINDOW)
            process = start()
            def probe(previous=None):
                deadline = time.monotonic() + 35
                while time.monotonic() < deadline:
                    try:
                        with urllib.request.urlopen(f'http://127.0.0.1:{port}/probe', timeout=8) as response:
                            result = json.load(response)
                        assert result['title'] == 'Browser works'
                        assert result['loop'] == 'ProactorEventLoop'
                        if result['pid'] != previous:
                            return result
                    except (OSError, ValueError):
                        pass
                    time.sleep(.5)
                log.flush()
                raise AssertionError((folder / 'server.log').read_text())
            try:
                first = probe()
                print('PASS HTTP endpoint launched Chromium with Proactor loop', flush=True)
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
                process.wait(timeout=10)
                process = start()
                second = probe(first['pid'])
                print('PASS clean restart with run_server.py; Chromium still opens', flush=True)
            finally:
                subprocess.run(['taskkill', '/PID', str(process.pid), '/T', '/F'], capture_output=True)
                process.wait(timeout=10)


if __name__ == '__main__':
    main()
