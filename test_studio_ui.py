"""Offline browser smoke: temporary DB/profile metadata; no ChatGPT/Dola calls."""
import json
import socket
import tempfile
import threading
import time
from pathlib import Path
from unittest.mock import AsyncMock

import uvicorn
from fastapi import FastAPI, HTTPException
from fastapi.staticfiles import StaticFiles
from patchright.sync_api import sync_playwright

from film_studio import register_film_studio
from image_library import ImageLibrary
from studio_api import register_studio_workflow


def main():
    with tempfile.TemporaryDirectory() as tmp:
        root = Path(tmp); (root/'Profile 7').mkdir()
        (root/'Local State').write_text(json.dumps({'profile': {'info_cache': {'Profile 7': {'name': 'Test profile', 'user_name': 'fixture@example.com'}}}}))
        app = FastAPI()
        def auth(key):
            if key != 'fixture-key': raise HTTPException(401)
        registry = register_film_studio(app, auth, root/'studio.db', root)
        register_studio_workflow(app, auth, registry, ImageLibrary(root/'images'), AsyncMock(), lambda _: None)
        app.mount('/', StaticFiles(directory='web', html=True))
        sock = socket.socket(); sock.bind(('127.0.0.1', 0)); port = sock.getsockname()[1]
        server = uvicorn.Server(uvicorn.Config(app, host='127.0.0.1', port=port, log_level='error'))
        thread = threading.Thread(target=lambda: server.run(sockets=[sock]), daemon=True); thread.start()
        deadline = time.monotonic()+10
        while not server.started and time.monotonic() < deadline: time.sleep(.05)
        assert server.started, 'Test server failed to start'
        try:
            with sync_playwright() as p:
                browser = p.chromium.launch(channel='chrome', headless=True)
                try:
                    page = browser.new_page(viewport={'width': 1280, 'height': 960})
                    errors = []; page.on('pageerror', lambda error: errors.append(str(error)))
                    page.goto(f'http://127.0.0.1:{port}/film-studio.html')
                    page.evaluate("localStorage.setItem('dola_admin_key','fixture-key')")
                    page.reload()
                    page.locator('#profile').select_option('Profile 7')
                    page.locator('#project').fill('https://chatgpt.com/g/g-p-fixture/project')
                    page.locator('#confirmed').check()
                    page.locator('#save').click()
                    page.get_by_text('Đã lưu. Worker tự động chưa được kết nối.').wait_for()
                    page.locator('#film-title').fill('UI test film')
                    page.locator('#screenplay').fill('A enters the room.')
                    page.locator('#new-film button').click()
                    page.locator('#run-detail h2').wait_for()
                    assert page.locator('#run-detail h2').inner_text() == 'UI test film'
                    assert 'script_review' in page.locator('#run-detail').inner_text()
                    page.reload()
                    page.get_by_role('button', name='UI test film — script_review').click()
                    page.locator('#run-detail h2').wait_for()
                    assert page.locator('#profile').input_value() == 'Profile 7'
                    assert not errors, errors
                    print('Film Studio UI: profile save, screenplay create, persisted reload, and no JS errors PASS')
                finally: browser.close()
        finally:
            server.should_exit = True; thread.join(timeout=10); sock.close()


if __name__ == '__main__': main()
