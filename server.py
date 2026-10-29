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


    def invalidate_tools(self):
        if hasattr(self.engine, "lock"):
            with self.engine.lock:
                self.engine._status = None


class Handler(BaseHTTPRequestHandler):
    protocol_version = 'HTTP/1.1'

    def log_message(self, fmt, *args):
        # Avoid noisy polling and leaking complete local paths into terminal logs.
        if args and isinstance(args[0], str) and '/api/jobs' in args[0]:
            return
        super().log_message(fmt, *args)

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
            parsed = urllib.parse.urlsplit(origin)
            if parsed.netloc != self.headers.get('Host') or parsed.scheme not in ('http', 'https'):
                self.send_json({'error': 'Cross-origin requests are not allowed.'}, 403)
                return False
        if self.headers.get('Sec-Fetch-Site') == 'cross-site':
            self.send_json({'error': 'Cross-site requests are not allowed.'}, 403)
            return False
        return True

    def body_json(self):
        if self.headers.get('Content-Type', '').split(';')[0].strip() != 'application/json':
            raise ValueError('Expected application/json.')
        count = int(self.headers.get('Content-Length', '0'))
        if not 0 < count <= MAX_JSON:
            raise ValueError('Invalid JSON request size.')
        value = json.loads(self.rfile.read(count))
        if not isinstance(value, dict):
            raise ValueError('Expected a JSON object.')
        return value

    def register_source(self, source):
        paths = source.get('paths', []) if source.get('kind') == 'sequence' else [source.get('path')]
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
                return self.send_json(self.server.dependencies.status())
            if route == '/api/status':
                return self.send_json(self.server.engine.status())
            if route == '/api/jobs':
                return self.send_json({'jobs': self.server.engine.list_jobs()})
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
                return self.send_file(path)
            if route == '/api/features':
                return self.send_json({'text': (ROOT / 'FEATURES.md').read_text('utf-8')})
            return self.send_json({'error': 'Not found.'}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, KeyError, IndexError, FileNotFoundError) as e:
            self.send_json({'error': str(e)}, 400)
        except Exception as e:
            self.send_json({'error': str(e)}, 500)

    def do_POST(self):
        if not self.guard(mutate=True):
            self.close_connection = True
            return
        route = urllib.parse.urlsplit(self.path).path
        try:
            if route == '/api/upload':
                length = int(self.headers.get('Content-Length', '0'))
                if not 0 < length <= MAX_UPLOAD:
                    self.close_connection = True
                    return self.send_json({'error': 'Upload must be between 1 byte and 20 GiB. Use a local path for larger files.'}, 413)
                name = urllib.parse.parse_qs(urllib.parse.urlsplit(self.path).query).get('name', ['media'])[0]
                name = re.sub(r'[^\w. -]', '_', Path(name.replace('\\', '/')).name).strip(' .')[:180] or 'media'
                if Path(name).suffix.lower() not in MEDIA:
                    self.close_connection = True
                    return self.send_json({'error': 'Choose a video, GIF, Y4M or PNG file.'}, 400)
                directory = ROOT / 'data/uploads' / secrets.token_hex(8)
                directory.mkdir(parents=True, exist_ok=True)
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
                    raise
                self.register_source({'path': str(target)})
                return self.send_json({'path': str(target), 'name': name, 'bytes': length})
            body = self.body_json()
            if route == '/api/dependencies/install':
                return self.send_json(self.server.dependencies.install(body.get('tool')), 202)
            if route == '/api/config':
                return self.send_json(self.server.engine.configure(body))
            if route == '/api/probe':
                source = body.get('source', body)
                result = self.server.engine.probe(source)
                self.register_source(source)
                if result.get('path'):
                    self.register_source({'path': result['path']})
                return self.send_json(result)
            if route == '/api/plan':
                return self.send_json(self.server.engine.plan(body.get('settings', body)))
            if route == '/api/jobs':
                settings = body.get('settings', {})
                result = self.server.engine.submit(settings, body.get('mode', 'encode'))
                self.register_source(settings.get('source', {}))
                return self.send_json(result, 201)
            match = re.fullmatch(r'/api/jobs/([A-Za-z0-9_-]+)/cancel', route)
            if match:
                return self.send_json(self.server.engine.cancel(match[1]))
            return self.send_json({'error': 'Not found.'}, 404)
        except (BrokenPipeError, ConnectionResetError):
            pass
        except (ValueError, KeyError, IndexError, FileNotFoundError, json.JSONDecodeError) as e:
            self.send_json({'error': str(e)}, 400)
        except Exception as e:
            self.send_json({'error': str(e)}, 500)


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
        for job in engine.list_jobs():
            if job.get('status') in ('queued', 'running'):
                engine.cancel(job['id'])
        server.server_close()


if __name__ == '__main__':
    main()
