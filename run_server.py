"""Single-process launcher with subprocess support for Patchright on Windows."""
import argparse
import asyncio
import logging
import os
from pathlib import Path
import sys

import uvicorn


class LocalAddressLogFilter(logging.Filter):
    """Show a browser-usable local URL when binding to all interfaces."""
    def filter(self, record):
        args = record.args
        if (record.getMessage().startswith('Uvicorn running on ')
                and isinstance(args, tuple) and len(args) == 3 and args[1] == '0.0.0.0'):
            record.args = (args[0], '127.0.0.1', args[2])
        return True

def create_loop():
    if sys.platform == 'win32':
        return asyncio.ProactorEventLoop()
    return asyncio.new_event_loop()


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--host', default='127.0.0.1')
    parser.add_argument('--port', type=int, default=8000)
    args = parser.parse_args()
    os.chdir(Path(__file__).resolve().parent)
    server = uvicorn.Server(uvicorn.Config(
        'server:app', host=args.host, port=args.port, workers=1, reload=False))
    loop = create_loop()
    asyncio.set_event_loop(loop)
    logging_filter = LocalAddressLogFilter()
    uvicorn_logger = logging.getLogger('uvicorn.error')
    uvicorn_logger.addFilter(logging_filter)
    try:
        print(f'Server event loop: {type(loop).__name__}', flush=True)
        loop.run_until_complete(server.serve())
    finally:
        pending = asyncio.all_tasks(loop)
        for task in pending:
            task.cancel()
        if pending:
            loop.run_until_complete(asyncio.gather(*pending, return_exceptions=True))
        loop.run_until_complete(loop.shutdown_asyncgens())
        loop.run_until_complete(loop.shutdown_default_executor())
        asyncio.set_event_loop(None)
        loop.close()
        uvicorn_logger.removeFilter(logging_filter)


if __name__ == '__main__':
    main()
