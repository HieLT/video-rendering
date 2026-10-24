"""Explicit Dola risk blocks; never infer a block from generic busy text."""
import asyncio
import json
import sqlite3
from pathlib import Path
from urllib.parse import urlsplit

SHARK_BLOCKED = 710022002
REASON = 'Dola SHARK_BLOCKED (710022002); dispatch disabled; manual review required'


class AccountRiskBlockedError(RuntimeError):
    pass


def contains_shark_block(body):
    def blocked(value):
        if isinstance(value, dict):
            return any(str(value.get(k)) == str(SHARK_BLOCKED) for k in ('code', 'error_code')) or any(blocked(v) for v in value.values())
        return isinstance(value, list) and any(blocked(v) for v in value)
    for text in [body, *[line[5:].strip() for line in body.splitlines() if line.startswith('data:')]]:
        try:
            if blocked(json.loads(text)):
                return True
        except (ValueError, TypeError):
            pass
    return False


def disable_domain(conn, domain):
    domain = domain.strip().casefold()
    if not domain or '@' in domain:
        raise ValueError('Expected an email domain')
    rows = conn.execute('SELECT name,email,scheduling,dispatch_manual FROM accounts_meta').fetchall()
    matches = [tuple(r) for r in rows if str(r[1] or '').strip().casefold().rsplit('@', 1)[-1] == domain and '@' in str(r[1] or '')]
    conn.executemany('UPDATE accounts_meta SET scheduling=0,dispatch_manual=1 WHERE name=?', [(r[0],) for r in matches])
    return matches


def quarantine(account, db_path='pool_usage.db'):
    # mode=rw prevents silently creating a database at a wrong working directory.
    with sqlite3.connect(Path(db_path).resolve().as_uri()+'?mode=rw', uri=True, timeout=10) as conn:
        row = conn.execute('SELECT email FROM accounts_meta WHERE name=?', (account,)).fetchone()
        if not row:
            raise RuntimeError('Risk-blocked account missing from pool database')
        # Quarantine only the account that produced the provider risk signal.
        # A domain-wide disable used to make one bad account remove every
        # otherwise-valid account from dispatch (and looked like mass session
        # loss for shared domains such as azalea.raxio.us).
        # A provider risk response disables dispatch only. It does not prove that
        # the browser session or Google login expired, so preserve auth_state.
        conn.execute('UPDATE accounts_meta SET scheduling=0,dispatch_manual=1 WHERE name=?',
                     (account,))


async def install_risk_monitor(context, account):
    tasks = set()
    context._dola_risk_error = None

    async def capture(response):
        u = urlsplit(response.url)
        if u.hostname != 'www.dola.com' or not (u.path == '/chat/completion' or u.path.startswith('/alice/')):
            return
        try:
            body = await response.text()
        except Exception:
            return  # Navigation can make a response body unavailable.
        if contains_shark_block(body) and context._dola_risk_error is None:
            context._dola_risk_error = REASON

    def received(response):
        task = asyncio.create_task(capture(response))
        tasks.add(task)
        task.add_done_callback(tasks.discard)

    context.on('response', received)
    context.on('close', lambda *_: [task.cancel() for task in list(tasks)])

    async def preserve_session(route):
        # Dola's frontend logs the user out immediately after SHARK_BLOCKED.
        # Keep the valid session so the scheduler can cool it down and retry
        # later in the same account/profile environment.
        if context._dola_risk_error:
            await route.abort()
        else:
            await route.continue_()

    registered = context.route('**/passport/web/logout/**', preserve_session)
    # Playwright returns an awaitable; lightweight unit-test contexts may
    # expose a synchronous registration stub.
    import inspect
    if inspect.isawaitable(registered):
        await registered


def check_risk(context):
    error = getattr(context, '_dola_risk_error', None)
    if isinstance(error, str) and error:
        raise AccountRiskBlockedError(error)
