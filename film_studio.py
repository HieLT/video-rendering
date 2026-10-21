"""Local Film Studio session registry. Does not automate or inspect ChatGPT."""
import json
import os
import re
import sqlite3
from typing import Literal
from contextlib import closing
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Header, HTTPException
from pydantic import BaseModel, ConfigDict, Field, field_validator

ROLES = {
    'showrunner': '00 — Showrunner',
    'script_editor': '01 — Script Analyst & Editor',
    'script_doctor': '02 — Script Doctor',
    'director': '03 — Production Architect & Director',
    'reference_planner': '04 — Reference Planner',
    'reference_creator': '05 — Reference Creator',
    'reference_selector': '05a — Reference Candidate Selector',
    'reference_qa': '06 — Reference QA',
    'prompt_engineer': '07 — Prompt Engineer',
    'video_qa': '08 — Video QA',
}


def chrome_profiles(root):
    """Read only display metadata; never read cookies or copy profile contents."""
    root = Path(root)
    state = root / 'Local State'
    if not state.exists():
        return []
    data = json.loads(state.read_text(encoding='utf-8'))
    result = []
    for directory, info in data.get('profile', {}).get('info_cache', {}).items():
        if not re.fullmatch(r'Default|Profile \d+', directory):
            continue
        if not isinstance(info, dict) or not (root / directory).is_dir():
            continue
        result.append({'id': directory, 'name': info.get('name', directory),
                       'email': info.get('user_name', '')})
    return sorted(result, key=lambda p: (p['name'], p['id']))


def checked_url(value, project=False):
    value = value.strip()
    if not value:
        return value
    parsed = urlsplit(value)
    pattern = r'/g/g-p-[A-Za-z0-9-]+/project' if project else r'(?:/g/[A-Za-z0-9-]+)?/c/[A-Za-z0-9%_-]+'
    if (parsed.scheme != 'https' or parsed.netloc != 'chatgpt.com'
            or parsed.query or parsed.fragment or not re.fullmatch(pattern, parsed.path)):
        raise ValueError('Liên kết phải là URL Project/chat hợp lệ trên https://chatgpt.com.')
    return value


class StudioSettings(BaseModel):
    model_config = ConfigDict(extra='forbid')
    profile_id: str = Field(default='', max_length=80)
    account_id: str = Field(default='', max_length=80)
    chatgpt_profile_id: str = Field(default='', max_length=80)
    browser: Literal['chrome', 'edge'] = 'chrome'
    project_url: str = Field(default='', max_length=500)
    instructions_confirmed: bool = False
    sessions: dict[str, str] = Field(default_factory=dict)

    @field_validator('project_url')
    @classmethod
    def project_link(cls, value):
        return checked_url(value, project=True)

    @field_validator('sessions')
    @classmethod
    def session_links(cls, values):
        if set(values) - set(ROLES):
            raise ValueError('Vai trò agent không hợp lệ.')
        cleaned = {role: checked_url(url) for role, url in values.items()}
        urls = [url for url in cleaned.values() if url]
        if len(urls) != len(set(urls)):
            raise ValueError('Mỗi agent phải có một chat riêng.')
        return cleaned


class StudioRegistry:
    def __init__(self, database, profile_root):
        self.database = str(database)
        self.profile_root = Path(profile_root)
        self.edge_root = Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft' / 'Edge' / 'User Data'
        self.accounts_root = Path(__file__).resolve().parent / 'accounts'

    @property
    def managed_root(self):
        return Path(self.database).resolve().parent / '.runtime' / 'film-studio-chrome'

    def profile_path(self, profile_id):
        if profile_id == 'film-studio-managed':
            return self.managed_root / 'Default'
        return self.profile_root / profile_id

    def browser_root(self, browser):
        return self.edge_root if browser == 'edge' else self.profile_root

    def account_root(self, account_id):
        if not re.fullmatch(r'[0-9a-fA-F-]{36}', account_id or ''):
            raise ValueError('Account ID không hợp lệ.')
        return self.accounts_root / account_id

    def selected_browser_root(self, settings):
        return self.account_root(settings.account_id) if settings.account_id else self.browser_root(settings.browser)

    def selected_profile_id(self, settings):
        return 'Default' if settings.account_id else settings.profile_id

    def selected_profile_path(self, settings):
        return self.selected_browser_root(settings) / self.selected_profile_id(settings)

    def selected_chatgpt_browser_root(self, settings):
        return self.browser_root(settings.browser)

    def selected_chatgpt_profile_id(self, settings):
        return settings.chatgpt_profile_id or settings.profile_id

    def _connect(self):
        db = sqlite3.connect(self.database, timeout=10)
        db.execute('CREATE TABLE IF NOT EXISTS studio_settings (id INTEGER PRIMARY KEY CHECK(id=1), payload TEXT NOT NULL)')
        return db

    def read(self):
        with closing(self._connect()) as db:
            row = db.execute('SELECT payload FROM studio_settings WHERE id=1').fetchone()
        return StudioSettings.model_validate_json(row[0]) if row else StudioSettings()

    def save(self, settings):
        if settings.account_id:
            root = self.account_root(settings.account_id)
            if not (root / 'Default').is_dir():
                raise ValueError('Account List browser session không tồn tại.')
        elif settings.profile_id and settings.profile_id != 'film-studio-managed' and settings.profile_id not in {p['id'] for p in chrome_profiles(self.browser_root(settings.browser))}:
            raise ValueError('Chrome profile không còn tồn tại. Hãy tải lại danh sách.')
        with closing(self._connect()) as db, db:
            db.execute('INSERT INTO studio_settings VALUES (1, ?) ON CONFLICT(id) DO UPDATE SET payload=excluded.payload',
                       (settings.model_dump_json(),))


def register_film_studio(app, auth, database, profile_root=None):
    root = profile_root or Path(os.environ.get('LOCALAPPDATA', '')) / 'Google/Chrome/User Data'
    registry = StudioRegistry(database, root)

    @app.get('/api/admin/film-studio')
    def get_settings(x_admin_key: str | None = Header(default=None)):
        auth(x_admin_key)
        try:
            profiles = chrome_profiles(root)
            profiles.append({'id': 'film-studio-managed', 'name': 'Film Studio — Chrome riêng', 'email': ''})
        except (OSError, ValueError):
            raise HTTPException(503, 'Không đọc được danh sách Chrome profile.')
        edge_root = Path(os.environ.get('LOCALAPPDATA', '')) / 'Microsoft' / 'Edge' / 'User Data'
        edge_profiles = chrome_profiles(edge_root)
        accounts = []
        accounts_db = Path(database).with_name('pool_usage.db')
        if accounts_db.exists():
            try:
                with closing(sqlite3.connect(accounts_db)) as account_db:
                    rows = account_db.execute('SELECT uuid,email,display_name,auth_state FROM accounts_meta WHERE lower(email)=lower(?)',
                                              ('hieuducle0105@gmail.com',)).fetchall()
                accounts = [{'id': row[0], 'email': row[1], 'name': row[2] or row[1], 'auth_state': row[3]} for row in rows]
            except sqlite3.Error:
                accounts = []
        worker = getattr(app.state, 'film_studio_chatgpt', None)
        worker_status = worker.status() if worker else {}
        return {'settings': registry.read().model_dump(), 'profiles': profiles, 'edge_profiles': edge_profiles,
                'accounts': accounts, 'roles': ROLES,
                'automation_status': worker_status.get('chatgpt_transport', 'not_connected'),
                **worker_status}

    @app.put('/api/admin/film-studio')
    def put_settings(body: StudioSettings, x_admin_key: str | None = Header(default=None)):
        auth(x_admin_key)
        try:
            registry.save(body)
        except ValueError as exc:
            raise HTTPException(400, str(exc))
        except OSError:
            raise HTTPException(503, 'Không đọc được danh sách Chrome profile.')
        return {'settings': body.model_dump(), 'automation_status': 'not_connected'}

    return registry
