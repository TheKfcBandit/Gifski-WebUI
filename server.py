#!/usr/bin/env python3
"""Gifski WebUI. Local HTTP interface; Python standard library only."""
from __future__ import annotations

import argparse
import http.cookies
import json
import mimetypes
import os
from pathlib import Path
import re
import secrets
import shutil
import socket
import threading
import urllib.parse
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

from engine import Engine
from dependencies import DependencyManager

ROOT = Path(__file__).resolve().parent
MAX_JSON = 2 * 1024 * 1024
MAX_UPLOAD = 20 * 1024 ** 3
MEDIA = {'.mp4', '.m4v', '.webm', '.mov', '.mkv', '.avi', '.gif', '.png', '.y4m', '.apng', '.m2ts', '.ts', '.mpeg', '.mpg', '.wmv', '.avif', '.webp'}


class Server(ThreadingHTTPServer):
    daemon_threads = True
    allow_reuse_address = True

    def __init__(self, address, engine, preview=False):
        super().__init__(address, Handler)
        self.engine = engine
        self.dependencies = DependencyManager(engine.root, on_change=self.invalidate_tools)
        self.token = secrets.token_urlsafe(32)
        self.preview = preview
        self.sources = set()
        self.source_lock = threading.Lock()
        self.operation_lock = threading.RLock()


    def invalidate_tools(self):
        self.engine.invalidate_status()


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def setup(self):
        super().setup()
        self.connection.settimeout(60)

    def handle_one_request(self):
        self._response_started = False
        try:
            super().handle_one_request()
        except (ConnectionError, TimeoutError):
            # Browsers cancel media requests when seeking or changing source.
            # Windows reports some cancellations as ConnectionAbortedError.
            self.close_connection = True

    def send_response_only(self, code, message=None):
        self._response_status = code
        super().send_response_only(code, message)

    def end_headers(self):
        # Mark before writing: even a partial header write starts the response.
        # An interim 100 Continue still permits a later final error response.
        if self._response_status >= 200:
            self._response_started = True
        super().end_headers()

    def request_error(self, error, code):
        if self._response_started:
            # A streamed response cannot be replaced with a second HTTP reply.
            self.close_connection = True
            return
        self.send_json({'error': str(error)}, code)

    def log_message(self, fmt, *args):
        # Avoid noisy polling and leaking complete local paths into terminal logs.
        # Query strings can contain private local paths. Keep request logs off.
        return

    def headers_common(self):
        self.send_header('X-Content-Type-Options', 'nosniff')
        self.send_header('Referrer-Policy', 'no-referrer')
        self.send_header('Cache-Control', 'no-store')
        self.send_header('Content-Security-Policy', "default-src 'self'; script-src 'self'; style-src 'self' 'unsafe-inline'; img-src 'self' blob: data:; media-src 'self' blob:; connect-src 'self'; frame-ancestors 'none'; base-uri 'none'; object-src 'none'")

    def send_json(self, value, code=200):
        data = json.dumps(value, ensure_ascii=False).encode('utf-8')
        self.send_response(code)
        self.headers_common()
        self.send_header('Content-Type', 'application/json; charset=utf-8')
        self.send_header('Content-Length', str(len(data)))
        self.end_headers()
        if self.command != 'HEAD':
            self.wfile.write(data)

    def allowed_host(self):
        if len(self.headers.get_all('Host', [])) != 1:
            return False
        host = self.headers.get('Host', '')
        if self.server.preview:
            return bool(host)
        return host in {f'127.0.0.1:{self.server.server_port}', f'localhost:{self.server.server_port}'}

    def authenticated(self):
        try:
            cookies = http.cookies.SimpleCookie(self.headers.get('Cookie', ''))
            return secrets.compare_digest(cookies.get('gifski_session').value, self.server.token)
        except (AttributeError, http.cookies.CookieError):
            return False

    def guard(self, mutate=False):
        if not self.allowed_host():
            self.send_json({'error': 'Unrecognized host. Open the localhost URL printed by the launcher.'}, 403)
            return False
        if not self.authenticated():
            self.send_json({'error': 'Session expired. Reload the app.'}, 401)
            return False
        origin = self.headers.get('Origin')
        if origin:
            try:
                parsed = urllib.parse.urlsplit(origin)
                valid = (parsed.netloc == self.headers.get('Host') and parsed.scheme in ('http', 'https')
                         and not parsed.path and not parsed.query and not parsed.fragment)
            except ValueError:
                valid = False
            if not valid:
                self.send_json({'error': 'Cross-origin requests are not allowed.'}, 403)
                return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.send_json({'error': 'Cross-site requests are not allowed.'}, 403)
            return False
        return True

    def body_length(self, maximum):
        lengths = self.headers.get_all('Content-Length', [])
        if self.headers.get('Transfer-Encoding') or len(lengths) != 1 or not re.fullmatch(r'[0-9]{1,12}', lengths[0]):
            self.close_connection = True
            raise ValueError('A single valid Content-Length is required; chunked requests are not supported.')
        count = int(lengths[0])
        if not 0 < count <= maximum:
            self.close_connection = True
            raise ValueError('Invalid request size.')
        return count

    def body_json(self):
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            self.close_connection = True
            raise ValueError('Expected application/json.')
        count = self.body_length(MAX_JSON)
        raw = self.rfile.read(count)
        if len(raw) != count:
            self.close_connection = True
            raise ValueError('Request body interrupted.')
        def invalid_number(value):
            raise ValueError('JSON numbers must be finite.')
        try:
            value = json.loads(raw, parse_constant=invalid_number)
        except (RecursionError, UnicodeDecodeError) as e:
            raise ValueError('Invalid JSON encoding or nesting.') from e
        if not isinstance(value, dict):
            raise ValueError('Expected a JSON object.')
        return value

    @staticmethod
    def object_field(body, key, fallback=None):
        value = body.get(key, fallback)
        if not isinstance(value, dict):
            raise ValueError(f'{key} must be an object.')
        return value

    def register_source(self, source):
        if not isinstance(source, dict):
            raise ValueError('Source must be an object.')
        paths = source.get('paths', []) if source.get('kind') == 'sequence' else [source.get('path')]
        if not isinstance(paths, list) or any(path is not None and not isinstance(path, str) for path in paths):
            raise ValueError('Source paths must be strings.')
        with self.server.source_lock:
            for path in paths:
                if path:
                    p = Path(path.strip().strip('"')).expanduser().resolve()
                    if p.is_file() and p.suffix.lower() in MEDIA:
                        self.server.sources.add(str(p))

    def send_file(self, path, download=False, cookie=False):
        path = Path(path)
        if not path.is_file():
            return self.send_json({'error': 'File not found.'}, 404)
        size = path.stat().st_size
        start, end, partial = 0, max(0, size - 1), False
        range_value = self.headers.get('Range')
        if range_value and size:
            match = re.fullmatch(r'bytes=(\d*)-(\d*)', range_value)
            if match and (match[1] or match[2]):
                if match[1]:
                    start = int(match[1])
                    end = min(size - 1, int(match[2])) if match[2] else size - 1
                else:
                    start = max(0, size - int(match[2]))
                if start >= size or start > end:
                    self.send_response(416)
                    self.send_header('Content-Range', f'bytes */{size}')
                    self.send_header('Content-Length', '0')
                    self.end_headers()
                    return
                partial = True
        length = end - start + 1 if size else 0
        self.send_response(206 if partial else 200)
        self.headers_common()
        mime = mimetypes.guess_type(path.name)[0] or 'application/octet-stream'
        self.send_header('Content-Type', mime)
        self.send_header('Content-Length', str(length))
        self.send_header('Accept-Ranges', 'bytes')
        if partial:
            self.send_header('Content-Range', f'bytes {start}-{end}/{size}')
        if cookie:
            self.send_header('Set-Cookie', f'gifski_session={self.server.token}; Path=/; HttpOnly; SameSite=Strict')
        if download:
            safe_name = re.sub(r'[^A-Za-z0-9_.-]', '_', path.name)
            self.send_header('Content-Disposition', f'attachment; filename="{safe_name}"; filename*=UTF-8\'\'{urllib.parse.quote(path.name)}')
        self.end_headers()
        if self.command == 'HEAD':
            return
        with path.open('rb') as f:
            f.seek(start)
            remaining = length
            while remaining:
                data = f.read(min(1024 * 1024, remaining))
                if not data:
                    break
                self.wfile.write(data)
                remaining -= len(data)

    def do_HEAD(self):
        self.do_GET()

    def do_GET(self):
        parsed = urllib.parse.urlsplit(self.path)
        route = parsed.path
        try:
            if not self.allowed_host():
                return self.send_json({'error': 'Unrecognized host.'}, 403)
            if route == '/':
                return self.send_file(ROOT / 'static/index.html', cookie=True)
            if route in ('/app.js', '/style.css'):
                return self.send_file(ROOT / 'static' / route[1:])
            if route == '/favicon.ico':
                return self.send_json({}, 404)
            if not self.guard():
                return
            if route == '/api/dependencies':
                return self.send_json(self.server.dependencies.status(self.server.engine.status().get('tools', {})))
            if route == '/api/status':
                return self.send_json(self.server.engine.status())
            if route == '/api/jobs':
                summaries = urllib.parse.parse_qs(parsed.query).get('summary') == ['1']
                jobs = self.server.engine.list_job_summaries() if summaries else self.server.engine.list_jobs()
                return self.send_json({'jobs': jobs})
            match = re.fullmatch(r'/api/jobs/([A-Za-z0-9_-]+)', route)
            if match:
                job = self.server.engine.get_job(match[1])
                return self.send_json(job if job else {'error': 'Job not found.'}, 200 if job else 404)
            match = re.fullmatch(r'/api/files/([A-Za-z0-9_-]+)/(\d+)', route)
            if match:
                return self.send_file(self.server.engine.file_for(match[1], int(match[2])), 'download' in urllib.parse.parse_qs(parsed.query))
            if route == '/api/source':
                raw = urllib.parse.parse_qs(parsed.query).get('path', [''])[0]
                path = str(Path(raw).expanduser().resolve())
                with self.server.source_lock:
                    allowed = path in self.server.sources
                if not allowed:
                    return self.send_json({'error': 'Select and inspect this media file first.'}, 403)
                if str(Path(path).resolve()) != path:
                    return self.send_json({'error': 'Source changed. Select and inspect it again.'}, 409)
                return self.send_file(path)
            if route == '/api/features':
                return self.send_json({'text': (ROOT / 'FEATURES.md').read_text('utf-8')})
            return self.send_json({'error': 'Not found.'}, 404)
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except (ValueError, KeyError, IndexError, FileNotFoundError) as e:
            self.request_error(e, 400)
        except Exception as e:
            self.request_error(e, 500)

    def do_POST(self):
        if not self.guard(mutate=True):
            self.close_connection = True
            return
        route = urllib.parse.urlsplit(self.path).path
        try:
            if route == '/api/upload':
                try:
                    length = self.body_length(MAX_UPLOAD)
                except ValueError:
                    self.close_connection = True
                    return self.send_json({'error': 'Upload must be between 1 byte and 20 GiB. Use a local path for larger files.'}, 413)
                name = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('name', ['media'])[0]
                name = re.sub(r'[^\w. -]', '_', Path(name.replace('\\', '/')).name).strip(' .')[:180] or 'media'
                if Path(name).suffix.lower() not in MEDIA:
                    self.close_connection = True
                    return self.send_json({'error': 'Choose a video, GIF, Y4M or PNG file.'}, 400)
                directory = ROOT / 'data/uploads' / secrets.token_hex(8)
                directory.mkdir(parents=True, exist_ok=True)
                if shutil.disk_usage(directory).free < length + 64 * 1024 ** 2:
                    directory.rmdir()
                    self.close_connection = True
                    return self.send_json({'error': 'Insufficient disk space for an upload copy. Use a local path instead.'}, 413)
                target = directory / name
                try:
                    with target.open('xb') as f:
                        remaining = length
                        while remaining:
                            block = self.rfile.read(min(1024 * 1024, remaining))
                            if not block:
                                raise ValueError('Upload interrupted.')
                            f.write(block)
                            remaining -= len(block)
                except Exception:
                    target.unlink(missing_ok=True)
                    directory.rmdir()
                    raise
                self.register_source({'path': str(target)})
                return self.send_json({'path': str(target), 'name': name, 'bytes': length})
            body = self.body_json()
            if route == '/api/status/refresh':
                self.server.invalidate_tools()
                return self.send_json(self.server.engine.status())
            if route == '/api/dependencies/check-updates':
                return self.send_json(self.server.dependencies.check_updates(self.server.engine.status().get('tools', {})), 202)
            if route == '/api/dependencies/install-missing':
                with self.server.operation_lock:
                    if self.server.engine.has_active_jobs():
                        return self.send_json({'error': 'Finish or cancel queued jobs before installing tools.'}, 409)
                    self.server.invalidate_tools()
                    installed = self.server.engine.status().get('tools', {})
                    return self.send_json(self.server.dependencies.install_missing(installed), 202)
            if route == '/api/dependencies/install':
                with self.server.operation_lock:
                    if self.server.engine.has_active_jobs():
                        return self.send_json({'error': 'Finish or cancel queued jobs before installing tools.'}, 409)
                    return self.send_json(self.server.dependencies.install(body.get('tool')), 202)
            if route == '/api/config':
                with self.server.operation_lock:
                    if self.server.dependencies.status()['busy'] or self.server.engine.has_active_jobs():
                        return self.send_json({'error': 'Finish active jobs and installations before changing executable paths.'}, 409)
                    return self.send_json(self.server.engine.configure(body))
            if route == '/api/probe':
                source = self.object_field(body, 'source', body)
                result = self.server.engine.probe(source)
                self.register_source(source)
                if result.get('path'):
                    self.register_source({'path': result['path']})
                return self.send_json(result)
            if route == '/api/plan':
                return self.send_json(self.server.engine.plan(self.object_field(body, 'settings', body)))
            if route == '/api/jobs':
                settings = self.object_field(body, 'settings', {})
                self.object_field(settings, 'source', {})
                with self.server.operation_lock:
                    if self.server.dependencies.status()['busy']:
                        return self.send_json({'error': 'Wait for the dependency installation to finish before starting a job.'}, 409)
                    result = self.server.engine.submit(settings, body.get('mode', 'encode'))
                self.register_source(settings.get('source', {}))
                return self.send_json(result, 201)
            match = re.fullmatch(r'/api/jobs/([A-Za-z0-9_-]+)/cancel', route)
            if match:
                return self.send_json(self.server.engine.cancel(match[1]))
            return self.send_json({'error': 'Not found.'}, 404)
        except (ConnectionError, TimeoutError):
            self.close_connection = True
        except (ValueError, KeyError, IndexError, FileNotFoundError, json.JSONDecodeError) as e:
            self.close_connection = True
            self.request_error(e, 400)
        except Exception as e:
            self.request_error(e, 500)


def main():
    parser = argparse.ArgumentParser(description='Local Gifski WebUI')
    parser.add_argument('--port', type=int, default=8765)
    parser.add_argument('--no-browser', action='store_true')
    parser.add_argument('--preview', action='store_true', help=argparse.SUPPRESS)
    parser.add_argument('--host', help=argparse.SUPPRESS)
    parser.add_argument('--strictPort', action='store_true', help=argparse.SUPPRESS)
    args = parser.parse_args()
    port = int(os.environ.get('PORT', args.port)) if args.preview else args.port
    engine = Engine(ROOT)
    try:
        server = Server(('0.0.0.0' if args.preview else '127.0.0.1', port), engine, args.preview)
    except OSError as e:
        raise SystemExit(f'Cannot open port {port}: {e}\nClose the other instance or run: python server.py --port 8766')
    url = f'http://127.0.0.1:{server.server_port}'
    print(f'\nGifski WebUI\n{url}\nKeep this window open. Ctrl+C stops the app.\n', flush=True)
    if not args.no_browser and not args.preview:
        threading.Timer(0.7, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print('\nStopping active jobs...')
    finally:
        for job in engine.list_job_summaries():
            if job.get('status') in ('queued', 'running'):
                engine.cancel(job['id'])
        server.server_close()


if __name__ == '__main__':
    main()
