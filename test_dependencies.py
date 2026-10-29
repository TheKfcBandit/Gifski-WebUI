"""Dependency installer safety and lifecycle checks. No external downloads."""
import hashlib
import io
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


if __name__ == '__main__':
    unittest.main()
