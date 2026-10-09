"""In-memory Google import queue; credentials are never included in progress."""
import asyncio

MAX_CONCURRENCY = 10
START_INTERVAL = 20.0


def parse_accounts(text):
    accounts, seen = [], set()
    for number, line in enumerate(text.splitlines(), 1):
        if not line.strip():
            continue
        email, separator, password = line.strip().partition('|')
        email = email.strip()
        if not separator or not password or '@' not in email or any(c.isspace() for c in email):
            raise ValueError(f'Line {number}: expected email|password')
        if email.casefold() in seen:
            continue
        seen.add(email.casefold())
        accounts.append((email, password))
    if not accounts or len(accounts) > 500:
        raise ValueError('Enter between 1 and 500 unique accounts')
    return accounts


async def run_import(accounts, concurrency, progress, submit, job_status):
    concurrency = min(MAX_CONCURRENCY, max(1, concurrency))
    queue = asyncio.Queue()
    for index, credentials in enumerate(accounts):
        queue.put_nowait((index, credentials))
    accounts.clear()
    start_lock = asyncio.Lock()
    last_start = None

    async def worker():
        nonlocal last_start
        while not queue.empty():
            index, (email, password) = queue.get_nowait()
            row = progress['rows'][index]
            try:
                async with start_lock:
                    loop = asyncio.get_running_loop()
                    if last_start is not None:
                        while (remaining := START_INTERVAL - (loop.time() - last_start)) > 0:
                            await asyncio.sleep(remaining)
                    row['status'] = 'running'
                    last_start = loop.time()
                identifier = await submit(email, password)
                password = None
                row['uuid'] = identifier
                while job_status(identifier) == 'running':
                    await asyncio.sleep(.5)
                row['status'] = 'success' if job_status(identifier) == 'success' else 'failed'
            except Exception:
                row['status'] = 'failed'
                row['error'] = 'Could not add account (possibly already registered); check Accounts.'
            finally:
                password = None
                queue.task_done()
    try:
        await asyncio.gather(*(worker() for _ in range(concurrency)))
    finally:
        progress['status'] = 'finished'
