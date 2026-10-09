"""Offline proxy paste, persistence, duplicate and in-use deletion checks."""
import json
from test_account_connections import ConnectionTests
import account_connections as connections


class ManagementTests(ConnectionTests):
    def distribute(self, preview=False, auth=True):
        return self.http.post('/api/admin/account-connections/distribute'+('?preview=true' if preview else ''),
                              headers={'X-Admin-Key': 'fixture-admin'} if auth else {})

    def test_auto_distribution_balances_all_accounts_and_never_selects_direct(self):
        self.import_paste('203.0.113.10:8080:u:p | ID: 100\n203.0.113.11:8080:u:p | ID: 101')
        for index in range(8):
            name=f'extra-{index}'
            (self.root/'accounts'/name).mkdir()
            self.pool._ensure_meta(name)
        before=self.path.read_bytes()
        preview=self.distribute(preview=True).json()
        self.assertEqual(preview['eligible'],10)
        self.assertEqual(self.path.read_bytes(),before)
        result=self.distribute().json()
        self.assertEqual(sorted(p['count'] for p in result['distribution']),[3,3,4])
        self.assertTrue(all(connections.account_connection(name)!='direct' for name in self.pool.accounts))
        repeated=self.distribute().json()
        self.assertEqual(repeated['changed'],0)

    def test_auto_keeps_busy_accounts_and_counts_their_proxy_assignments(self):
        self.import_paste('203.0.113.10:8080:u:p | ID: 100')
        connections.assign(['two'],'fixture')
        self.jobs['one']={'status':'running'}
        result=self.distribute().json()
        self.assertEqual(result['eligible'],1)
        self.assertEqual(len(result['skipped']),1)
        self.assertEqual(connections.account_connection('one'),'direct')
        self.jobs.clear()
        self.scheduler.reservation_ids.side_effect=lambda name:['video'] if name=='two' else []
        previous=connections.account_connection('two')
        result=self.distribute().json()
        self.assertEqual(connections.account_connection('two'),previous)
        self.assertNotEqual(connections.account_connection('one'),'direct')
        self.assertEqual(sorted(p['count'] for p in result['distribution']),[1,1])

    def test_auto_rechecks_busy_state_after_preview(self):
        preview=self.distribute(preview=True).json()
        self.assertEqual(preview['eligible'],2)
        self.web['one']={'status':'open'}
        result=self.distribute().json()
        self.assertEqual(result['eligible'],1)
        self.assertEqual(connections.account_connection('one'),'direct')
        self.assertEqual(connections.account_connection('two'),'fixture')

    def test_auto_requires_proxy_and_admin_auth(self):
        self.assertEqual(self.distribute(auth=False).status_code,401)
        connections.remove_proxy('fixture',set(self.pool.accounts))
        before=self.path.read_bytes()
        self.assertEqual(self.distribute().status_code,422)
        self.assertEqual(self.path.read_bytes(),before)

    def test_auto_preserves_existing_balanced_routes(self):
        self.import_paste('203.0.113.10:8080:u:p | ID: 100\n203.0.113.11:8080:u:p | ID: 101')
        for name in ('three','four'):
            (self.root/'accounts'/name).mkdir()
            self.pool._ensure_meta(name)
        connections.assign(['one','two'],'proxy-100')
        connections.assign(['three'],'proxy-101')
        connections.assign(['four'],'fixture')
        self.assertEqual(self.distribute().json()['changed'],0)

    def import_paste(self, text, auth=True):
        return self.http.post('/api/admin/proxies/import', json={'text': text},
                             headers={'X-Admin-Key': 'fixture-admin'} if auth else {})

    def test_multi_import_duplicate_and_persistence(self):
        paste = '\n203.0.113.10:8080:fixture:secret-A | ID: 100\n203.0.113.11:8081:fixture:secret-B | ID: 101\n\n203.0.113.10:8080:fixture:secret-A | ID: 100'
        response = self.import_paste(paste)
        self.assertEqual(response.status_code, 200)
        result = response.json()
        self.assertEqual((result['added'], result['skipped']), (2, 1))
        self.assertEqual(result['duplicates'], [{'line': 5, 'id': 'proxy-100', 'label': 'Proxy 100', 'server': 'http://203.0.113.10:8080', 'first_line': 2}])
        repeated = self.import_paste(paste).json()
        self.assertEqual((repeated['added'], repeated['skipped']), (0, 3))
        self.assertEqual([d['line'] for d in repeated['duplicates']], [2, 3, 5])
        self.assertTrue(all(d['first_line'] is None for d in repeated['duplicates']))
        self.assertNotIn('secret-A', response.text)
        self.assertNotIn('fixture', json.dumps(result['duplicates']))
        choices = connections.choices()
        self.assertIn('proxy-100', [p['id'] for p in choices])
        connections.assign(['one'], 'proxy-100')
        self.assertEqual(connections.browser_proxy('one')['server'], 'http://203.0.113.10:8080')
        saved = json.loads(self.path.read_text())
        self.assertEqual(saved['connections']['proxy-101']['password'], 'secret-B')

    def test_validation_is_atomic_and_does_not_echo_credentials(self):
        before = self.path.read_bytes()
        response = self.import_paste('203.0.113.10:8080:fixture:private-password | ID: 100\ninvalid:0:fixture:private-password')
        self.assertEqual(response.status_code, 422)
        self.assertIn('Dòng 2', response.json()['detail'])
        self.assertNotIn('private-password', response.text)
        self.assertEqual(self.path.read_bytes(), before)
        for text in ('', '999.999.999.999:8080:u:p', '203.0.113.10:65536:u:p',
                     '203.0.113.10:8080::p', '203.0.113.10:8080:u:',
                     '203.0.113.10:8080:u:p | ID: <script>', 'x' * 200_001):
            with self.subTest(text=text[:30]):
                self.assertEqual(self.import_paste(text).status_code, 422)
        self.assertEqual(self.path.read_bytes(), before)

    def test_conflicting_id_or_password_never_overwrites(self):
        self.assertEqual(self.import_paste('203.0.113.10:8080:fixture:secret-A | ID: 100').status_code, 200)
        before = self.path.read_bytes()
        for text in ('203.0.113.11:8080:fixture:secret-B | ID: 100',
                     '203.0.113.10:8080:fixture:secret-B | ID: 101'):
            self.assertEqual(self.import_paste(text).status_code, 422)
        self.assertEqual(self.path.read_bytes(), before)

    def test_optional_id_and_colon_in_password(self):
        response = self.import_paste('proxy.example.com:8080:fixture:secret:with:colons')
        self.assertEqual(response.status_code, 200)
        entries = connections._read()['connections']
        imported = next(p for p in entries.values() if p['server'] == 'http://proxy.example.com:8080')
        self.assertEqual(imported['password'], 'secret:with:colons')

    def test_list_no_credentials_and_delete_guard(self):
        self.pool.set_email('one', 'one@example.com')
        connections.assign(['one'], 'fixture')
        headers = {'X-Admin-Key': 'fixture-admin'}
        response = self.http.get('/api/admin/proxies', headers=headers)
        self.assertEqual(response.status_code, 200)
        self.assertNotIn('fixture-secret', response.text)
        self.assertNotIn('fixture-user', response.text)
        entry = response.json()['proxies'][0]
        self.assertEqual(entry['accounts'][0]['label'], 'one@example.com')
        self.assertEqual(self.http.delete('/api/admin/proxies/fixture', headers=headers).status_code, 409)
        self.assertEqual(connections.account_connection('one'), 'fixture')
        connections.assign(['one'], 'direct')
        self.assertEqual(self.http.delete('/api/admin/proxies/fixture', headers=headers).status_code, 200)
        self.assertEqual(self.http.delete('/api/admin/proxies/fixture', headers=headers).status_code, 404)
        self.assertEqual(self.http.delete('/api/admin/proxies/direct', headers=headers).status_code, 404)

    def test_management_requires_admin_auth_and_cleans_deleted_account_mapping(self):
        self.assertEqual(self.import_paste('203.0.113.10:8080:u:p', auth=False).status_code, 401)
        self.assertEqual(self.http.get('/api/admin/proxies').status_code, 401)
        self.assertEqual(self.http.delete('/api/admin/proxies/fixture').status_code, 401)
        connections.assign(['deleted-account'], 'fixture')
        headers = {'X-Admin-Key': 'fixture-admin'}
        self.assertEqual(self.http.delete('/api/admin/proxies/fixture', headers=headers).status_code, 200)
        self.assertNotIn('deleted-account', connections._read()['accounts'])
