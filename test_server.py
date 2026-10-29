"""HTTP contract tests, isolated from the user's media and installed executables."""
import http.client
import io
import json
from pathlib import Path
import tempfile
import threading
import socket
import unittest
from unittest.mock import patch
from urllib.parse import quote

import server


class FaultSocket:
    """Exercise the real handler with deterministic socket-write failures."""
    def __init__(self, request, fail_at=None, error=None):
        self.incoming = io.BytesIO(request)
        self.fail_at = fail_at
        self.error = error
        self.attempts = 0
        self.sent = []

    def makefile(self, *args):
        return self.incoming

    def settimeout(self, value):
        pass

    def sendall(self, data):
        self.attempts += 1
        if self.attempts == self.fail_at:
            raise self.error
        self.sent.append(bytes(data))


class FakeEngine:
    def __init__(self, root):
        self.root = root
        self.calls = []

    def status(self):
        return {'tools': {}, 'config': {}}

    def list_jobs(self):
        return []

    def list_job_summaries(self):
        return []

    def has_active_jobs(self):
        return False

    def invalidate_status(self):
        pass

    def get_job(self, job_id):
        return None

    def file_for(self, job_id, index=0):
        if job_id != 'testjob' or index != 0:
            raise FileNotFoundError('Unknown output')
        return self.root / 'output.gif'

    def configure(self, data):
        self.calls.append(('configure', data))
        return {'config': data}

    def plan(self, data):
        return {'settings': data}

    def submit(self, data, mode='encode'):
        return {'settings': data, 'mode': mode}

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

    def test_update_check_contract_and_origin_guard(self):
        with patch.object(self.http.dependencies, 'check_updates', return_value={'updates': {'state': 'checking'}}) as check, patch.object(self.engine, 'invalidate_status') as invalidate:
            code, _, data = self.json_post('/api/dependencies/check-updates', {})
            self.assertEqual(code, 202)
            self.assertEqual(json.loads(data)['updates']['state'], 'checking')
            check.assert_called_once_with({})
            invalidate.assert_not_called()
        self.assertEqual(self.json_post('/api/dependencies/check-updates', {}, auth=False)[0], 401)

    def test_explicit_installed_version_refresh_invalidates_status(self):
        with patch.object(self.http, 'invalidate_tools') as invalidate:
            self.assertEqual(self.json_post('/api/status/refresh', {})[0], 200)
            invalidate.assert_called_once_with()
        self.assertEqual(self.json_post('/api/status/refresh', {}, auth=False)[0], 401)

    def test_tool_changes_and_job_submission_do_not_overlap(self):
        with patch.object(self.engine, 'has_active_jobs', return_value=True):
            self.assertEqual(self.json_post('/api/dependencies/install', {'tool': 'gifski'})[0], 409)
            self.assertEqual(self.json_post('/api/dependencies/install-missing', {})[0], 409)
            self.assertEqual(self.json_post('/api/config', {'gifski': 'new'})[0], 409)
        with patch.object(self.http.dependencies, 'status', return_value={'busy': True}):
            self.assertEqual(self.json_post('/api/jobs', {'settings': {'source': {}}})[0], 409)
            self.assertEqual(self.json_post('/api/config', {'ffmpeg': 'new'})[0], 409)

    def test_install_missing_refreshes_detection_and_requires_session(self):
        snapshot = {'ffmpeg': {'available': True, 'origin': 'path'}}
        with patch.object(self.engine, 'status', return_value={'tools': snapshot}), patch.object(self.engine, 'invalidate_status') as invalidate, patch.object(self.http.dependencies, 'install_missing', return_value={'busy': True}) as install:
            self.assertEqual(self.json_post('/api/dependencies/install-missing', {})[0], 202)
            invalidate.assert_called_once_with()
            install.assert_called_once_with(snapshot)
            self.assertEqual(self.json_post('/api/dependencies/install-missing', {}, auth=False)[0], 401)
            self.assertEqual(self.json_post('/api/dependencies/install-missing', {}, headers={'Origin': 'https://untrusted.example'})[0], 403)
            self.assertEqual(install.call_count, 1)

    def test_history_summary_does_not_copy_full_jobs(self):
        with patch.object(self.engine, 'list_job_summaries', return_value=[{'id': 'one', 'status': 'running'}]) as summaries, patch.object(self.engine, 'list_jobs', side_effect=AssertionError('Full jobs should not be loaded')):
            code, _, data = self.request('GET', '/api/jobs?summary=1')
            self.assertEqual(code, 200)
            self.assertEqual(json.loads(data)['jobs'][0]['id'], 'one')
            summaries.assert_called_once_with()

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

    def simulate_request(self, requests, fail_at=None, error=None):
        connection = FaultSocket(requests.encode(), fail_at, error)
        handler = server.Handler(connection, ('127.0.0.1', 12345), self.http)
        return handler, connection

    def test_media_disconnect_never_attempts_a_second_response(self):
        request = f'GET /api/files/testjob/0 HTTP/1.1\r\nHost: {self.host}\r\nCookie: {self.cookie}\r\nConnection: close\r\n\r\n'
        for error_type in (ConnectionAbortedError, ConnectionResetError, BrokenPipeError, TimeoutError):
            for fail_at in (1, 2):
                with self.subTest(error=error_type.__name__, phase='headers' if fail_at == 1 else 'body'):
                    handler, connection = self.simulate_request(request, fail_at, error_type(10053, 'Connection aborted'))
                    self.assertTrue(handler.close_connection)
                    self.assertEqual(connection.attempts, fail_at)
                    self.assertNotIn(b'500 Internal Server Error', b''.join(connection.sent))

    def test_disconnect_while_sending_post_guard_or_parser_error(self):
        for request in (
            f'POST /api/config HTTP/1.1\r\nHost: {self.host}\r\nContent-Length: 2\r\n\r\n{{}}',
            f'PATCH /api/config HTTP/1.1\r\nHost: {self.host}\r\n\r\n',
        ):
            handler, connection = self.simulate_request(request, 1, ConnectionAbortedError(10053, 'Connection aborted'))
            self.assertTrue(handler.close_connection)
            self.assertEqual(connection.attempts, 1)

    def test_disconnect_while_sending_fallback_error_is_quiet(self):
        request = f'GET /api/status HTTP/1.1\r\nHost: {self.host}\r\nCookie: {self.cookie}\r\nConnection: close\r\n\r\n'
        with patch.object(self.engine, 'status', side_effect=RuntimeError('Engine unavailable')):
            handler, connection = self.simulate_request(request, 1, ConnectionAbortedError(10053, 'Connection aborted'))
        self.assertTrue(handler.close_connection)
        self.assertEqual(connection.attempts, 1)
        with patch.object(self.engine, 'status', side_effect=RuntimeError('Engine unavailable')):
            _, connection = self.simulate_request(request)
        self.assertIn(b'500 Internal Server Error', b''.join(connection.sent))
        self.assertIn(b'Engine unavailable', b''.join(connection.sent))

    def test_midstream_file_error_closes_without_appending_json(self):
        request = f'GET /api/files/testjob/0 HTTP/1.1\r\nHost: {self.host}\r\nCookie: {self.cookie}\r\nConnection: close\r\n\r\n'
        with patch.object(Path, 'open', side_effect=OSError('Read failure')):
            handler, connection = self.simulate_request(request)
        self.assertTrue(handler.close_connection)
        self.assertEqual(connection.attempts, 1)
        self.assertIn(b'200 OK', connection.sent[0])
        self.assertNotIn(b'500 Internal Server Error', connection.sent[0])

    def test_keep_alive_resets_response_state_before_next_error(self):
        request = (f'GET /api/status HTTP/1.1\r\nHost: {self.host}\r\nCookie: {self.cookie}\r\n\r\n'
                   f'POST /api/config HTTP/1.1\r\nHost: {self.host}\r\nCookie: {self.cookie}\r\n'
                   'Content-Type: application/json\r\nContent-Length: 2\r\nConnection: close\r\n\r\n[]')
        _, connection = self.simulate_request(request)
        sent = b''.join(connection.sent)
        self.assertEqual(sent.count(b'HTTP/1.1'), 2)
        self.assertIn(b'200 OK', sent)
        self.assertIn(b'400 Bad Request', sent)

    def test_interim_continue_does_not_block_final_error_response(self):
        request = (f'POST /api/config HTTP/1.1\r\nHost: {self.host}\r\nCookie: {self.cookie}\r\n'
                   'Expect: 100-continue\r\nContent-Type: application/json\r\n'
                   'Content-Length: 2\r\nConnection: close\r\n\r\n[]')
        _, connection = self.simulate_request(request)
        sent = b''.join(connection.sent)
        self.assertEqual(sent.count(b'HTTP/1.1'), 2)
        self.assertIn(b'100 Continue', sent)
        self.assertIn(b'400 Bad Request', sent)

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

    def test_rejects_invalid_numbers_nested_types_and_origins(self):
        for raw in ('{"fps":NaN}', '{"fps":Infinity}', '[' * 1100 + '0' + ']' * 1100):
            self.assertEqual(self.request('POST', '/api/plan', raw, headers={'Content-Type': 'application/json'})[0], 400)
        for route, body in (('/api/probe', {'source': []}), ('/api/plan', {'settings': 'bad'}), ('/api/jobs', {'settings': {'source': None}})):
            self.assertEqual(self.json_post(route, body)[0], 400)
        for origin in ('http://[invalid', 'null', f'http://{self.host}/unexpected'):
            self.assertEqual(self.json_post('/api/config', {}, headers={'Origin': origin})[0], 403)

    def test_ambiguous_framing_is_rejected_and_connection_closed(self):
        for extra in ('Content-Length: 2\r\nContent-Length: 2', 'Content-Length: 2\r\nTransfer-Encoding: chunked'):
            with socket.create_connection(('127.0.0.1', self.http.server_port), timeout=3) as conn:
                wire = f'POST /api/config HTTP/1.1\r\nHost: {self.host}\r\nCookie: {self.cookie}\r\nContent-Type: application/json\r\n{extra}\r\n\r\n{{}}'
                conn.sendall(wire.encode())
                chunks = []
                while True:
                    block = conn.recv(8192)
                    if not block:
                        break
                    chunks.append(block)
                response = b''.join(chunks)
                self.assertIn(b'400 Bad Request', response)

    def test_rejected_body_closes_keep_alive_connection(self):
        with socket.create_connection(('127.0.0.1', self.http.server_port), timeout=3) as conn:
            wire = f'POST /api/config HTTP/1.1\r\nHost: {self.host}\r\nCookie: {self.cookie}\r\nContent-Type: text/plain\r\nContent-Length: 2\r\n\r\n{{}}'
            conn.sendall(wire.encode())
            response = b''
            while True:
                block = conn.recv(8192)
                if not block:
                    break
                response += block
            self.assertIn(b'400 Bad Request', response)

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
