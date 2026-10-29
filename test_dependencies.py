"""Dependency installer safety and lifecycle checks. No external downloads."""
import hashlib
import io
import json
from pathlib import Path
import socket
import ssl
import struct
import tarfile
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
import zipfile

import dependencies as dep


def executable(machine=b'\x64\x86'):
    payload = bytearray(128)
    payload[:2] = b'MZ'
    struct.pack_into('<I', payload, 60, 64)
    payload[64:70] = b'PE\x00\x00' + machine
    return bytes(payload)


class DependencyTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)

    def tearDown(self):
        self.temp.cleanup()

    def archive(self, entries):
        target = self.root / 'fixture.zip'
        with zipfile.ZipFile(target, 'w') as z:
            for name, data in entries:
                z.writestr(name, data)
        return target

    def test_url_scope_rejects_downgrades_credentials_and_untrusted_hosts(self):
        for url in ('http://github.com/x', 'https://github.com.evil.test/x',
                    'https://localhost/x', 'https://user:password@github.com/x',
                    'https://github.com:8080/x', 'file:///tmp/a'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                dep.validate_url(url)
        self.assertEqual(dep.validate_url(dep.FFMPEG_URL), dep.FFMPEG_URL)

    def test_checksum_requires_valid_sha256(self):
        digest = 'a' * 64
        self.assertEqual(dep.checksum('sha256:' + digest), digest)
        self.assertEqual(dep.checksum(digest + '  file.zip\n'), digest)
        for value in ('', 'sha256:garbage', 'md5:' + digest, digest + 'extra'):
            with self.assertRaises(ValueError):
                dep.checksum(value)

    def test_extracts_only_required_binaries_and_license(self):
        archive = self.archive([('release/bin/ffmpeg.exe', executable()),
                                ('release/bin/ffprobe.exe', executable()),
                                ('release/LICENSE', b'license'), ('release/bin/unrelated.exe', executable())])
        stage = self.root / 'stage'; stage.mkdir()
        results = dep.extract_executables(archive, stage, ['ffmpeg.exe', 'ffprobe.exe'])
        self.assertEqual(set(results), {'ffmpeg.exe', 'ffprobe.exe'})
        self.assertFalse((stage / 'unrelated.exe').exists())
        self.assertTrue((stage / 'licenses/1-LICENSE').is_file())

    def test_tar_extracts_regular_gifski_binary(self):
        archive = self.root / 'fixture.tar.xz'
        with tarfile.open(archive, 'w:xz') as tar:
            entry = tarfile.TarInfo('win/gifski.exe'); entry.size = len(executable())
            tar.addfile(entry, io.BytesIO(executable()))
        stage = self.root / 'stage'; stage.mkdir()
        self.assertIn('gifski.exe', dep.extract_executables(archive, stage, ['gifski.exe']))

    def test_traversal_rejected_even_in_unselected_archive_entries(self):
        for name in ('../outside', '/absolute', 'C:/outside', '..\\outside'):
            with self.subTest(name=name):
                archive = self.archive([(name, b'x'), ('gifski.exe', executable())])
                with self.assertRaises(ValueError):
                    dep.extract_executables(archive, self.root, ['gifski.exe'])
        self.assertFalse((self.root.parent / 'outside').exists())

    def test_wrong_architecture_rejected(self):
        archive = self.archive([('gifski.exe', executable(b'\x4c\x01'))])
        with self.assertRaisesRegex(ValueError, 'x64'):
            dep.extract_executables(archive, self.root, ['gifski.exe'])

    def test_missing_duplicate_and_symlink_executables_rejected(self):
        archive = self.archive([('other.exe', executable())])
        with self.assertRaises(ValueError):
            dep.extract_executables(archive, self.root, ['gifski.exe'])
        archive = self.archive([('a/gifski.exe', executable()), ('b/gifski.exe', executable())])
        with self.assertRaises(ValueError):
            dep.extract_executables(archive, self.root, ['gifski.exe'])
        (self.root / 'gifski.exe').unlink(missing_ok=True)
        with zipfile.ZipFile(archive, 'w') as z:
            link = zipfile.ZipInfo('gifski.exe'); link.external_attr = 0o120777 << 16
            z.writestr(link, b'other.exe')
        with self.assertRaises(ValueError):
            dep.extract_executables(archive, self.root, ['gifski.exe'])

    def test_unsupported_platform_and_invalid_tool_do_not_start_download(self):
        manager = dep.DependencyManager(self.root)
        manager.supported = False
        with self.assertRaises(ValueError): manager.install('gifski')
        with self.assertRaises(ValueError): manager.install('arbitrary')
        self.assertFalse((self.root / 'bin').exists())

    def test_single_install_lock(self):
        manager = dep.DependencyManager(self.root); manager.supported = True
        manager.tools['gifski']['state'] = 'installing'
        with self.assertRaises(ValueError): manager.install('ffmpeg')

    def test_download_hash_failure_never_publishes(self):
        manager = dep.DependencyManager(self.root)
        payload = b'fixture download'
        class Response(io.BytesIO):
            headers = {'Content-Length': str(len(payload))}
        with patch.object(dep, '_open', return_value=Response(payload)):
            with self.assertRaisesRegex(ValueError, 'SHA-256'):
                manager._download('gifski', dep.GITHUB_RELEASE, self.root / 'download', '0' * 64)
        self.assertFalse((self.root / 'bin').exists())

    def test_successful_lifecycle_calls_status_refresh(self):
        archive = self.archive([('win/gifski.exe', executable()), ('LICENSE', b'license')])
        from types import SimpleNamespace
        from unittest.mock import Mock
        callback = Mock()
        manager = dep.DependencyManager(self.root, on_change=callback)
        def download(tool, url, destination, digest):
            destination.write_bytes(archive.read_bytes())
        with patch.object(manager, '_metadata', return_value=(dep.GITHUB_RELEASE, 'a' * 64, ['gifski.exe'])), \
             patch.object(manager, '_download', side_effect=download), \
             patch.object(dep.subprocess, 'run', return_value=SimpleNamespace(returncode=0, stdout='gifski 1.34.0', stderr='')):
            manager._install_worker('gifski')
        callback.assert_called_once_with()
        state = manager.status()['tools']['gifski']
        self.assertEqual(state['state'], 'completed')
        self.assertTrue(state['installed'])
        self.assertEqual(state['progress'], 100)
        self.assertFalse(list((self.root / 'bin').glob('.install-*')))

    def test_publish_rolls_back_partial_update(self):
        manager = dep.DependencyManager(self.root)
        bin_dir = self.root / 'bin'; bin_dir.mkdir()
        (bin_dir / 'ffmpeg.exe').write_bytes(b'old ffmpeg')
        (bin_dir / 'ffprobe.exe').write_bytes(b'old ffprobe')
        stage = self.root / 'stage'; stage.mkdir()
        (stage / 'ffmpeg.exe').write_bytes(b'new ffmpeg')
        (stage / 'ffprobe.exe').write_bytes(b'new ffprobe')
        real_replace = dep.os.replace
        def replace(source, target):
            if Path(source) == stage / 'ffprobe.exe': raise PermissionError('File in use')
            return real_replace(source, target)
        with patch.object(dep.os, 'replace', side_effect=replace):
            with self.assertRaises(PermissionError):
                manager._publish('ffmpeg', {name: stage / name for name in ('ffmpeg.exe', 'ffprobe.exe')}, stage)
        self.assertEqual((bin_dir / 'ffmpeg.exe').read_bytes(), b'old ffmpeg')
        self.assertEqual((bin_dir / 'ffprobe.exe').read_bytes(), b'old ffprobe')

    def test_reading_status_never_checks_network(self):
        manager = dep.DependencyManager(self.root)
        with patch.object(dep, '_read_small') as read:
            self.assertEqual(manager.status()['updates']['state'], 'idle')
            read.assert_not_called()

    def test_release_parser_orders_stable_releases_numerically(self):
        manager = dep.DependencyManager(self.root)
        html = b'<a href="ffmpeg-8.0.9.tar.xz">old</a><a href="ffmpeg-9.0.2.tar.xz">new</a><a href="ffmpeg-10.0-rc1.tar.xz">pre</a>'
        with patch.object(dep, '_read_small', return_value=html):
            self.assertEqual(manager._latest_release('ffmpeg'), '9.0.2')
        for release in ({'tag_name': '1.34.0', 'prerelease': True}, {'tag_name': '1.35.0-beta'}):
            with patch.object(dep, '_read_small', return_value=json.dumps(release).encode()):
                with self.assertRaises(ValueError):
                    manager._latest_release('gifski')

    def test_version_states_compare_stable_vendor_bases_but_not_snapshots(self):
        manager = dep.DependencyManager(self.root)
        manager.release_cache = {'gifski': '1.34.0', 'ffmpeg': '8.0.1'}
        manager.check_state = 'completed'
        cases = [('ffmpeg version 8.0.1', 'current'),
                 ('ffmpeg version 7.1.1', 'update_available'),
                 ('ffmpeg version 7.1.1-essentials_build-www.gyan.dev', 'update_available'),
                 ('ffmpeg version 9.0.0', 'newer'),
                 ('ffmpeg version 8.0.1-essentials_build-www.gyan.dev', 'current'),
                 ('ffmpeg version n8.0.1-20251001', 'current'),
                 ('ffmpeg version n7.1.1-20251001', 'update_available'),
                 ('ffmpeg version 8.0.1-full_build-www.gyan.dev', 'current'),
                 ('ffmpeg version 8.0.1-0+deb12u1', 'current'),
                 ('ffmpeg version 8.0.1-12-gabc123', 'unknown'),
                 ('ffmpeg version n8.0.1-12-gabc123-20251001', 'unknown'),
                 ('ffmpeg version N-119000-gabc123', 'unknown'),
                 ('ffmpeg version 2026-10-25-git-46f23be8b1-full_build-www.gyan.dev', 'unknown'),
                 ('ffmpeg version n8.1-dev-20251001', 'unknown'),
                 ('ffmpeg version 8.0.1-rc1', 'unknown')]
        for value, expected in cases:
            with self.subTest(value=value):
                status = manager.status({'ffmpeg': {'available': True, 'version': value}})
                self.assertEqual(status['updates']['tools']['ffmpeg']['state'], expected)
        status = manager.status({'gifski': {'available': True, 'version': 'gifski 1.34.0'}})
        self.assertEqual(status['updates']['tools']['gifski']['state'], 'current')
        self.assertEqual(status['updates']['tools']['ffprobe']['state'], 'not_installed')

    def test_check_cache_and_inflight_coalescing(self):
        manager = dep.DependencyManager(self.root)
        with patch.object(manager, '_latest_release', side_effect=lambda name: {'gifski': '1.34.0', 'ffmpeg': '8.0.1'}[name]):
            manager._check_worker()
        with patch.object(dep.threading, 'Thread') as thread:
            self.assertTrue(manager.check_updates()['updates']['cached'])
            thread.assert_not_called()
            manager.check_state = 'checking'
            self.assertEqual(manager.check_updates()['updates']['state'], 'checking')
            thread.assert_not_called()

    def test_provider_failure_isolated_and_error_does_not_leak_url(self):
        manager = dep.DependencyManager(self.root)
        def latest(name):
            if name == 'gifski':
                raise ValueError('https://signed.example/secret')
            return '8.0.1'
        with patch.object(manager, '_latest_release', side_effect=latest):
            manager._check_worker()
        status = manager.status()['updates']
        self.assertEqual(status['state'], 'partial')
        self.assertEqual(status['tools']['gifski']['state'], 'error')
        self.assertEqual(status['tools']['ffmpeg']['latestVersion'], '8.0.1')
        self.assertNotIn('secret', json.dumps(status))

    def test_gifski_api_rate_limit_falls_back_to_exact_release_page_identity(self):
        manager = dep.DependencyManager(self.root)
        error = dep.urllib.error.HTTPError(dep.GITHUB_RELEASE, 403, 'blocked', {'X-RateLimit-Remaining': '0'}, None)
        html = b'<a href="/ImageOptim/gifski/releases/tag/99.0.0">Unrelated link</a><meta content="/ImageOptim/gifski/releases/tag/1.34.0" property="og:url">'
        with patch.object(dep, '_read_small', side_effect=[error, html]) as read:
            self.assertEqual(manager._latest_release('gifski'), '1.34.0')
        self.assertEqual([call.args[0] for call in read.call_args_list], [dep.GITHUB_RELEASE, dep.GIFSKI_RELEASE_PAGE])
        self.assertEqual(manager.release_sources['gifski']['sourceUsed'], 'Gifski release page')
        for url in ('https://evil.test/ImageOptim/gifski/releases/tag/1.34.0',
                    '/Another/gifski/releases/tag/1.34.0', '/ImageOptim/gifski/releases/tag/1.35.0-beta'):
            with self.subTest(url=url), self.assertRaises(ValueError):
                manager._gifski_page(f'<meta property="og:url" content="{url}">'.encode())

    def test_ffmpeg_outage_falls_back_to_stable_tags_on_official_mirror(self):
        manager = dep.DependencyManager(self.root)
        tags = [{'name': value} for value in ('n9.0.1', 'n9.1-dev', 'n8.1.3', 'n9.0.2', 'v0.6.1', 'n10.0-rc1')]
        error = dep.urllib.error.HTTPError(dep.FFMPEG_RELEASES, 503, 'unavailable', {}, None)
        with patch.object(dep, '_read_small', side_effect=[error, json.dumps(tags).encode()]):
            self.assertEqual(manager._latest_release('ffmpeg'), '9.0.2')
        self.assertEqual(manager.release_sources['ffmpeg']['sourceUrl'], 'https://github.com/FFmpeg/FFmpeg/tags')

    def test_provider_diagnostics_are_actionable_and_do_not_expose_raw_errors(self):
        url = 'https://signed.example/secret'
        cases = [
            (dep.urllib.error.URLError(socket.gaierror(-2, url)), 'dns'),
            (dep.urllib.error.URLError(ssl.SSLCertVerificationError(1, url)), 'certificate'),
            (dep.urllib.error.URLError(TimeoutError(url)), 'timeout'),
            (dep.urllib.error.HTTPError(url, 429, url, {}, None), 'rate_limited'),
            (dep.urllib.error.HTTPError(url, 503, url, {}, None), 'provider_unavailable'),
            (dep.urllib.error.HTTPError(url, 407, url, {}, None), 'proxy'),
            (ValueError(url), 'invalid_response'),
        ]
        for error, code in cases:
            with self.subTest(code=code):
                result = dep.provider_error(error)
                self.assertEqual(result['errorCode'], code)
                self.assertNotIn('secret', json.dumps(result))

    def test_checks_publish_fast_result_while_other_provider_is_pending(self):
        manager = dep.DependencyManager(self.root)
        waiting = threading.Event()
        release = threading.Event()
        def latest(name):
            if name == 'ffmpeg':
                waiting.set()
                if not release.wait(2):
                    raise TimeoutError('Test release was not signaled.')
            return {'gifski': '1.34.0', 'ffmpeg': '8.0.1'}[name]
        with patch.object(manager, '_latest_release', side_effect=latest):
            worker = threading.Thread(target=manager._check_worker)
            worker.start()
            try:
                self.assertTrue(waiting.wait(2))
                # The Gifski worker may be scheduled just after FFmpeg. Join
                # only that worker so the assertion never depends on a sleep.
                for child in threading.enumerate():
                    if child.name == 'check-gifski':
                        child.join(2)
                status = manager.status()['updates']
                self.assertEqual(status['state'], 'checking')
                self.assertEqual(status['tools']['gifski']['latestVersion'], '1.34.0')
                self.assertNotEqual(status['tools']['gifski']['state'], 'checking')
                self.assertEqual(status['tools']['ffmpeg']['state'], 'checking')
            finally:
                release.set()
                worker.join(2)
        self.assertEqual(manager.status()['updates']['state'], 'completed')

    def test_setup_only_queues_missing_tools_and_preserves_available_tools(self):
        manager = dep.DependencyManager(self.root); manager.supported = True
        installed = {name: {'available': True, 'path': '/existing/' + name} for name in ('gifski', 'ffmpeg', 'ffprobe')}
        with patch.object(dep.threading, 'Thread') as thread:
            ready = manager.install_missing(installed)
            thread.assert_not_called()
        self.assertEqual(ready['setup']['state'], 'completed')
        self.assertEqual(ready['missing'], [])
        self.assertFalse((self.root / 'bin').exists())
        installed['ffprobe']['available'] = False
        with patch.object(dep.threading, 'Thread') as thread:
            status = manager.install_missing(installed)
        self.assertTrue(status['busy'])
        self.assertEqual(status['setup']['remaining'], ['ffmpeg'])
        self.assertEqual(status['tools']['gifski']['state'], 'idle')
        self.assertTrue(status['tools']['gifski']['available'])
        self.assertEqual(status['tools']['ffmpeg']['state'], 'queued')
        self.assertEqual(thread.call_args.kwargs['args'], (['ffmpeg'],))
        with self.assertRaises(ValueError):
            manager.install('gifski')
        with self.assertRaises(ValueError):
            manager.install_missing(installed)

    def test_setup_runs_sequentially_and_reports_partial_failure(self):
        manager = dep.DependencyManager(self.root); manager.supported = True
        with patch.object(dep.threading, 'Thread'):
            manager.install_missing({})
        calls = []
        def install(name):
            calls.append(name)
            state = manager.status()
            self.assertTrue(state['busy'])
            self.assertEqual(state['tools'][name]['state'], 'installing')
            if name == 'gifski':
                self.assertEqual(state['tools']['ffmpeg']['state'], 'queued')
            manager._update(name, state='failed' if name == 'gifski' else 'completed')
        with patch.object(manager, '_install_worker', side_effect=install):
            manager._install_missing_worker(['gifski', 'ffmpeg'])
        self.assertEqual(calls, ['gifski', 'ffmpeg'])
        self.assertEqual(manager.status()['setup']['state'], 'partial')
        self.assertFalse(manager.status()['busy'])

    def test_setup_start_failure_and_unsupported_platform_leave_no_busy_state(self):
        manager = dep.DependencyManager(self.root); manager.supported = False
        with self.assertRaises(ValueError):
            manager.install_missing({})
        manager.supported = True
        with patch.object(dep.threading, 'Thread', side_effect=RuntimeError('cannot start')):
            status = manager.install_missing({})
        self.assertFalse(status['busy'])
        self.assertEqual(status['setup']['state'], 'failed')
        self.assertTrue(all(tool['state'] == 'failed' for tool in status['tools'].values()))

    def test_changed_local_version_is_compared_against_cached_release(self):
        manager = dep.DependencyManager(self.root)
        manager.release_cache = {'gifski': '1.34.0'}
        manager.check_state = 'completed'
        for version, expected in [('1.33.0', 'update_available'), ('1.34.0', 'current')]:
            state = manager.status({'gifski': {'available': True, 'version': 'gifski ' + version}})
            self.assertEqual(state['updates']['tools']['gifski']['state'], expected)

    def test_build_diagnostics_recognize_vendor_tags_and_do_not_order_nightlies(self):
        cases = [
            ('ffmpeg', 'ffmpeg version n9.0.2-20261025', 'vendor', '9.0.2', (9, 0, 2)),
            ('ffprobe', 'ffprobe version 9.0.2-essentials_build-www.gyan.dev', 'vendor', '9.0.2', (9, 0, 2)),
            ('ffmpeg', 'ffmpeg version n9.0.2-35-g9a9b70bb8c-20261025', 'snapshot', '9.0.2', None),
            ('ffmpeg', 'ffmpeg version N-128094-g31855b2a70-20261025', 'snapshot', '', None),
            ('ffmpeg', 'ffmpeg version 2026-10-25-git-46f23be8b1-full_build-www.gyan.dev', 'snapshot', '', None),
            ('ffmpeg', 'ffmpeg version n9.1-dev-20261025', 'snapshot', '9.1', None),
            ('ffmpeg', 'ffmpeg version 9.0-rc2', 'prerelease', '9.0', None),
            ('ffmpeg', 'Wrapper startup message\nffmpeg version 9.0.2', 'stable', '9.0.2', (9, 0, 2)),
            ('gifski', 'gifski 1.34.0', 'stable', '1.34.0', (1, 34, 0)),
            ('ffmpeg', 'unrecognized executable output', 'unknown', '', None),
        ]
        for name, banner, kind, base, comparable in cases:
            with self.subTest(banner=banner):
                build = dep.installed_build(name, banner)
                self.assertEqual((build['kind'], build['baseVersion'], build['comparable']), (kind, base, comparable))

    def test_active_path_version_is_compared_instead_of_unused_portable_copy(self):
        manager = dep.DependencyManager(self.root); manager.supported = True
        (self.root / 'bin').mkdir()
        for name in ('ffmpeg', 'ffprobe'):
            (self.root / 'bin' / (name + '.exe')).write_bytes(executable())
        manager.release_cache = {'ffmpeg': '9.0.2'}
        manager.check_state = 'completed'
        installed = {name: {'available': True, 'origin': 'path', 'path': '/system/' + name,
                            'version': name + ' version 8.1.2-essentials_build-www.gyan.dev'}
                     for name in ('ffmpeg', 'ffprobe')}
        status = manager.status(installed)
        active = status['updates']['tools']['ffmpeg']
        self.assertEqual(active['state'], 'update_available')
        self.assertEqual(active['installedOrigin'], 'path')
        self.assertEqual(active['installedPath'], '/system/ffmpeg')
        self.assertEqual(active['installedBaseVersion'], '8.1.2')
        self.assertEqual(active['updateMethod'], 'external')
        self.assertIn('PATH', active['updateHint'])
        self.assertTrue(status['tools']['ffmpeg']['installed'])
        self.assertFalse(status['tools']['ffmpeg']['portableUpdateAffectsActive'])
        for name in installed:
            installed[name].update(origin='configured', path=str(self.root / 'bin' / (name + '.exe')),
                                   version=name + ' version 9.0.2-essentials_build-www.gyan.dev')
        status = manager.status(installed)
        active = status['updates']['tools']['ffmpeg']
        self.assertEqual(active['state'], 'current')
        self.assertEqual(active['installedKind'], 'vendor')
        self.assertEqual(active['updateMethod'], 'portable')
        self.assertIn('Vendor rebuilds', active['message'])
        self.assertTrue(status['tools']['ffmpeg']['portableUpdateAffectsActive'])

    def test_manual_check_refreshes_active_version_asynchronously_even_with_cached_upstream(self):
        manager = dep.DependencyManager(self.root)
        manager.release_cache = {'ffmpeg': '9.0.2', 'gifski': '1.34.0'}
        manager.check_state = 'completed'
        manager.checked_monotonic = time.monotonic()
        manager.checked_at = 1234
        manager.status({'ffmpeg': {'available': True, 'origin': 'path', 'version': 'ffmpeg version 8.0.1'}})
        entered, release = threading.Event(), threading.Event()
        def refresh():
            entered.set()
            if not release.wait(2):
                raise TimeoutError('Test release was not signaled.')
            return {'ffmpeg': {'available': True, 'origin': 'path', 'version': 'ffmpeg version 9.0.2-full_build-www.gyan.dev'}}
        with patch.object(manager, '_latest_release') as latest:
            try:
                status = manager.check_updates(refresh_installed=refresh)
                self.assertTrue(entered.wait(2))
                self.assertEqual(status['updates']['state'], 'checking')
                self.assertEqual(status['updates']['phase'], 'installed')
                self.assertIn('Refreshing', status['updates']['tools']['ffmpeg']['message'])
                with patch.object(dep.threading, 'Thread') as thread:
                    manager.check_updates(refresh_installed=lambda: self.fail('Duplicate refresh'))
                    thread.assert_not_called()
            finally:
                release.set()
                for worker in threading.enumerate():
                    if worker.name == 'check-updates':
                        worker.join(2)
            latest.assert_not_called()
        final = manager.status()['updates']
        self.assertEqual(final['state'], 'completed')
        self.assertEqual(final['phase'], '')
        self.assertTrue(final['cached'])
        self.assertEqual(final['checkedAt'], 1234)
        self.assertEqual(final['tools']['ffmpeg']['state'], 'current')

    def test_local_refresh_failure_finishes_with_sanitized_diagnostic(self):
        manager = dep.DependencyManager(self.root)
        with patch.object(dep.threading, 'Thread'):
            manager.check_updates(refresh_installed=lambda: {})
        def fail():
            raise RuntimeError('secret-user-path-or-token')
        manager._check_worker(fail)
        state = manager.status()['updates']
        self.assertEqual(state['state'], 'failed')
        self.assertEqual(state['phase'], '')
        self.assertFalse(manager.check_pending)
        self.assertEqual(state['tools']['ffmpeg']['errorCode'], 'local_detection')
        self.assertIn('Restart', state['tools']['ffmpeg']['message'])
        self.assertNotIn('secret', json.dumps(state))
        with patch.object(manager, '_latest_release', side_effect=lambda name: {'gifski': '1.34.0', 'ffmpeg': '9.0.2'}[name]):
            manager._check_worker(lambda: {'ffmpeg': {'available': True, 'origin': 'path', 'version': 'ffmpeg version 9.0.2'}})
        self.assertEqual(manager.status()['updates']['tools']['ffmpeg']['state'], 'current')
        self.assertEqual(manager.status()['updates']['state'], 'completed')

    def test_metadata_size_limit(self):
        with patch.object(dep, '_open', return_value=io.BytesIO(b'123456')):
            with self.assertRaisesRegex(ValueError, 'size limit'):
                dep._read_small(dep.GITHUB_RELEASE, limit=5)
        with patch.object(dep, '_open', return_value=io.BytesIO(b'12345')):
            self.assertEqual(dep._read_small(dep.GITHUB_RELEASE, limit=5), b'12345')

    def test_license_retention_is_bounded(self):
        entries = [('gifski.exe', executable())]
        entries += [(f'licenses/LICENSE.{i}', b'x' * 1024) for i in range(40)]
        archive = self.archive(entries)
        stage = self.root / 'stage'; stage.mkdir()
        dep.extract_executables(archive, stage, ['gifski.exe'])
        self.assertEqual(len(list((stage / 'licenses').iterdir())), 32)

    def test_publish_rejects_symlink_destinations(self):
        stage = self.root / 'stage'; stage.mkdir()
        (stage / 'gifski.exe').write_bytes(b'new')
        destination = self.root / 'bin'; destination.mkdir()
        outside = self.root / 'outside'; outside.write_bytes(b'old')
        try:
            (destination / 'gifski.exe').symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('Symbolic links are unavailable on this platform.')
        manager = dep.DependencyManager(self.root)
        with self.assertRaisesRegex(ValueError, 'regular file'):
            manager._publish('gifski', {'gifski.exe': stage / 'gifski.exe'}, stage)
        self.assertEqual(outside.read_bytes(), b'old')


if __name__ == '__main__':
    unittest.main()
