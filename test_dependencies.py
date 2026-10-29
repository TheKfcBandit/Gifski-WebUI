"""Dependency installer safety and lifecycle checks. No external downloads."""
import hashlib
import io
import json
from pathlib import Path
import struct
import tarfile
import tempfile
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

    def test_version_states_do_not_claim_custom_builds_are_current(self):
        manager = dep.DependencyManager(self.root)
        manager.release_cache = {'gifski': '1.34.0', 'ffmpeg': '8.0.1'}
        manager.check_state = 'completed'
        cases = [('ffmpeg version 8.0.1', 'current'),
                 ('ffmpeg version 7.1.1', 'update_available'),
                 ('ffmpeg version 7.1.1-essentials_build-www.gyan.dev', 'update_available'),
                 ('ffmpeg version 9.0.0', 'newer'),
                 ('ffmpeg version 8.0.1-essentials_build-www.gyan.dev', 'unknown'),
                 ('ffmpeg version 8.0.1-12-gabc123', 'unknown'),
                 ('ffmpeg version N-119000-gabc123', 'unknown'),
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
        with patch.object(manager, '_latest_release', side_effect=['1.34.0', '8.0.1']):
            manager._check_worker()
        with patch.object(dep.threading, 'Thread') as thread:
            self.assertTrue(manager.check_updates()['updates']['cached'])
            thread.assert_not_called()
            manager.check_state = 'checking'
            self.assertEqual(manager.check_updates()['updates']['state'], 'checking')
            thread.assert_not_called()

    def test_provider_failure_isolated_and_error_does_not_leak_url(self):
        manager = dep.DependencyManager(self.root)
        with patch.object(manager, '_latest_release', side_effect=[ValueError('https://signed.example/secret'), '8.0.1']):
            manager._check_worker()
        status = manager.status()['updates']
        self.assertEqual(status['state'], 'partial')
        self.assertEqual(status['tools']['gifski']['state'], 'error')
        self.assertEqual(status['tools']['ffmpeg']['latestVersion'], '8.0.1')
        self.assertNotIn('secret', json.dumps(status))

    def test_changed_local_version_is_compared_against_cached_release(self):
        manager = dep.DependencyManager(self.root)
        manager.release_cache = {'gifski': '1.34.0'}
        manager.check_state = 'completed'
        for version, expected in [('1.33.0', 'update_available'), ('1.34.0', 'current')]:
            state = manager.status({'gifski': {'available': True, 'version': 'gifski ' + version}})
            self.assertEqual(state['updates']['tools']['gifski']['state'], expected)

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
