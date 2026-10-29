"""Explicit, app-local dependency installation from fixed trusted providers.

The web server must guard installation requests with its local-session and
same-origin checks. No installer is run and no system PATH or registry is edited.
"""
from __future__ import annotations

import copy
import hashlib
import json
import os
from pathlib import Path, PurePosixPath
import platform
import re
import shutil
import stat
import struct
import subprocess
import tarfile
import tempfile
import threading
import time
import urllib.parse
import urllib.request
import zipfile

GITHUB_RELEASE = 'https://api.github.com/repos/ImageOptim/gifski/releases/latest'
FFMPEG_URL = 'https://www.gyan.dev/ffmpeg/builds/ffmpeg-release-essentials.zip'
FFMPEG_RELEASES = 'https://ffmpeg.org/releases/'
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
        'User-Agent': 'Gifski-WebUI/1.1', 'Accept': 'application/vnd.github+json' if 'api.github.com' in url else '*/*',
    })
    response = urllib.request.build_opener(TrustedRedirect()).open(request, timeout=timeout)
    validate_url(response.geturl())
    return response


def _read_small(url, limit=2 * 1024 * 1024):
    deadline = time.monotonic() + 45
    data = bytearray()
    with _open(url, timeout=15) as response:
        read = getattr(response, 'read1', response.read)
        while len(data) <= limit:
            if time.monotonic() > deadline:
                raise ValueError('The provider metadata request timed out.')
            block = read(min(65536, limit + 1 - len(data)))
            if not block:
                return bytes(data)
            data.extend(block)
    raise ValueError('Provider metadata exceeded the size limit.')


def checksum(text):
    match = re.fullmatch(r'\s*(?:sha256:)?([a-fA-F0-9]{64})(?:\s+[^\r\n]*)?\s*', text)
    if not match:
        raise ValueError('The provider did not supply a valid SHA-256 checksum. Install manually instead.')
    return match[1].lower()


def release_version(value):
    """Comparable stable release tuple, deliberately excluding prerelease tags."""
    match = re.fullmatch(r'v?(\d{1,4})\.(\d{1,4})(?:\.(\d{1,4}))?', value)
    return tuple(int(part or 0) for part in match.groups()) if match else None


def installed_version(tool, value):
    """Return the release base and vendor marker; snapshots cannot be ordered."""
    prefix = r'gifski\s+' if tool == 'gifski' else re.escape(tool) + r'\s+version\s+'
    match = re.match(prefix + r'(\S+)', value, re.IGNORECASE)
    token = match[1] if match else ''
    # Git snapshots and prereleases may contain a release number but do not
    # identify a released build. Never call these current or outdated.
    if re.search(r'(?:git|snapshot|nightly|alpha|beta|rc\d|-[0-9]+-g[0-9a-f])', token, re.I):
        return None, True
    base = re.match(r'v?(\d{1,4}\.\d{1,4}(?:\.\d{1,4})?)(?=$|[-+])', token)
    if not base or re.match(r'[-+]dev(?:\d|[.-]|$)', token[base.end():], re.I):
        return None, True
    return release_version(base[1]), bool(token[base.end():])


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
        self.checked_at = None
        self.checked_monotonic = None
        self.check_state = 'idle'
        self.check_cached = False
        self.installed_tools = {}
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
        for name, item in tools.items():
            wanted = [name] if name == 'gifski' else ['ffmpeg', 'ffprobe']
            item['installed'] = all((self.root / 'bin' / f'{tool}.exe').is_file() for tool in wanted)
        return {'platform': f'{platform.system()} {platform.machine()}', 'supported': self.supported,
                'busy': any(t['state'] == 'installing' for t in tools.values()),
                'tools': tools, 'updates': updates, 'destination': 'bin/',
                'manual': {'gifski': 'Install Gifski from its official releases, or run brew install gifski on macOS. Then select its path in Engine settings.',
                           'ffmpeg': 'Install FFmpeg and FFprobe with your OS package manager, or select existing executable paths in Engine settings.'}}

    def _update(self, tool, **values):
        with self.lock:
            self.tools[tool].update(values)

    def _update_status(self):
        """Caller holds the lock. Reading status never accesses the network."""
        tools = {}
        for name in ('gifski', 'ffmpeg', 'ffprobe'):
            upstream = 'gifski' if name == 'gifski' else 'ffmpeg'
            local = self.installed_tools.get(name, {})
            version = str(local.get('version') or '')[:240]
            latest = self.release_cache.get(upstream, '')
            item = {'state': 'not_checked', 'installedVersion': version,
                    'latestVersion': latest,
                    'sourceUrl': ('https://github.com/ImageOptim/gifski/releases' if upstream == 'gifski'
                                  else 'https://ffmpeg.org/download.html'),
                    'message': 'Check updates to compare with the latest upstream release.'}
            if self.check_state == 'checking':
                item.update(state='checking', message='Checking the official release source…')
            elif upstream in self.release_errors:
                item.update(state='error', message=self.release_errors[upstream])
            elif latest:
                local_version, vendor = installed_version(name, version)
                remote_version = release_version(latest)
                if not local.get('available'):
                    item.update(state='not_installed', message='No working executable is selected.')
                elif local_version is None or remote_version is None:
                    item.update(state='unknown', message='This custom, development, or unrecognized build cannot be compared reliably with stable releases.')
                elif local_version < remote_version:
                    item.update(state='update_available', message='A newer upstream release is available. OS and vendor packages may carry their own fixes.')
                elif local_version > remote_version:
                    item.update(state='newer', message='This executable reports a version newer than the latest upstream stable release.')
                elif vendor:
                    item.update(state='unknown', message='Based on the latest upstream release; vendor-specific revisions cannot be verified here.')
                else:
                    item.update(state='current', message='Matches the latest upstream stable release.')
            tools[name] = item
        return {'state': self.check_state, 'checkedAt': self.checked_at,
                'cached': self.check_cached, 'tools': tools}

    def check_updates(self, installed_tools=None):
        """Explicit user action; metadata only, cached to avoid provider rate limits."""
        with self.lock:
            if installed_tools is not None:
                self.installed_tools = copy.deepcopy(installed_tools)
            if self.check_state == 'checking':
                return self.status()
            if (self.checked_monotonic is not None
                    and time.monotonic() - self.checked_monotonic < UPDATE_CACHE_SECONDS
                    and self.check_state == 'completed'):
                self.check_cached = True
                return self.status()
            self.check_state = 'checking'
            self.check_cached = False
        try:
            threading.Thread(target=self._check_worker, daemon=True, name='check-updates').start()
        except RuntimeError:
            with self.lock:
                self.check_state = 'failed'
                self.release_errors = {tool: 'Could not start the update check. Try again.' for tool in ('gifski', 'ffmpeg')}
        return self.status()

    def _latest_release(self, tool):
        if tool == 'gifski':
            release = json.loads(_read_small(GITHUB_RELEASE))
            version = str(release.get('tag_name', '')).removeprefix('v')
            if release.get('draft') or release.get('prerelease') or release_version(version) is None:
                raise ValueError('The provider did not return a stable Gifski release.')
            return version
        page = _read_small(FFMPEG_RELEASES).decode('utf-8', errors='replace')
        versions = set(re.findall(r'href=[\"\']ffmpeg-(\d{1,4}\.\d{1,4}(?:\.\d{1,4})?)\.tar\.xz[\"\']', page))
        if not versions:
            raise ValueError('The FFmpeg release listing could not be understood.')
        return max(versions, key=release_version)

    def _check_worker(self):
        # Isolate provider failures so one outage does not hide the other result.
        releases, errors = {}, {}
        for name in ('gifski', 'ffmpeg'):
            try:
                releases[name] = self._latest_release(name)
            except Exception:
                errors[name] = 'Could not check the official release source. Try again later or open its release page.'
        with self.lock:
            self.release_cache = releases
            self.release_errors = errors
            self.checked_at = time.time()
            self.checked_monotonic = time.monotonic()
            self.check_state = 'completed' if not errors else ('partial' if releases else 'failed')

    def install(self, tool):
        if tool not in self.tools:
            raise ValueError('Choose gifski or ffmpeg.')
        if not self.supported:
            raise ValueError('Automatic portable installation is available for Windows x64. Use the manual instructions on this platform.')
        with self.lock:
            if any(t['state'] == 'installing' for t in self.tools.values()):
                raise ValueError('A dependency installation is already running.')
            self.tools[tool].update(state='installing', progress=0, error=None, message='Checking the provider release…')
        try:
            threading.Thread(target=self._install_worker, args=(tool,), daemon=True, name=f'install-{tool}').start()
        except RuntimeError:
            self._update(tool, state='failed', error='Could not start installation. Try again.', message='Installation did not start.')
        return self.status()

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
            if isinstance(error, urllib.error.URLError):
                message = 'Could not download from the dependency provider. Check the connection or install manually.'
            else:
                message = str(error)
            self._update(tool, state='failed', error=message, message='Installation failed. Existing executable paths are unchanged.')
