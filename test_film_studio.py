import json
import tempfile
import unittest
from pathlib import Path

from fastapi import FastAPI, HTTPException
from fastapi.testclient import TestClient

from film_studio import register_film_studio


class FilmStudioTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self.tmp.cleanup)
        self.root = Path(self.tmp.name)
        (self.root / 'Profile 7').mkdir()
        (self.root / 'Local State').write_text(json.dumps({'profile': {'info_cache': {
            'Profile 7': {'name': 'Test', 'user_name': 'test@example.com'},
            '../escape': {'name': 'Invalid'},
        }}}), encoding='utf-8')
        self.headers = {'X-Admin-Key': 'test-key'}
        self.client = self.make_client()

    def make_client(self):
        app = FastAPI()
        def auth(key):
            if key != 'test-key':
                raise HTTPException(401)
        register_film_studio(app, auth, self.root / 'settings.db', self.root)
        return TestClient(app)

    def test_auth_and_profile_metadata(self):
        self.assertEqual(self.client.get('/api/admin/film-studio').status_code, 401)
        self.assertEqual(self.client.put('/api/admin/film-studio', json={}).status_code, 401)
        data = self.client.get('/api/admin/film-studio', headers=self.headers).json()
        self.assertEqual([p['id'] for p in data['profiles']], ['Profile 7', 'film-studio-managed'])
        self.assertEqual(data['automation_status'], 'not_connected')

    def test_persists_across_registry_restart(self):
        payload = {'profile_id': 'Profile 7', 'account_id': '', 'browser': 'chrome', 'project_url': 'https://chatgpt.com/g/g-p-example/project',
                   'instructions_confirmed': True, 'sessions': {'showrunner': 'https://chatgpt.com/c/example'}}
        response = self.client.put('/api/admin/film-studio', json=payload, headers=self.headers)
        self.assertEqual(response.status_code, 200, response.text)
        data = self.make_client().get('/api/admin/film-studio', headers=self.headers).json()
        self.assertEqual(data['settings'], payload)

    def test_rejects_invalid_links_duplicates_and_missing_profiles(self):
        for payload in [
            {'project_url': 'https://chatgpt.com.evil.example/g/g-p-test/project'},
            {'project_url': 'javascript:alert(1)'},
            {'sessions': {'unknown': 'https://chatgpt.com/c/test'}},
            {'sessions': {'showrunner': 'https://chatgpt.com/c/test', 'video_qa': 'https://chatgpt.com/c/test'}},
        ]:
            with self.subTest(payload=payload):
                self.assertEqual(self.client.put('/api/admin/film-studio', json=payload,
                                                headers=self.headers).status_code, 422)
        response = self.client.put('/api/admin/film-studio', json={'profile_id': '../escape'}, headers=self.headers)
        self.assertEqual(response.status_code, 400)
        self.assertEqual(self.client.get('/api/admin/film-studio', headers=self.headers).json()['settings']['profile_id'], '')

    def test_corrupt_chrome_metadata_is_reported(self):
        (self.root / 'Local State').write_text('broken', encoding='utf-8')
        self.assertEqual(self.client.get('/api/admin/film-studio', headers=self.headers).status_code, 503)


if __name__ == '__main__':
    unittest.main()
