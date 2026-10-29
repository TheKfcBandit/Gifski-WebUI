"""History preview safety, caching, and lifecycle checks without media downloads."""
from pathlib import Path
import os
import stat
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import shutil
from types import SimpleNamespace
from unittest.mock import patch

from thumbnails import MAX_PENDING, MAX_THUMBNAIL_BYTES, ThumbnailCache


JPEG = b'\xff\xd8preview\xff\xd9'


class FakeEngine:
    def __init__(self, root):
        self.root = root
        self.items = {}

    def add(self, jid, index=0, status='completed'):
        directory = self.root / 'data' / 'jobs' / jid
        directory.mkdir(parents=True, exist_ok=True)
        path = directory / f'output-{index}.gif'
        path.write_bytes(b'GIF89a')
        self.items[jid] = {'id': jid, 'status': status, 'index': index, 'path': path}
        return {'id': jid, 'status': status, 'thumbnailIndex': index, 'resultCount': index + 1}

    def thumbnail_input(self, jid):
        return self.items.get(jid)

    def _tool(self, name):
        return name


class FinishedProcess:
    def __init__(self, command, payload=JPEG, **kwargs):
        self.returncode = 0
        Path(command[-1]).write_bytes(payload)

    def poll(self):
        return self.returncode

    def wait(self, timeout=None):
        return self.returncode


class ThumbnailTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory(prefix='gifski-thumbnail-tests-')
        self.root = Path(self.temp.name)
        self.engine = FakeEngine(self.root)
        self.operation_lock = threading.RLock()
        self.cache = ThumbnailCache(self.engine, self.operation_lock)

    def tearDown(self):
        self.cache.close()
        self.temp.cleanup()

    def wait_until(self, predicate):
        deadline = time.monotonic() + 2
        while not predicate() and time.monotonic() < deadline:
            time.sleep(.01)
        self.assertTrue(predicate())

    def test_background_preview_is_cached_and_never_serves_the_gif(self):
        summary = self.engine.add('one')
        with patch('thumbnails.subprocess.Popen', side_effect=FinishedProcess) as run:
            first = self.cache.decorate([summary])
            self.assertIsNone(first[0]['thumbnailUrl'])
            self.assertNotIn('thumbnailUrl', summary)
            self.wait_until(lambda: not self.cache.pending)
            ready = self.cache.decorate([summary])[0]
            self.assertEqual(ready['thumbnailUrl'], '/api/jobs/one/thumbnail')
            self.assertEqual(self.cache.file_for('one').read_bytes(), JPEG)
            self.assertEqual(self.cache.file_for('one').suffix, '.jpg')
            self.assertEqual(run.call_count, 1)
            command = run.call_args.args[0]
            self.assertIn('file,pipe', command)
            self.assertEqual(command[command.index('-frames:v') + 1], '1')
            self.assertEqual(command[command.index('-threads') + 1], '1')
            self.assertEqual(run.call_args.kwargs['stdout'], subprocess.DEVNULL)
            self.assertEqual(run.call_args.kwargs['stderr'], subprocess.DEVNULL)
            self.assertNotIn('shell', run.call_args.kwargs)

    def test_pending_previews_are_bounded_and_deduplicated(self):
        summaries = [self.engine.add(f'job{i}') for i in range(100)]
        self.operation_lock.acquire()
        try:
            self.cache.decorate(summaries)
            self.assertLessEqual(len(self.cache.pending), MAX_PENDING + 1)
            before = set(self.cache.pending)
            self.cache.decorate([s for s in summaries if (s['id'], 0) in before])
            self.assertEqual(self.cache.pending, before)
        finally:
            self.cache.closed.set()
            self.operation_lock.release()

    def test_failure_is_nonfatal_and_not_retried_on_every_poll(self):
        summary = self.engine.add('one')
        with patch('thumbnails.subprocess.Popen', side_effect=OSError('Unavailable')) as run:
            self.cache.decorate([summary])
            self.wait_until(lambda: ('one', 0) in self.cache.retry_after)
            self.assertIsNone(self.cache.decorate([summary])[0]['thumbnailUrl'])
            self.assertEqual(run.call_count, 1)
            self.assertEqual(self.engine.items['one']['status'], 'completed')
            self.assertFalse(list((self.root / 'data/jobs/one').glob('.thumbnail-*')))

    def test_installer_busy_defers_without_launching_an_encoder(self):
        self.cache.can_generate = lambda: False
        summary = self.engine.add('one')
        with patch('thumbnails.subprocess.Popen') as run:
            self.cache.decorate([summary])
            self.wait_until(lambda: ('one', 0) in self.cache.retry_after)
            run.assert_not_called()
            self.assertIsNone(self.cache.decorate([summary])[0]['thumbnailUrl'])

    def test_thumbnail_tracks_selected_candidate_and_terminal_status(self):
        summary = self.engine.add('one', index=1)
        directory = self.root / 'data/jobs/one'
        (directory / 'thumbnail-0.jpg').write_bytes(b'\xff\xd8zero\xff\xd9')
        (directory / 'thumbnail-1.jpg').write_bytes(JPEG)
        self.assertEqual(self.cache.file_for('one').name, 'thumbnail-1.jpg')
        self.engine.items['one']['index'] = 0
        self.assertEqual(self.cache.file_for('one').name, 'thumbnail-0.jpg')
        self.engine.items['one']['status'] = 'running'
        summary['status'] = 'running'
        self.assertIsNone(self.cache.decorate([summary])[0]['thumbnailUrl'])
        with self.assertRaises(FileNotFoundError):
            self.cache.file_for('one')
        summary.update(status='completed', thumbnailIndex=None)
        self.assertIsNone(self.cache.decorate([summary])[0]['thumbnailUrl'])

    def test_paths_and_output_cannot_escape_job_directory(self):
        summary = self.engine.add('one')
        for jid in ('../one', '/one', '', 'a/b', 'a' * 65):
            with self.subTest(jid=jid), self.assertRaises(FileNotFoundError):
                self.cache.file_for(jid)
        outside = self.root / 'private.gif'
        outside.write_bytes(b'GIF89a')
        self.engine.items['one']['path'] = outside
        with patch('thumbnails.subprocess.Popen') as run, self.assertRaises(ValueError):
            self.cache._render('one', 0)
        run.assert_not_called()
        self.assertIsNone(self.cache.decorate([dict(summary, id='../one')])[0]['thumbnailUrl'])

    def test_source_fallback_uses_only_validated_local_media_and_position(self):
        summary = self.engine.add('estimate')
        source = self.root / 'clip.mp4'
        source.write_bytes(b'test video')
        self.engine.items['estimate'].update(index='source', path=source, seek=1.5)
        summary.update(thumbnailIndex='source', resultCount=0)
        with patch('thumbnails.subprocess.Popen', side_effect=FinishedProcess) as run:
            self.cache.decorate([summary])
            self.wait_until(lambda: not self.cache.pending)
            self.assertEqual(self.cache.file_for('estimate').name, 'thumbnail-source.jpg')
            command = run.call_args.args[0]
            self.assertEqual(command[command.index('-ss') + 1], '1.5')
            self.assertEqual(command[command.index('-i') + 1], str(source.resolve()))
        self.cache.file_for('estimate').unlink()
        for seek in (float('nan'), float('inf'), -1, 8_640_001, True, '1'):
            self.engine.items['estimate']['seek'] = seek
            with self.subTest(seek=seek), self.assertRaises(ValueError):
                self.cache._render('estimate', 'source')
        self.engine.items['estimate']['seek'] = 0
        for path in ('https://example.test/clip.mp4', '//server/share/clip.mp4', '\\\\server\\share\\clip.mp4',
                     ' "\\\\server\\share\\clip.mp4" ', ' "https://example.test/clip.mp4" ', 'relative.mp4'):
            self.engine.items['estimate']['path'] = path
            with self.subTest(path=path), patch('thumbnails.subprocess.Popen') as run, self.assertRaises(ValueError):
                self.cache._render('estimate', 'source')
            run.assert_not_called()

    def test_legitimate_source_path_normalization_is_not_a_redirect(self):
        self.engine.add('estimate')
        source = self.root / 'clip.mp4'
        source.write_bytes(b'test video')
        (self.root / 'folder').mkdir()
        spelling = self.root / 'folder' / '..' / 'clip.mp4'
        self.assertNotEqual(spelling.absolute(), spelling.resolve())
        self.engine.items['estimate'].update(index='source', path=spelling, seek=0)
        with patch('thumbnails.subprocess.Popen', side_effect=FinishedProcess) as run:
            self.cache._render('estimate', 'source')
        self.assertEqual(self.cache.file_for('estimate').read_bytes(), JPEG)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('-i') + 1], str(source.resolve()))

    def test_windows_junction_tag_is_rejected_even_without_symlink_mode(self):
        path = self.root / 'junction' / 'clip.mp4'
        junction_tag = object()
        def metadata(component):
            return SimpleNamespace(st_mode=stat.S_IFDIR if component.name == 'junction' else stat.S_IFREG,
                                   st_reparse_tag=junction_tag if component.name == 'junction' else 0)
        with patch.object(stat, 'IO_REPARSE_TAG_MOUNT_POINT', junction_tag, create=True), patch.object(Path, 'lstat', metadata):
            self.assertTrue(self.cache._has_redirect(path))

    @unittest.skipUnless(os.name == 'nt', 'Windows short-path API required')
    def test_windows_short_source_path_renders_using_canonical_path(self):
        import ctypes
        self.engine.add('estimate')
        directory = self.root / 'Long source directory for thumbnail test'
        directory.mkdir()
        source = directory / 'Long source filename.mp4'
        source.write_bytes(b'test video')
        get_short_path = ctypes.windll.kernel32.GetShortPathNameW
        get_short_path.argtypes = (ctypes.c_wchar_p, ctypes.c_wchar_p, ctypes.c_uint)
        get_short_path.restype = ctypes.c_uint
        buffer = ctypes.create_unicode_buffer(32768)
        length = get_short_path(str(source), buffer, len(buffer))
        if not 0 < length < len(buffer) or Path(buffer.value) == source.resolve():
            self.skipTest('This filesystem does not expose a short-path alias')
        alias = Path(buffer.value)
        self.assertEqual(alias.resolve(), source.resolve())
        self.engine.items['estimate'].update(index='source', path=alias, seek=0)
        with patch('thumbnails.subprocess.Popen', side_effect=FinishedProcess) as run:
            self.cache._render('estimate', 'source')
        self.assertEqual(self.cache.file_for('estimate').read_bytes(), JPEG)
        command = run.call_args.args[0]
        self.assertEqual(command[command.index('-i') + 1], str(source.resolve()))

    @unittest.skipUnless(shutil.which('ffmpeg'), 'Real FFmpeg unavailable')
    def test_estimate_without_gif_generates_real_static_source_thumbnail(self):
        summary = self.engine.add('estimate')
        source = self.root / 'source.mp4'
        subprocess.run(['ffmpeg', '-v', 'error', '-f', 'lavfi', '-i',
                        'testsrc2=size=160x90:rate=5', '-t', '1', '-c:v', 'mpeg4',
                        '-threads', '1', '-y', str(source)], check=True, timeout=5,
                       stdin=subprocess.DEVNULL, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.engine.items['estimate'].update(index='source', path=source, seek=.2)
        summary.update(thumbnailIndex='source', resultCount=0)
        self.cache.decorate([summary])
        self.wait_until(lambda: not self.cache.pending)
        target = self.cache.file_for('estimate')
        self.assertTrue(self.cache._valid(target))
        self.assertLess(target.stat().st_size, MAX_THUMBNAIL_BYTES)
        self.assertEqual(self.cache.decorate([summary])[0]['thumbnailUrl'], '/api/jobs/estimate/thumbnail')

    def test_symlink_cache_and_directory_are_rejected(self):
        summary = self.engine.add('one')
        outside = self.root / 'private.jpg'
        outside.write_bytes(JPEG)
        target = self.root / 'data/jobs/one/thumbnail-0.jpg'
        try:
            target.symlink_to(outside)
        except (OSError, NotImplementedError):
            self.skipTest('Symlinks unavailable on this system')
        with self.assertRaises(FileNotFoundError):
            self.cache.file_for('one')
        target.unlink()
        source_link = self.root / 'changed.gif'
        source_link.symlink_to(self.engine.items['one']['path'])
        self.engine.items['one'].update(index='source', path=source_link, seek=0)
        with patch('thumbnails.subprocess.Popen') as run, self.assertRaises(ValueError):
            self.cache._render('one', 'source')
        run.assert_not_called()
        (self.root / 'data/jobs/link').symlink_to(self.root / 'data/jobs/one', target_is_directory=True)
        self.engine.items['link'] = dict(self.engine.items['one'], id='link')
        with self.assertRaises(FileNotFoundError):
            self.cache.file_for('link')

    def test_oversized_or_truncated_jpeg_is_not_published(self):
        self.engine.add('one')
        for payload in (b'\xff\xd8truncated', b'\xff\xd8' + b'x' * MAX_THUMBNAIL_BYTES + b'\xff\xd9'):
            with self.subTest(length=len(payload)), patch('thumbnails.subprocess.Popen', side_effect=lambda command, **kwargs: FinishedProcess(command, payload, **kwargs)):
                with self.assertRaises(ValueError):
                    self.cache._render('one', 0)
                self.assertFalse((self.root / 'data/jobs/one/thumbnail-0.jpg').exists())
                self.assertFalse(list((self.root / 'data/jobs/one').glob('.thumbnail-*')))

    def test_shutdown_terminates_and_reaps_active_child(self):
        summary = self.engine.add('one')
        real_popen = subprocess.Popen
        children = []
        def sleeping_child(command, **kwargs):
            process = real_popen([sys.executable, '-c', 'import time; time.sleep(30)'], **kwargs)
            children.append(process)
            return process
        with patch('thumbnails.subprocess.Popen', side_effect=sleeping_child):
            self.cache.decorate([summary])
            self.wait_until(lambda: bool(children) and self.cache.process is not None)
            start = time.monotonic()
            self.cache.close()
            self.assertLess(time.monotonic() - start, 2)
            self.assertIsNotNone(children[0].poll())
            self.assertFalse(self.cache.worker.is_alive())
            self.assertFalse(list((self.root / 'data/jobs/one').glob('.thumbnail-*')))


if __name__ == '__main__':
    unittest.main()
