"""Local account routing. Credentials and assignments stay outside Git."""
import json
import os
from pathlib import Path
import tempfile
import ipaddress
import re
import uuid

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
    return routing_snapshot()['connections']


def routing_snapshot():
    data = _read()
    # Read once for the account list, regardless of how many accounts/proxies exist.
    return {'accounts': dict(data['accounts']), 'connections':
        [DIRECT.copy()] + [{'id': key, 'label': value['label']}
                           for key, value in data['connections'].items()]}


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
    _write(data)


def _write(data):
    # Replace one file atomically, so a whole batch changes together.
    handle, temporary = tempfile.mkstemp(prefix='.connections-', suffix='.tmp', dir=SETTINGS_PATH.parent)
    try:
        with os.fdopen(handle, 'w', encoding='utf-8') as file:
            json.dump(data, file, ensure_ascii=False, indent=2)
        os.replace(temporary, SETTINGS_PATH)
    finally:
        if os.path.exists(temporary):
            os.unlink(temporary)


def import_proxies(text):
    """Validate a whole paste before saving; errors never quote credentials."""
    if not isinstance(text, str) or len(text) > 200_000:
        raise ValueError('Nội dung proxy quá dài (tối đa 200.000 ký tự)')
    lines = [(number, line.strip()) for number, line in enumerate(text.splitlines(), 1) if line.strip()]
    if not 1 <= len(lines) <= 500:
        raise ValueError('Nhập từ 1 đến 500 proxy, mỗi proxy một dòng')
    data = _read()
    added, skipped = [], 0
    for number, line in lines:
        suffix = re.search(r'\s*\|\s*ID\s*:\s*([A-Za-z0-9_-]{1,64})\s*$', line, re.I)
        external_id = suffix.group(1) if suffix else ''
        value = line[:suffix.start()].strip() if suffix else line
        parts = value.split(':', 3)
        if len(parts) != 4:
            raise ValueError(f'Dòng {number}: cần host:port:username:password | ID: mã (ID không bắt buộc)')
        host, port_text, username, password = parts
        host, port_text, username = host.strip().lower(), port_text.strip(), username.strip()
        if (len(host) > 253 or not re.fullmatch(r'(?:[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?\.)*[a-z0-9](?:[a-z0-9-]{0,61}[a-z0-9])?', host)
                or not 1 <= len(port_text) <= 5 or not port_text.isascii() or not port_text.isdigit()
                or not 1 <= int(port_text) <= 65535
                or not username or not password or '|' in password
                or any(ord(c) < 32 for c in username + password)
                or len(username) > 256 or len(password) > 1024):
            raise ValueError(f'Dòng {number}: host, port hoặc thông tin đăng nhập không hợp lệ')
        if re.fullmatch(r'[0-9.]+', host):
            try:
                ipaddress.IPv4Address(host)
            except ValueError as exc:
                raise ValueError(f'Dòng {number}: địa chỉ IPv4 không hợp lệ') from exc
        server = f'http://{host}:{int(port_text)}'
        identifier = 'proxy-' + external_id if external_id else 'proxy-' + uuid.uuid4().hex
        entry = {'label': f'Proxy {external_id or host + ":" + str(int(port_text)) + " · " + identifier[-6:]}',
                 'server': server, 'username': username, 'password': password}
        if external_id:
            entry['external_id'] = external_id
        existing = data['connections'].get(identifier)
        if existing and any(existing.get(key) != entry[key] for key in ('server', 'username', 'password')):
            raise ValueError(f'Dòng {number}: ID proxy đã tồn tại với cấu hình khác; không ghi đè')
        duplicates = [item for item in data['connections'].values()
                      if item.get('server') == server and item.get('username') == username]
        if duplicates:
            if any(item.get('password') != password for item in duplicates):
                raise ValueError(f'Dòng {number}: proxy đã tồn tại với mật khẩu khác; không ghi đè')
            skipped += 1
            continue
        data['connections'][identifier] = entry
        added.append(identifier)
    if added:
        _write(data)
    return {'added': len(added), 'skipped': skipped}


def proxy_records(accounts):
    data = _read()
    result = []
    for identifier, entry in data['connections'].items():
        members = [account for account in accounts
                   if data['accounts'].get(account['name']) == identifier]
        result.append({'id': identifier, 'label': entry['label'], 'server': entry['server'],
                       'accounts': [{'uuid': account['uuid'],
                                     'label': account.get('email') or account.get('display_name') or account['uuid']}
                                    for account in members]})
    return result


def remove_proxy(identifier, existing_accounts):
    data = _read()
    if identifier not in data['connections']:
        raise KeyError('Proxy không tồn tại')
    if any(data['accounts'].get(name) == identifier for name in existing_accounts):
        raise ValueError('Proxy đang được account sử dụng. Đổi kết nối các account trước khi xóa.')
    del data['connections'][identifier]
    data['accounts'] = {name: selected for name, selected in data['accounts'].items() if selected != identifier}
    _write(data)


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
