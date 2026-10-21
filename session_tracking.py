"""Session lifecycle evidence. Never records cookie values, headers or URL queries."""
import asyncio
import json
import os
import sqlite3
import uuid
from contextlib import closing
from datetime import datetime, timezone
from pathlib import Path
from urllib.parse import urlsplit

LOG_PATH = Path(__file__).with_name('session_tracking.jsonl')
AUTH_COOKIES = {'sessionid', 'sessionid_ss', 'sid_tt', 'sid_guard', 'uid_tt'}


def emit(account, run, event, **data):
    try:
        record = dict(time=datetime.now(timezone.utc).isoformat(), pid=os.getpid(),
                      account=account, run=run, event=event, **data)
        with LOG_PATH.open('a', encoding='utf-8') as stream:
            stream.write(json.dumps(record, ensure_ascii=True) + '\n')
    except Exception:
        pass  # Diagnostics must never break a login or generation.


def disk_snapshot(profile_dir):
    result = []
    for path in Path(profile_dir).glob('*/Network/Cookies'):
        entry = {'profile': path.parts[-3], 'bytes': path.stat().st_size}
        try:
            # mode=ro preserves WAL visibility without creating/modifying the DB.
            with closing(sqlite3.connect(path.resolve().as_uri() + '?mode=ro', uri=True,
                                         timeout=0.1)) as db:
                entry['total'] = db.execute('SELECT count(*) FROM cookies').fetchone()[0]
                entry['auth'] = [dict(domain=r[0], name=r[1], persistent=bool(r[2]),
                                      expires_chrome_us=r[3], encrypted_bytes=r[4])
                                 for r in db.execute(
                                     'SELECT host_key,name,is_persistent,expires_utc,length(encrypted_value) '
                                     'FROM cookies ORDER BY host_key,name')
                                 if r[1] in AUTH_COOKIES and
                                 (r[0].lstrip('.') == 'dola.com' or r[0].endswith('.dola.com'))]
        except Exception as exc:
            entry['error'] = type(exc).__name__
        result.append(entry)
    return result


def trace_disk(account, profile_dir, event, run=None):
    run = run or uuid.uuid4().hex
    try:
        emit(account, run, event, profile_root=str(Path(profile_dir).resolve()),
             disk=disk_snapshot(profile_dir))
    except Exception as exc:
        emit(account, run, event, error=type(exc).__name__)
    return run


async def track_session(context, account, profile_dir, run):
    """Track startup, cookie transitions, server deletion and graceful/manual close."""
    previous = None
    tasks = set()
    closed = False

    async def snapshot(event):
        nonlocal previous
        try:
            cookies = await context.cookies('https://www.dola.com')
            state = sorted([dict(name=c['name'], domain=c['domain'],
                                 present=bool(c.get('value')), expires=c.get('expires'),
                                 secure=c.get('secure'), http_only=c.get('httpOnly'))
                            for c in cookies if c['name'] in AUTH_COOKIES],
                           key=lambda c: (c['domain'], c['name']))
            if event != 'cookie_change' or state != previous:
                emit(account, run, event, cookies=state, previous=previous)
                if previous is not None:
                    old_auth = any(c.get('name') in ('sessionid', 'sessionid_ss') and c.get('present')
                                   for c in previous)
                    new_auth = any(c.get('name') in ('sessionid', 'sessionid_ss') and c.get('present')
                                   for c in state)
                    if old_auth and not new_auth:
                        # This is the first explicit marker for a real local
                        # session loss. The preceding auth_response event (if
                        # any) identifies a server Set-Cookie/401/403 trigger;
                        # absence of one means the cause was outside the
                        # observed browser response stream.
                        emit(account, run, 'session_lost', trigger='cookie_transition',
                             previous=previous, cookies=state)
                    elif not old_auth and new_auth:
                        emit(account, run, 'session_restored', trigger='cookie_transition',
                             previous=previous, cookies=state)
                previous = state
        except Exception as exc:
            if not closed:
                emit(account, run, 'snapshot_error', stage=event, error=type(exc).__name__)

    async def response_seen(response):
        try:
            url = urlsplit(response.url)
            if url.hostname not in ('www.dola.com', 'dola.com'):
                return
            # Parse only allowlisted attributes; never write raw Set-Cookie.
            changes = []
            for header in await response.header_values('set-cookie'):
                parts = header.split(';')
                name, _, value = parts[0].strip().partition('=')
                if name not in AUTH_COOKIES:
                    continue
                attrs = {}
                for part in parts[1:]:
                    key, _, val = part.strip().partition('=')
                    if key.lower() in ('expires', 'max-age'):
                        attrs[key.lower()] = val
                changes.append(dict(name=name, nonempty=bool(value), **attrs))
            if changes or response.status in (401, 403):
                emit(account, run, 'auth_response', host=url.hostname,
                     status=response.status, cookies=changes)
        except Exception as exc:
            if not closed:
                emit(account, run, 'response_error', error=type(exc).__name__)

    def enqueue(response):
        task = asyncio.create_task(response_seen(response))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    def failed(request):
        try:
            url = urlsplit(request.url)
            if url.hostname in ('www.dola.com', 'dola.com', 'accounts.google.com'):
                failure = str(request.failure or '')
                import re
                code = re.search(r'net::ERR_[A-Z_]+', failure)
                emit(account, run, 'request_failed', host=url.hostname,
                     error=code.group(0) if code else 'request_failed')
        except Exception:
            pass

    async def poll():
        while True:
            await asyncio.sleep(2)
            await snapshot('cookie_change')

    await snapshot('context_open')
    poller = asyncio.create_task(poll())
    context.on('response', enqueue)
    context.on('requestfailed', failed)

    def on_close(*_):
        nonlocal closed
        closed = True
        poller.cancel()
        for task in list(tasks):
            task.cancel()
        trace_disk(account, profile_dir, 'context_closed', run)

    context.on('close', on_close)
    original_close = context.close

    async def close(*args, **kwargs):
        if not closed:
            await snapshot('before_close')
        try:
            return await original_close(*args, **kwargs)
        finally:
            # close event can occur before Chromium finishes flushing SQLite.
            trace_disk(account, profile_dir, 'after_close_return', run)

    context.close = close
