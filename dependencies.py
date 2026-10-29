"""Explicit, app-local dependency installation from fixed trusted providers.

The web server must guard installation requests with its local-session and
same-origin checks. No installer is run and no system PATH or registry is edited.
"""
from __future__ import annotations

import copy
import hashlib
from html.parser import HTMLParser
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import socket
import ssl
import stat
import struct
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
import uuid
import zipfile

GITHUB_RELEASE = 'https://api.github.com/repos/ImageOptim/gifski/releases/latest'
GIFSKI_RELEASE_PAGE = 'https://github.com/ImageOptim/gifski/releases/latest'
FFMPEG_URL = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
FFMPEG_RELEASES = 'https://ffmpeg.org/releases/'
FFMPEG_TAGS = 'https://api.github.com/repos/FFmpeg/FFmpeg/tags?per_page=100'
UPDATE_CACHE_SECONDS = 15 * 60
MAX_LICENSE_BYTES = 4 * 1024 * 1024
MAX_ARCHIVE = 512 * 1024 * 1024
MAX_EXPANDED = 1024 * 1024 * 1024
MAX_EXECUTABLE = 512 * 1024 * 1024
INSTALL_TIMEOUT_SECONDS = 20 * 60
INSTALL_IDLE_SECONDS = 45
INSTALL_SOCKET_TIMEOUT_SECONDS = 8
INSTALL_CANCEL_GRACE_SECONDS = 20
INSTALL_ACTIVE_STATES = ('queued', 'installing', 'cancelling')
ALLOWED_HOSTS = frozenset({
    'api.github.com', 'github.com', 'release-assets.githubusercontent.com',
    'objects.githubusercontent.com', 'www.gyan.dev', 'gyan.dev', 'ffmpeg.org', 'www.ffmpeg.org',
})


def validate_url(url):
    parsed = urllib.parse.urlsplit(url)
    if (parsed.scheme != 'https' or parsed.hostname not in ALLOWED_HOSTS
            or parsed.port not in (None, 443) or parsed.username or parsed.password):
        raise ValueError('The download was redirected outside the trusted providers.')
    return url


class TrustedRedirect(urllib.request.HTTPRedirectHandler):
    def redirect_request(self, req, fp, code, msg, headers, newurl):
        validate_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _open(url, timeout=30):
    validate_url(url)
    request = urllib.request.Request(url, headers={
        'User-Agent': 'Gifski-WebUI', 'Accept': 'application/vnd.github+json' if 'api.github.com' in url else '*/*',
    })
    response = urllib.request.build_opener(TrustedRedirect()).open(request, timeout=timeout)
    validate_url(response.geturl())
    return response


def _read_small(url, limit=2 * 1024 * 1024, check=None):
    deadline = time.monotonic() + 16
    data = bytearray()
    if check:
        check()
    with _open(url, timeout=8) as response:
        read = getattr(response, 'read1', response.read)
        while len(data) <= limit:
            if check:
                check()
            if time.monotonic() > deadline:
                raise TimeoutError('The provider metadata request timed out.')
            block = read(min(65536, limit + 1 - len(data)))
            if check:
                check()
            if not block:
                return bytes(data)
            data.extend(block)
    raise ValueError('Provider metadata exceeded the size limit.')


class UpdateCheckError(ValueError):
    """Only sanitized provider diagnostics are carried into the UI."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


class InstallationCancelled(Exception):
    pass


class InstallationTimeout(TimeoutError):
    pass


def provider_error(error):
    """Describe common network failures without exposing URLs, credentials or paths."""
    if isinstance(error, UpdateCheckError):
        return {'errorCode': error.code, 'message': str(error)}
    if isinstance(error, urllib.error.HTTPError):
        headers = error.headers or {}
        if error.code == 429 or (error.code == 403 and headers.get('X-RateLimit-Remaining') == '0'):
            return {'errorCode': 'rate_limited', 'message': 'The provider rate limit was reached. Wait a few minutes, then retry.'}
        if error.code == 407:
            return {'errorCode': 'proxy', 'message': 'Your proxy requires authentication. Check the system proxy configuration.'}
        if error.code in (401, 403):
            return {'errorCode': 'access_denied', 'message': 'The provider denied this request. Check firewall or proxy access to the release source.'}
        if error.code >= 500:
            return {'errorCode': 'provider_unavailable', 'message': f'The release source returned HTTP {error.code}. Try again later.'}
        return {'errorCode': 'http_error', 'message': f'The release source returned HTTP {error.code}. Open its release page or retry later.'}
    reason = error.reason if isinstance(error, urllib.error.URLError) else error
    if isinstance(reason, ssl.SSLCertVerificationError) or 'CERTIFICATE_VERIFY_FAILED' in str(reason):
        return {'errorCode': 'certificate', 'message': 'HTTPS certificate verification failed. Check your clock, Python certificates, and any HTTPS-inspecting proxy.'}
    if isinstance(reason, ssl.SSLError):
        return {'errorCode': 'tls', 'message': 'The secure connection failed. Check Python updates and your proxy or firewall.'}
    if isinstance(reason, socket.gaierror):
        return {'errorCode': 'dns', 'message': 'The release host could not be found. Check your connection, DNS, or proxy settings.'}
    if isinstance(reason, (TimeoutError, socket.timeout)) or 'timed out' in str(reason).lower():
        return {'errorCode': 'timeout', 'message': 'The release source timed out. Check your connection or retry later.'}
    if isinstance(error, (ValueError, TypeError, KeyError)):
        return {'errorCode': 'invalid_response', 'message': 'The release source returned unrecognized metadata. Open its release page or retry later.'}
    return {'errorCode': 'network', 'message': 'Could not reach the release source. Check your connection, proxy, and firewall settings.'}


class ReleasePageParser(HTMLParser):
    """Read only canonical page identity, never arbitrary linked release tags."""
    def __init__(self):
        super().__init__()
        self.urls = []

    def handle_starttag(self, tag, attrs):
        attrs = dict(attrs)
        if tag == 'meta' and attrs.get('property') == 'og:url':
            self.urls.append(attrs.get('content', ''))
        elif tag == 'link' and attrs.get('rel') == 'canonical':
            self.urls.append(attrs.get('href', ''))


def checksum(text):
    match = re.fullmatch(r'\s*(?:sha256:)?([a-fA-F0-9]{64})(?:\s+[^\r\n]*)?\s*', text)
    if not match:
        raise ValueError('The provider did not supply a valid SHA-256 checksum. Install manually instead.')
    return match[1].lower()


def release_version(value):
    """Comparable stable release tuple, deliberately excluding prerelease tags."""
    match = re.fullmatch(r'v?(\d{1,4})\.(\d{1,4})(?:\.(\d{1,4}))?', value)
    return tuple(int(part or 0) for part in match.groups()) if match else None


def installed_build(tool, value):
    """Identify upstream release bases without treating Git snapshots as releases."""
    prefix = r'gifski\s+' if tool == 'gifski' else re.escape(tool) + r'\s+version\s+'
    match = re.search(r'^\s*\ufeff?' + prefix + r'(\S+)', value, re.IGNORECASE | re.MULTILINE)
    token = match[1] if match else ''
    # Official FFmpeg tag builds use the n prefix. BtbN adds YYYYMMDD to
    # exact tag builds; Gyan, Ubuntu, Debian and others add vendor suffixes.
    prefix = 'v?' if tool == 'gifski' else '[nv]?'
    base = re.match(prefix + r'(\d{1,4}\.\d{1,4}(?:\.\d{1,4})?)(?=$|[-+])', token, re.I)
    base_version = base[1] if base else ''
    result = {'token': token, 'kind': 'unknown', 'baseVersion': base_version, 'comparable': None}
    if re.search(r'(?:^N-\d+-g[0-9a-f]+|(?:^|[-+])(?:git|snapshot|nightly|dev|dirty)(?:$|[-+.\d])|-\d+-g[0-9a-f]+)', token, re.I):
        result['kind'] = 'snapshot'
    elif re.search(r'(?:^|[-+.])(?:alpha|beta|rc)(?:$|[-+.\d])', token, re.I):
        result['kind'] = 'prerelease'
    elif base:
        result.update(kind='vendor' if token[base.end():] else 'stable', comparable=release_version(base_version))
    return result


def installed_version(tool, value):
    """Compatibility helper returning an orderable stable base and vendor marker."""
    build = installed_build(tool, value)
    return build['comparable'], build['kind'] != 'stable'


def _safe_name(name):
    # Reject all traversal, not just selected entries; never use extractall.
    name = name.replace('\\', '/')
    path = PurePosixPath(name)
    if (path.is_absolute() or '..' in path.parts or ':' in name
            or len(name) > 1024 or any(ord(char) < 32 for char in name)):
        raise ValueError('The archive contains an unsafe path.')
    return path


def extract_executables(archive, staging, wanted, check=None):
    """Copy only named regular binaries, retaining bounded license text separately."""
    staging = Path(staging)
    staged = {}
    licenses = []
    expanded = 0
    license_bytes = 0

    def copy_entry(name, size, regular, opener):
        nonlocal expanded, license_bytes
        if check:
            check()
        path = _safe_name(name)
        expanded += size
        if size < 0 or expanded > MAX_EXPANDED:
            raise ValueError('The archive exceeds the expanded size limit.')
        base = path.name.lower()
        if base not in wanted:
            if (regular and 0 < size <= 256 * 1024 and len(licenses) < 32
                    and license_bytes + size <= MAX_LICENSE_BYTES
                    and re.match(r'^(license|copying)(\.|$)', base)):
                with opener() as source:
                    data = source.read(size)
                licenses.append((path.name, data))
                license_bytes += len(data)
            return
        if not regular or size <= 0 or size > MAX_EXECUTABLE:
            raise ValueError('An executable is not a bounded regular file.')
        if base in staged:
            raise ValueError('The archive contains duplicate executables.')
        destination = staging / base
        with opener() as source, destination.open('xb') as target:
            remaining = size
            while remaining:
                if check:
                    check()
                block = source.read(min(1024 * 1024, remaining))
                if not block:
                    raise ValueError('An executable in the archive is truncated.')
                target.write(block)
                remaining -= len(block)
        with destination.open('rb') as executable:
            header = executable.read(64)
            if len(header) != 64 or header[:2] != b'MZ':
                raise ValueError('The download does not contain Windows executables.')
            pe_offset = struct.unpack_from('<I', header, 60)[0]
            if pe_offset > size - 6:
                raise ValueError('The Windows executable header is invalid.')
            executable.seek(pe_offset)
            if executable.read(6) != b'PE\x00\x00\x64\x86':
                raise ValueError('The download is not a Windows x64 executable.')
        staged[base] = destination

    if zipfile.is_zipfile(archive):
        with zipfile.ZipFile(archive) as z:
            if len(z.infolist()) > 20000:
                raise ValueError('The archive contains too many entries.')
            for entry in z.infolist():
                mode = entry.external_attr >> 16
                regular = not entry.is_dir() and stat.S_IFMT(mode) in (0, stat.S_IFREG)
                copy_entry(entry.filename, entry.file_size, regular, lambda e=entry: z.open(e))
    else:
        with tarfile.open(archive, 'r:*') as t:
            for index, entry in enumerate(t):
                if index >= 20000:
                    raise ValueError('The archive contains too many entries.')
                copy_entry(entry.name, entry.size, entry.isfile(), lambda e=entry: t.extractfile(e))
    if set(staged) != set(wanted):
        raise ValueError('The download is missing a required executable.')
    license_dir = staging / 'licenses'
    license_dir.mkdir(exist_ok=True)
    for index, (name, data) in enumerate(licenses):
        (license_dir / f'{index + 1}-{name}').write_bytes(data)
    return staged


class DependencyManager:
    def __init__(self, root, on_change=None):
        self.root = Path(root).resolve()
        self.on_change = on_change
        self.lock = threading.RLock()
        self.release_cache = {}
        self.release_errors = {}
        self.release_sources = {}
        self.check_pending = set()
        self.checked_at = None
        self.checked_monotonic = None
        self.check_state = 'idle'
        self.check_phase = ''
        self.check_cached = False
        self.refresh_error = ''
        self.installed_tools = {}
        self._operation = None
        self._install_thread = None
        self._closed = False
        self.installation = {'id': None, 'state': 'idle', 'tool': None, 'phase': '', 'startedAt': None,
                             'progress': 0, 'elapsedSeconds': 0, 'idleSeconds': 0, 'bytesReceived': 0, 'totalBytes': 0, 'bytesPerSecond': 0,
                             'etaSeconds': None, 'canCancel': False, 'cleanupPending': False, 'message': '', 'error': None}
        self.setup = {'state': 'idle', 'remaining': [], 'message': ''}
        self.supported = platform.system() == 'Windows' and platform.machine().lower() in ('amd64', 'x86_64')
        self.tools = {name: {'state': 'idle', 'progress': 0, 'message': '', 'error': None,
                             'version': '', 'sourceUrl': source}
                      for name, source in [('gifski', 'https://github.com/ImageOptim/gifski/releases'),
                                           ('ffmpeg', 'https://www.gyan.dev/ffmpeg/builds/')]}

    def status(self, installed_tools=None):
        with self.lock:
            tools = copy.deepcopy(self.tools)
            if installed_tools is not None:
                self.installed_tools = copy.deepcopy(installed_tools)
            updates = self._update_status()
            setup = copy.deepcopy(self.setup)
            installed = copy.deepcopy(self.installed_tools)
            installation = copy.deepcopy(self.installation)
            operation = self._operation
            now = time.monotonic()
            if operation and installation['state'] in INSTALL_ACTIVE_STATES:
                installation['elapsedSeconds'] = round(max(0, now - operation['started']), 1)
                installation['idleSeconds'] = round(max(0, now - operation['lastProgress']), 1)
            for name, item in tools.items():
                if operation and name in operation['names'] and item['state'] in INSTALL_ACTIVE_STATES:
                    item['elapsedSeconds'] = round(max(0, now - operation['started']), 1)
            busy = installation['cleanupPending'] or installation['state'] in INSTALL_ACTIVE_STATES or setup['state'] in INSTALL_ACTIVE_STATES or any(t['state'] in INSTALL_ACTIVE_STATES for t in tools.values())
            pending = [name for name, item in tools.items() if item['state'] in INSTALL_ACTIVE_STATES]
            affects_active = any(not installed.get(tool, {}).get('available') or self._is_portable_active(installed[tool])
                                 for name in pending for tool in ([name] if name == 'gifski' else ['ffmpeg', 'ffprobe']))
        for name, item in tools.items():
            wanted = [name] if name == 'gifski' else ['ffmpeg', 'ffprobe']
            item['installed'] = all((self.root / 'bin' / f'{tool}.exe').is_file() for tool in wanted)
            item['available'] = all(installed.get(tool, {}).get('available', False) for tool in wanted)
            item['portableUpdateAffectsActive'] = all(
                not installed.get(tool, {}).get('available') or self._is_portable_active(installed[tool]) for tool in wanted)
        return {'platform': f'{platform.system()} {platform.machine()}', 'supported': self.supported,
                'busy': busy, 'blocksEncoding': busy and affects_active and not installation['cleanupPending'],
                'tools': tools, 'updates': updates, 'setup': setup, 'installation': installation, 'destination': 'bin/',
                'missing': [name for name in ('gifski', 'ffmpeg', 'ffprobe') if not installed.get(name, {}).get('available', False)],
                'manual': {'gifski': 'Install Gifski from its official releases, or run brew install gifski on macOS. Then select its path in Engine settings.',
                           'ffmpeg': 'Install FFmpeg and FFprobe with your OS package manager, or select existing executable paths in Engine settings.'}}

    def _update(self, tool, **values):
        with self.lock:
            self.tools[tool].update(values)

    def _is_portable_active(self, local):
        if not local.get('available'):
            return False
        if local.get('origin') in ('local', 'portable'):
            return True
        path = local.get('path')
        if not path:
            return False
        try:
            return Path(path).resolve().parent == (self.root / 'bin').resolve()
        except (OSError, ValueError, RuntimeError):
            return False

    def _update_status(self):
        """Caller holds the lock. Reading status never accesses the network."""
        tools = {}
        for name in ('gifski', 'ffmpeg', 'ffprobe'):
            upstream = 'gifski' if name == 'gifski' else 'ffmpeg'
            local = self.installed_tools.get(name, {})
            version = str(local.get('version') or '')[:1000]
            build = installed_build(name, version)
            origin = local.get('origin', '')
            origin = 'configured' if origin in ('config', 'explicit') else origin
            origin_label = {'path': 'PATH', 'configured': 'the configured path', 'local': 'the app folder',
                            'portable': 'the app folder'}.get(origin, 'the selected executable')
            portable = self._is_portable_active(local)
            latest = self.release_cache.get(upstream, '')
            item = {'state': 'not_checked', 'installedVersion': version,
                    'latestVersion': latest, 'errorCode': None, 'sourceUsed': '',
                    'installedOrigin': origin, 'installedPath': str(local.get('path') or '')[:4096],
                    'installedKind': build['kind'], 'installedBaseVersion': build['baseVersion'],
                    'versionToken': build['token'], 'comparisonScope': 'upstream_stable',
                    'updateMethod': 'portable' if portable else 'external',
                    'updateHint': ('The app-local installer updates this executable in bin/.' if portable and self.supported
                                   else f'Update the active {name} installation with its original package manager or installer. A portable copy does not replace an external PATH or configured installation.'),
                    'sourceUrl': ('https://github.com/ImageOptim/gifski/releases' if upstream == 'gifski'
                                  else 'https://ffmpeg.org/download.html'),
                    'message': (f'Detected through {origin_label}. Check updates to compare with the latest upstream stable release.'
                                if local.get('available') else 'No working executable is selected. Scan tools or set an executable path.')}
            item.update(self.release_sources.get(upstream, {}))
            if self.refresh_error:
                item.update(state='error', errorCode='local_detection', message=self.refresh_error)
            elif upstream in self.check_pending and self.check_phase == 'installed':
                item.update(state='checking', message='Refreshing the active installed executable and version…')
            elif upstream in self.check_pending:
                item.update(state='checking', message='Checking the official release source…')
            elif upstream in self.release_errors:
                item.update(state='error', **self.release_errors[upstream])
            elif latest:
                local_version = build['comparable']
                remote_version = release_version(latest)
                if not local.get('available'):
                    item.update(state='not_installed', message='No working executable was detected. Scan tools or set a path; restart the app after changing system PATH.')
                elif build['kind'] == 'snapshot':
                    item.update(state='unknown', message=f'Detected through {origin_label}: a Git, nightly, or development build. Stable release {latest} is shown for reference; this build cannot be marked current or outdated by that comparison. Check its build provider for nightly updates.')
                elif build['kind'] == 'prerelease':
                    item.update(state='unknown', message=f'Detected through {origin_label}: a prerelease build. Its version cannot be reliably ordered against stable release {latest}.')
                elif local_version is None or remote_version is None:
                    item.update(state='unknown', message=f'The executable works through {origin_label}, but its version banner is not recognized. Check the installed version shown here or its build provider.')
                elif local_version < remote_version:
                    item.update(state='update_available', message=f'Detected through {origin_label}. A newer upstream stable release is available. OS and vendor packages may carry their own fixes.')
                elif local_version > remote_version:
                    item.update(state='newer', message=f'Detected through {origin_label}. This executable reports an upstream version newer than the latest stable release.')
                elif build['kind'] == 'vendor':
                    item.update(state='current', message=f'Detected through {origin_label}. Matches the latest upstream stable release. Vendor rebuilds and package-specific revisions are checked separately by its package manager or build provider.')
                else:
                    item.update(state='current', message=f'Detected through {origin_label}. Matches the latest upstream stable release.')
            tools[name] = item
        return {'state': self.check_state, 'checkedAt': self.checked_at,
                'phase': self.check_phase, 'cached': self.check_cached, 'tools': tools}

    def check_updates(self, installed_tools=None, refresh_installed=None):
        """Explicit user action; metadata only, cached to avoid provider rate limits."""
        if refresh_installed is not None and not callable(refresh_installed):
            raise ValueError('The installed-tool refresh must be callable.')
        with self.lock:
            if installed_tools is not None:
                self.installed_tools = copy.deepcopy(installed_tools)
            if self.check_state == 'checking':
                return self.status()
            use_cache = (self.checked_monotonic is not None
                    and time.monotonic() - self.checked_monotonic < UPDATE_CACHE_SECONDS
                    and self.check_state == 'completed')
            if use_cache and refresh_installed is None:
                self.check_cached = True
                return self.status()
            self.check_state = 'checking'
            self.check_phase = 'installed' if refresh_installed is not None else 'upstream'
            self.check_cached = False
            self.refresh_error = ''
            self.check_pending = {'gifski', 'ffmpeg'}
            if not use_cache:
                self.release_cache = {}
                self.release_errors = {}
                self.release_sources = {}
        try:
            threading.Thread(target=self._check_worker, args=(refresh_installed, use_cache), daemon=True, name='check-updates').start()
        except RuntimeError:
            with self.lock:
                self.check_state = 'failed'
                self.check_phase = ''
                self.check_pending.clear()
                self.release_errors = {tool: {'errorCode': 'startup', 'message': 'Could not start the update check. Try again.'}
                                       for tool in ('gifski', 'ffmpeg')}
        return self.status()

    def _latest_release(self, tool):
        sources = ((GITHUB_RELEASE, 'GitHub release API', self._gifski_api),
                   (GIFSKI_RELEASE_PAGE, 'Gifski release page', self._gifski_page)) if tool == 'gifski' else (
                   (FFMPEG_RELEASES, 'FFmpeg release archive', self._ffmpeg_archive),
                   (FFMPEG_TAGS, 'FFmpeg release tags (official mirror)', self._ffmpeg_tags))
        failures = []
        for url, label, parse in sources:
            try:
                version = parse(_read_small(url))
                with self.lock:
                    self.release_sources[tool] = {'sourceUsed': label, 'sourceUrl': (
                        'https://github.com/FFmpeg/FFmpeg/tags' if url == FFMPEG_TAGS else url)}
                return version
            except Exception as error:
                failures.append((label, provider_error(error)))
        message = ' '.join(f'{label}: {error["message"]}' for label, error in failures)
        raise UpdateCheckError(failures[0][1]['errorCode'], message)

    @staticmethod
    def _gifski_api(data):
        release = json.loads(data)
        if not isinstance(release, dict):
            raise ValueError('Invalid release metadata.')
        version = str(release.get('tag_name', '')).removeprefix('v')
        if release.get('draft') or release.get('prerelease') or release_version(version) is None:
            raise ValueError('The provider did not return a stable Gifski release.')
        return version

    @staticmethod
    def _gifski_page(data):
        page = ReleasePageParser()
        page.feed(data.decode('utf-8', errors='replace'))
        for value in page.urls:
            url = urllib.parse.urlsplit(urllib.parse.urljoin(GIFSKI_RELEASE_PAGE, value))
            if url.scheme != 'https' or url.netloc != 'github.com':
                continue
            match = re.fullmatch(r'/ImageOptim/gifski/releases/tag/(v?\d{1,4}\.\d{1,4}(?:\.\d{1,4})?)', url.path)
            if match and release_version(match[1]):
                return match[1].removeprefix('v')
        raise ValueError('The Gifski release page could not be understood.')

    @staticmethod
    def _ffmpeg_archive(data):
        page = data.decode('utf-8', errors='replace')
        versions = set(re.findall(r'href=[\"\']ffmpeg-(\d{1,4}\.\d{1,4}(?:\.\d{1,4})?)\.tar\.xz[\"\']', page))
        if not versions:
            raise ValueError('The FFmpeg release listing could not be understood.')
        return max(versions, key=release_version)

    @staticmethod
    def _ffmpeg_tags(data):
        # FFmpeg identifies stable releases with nX.Y[.Z] tags. GitHub's
        # release section is not current; only the official mirror tags apply.
        tags = json.loads(data)
        if not isinstance(tags, list):
            raise ValueError('Invalid FFmpeg tag metadata.')
        versions = [tag['name'][1:] for tag in tags if isinstance(tag, dict)
                    and isinstance(tag.get('name'), str) and tag['name'].startswith('n')
                    and release_version(tag['name'][1:])]
        if not versions:
            raise ValueError('No stable FFmpeg release tags were found.')
        return max(versions, key=release_version)

    def _check_worker(self, refresh_installed=None, use_cache=False):
        # Publish each independent result immediately. A slow source must not
        # hide another tool's version or make checks take the sum of timeouts.
        if refresh_installed is not None:
            try:
                installed = refresh_installed()
                if not isinstance(installed, dict) or any(not isinstance(value, dict) for value in installed.values()):
                    raise ValueError('Invalid local tool snapshot.')
                with self.lock:
                    self.installed_tools = copy.deepcopy(installed)
            except Exception:
                with self.lock:
                    self.check_state = 'failed'
                    self.check_phase = ''
                    self.check_pending.clear()
                    self.refresh_error = 'Could not refresh the active installed tools. Check executable paths, keep the launcher open, and retry. Restart the app after changing system PATH.'
                return
        if use_cache:
            with self.lock:
                self.check_state = 'completed'
                self.check_phase = ''
                self.check_pending.clear()
                self.check_cached = True
            return
        with self.lock:
            self.check_state = 'checking'
            self.check_phase = 'upstream'
            self.check_pending = {'gifski', 'ffmpeg'}
            self.refresh_error = ''
            self.release_cache = {}
            self.release_errors = {}
            self.release_sources = {}

        def check(name):
            try:
                version = self._latest_release(name)
                with self.lock:
                    self.release_cache[name] = version
            except Exception as error:
                with self.lock:
                    self.release_errors[name] = provider_error(error)
            finally:
                with self.lock:
                    self.check_pending.discard(name)

        workers = []
        for name in ('gifski', 'ffmpeg'):
            try:
                worker = threading.Thread(target=check, args=(name,), daemon=True, name=f'check-{name}')
                worker.start()
                workers.append(worker)
            except RuntimeError:
                with self.lock:
                    self.release_errors[name] = {'errorCode': 'startup', 'message': 'Could not start this update check. Try again.'}
                    self.check_pending.discard(name)
        for worker in workers:
            worker.join()
        with self.lock:
            self.checked_at = time.time()
            self.checked_monotonic = time.monotonic()
            self.check_state = 'completed' if not self.release_errors else ('partial' if self.release_cache else 'failed')
            self.check_phase = ''

    def install(self, tool, installed_tools=None):
        if not isinstance(tool, str) or tool not in self.tools:
            raise ValueError('Choose gifski or ffmpeg.')
        if not self.supported:
            raise ValueError('Automatic portable installation is available for Windows x64. Use the manual instructions on this platform.')
        self._start_install([tool], installed_tools, batch=False)
        return self.status()

    def install_missing(self, installed_tools):
        """Explicit setup action: install only absent tools, into this app's bin/."""
        if not self.supported:
            raise ValueError('Automatic portable installation is available for Windows x64. Use the manual instructions on this platform.')
        with self.lock:
            self._assert_install_ready()
            self.installed_tools = copy.deepcopy(installed_tools)
            queue = []
            if not installed_tools.get('gifski', {}).get('available'):
                queue.append('gifski')
            if not all(installed_tools.get(name, {}).get('available') for name in ('ffmpeg', 'ffprobe')):
                queue.append('ffmpeg')
            if queue:
                self._start_install(queue, installed_tools, batch=True)
            else:
                self.setup = {'state': 'completed', 'remaining': [], 'message': 'All required tools are already available.'}
        return self.status()

    def _install_missing_worker(self, queue):
        self._run_installation(queue)

    def _assert_install_ready(self):
        if self._closed:
            raise ValueError('The app is shutting down. Restart it before installing tools.')
        if (self._install_thread is not None and self._install_thread.is_alive()) or self.status()['busy']:
            raise ValueError('A dependency installation is already running or finishing cleanup.')

    def _start_install(self, names, installed_tools=None, batch=False):
        with self.lock:
            self._assert_install_ready()
            if installed_tools is not None:
                self.installed_tools = copy.deepcopy(installed_tools)
            started = time.monotonic()
            operation = {'id': uuid.uuid4().hex, 'names': list(names), 'batch': batch, 'cancel': threading.Event(),
                         'done': threading.Event(), 'started': started, 'lastProgress': started,
                         'abortAt': None, 'failure': None, 'publishing': False}
            self._operation = operation
            self.installation.update(id=operation['id'], state='installing', tool=names[0], phase='metadata',
                                     startedAt=time.time(), progress=0, elapsedSeconds=0, idleSeconds=0, bytesReceived=0, totalBytes=0,
                                     bytesPerSecond=0, etaSeconds=None, canCancel=True, cleanupPending=False,
                                     message='Starting dependency installation…', error=None)
            self.setup = {'state': 'installing' if batch else 'idle', 'remaining': list(names) if batch else [],
                          'message': 'Installing missing tools into bin/…' if batch else ''}
            for name in names:
                self.tools[name].update(state='queued', progress=0, error=None, message='Waiting to install locally…',
                                        phase='queued', startedAt=self.installation['startedAt'], elapsedSeconds=0,
                                        bytesReceived=0, totalBytes=0, bytesPerSecond=0, etaSeconds=None)
        worker = None
        try:
            worker = threading.Thread(target=self._run_installation, args=(list(names),), daemon=True, name='install-dependencies')
            with self.lock:
                self._install_thread = worker
            worker.start()
            threading.Thread(target=self._watch_installation, args=(operation,), daemon=True, name='watch-dependencies').start()
        except RuntimeError:
            if worker is None or not worker.is_alive():
                with self.lock:
                    for name in names:
                        self.tools[name].update(state='failed', error='Could not start installation. Try again.', message='Installation did not start.')
                    self.installation.update(state='failed', canCancel=False, phase='', error='Could not start installation.', message='Installation did not start.')
                    if batch:
                        self.setup.update(state='failed', remaining=[], message='Could not start setup. Try again.')
                    operation['done'].set()
            # A watcher failure does not abort a working installer: every I/O
            # loop also enforces cancellation and deadlines itself.

    def cancel(self):
        """Nonblocking cancellation. HTTP/socket closure belongs to the worker."""
        with self.lock:
            operation = self._operation
            if operation and not operation['done'].is_set():
                operation['cancel'].set()
                operation['abortAt'] = operation['abortAt'] or time.monotonic()
                self.installation.update(state='cancelling', canCancel=False, message='Cancelling installation and removing temporary files…')
                for name in operation['names']:
                    if self.tools[name]['state'] in INSTALL_ACTIVE_STATES:
                        self.tools[name].update(state='cancelling', message='Cancelling installation…')
                if operation['batch']:
                    self.setup.update(state='cancelling', message='Cancelling setup…')
        return self.status()

    def close(self, timeout=2):
        """Bounded server shutdown; an interrupted worker can never publish later."""
        with self.lock:
            self._closed = True
            worker = self._install_thread
        self.cancel()
        if worker is not None and worker is not threading.current_thread() and worker.is_alive():
            worker.join(max(0, min(float(timeout), 5)))

    def _checkpoint(self, tool=None, progress=False):
        with self.lock:
            operation = self._operation
            if not operation:
                return
            now = time.monotonic()
            if not operation['cancel'].is_set():
                if now - operation['started'] >= INSTALL_TIMEOUT_SECONDS:
                    operation['failure'] = 'Installation exceeded the 20-minute limit. Retry on a faster connection or install manually.'
                elif now - operation['lastProgress'] >= INSTALL_IDLE_SECONDS:
                    operation['failure'] = 'Installation made no progress for 45 seconds. Check the connection and retry.'
                if operation['failure']:
                    operation['cancel'].set()
                    operation['abortAt'] = now
            if operation['cancel'].is_set():
                if operation['failure']:
                    raise InstallationTimeout(operation['failure'])
                raise InstallationCancelled('Installation cancelled.')
            if progress:
                operation['lastProgress'] = now

    def _phase(self, tool, phase, message, **values):
        self._checkpoint(tool, progress=True)
        with self.lock:
            self.tools[tool].update(phase=phase, message=message, **values)
            self.installation.update(tool=tool, phase=phase, message=message, **values)

    def _watch_installation(self, operation):
        while not operation['done'].wait(0.5):
            try:
                self._checkpoint()
            except (InstallationCancelled, InstallationTimeout):
                pass
            with self.lock:
                if self._operation is not operation:
                    return
                if operation['cancel'].is_set():
                    self.installation.update(state='cancelling', canCancel=False, message='Stopping installation and cleaning up…')
                    # OS DNS/file I/O can occasionally ignore socket deadlines.
                    # Before publication it is safe to release encoding while
                    # the cancelled worker finishes staging cleanup. No new
                    # installer may start until that worker actually exits.
                    if (operation['abortAt'] is not None and not operation['publishing']
                            and time.monotonic() - operation['abortAt'] >= INSTALL_CANCEL_GRACE_SECONDS):
                        state = 'failed' if operation['failure'] else 'cancelled'
                        message = operation['failure'] or 'Installation cancelled. Temporary-file cleanup is still finishing.'
                        for name in operation['names']:
                            if self.tools[name]['state'] in INSTALL_ACTIVE_STATES:
                                self.tools[name].update(state=state, message=message, error=operation['failure'])
                        self.installation.update(state=state, cleanupPending=True, phase='cleanup', message=message, error=operation['failure'])
                        if operation['batch']:
                            self.setup.update(state=state, remaining=[], message=message)
                        return

    def _run_installation(self, names):
        operation = self._operation
        try:
            for name in names:
                self._checkpoint(name)
                self._update(name, state='installing')
                self._phase(name, 'metadata', 'Checking the provider release…', progress=0, bytesReceived=0, totalBytes=0, bytesPerSecond=0, etaSeconds=None)
                self._install_worker(name)
                with self.lock:
                    self.setup['remaining'] = [tool for tool in self.setup['remaining'] if tool != name]
        except (InstallationCancelled, InstallationTimeout):
            pass
        except BaseException:
            # Even an unexpected worker exit must release the operation gate.
            with self.lock:
                operation['failure'] = 'Installation stopped unexpectedly. Retry setup or install the tools manually.'
                operation['cancel'].set()
        finally:
            with self.lock:
                cancelled = operation['cancel'].is_set()
                for name in names:
                    self.tools[name]['elapsedSeconds'] = round(max(0, time.monotonic() - operation['started']), 1)
                    if self.tools[name]['state'] in INSTALL_ACTIVE_STATES:
                        state = 'failed' if operation['failure'] or not cancelled else 'cancelled'
                        self.tools[name].update(state=state, error=operation['failure'], message=operation['failure'] or 'Installation cancelled. Temporary files removed.')
                succeeded = sum(self.tools[name]['state'] == 'completed' for name in names)
                state = 'completed' if succeeded == len(names) else (
                    ('failed' if operation['failure'] else 'cancelled') if cancelled else 'partial' if succeeded else 'failed')
                message = ('Installed locally. Existing PATH and configured tools keep priority.' if state == 'completed'
                           else ('Setup cancelled. Completed tool installations were kept.' if succeeded else 'Installation cancelled. Temporary files removed.') if state == 'cancelled'
                           else operation['failure'] or 'Installation did not complete. Review the tool error and retry.')
                errors = [self.tools[name].get('error') for name in names if self.tools[name].get('error')]
                self.installation.update(state=state, phase='', canCancel=False, cleanupPending=False,
                                         elapsedSeconds=round(max(0, time.monotonic() - operation['started']), 1),
                                         bytesPerSecond=0, etaSeconds=None, message=message,
                                         error=operation['failure'] or (errors[0] if errors else None))
                if state == 'completed':
                    self.installation['progress'] = 100
                if operation['batch']:
                    self.setup.update(state=state, remaining=[], message=message)
                operation['done'].set()

    def _metadata(self, tool):
        if tool == 'ffmpeg':
            digest = checksum(_read_small(FFMPEG_URL + '.sha256', 4096, check=lambda: self._checkpoint(tool)).decode('ascii'))
            return FFMPEG_URL, digest, ['ffmpeg.exe', 'ffprobe.exe']
        release = json.loads(_read_small(GITHUB_RELEASE, check=lambda: self._checkpoint(tool)))
        assets = [a for a in release.get('assets', [])
                  if re.fullmatch(r'gifski-[0-9][A-Za-z0-9.\-]*\.(?:tar\.xz|zip)', a.get('name', ''))]
        if len(assets) != 1:
            raise ValueError('The official release layout changed. Download Gifski manually from its release page.')
        asset = assets[0]
        url = asset.get('browser_download_url', '')
        if not url.startswith('https://github.com/ImageOptim/gifski/releases/download/'):
            raise ValueError('Unexpected Gifski release location.')
        if int(asset.get('size', 0)) > MAX_ARCHIVE:
            raise ValueError('The official archive exceeds the download limit.')
        digest = checksum(asset.get('digest') or '')
        return validate_url(url), digest, ['gifski.exe']

    def _download(self, tool, url, destination, digest):
        h = hashlib.sha256()
        received = 0
        started = time.monotonic()
        self._phase(tool, 'download', 'Connecting to the download provider…', bytesReceived=0, totalBytes=0, bytesPerSecond=0, etaSeconds=None)
        with _open(url, timeout=INSTALL_SOCKET_TIMEOUT_SECONDS) as response, destination.open('xb') as target:
            total = int(response.headers.get('Content-Length') or 0)
            if total < 0 or total > MAX_ARCHIVE:
                raise ValueError('The dependency download exceeds the size limit.')
            self._phase(tool, 'download', 'Downloading portable tools…', totalBytes=total)
            read = getattr(response, 'read1', response.read)
            while True:
                self._checkpoint(tool)
                data = read(256 * 1024)
                self._checkpoint(tool)
                if not data:
                    break
                received += len(data)
                if received > MAX_ARCHIVE:
                    raise ValueError('The dependency download exceeds the size limit.')
                h.update(data)
                target.write(data)
                progress = 5 + int(65 * received / total) if total else 10
                rate = received / max(0.01, time.monotonic() - started)
                self._phase(tool, 'download', f'Downloading: {received / 1048576:.1f} MiB',
                            progress=min(progress, 70), bytesReceived=received, totalBytes=total,
                            bytesPerSecond=round(rate), etaSeconds=round(max(0, total - received) / rate, 1) if total and rate else None)
        if not received or (total and received != total):
            raise ValueError('The dependency download was incomplete.')
        if h.hexdigest() != digest:
            raise ValueError('SHA-256 verification failed. Nothing was installed. Retry after checking the provider release.')

    def _publish(self, tool, binaries, staging):
        """Replace individual files atomically and roll back a failed multi-file update."""
        destination = self.root / 'bin'
        if destination.is_symlink():
            raise ValueError('The app bin folder must not be a symbolic link.')
        for name in binaries:
            target = destination / name
            if target.is_symlink() or (target.exists() and not target.is_file()):
                raise ValueError('An existing dependency is not a regular file.')
        backup = staging / 'backup'
        backup.mkdir()
        installed = []
        try:
            for name, path in binaries.items():
                self._checkpoint(tool, progress=True)
                target = destination / name
                if target.exists():
                    shutil.copy2(target, backup / name)
                os.replace(path, target)
                installed.append(name)
                self._checkpoint(tool, progress=True)
            licenses = staging / 'licenses'
            if licenses.is_dir():
                license_target = destination / 'licenses' / tool
                if (license_target.parent.is_symlink() or license_target.is_symlink()
                        or any((license_target / path.name).is_symlink() for path in licenses.iterdir())):
                    raise ValueError('The dependency license destination must not contain symbolic links.')
                license_target.mkdir(parents=True, exist_ok=True)
                for path in licenses.iterdir():
                    self._checkpoint(tool, progress=True)
                    shutil.copy2(path, license_target / path.name)
        except Exception:
            for name in reversed(installed):
                target = destination / name
                if (backup / name).exists():
                    os.replace(backup / name, target)
                else:
                    target.unlink(missing_ok=True)
            raise

    def _install_worker(self, tool):
        published = False
        versions = []
        try:
            self._checkpoint(tool)
            destination = self.root / 'bin'
            if destination.is_symlink():
                raise ValueError('The app bin folder must not be a symbolic link.')
            destination.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.install-', dir=destination) as temp:
                staging = Path(temp)
                url, digest, wanted = self._metadata(tool)
                self._checkpoint(tool, progress=True)
                archive = staging / 'download.archive'
                self._download(tool, url, archive, digest)
                self._phase(tool, 'extract', 'Checksum verified. Unpacking portable binaries…', progress=75, bytesPerSecond=0, etaSeconds=None)
                binaries = extract_executables(archive, staging, wanted, check=lambda: self._checkpoint(tool, progress=True))
                self._phase(tool, 'verify', 'Checking the downloaded executables…', progress=85)
                for name, path in binaries.items():
                    self._checkpoint(tool, progress=True)
                    flag = '--version' if name == 'gifski.exe' else '-version'
                    result = subprocess.run([str(path), flag], capture_output=True, text=True, errors='replace', timeout=15)
                    self._checkpoint(tool, progress=True)
                    if result.returncode:
                        raise ValueError(f'{name} could not run on this machine. Nothing was installed.')
                    output = (result.stdout + result.stderr).strip()
                    expected = 'gifski' if name == 'gifski.exe' else name[:-4] + ' version'
                    if not output.lower().startswith(expected + ' '):
                        raise ValueError(f'{name} did not report the expected version identity. Nothing was installed.')
                    versions.append(output.splitlines()[0] if output else name)
                self._phase(tool, 'publish', 'Installing in the app folder…', progress=95)
                with self.lock:
                    self._checkpoint(tool)
                    if self._operation:
                        self._operation['publishing'] = True
                self._publish(tool, binaries, staging)
                published = True
                # Keep the operation gate closed through staging cleanup and
                # detection invalidation, so the next job cannot use stale paths.
            if self.on_change:
                self.on_change()
            self._update(tool, state='completed', progress=100, version='; '.join(versions), phase='', message='Installed locally. No system settings were changed.')
        except Exception as error:
            if published:
                self._update(tool, state='completed', progress=100, version='; '.join(versions), phase='',
                             error='The files were installed, but final cleanup or tool detection refresh failed. Scan tools before exporting.',
                             message='Portable tools were installed. A final refresh needs attention.')
                return
            # Avoid returning signed redirect URLs from urllib errors.
            with self.lock:
                operation = self._operation
                if operation and operation['cancel'].is_set() and not operation['failure'] and not operation['publishing']:
                    error = InstallationCancelled('Installation cancelled.')
            cancelled = isinstance(error, InstallationCancelled)
            if isinstance(error, InstallationTimeout):
                message = str(error)
            elif cancelled:
                message = 'Installation cancelled. Temporary files removed.'
            elif isinstance(error, (urllib.error.URLError, ssl.SSLError, TimeoutError, socket.gaierror)):
                message = provider_error(error)['message']
            else:
                message = str(error)
            self._update(tool, state='cancelled' if cancelled else 'failed', error=None if cancelled else message,
                         phase='', bytesPerSecond=0, etaSeconds=None,
                         message=message if cancelled else 'Installation failed. Review the error and retry.')
        finally:
            with self.lock:
                if self._operation:
                    self._operation['publishing'] = False
