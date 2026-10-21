"""Single-process launcher with subprocess support for Patchright on Windows."""
import argparse
import asyncio
import os
from pathlib import Path
import sys

import uvicorn

from browser_queue import lock_file, unlock_file


def create_loop():
    if sys.platform == 'win32':
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    from windows_desktop import require_visible_desktop
    require_visible_desktop()
    root = Path(__file__).resolve().parent
    os.chdir(root)
    # Hold this for the entire process lifetime.  The application-level
    # scheduler lock is released during shutdown, while browser cleanup may
    # still be running.  Without this outer guard a replacement server can
    # start during that cleanup and leave an old Patchright/Chromium tree
    # behind.
    process_guard = lock_file(root / '.runtime' / 'server-process.lock')
    if process_guard is None:
        raise SystemExit('Another Film Studio server process is still running')
    server = uvicorn.Server(uvicorn.Config(
        'server:app', host=args.host, port=args.port, workers=1, reload=False))
    loop = create_loop()
    asyncio.set_event_loop(loop)
    try:
        print(f'Server event loop: {type(loop).__name__}', flush=True)
        loop.run_until_complete(server.serve())
    finally:
        try:
            pending = asyncio.all_tasks(loop)
            for task in pending:
                task.cancel()
            if pending:
                loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
            loop.run_until_complete(loop.shutdown_asyncgens())
            loop.run_until_complete(loop.shutdown_default_executor())
            asyncio.set_event_loop(None)
            loop.close()
        finally:
            unlock_file(process_guard)


if __name__ == '__main__':
    main()
