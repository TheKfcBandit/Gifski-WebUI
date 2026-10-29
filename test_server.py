"""HTTP contract tests, isolated from the user's media and installed executables."""
import http.client
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch
from urllib.parse import quote

import server


class FakeEngine:
    def __init__(self, root):
        self.root = root
        self.calls = []

    def status(self):
        return {'tools': {}, 'config': {}}

    def list_jobs(self):
        return []

    def get_job(self, job_id):
        return None

    def file_for(self, job_id, index=0):
        if job_id != 'testjob' or index != 0:
            raise FileNotFoundError('Unknown output')
        return self.root / 'output.gif'

    def configure(self, data):
        self.calls.append(('configure', data))
        return {'config': data}

    def probe(self, source):
        path = source.get('path') or source['paths'][0]
        path = Path(path.strip().strip('"')).resolve()
        if path.is_dir():
            path = sorted(path.glob('*.png'))[0]
        if not path.is_file():
            raise ValueError('File missing')
        return {'path': str(path), 'name': path.name}


class HTTPTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.temp = tempfile.TemporaryDirectory(prefix='gifski-http-tests-')
        cls.root = Path(cls.temp.name)
        (cls.root / 'static').mkdir()
        (cls.root / 'static/index.html').write_text('<!doctype html><title>Test</title>')
        (cls.root / 'static/app.js').write_text('/* test */')
        (cls.root / 'output.gif').write_bytes(b'GIF89a0123456789')
        (cls.root / 'secret.txt').write_text('private')
        cls.root_patch = patch.object(server, 'ROOT', cls.root)
        cls.root_patch.start()
        cls.engine = FakeEngine(cls.root)
        cls.http = server.Server(('127.0.0.1', 0), cls.engine)
        cls.thread = threading.Thread(target=cls.http.serve_forever, daemon=True)
        cls.thread.start()
        cls.host = f'127.0.0.1:{cls.http.server_port}'
        cls.cookie = f'gifski_session={cls.http.token}'

    @classmethod
    def tearDownClass(cls):
        cls.http.shutdown()
        cls.http.server_close()
        cls.thread.join(timeout=2)
        cls.root_patch.stop()
        cls.temp.cleanup()

    def test_dependency_routes_require_session_and_same_origin(self):
        code, _, _ = self.request('GET', '/api/dependencies', auth=False)
        self.assertEqual(code, 401)
        with patch.object(self.http.dependencies, 'install') as install:
            code, _, _ = self.request('POST', '/api/dependencies/install', body='{"tool":"gifski"}', headers={'Content-Type': 'application/json', 'Origin': 'https://untrusted.example'})
            self.assertEqual(code, 403)
            install.assert_not_called()

    def test_dependency_install_contract_uses_fixed_tool(self):
        with patch.object(self.http.dependencies, 'install', return_value={'busy': True}) as install:
            code, _, data = self.request('POST', '/api/dependencies/install', body='{"tool":"gifski"}', headers={'Content-Type': 'application/json'})
            self.assertEqual(code, 202)
            self.assertTrue(json.loads(data)['busy'])
            install.assert_called_once_with('gifski')

    def request(self, method, path, body=None, auth=True, headers=None):
        h = {'Connection': 'close'}
        if auth:
            h['Cookie'] = self.cookie
        if headers:
            h.update(headers)
        conn = http.client.HTTPConnection('127.0.0.1', self.http.server_port, timeout=5)
        try:
            conn.request(method, path, body=body, headers=h)
            response = conn.getresponse()
            return response.status, dict(response.getheaders()), response.read()
        finally:
            conn.close()

    def json_post(self, path, value, **kwargs):
        headers = {'Content-Type': 'application/json', **kwargs.pop('headers', {})}
        return self.request('POST', path, json.dumps(value), headers=headers, **kwargs)

    def test_page_sets_browser_session_and_security_headers(self):
        status, headers, body = self.request('GET', '/', auth=False)
        self.assertEqual(status, 200)
        cookie = headers['Set-Cookie']
        self.assertIn('HttpOnly', cookie)
        self.assertIn('SameSite=Strict', cookie)
        self.assertIn("frame-ancestors 'none'", headers['Content-Security-Policy'])
        self.assertEqual(headers['X-Content-Type-Options'], 'nosniff')

    def test_session_required_even_for_status_and_download(self):
        for path in ('/api/status', '/api/jobs', '/api/files/testjob/0'):
            self.assertEqual(self.request('GET', path, auth=False)[0], 401)
        self.assertEqual(self.request('GET', '/api/status')[0], 200)
        self.assertEqual(self.request('GET', '/api/status', headers={'Cookie': 'gifski_session=wrong'})[0], 401)

    def test_unrecognized_host_blocked_before_setting_cookie(self):
        status, headers, _ = self.request('GET', '/', auth=False, headers={'Host': 'attacker.example'})
        self.assertEqual(status, 403)
        self.assertNotIn('Set-Cookie', headers)

    def test_cross_origin_mutations_blocked(self):
        previous = len(self.engine.calls)
        self.assertEqual(self.json_post('/api/config', {}, headers={'Origin': 'https://attacker.example'})[0], 403)
        self.assertEqual(self.json_post('/api/config', {}, headers={'Sec-Fetch-Site': 'cross-site'})[0], 403)
        self.assertEqual(len(self.engine.calls), previous)
        self.assertEqual(self.json_post('/api/config', {}, headers={'Origin': f'http://{self.host}'})[0], 200)

    def test_json_contract_rejects_form_posts_and_non_objects(self):
        self.assertEqual(self.request('POST', '/api/config', 'gifski=evil', headers={'Content-Type': 'application/x-www-form-urlencoded'})[0], 400)
        self.assertEqual(self.request('POST', '/api/config', '[]', headers={'Content-Type': 'application/json'})[0], 400)

    def test_only_registered_media_can_be_previewed(self):
        source = self.root / 'clip.mp4'
        source.write_bytes(b'video bytes')
        url = '/api/source?path=' + quote(str(source), safe='')
        self.assertEqual(self.request('GET', url)[0], 403)
        self.assertEqual(self.json_post('/api/probe', {'source': {'kind': 'video', 'path': str(source)}})[0], 200)
        self.assertEqual(self.request('GET', url)[2], b'video bytes')
        secret_url = '/api/source?path=' + quote(str(self.root / 'secret.txt'), safe='')
        self.assertEqual(self.request('GET', secret_url)[0], 403)

    def test_path_traversal_does_not_serve_files(self):
        for path in ('/../secret.txt', '/%2e%2e/secret.txt', '/static/../secret.txt', '/server.py', '/api/files/../secret.txt'):
            status, _, body = self.request('GET', path)
            self.assertIn(status, (400, 404))
            self.assertNotIn(b'private', body)

    def test_upload_sanitizes_path_and_never_overwrites_original(self):
        paths = []
        for content in (b'first', b'second'):
            status, _, body = self.request('POST', '/api/upload?name=' + quote('../../clip.mp4', safe=''), content)
            self.assertEqual(status, 200)
            result = json.loads(body)
            path = Path(result['path'])
            self.assertTrue(path.is_relative_to(self.root / 'data/uploads'))
            self.assertEqual(path.name, 'clip.mp4')
            self.assertEqual(path.read_bytes(), content)
            paths.append(path)
        self.assertNotEqual(paths[0], paths[1])
        self.assertEqual(paths[0].read_bytes(), b'first')
        self.assertEqual(self.request('POST', '/api/upload?name=server.py', b'bad')[0], 400)
        self.assertEqual(self.request('POST', '/api/upload?name=empty.mp4', b'')[0], 413)

    def test_range_and_head_support_video_seeking(self):
        status, headers, body = self.request('GET', '/api/files/testjob/0', headers={'Range': 'bytes=3-7'})
        self.assertEqual(status, 206)
        self.assertEqual(body, b'89a01')
        self.assertEqual(headers['Content-Range'], 'bytes 3-7/16')
        status, _, body = self.request('GET', '/api/files/testjob/0', headers={'Range': 'bytes=-3'})
        self.assertEqual((status, body), (206, b'789'))
        self.assertEqual(self.request('GET', '/api/files/testjob/0', headers={'Range': 'bytes=100-'})[0], 416)
        status, headers, body = self.request('HEAD', '/api/files/testjob/0')
        self.assertEqual(status, 200)
        self.assertEqual(headers['Content-Length'], '16')
        self.assertEqual(body, b'')

    def test_download_sets_attachment_without_arbitrary_path(self):
        status, headers, _ = self.request('GET', '/api/files/testjob/0?download=1')
        self.assertEqual(status, 200)
        self.assertIn('attachment;', headers['Content-Disposition'])
        self.assertEqual(self.request('GET', '/api/files/unknown/0')[0], 400)


if __name__ == '__main__':
    unittest.main()
