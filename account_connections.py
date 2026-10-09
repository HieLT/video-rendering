"""Local account routing. Credentials and assignments stay outside Git."""
import json
import os
from pathlib import Path
import tempfile

SETTINGS_PATH = Path(__file__).with_name('.connections.local.json')
DIRECT = {'id': 'direct', 'label': 'Mạng hiện tại (không proxy)'}


def _read():
    if not SETTINGS_PATH.exists():
        return {'connections': {}, 'accounts': {}}
    try:
        data = json.loads(SETTINGS_PATH.read_text(encoding='utf-8'))
        if not isinstance(data.get('connections'), dict) or not isinstance(data.get('accounts'), dict):
            raise ValueError()
        return data
    except (ValueError, OSError) as exc:
        raise RuntimeError('Cannot read local connection settings; routing stopped') from exc


def choices():
    return [DIRECT.copy()] + [
        {'id': key, 'label': value['label']}
        for key, value in _read()['connections'].items()
    ]


def validate(identifier):
    if identifier != 'direct' and identifier not in _read()['connections']:
        raise ValueError('Unknown or unavailable connection')
    return identifier


def account_connection(account):
    return _read()['accounts'].get(account, 'direct')


def assign(accounts, identifier):
    validate(identifier)
    data = _read()
    for account in accounts:
        data['accounts'][account] = identifier
    # Replace one file atomically, so a whole batch changes together.
    handle, temporary = tempfile.mkstemp(prefix='.connections-', suffix='.tmp', dir=SETTINGS_PATH.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(temporary, SETTINGS_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def browser_proxy(account):
    data = _read()
    identifier = data['accounts'].get(account, 'direct')
    if identifier == 'direct':
        return None
    entry = data['connections'].get(identifier)
    if not entry or not entry.get('server'):
        raise RuntimeError('Assigned proxy is unavailable; routing stopped')
    return {key: entry[key] for key in ('server', 'username', 'password') if entry.get(key)}


def http_proxy(account):
    from aiohttp import BasicAuth
    options = browser_proxy(account)
    if not options:
        return {'proxy': None}
    result = {'proxy': options['server']}
    if options.get('username'):
        result['proxy_auth'] = BasicAuth(options['username'], options.get('password', ''))
    return result
