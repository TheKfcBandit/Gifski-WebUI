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


def _read_small(url, limit=2 * 1024 * 1024):
    deadline = time.monotonic() + 16
    data = bytearray()
    with _open(url, timeout=8) as response:
        read = getattr(response, 'read1', response.read)
        while len(data) <= limit:
            if time.monotonic() > deadline:
                raise TimeoutError('The provider metadata request timed out.')
            block = read(min(65536, limit + 1 - len(data)))
            if not block:
                return bytes(data)
            data.extend(block)
    raise ValueError('Provider metadata exceeded the size limit.')


class UpdateCheckError(ValueError):
    """Only sanitized provider diagnostics are carried into the UI."""
    def __init__(self, code, message):
        super().__init__(message)
        self.code = code


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


def extract_executables(archive, staging, wanted):
    """Copy only named regular binaries, retaining bounded license text separately."""
    staging = Path(staging)
    staged = {}
    licenses = []
    expanded = 0
    license_bytes = 0

    def copy_entry(name, size, regular, opener):
        nonlocal expanded, license_bytes
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
        for name, item in tools.items():
            wanted = [name] if name == 'gifski' else ['ffmpeg', 'ffprobe']
            item['installed'] = all((self.root / 'bin' / f'{tool}.exe').is_file() for tool in wanted)
            item['available'] = all(installed.get(tool, {}).get('available', False) for tool in wanted)
            item['portableUpdateAffectsActive'] = all(
                not installed.get(tool, {}).get('available') or self._is_portable_active(installed[tool]) for tool in wanted)
        return {'platform': f'{platform.system()} {platform.machine()}', 'supported': self.supported,
                'busy': setup['state'] == 'installing' or any(t['state'] in ('queued', 'installing') for t in tools.values()),
                'tools': tools, 'updates': updates, 'setup': setup, 'destination': 'bin/',
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

    def install(self, tool):
        if tool not in self.tools:
            raise ValueError('Choose gifski or ffmpeg.')
        if not self.supported:
            raise ValueError('Automatic portable installation is available for Windows x64. Use the manual instructions on this platform.')
        with self.lock:
            if self.setup['state'] == 'installing' or any(t['state'] in ('queued', 'installing') for t in self.tools.values()):
                raise ValueError('A dependency installation is already running.')
            self.tools[tool].update(state='installing', progress=0, error=None, message='Checking the provider release…')
        try:
            threading.Thread(target=self._install_worker, args=(tool,), daemon=True, name=f'install-{tool}').start()
        except RuntimeError:
            self._update(tool, state='failed', error='Could not start installation. Try again.', message='Installation did not start.')
        return self.status()

    def install_missing(self, installed_tools):
        """Explicit setup action: install only absent tools, into this app's bin/."""
        if not self.supported:
            raise ValueError('Automatic portable installation is available for Windows x64. Use the manual instructions on this platform.')
        with self.lock:
            if self.setup['state'] == 'installing' or any(t['state'] in ('queued', 'installing') for t in self.tools.values()):
                raise ValueError('A dependency installation is already running.')
            self.installed_tools = copy.deepcopy(installed_tools)
            queue = []
            if not installed_tools.get('gifski', {}).get('available'):
                queue.append('gifski')
            if not all(installed_tools.get(name, {}).get('available') for name in ('ffmpeg', 'ffprobe')):
                queue.append('ffmpeg')
            self.setup = {'state': 'installing' if queue else 'completed', 'remaining': list(queue),
                          'message': 'Installing missing tools into bin/…' if queue else 'All required tools are already available.'}
            for name in queue:
                self.tools[name].update(state='queued', progress=0, error=None, message='Waiting to install locally…')
        if queue:
            try:
                threading.Thread(target=self._install_missing_worker, args=(queue,), daemon=True, name='setup-dependencies').start()
            except RuntimeError:
                with self.lock:
                    for name in queue:
                        self.tools[name].update(state='failed', error='Could not start installation. Try again.', message='Installation did not start.')
                    self.setup.update(state='failed', remaining=[], message='Could not start setup. Try again.')
        return self.status()

    def _install_missing_worker(self, queue):
        for name in queue:
            self._update(name, state='installing', message='Checking the provider release…')
            self._install_worker(name)
            with self.lock:
                self.setup['remaining'] = [tool for tool in self.setup['remaining'] if tool != name]
        with self.lock:
            succeeded = sum(self.tools[name]['state'] == 'completed' for name in queue)
            state = 'completed' if succeeded == len(queue) else ('partial' if succeeded else 'failed')
            message = ('Missing tools were installed locally. Existing PATH and configured tools keep priority.' if state == 'completed'
                       else 'Some tools could not be installed. Review the tool messages and retry setup.')
            self.setup.update(state=state, remaining=[], message=message)

    def _metadata(self, tool):
        if tool == 'ffmpeg':
            digest = checksum(_read_small(FFMPEG_URL + '.sha256', 4096).decode('ascii'))
            return FFMPEG_URL, digest, ['ffmpeg.exe', 'ffprobe.exe']
        release = json.loads(_read_small(GITHUB_RELEASE))
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
        deadline = time.monotonic() + 1800
        with _open(url) as response, destination.open('xb') as target:
            total = int(response.headers.get('Content-Length') or 0)
            if total < 0 or total > MAX_ARCHIVE:
                raise ValueError('The dependency download exceeds the size limit.')
            read = getattr(response, 'read1', response.read)
            while True:
                if time.monotonic() > deadline:
                    raise ValueError('The dependency download timed out.')
                data = read(1024 * 1024)
                if not data:
                    break
                received += len(data)
                if received > MAX_ARCHIVE:
                    raise ValueError('The dependency download exceeds the size limit.')
                h.update(data)
                target.write(data)
                progress = 5 + int(65 * received / total) if total else 10
                self._update(tool, progress=min(progress, 70), message=f'Downloading: {received / 1048576:.1f} MiB')
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
                target = destination / name
                if target.exists():
                    shutil.copy2(target, backup / name)
                os.replace(path, target)
                installed.append(name)
            licenses = staging / 'licenses'
            if licenses.is_dir():
                license_target = destination / 'licenses' / tool
                if (license_target.parent.is_symlink() or license_target.is_symlink()
                        or any((license_target / path.name).is_symlink() for path in licenses.iterdir())):
                    raise ValueError('The dependency license destination must not contain symbolic links.')
                license_target.mkdir(parents=True, exist_ok=True)
                for path in licenses.iterdir():
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
        try:
            destination = self.root / 'bin'
            if destination.is_symlink():
                raise ValueError('The app bin folder must not be a symbolic link.')
            destination.mkdir(parents=True, exist_ok=True)
            with tempfile.TemporaryDirectory(prefix='.install-', dir=destination) as temp:
                staging = Path(temp)
                url, digest, wanted = self._metadata(tool)
                archive = staging / 'download.archive'
                self._download(tool, url, archive, digest)
                self._update(tool, progress=75, message='Checksum verified. Unpacking portable binaries…')
                binaries = extract_executables(archive, staging, wanted)
                versions = []
                for name, path in binaries.items():
                    flag = '--version' if name == 'gifski.exe' else '-version'
                    result = subprocess.run([str(path), flag], capture_output=True, text=True, errors='replace', timeout=15)
                    if result.returncode:
                        raise ValueError(f'{name} could not run on this machine. Nothing was installed.')
                    output = (result.stdout + result.stderr).strip()
                    expected = 'gifski' if name == 'gifski.exe' else name[:-4] + ' version'
                    if not output.lower().startswith(expected + ' '):
                        raise ValueError(f'{name} did not report the expected version identity. Nothing was installed.')
                    versions.append(output.splitlines()[0] if output else name)
                self._update(tool, progress=95, message='Installing in the app folder…')
                self._publish(tool, binaries, staging)
                self._update(tool, state='completed', progress=100, version='; '.join(versions), message='Installed locally. No system settings were changed.')
            if self.on_change:
                self.on_change()
        except Exception as error:
            # Avoid returning signed redirect URLs from urllib errors.
            if isinstance(error, (urllib.error.URLError, ssl.SSLError, TimeoutError, socket.gaierror)):
                message = provider_error(error)['message']
            else:
                message = str(error)
            self._update(tool, state='failed', error=message, message='Installation failed. Existing executable paths are unchanged.')
