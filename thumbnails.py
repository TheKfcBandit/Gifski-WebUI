"""Small, static history previews. Thumbnail failures never change export jobs."""
from __future__ import annotations

import os
import math
from pathlib import Path
import queue
import re
import subprocess
import tempfile
import threading
import time


MAX_THUMBNAIL_BYTES = 128 * 1024
THUMBNAIL_TIMEOUT = 5
MAX_PENDING = 32
TERMINAL_STATES = frozenset({'completed', 'cancelled', 'failed', 'interrupted'})
SOURCE_SUFFIXES = frozenset({'.mp4', '.m4v', '.webm', '.mov', '.mkv', '.avi', '.gif',
                             '.png', '.y4m', '.apng', '.m2ts', '.ts', '.mpeg', '.mpg',
                             '.wmv', '.avif', '.webp', '.jpg', '.jpeg', '.bmp', '.tif', '.tiff'})


class ThumbnailCache:
    def __init__(self, engine, operation_lock, can_generate=None):
        self.engine = engine
        self.root = (Path(engine.root) / 'data' / 'jobs').resolve()
        self.operation_lock = operation_lock
        self.can_generate = can_generate or (lambda: True)
        self.lock = threading.Lock()
        self.queue = queue.Queue(maxsize=MAX_PENDING)
        self.pending = set()
        self.retry_after = {}
        self.closed = threading.Event()
        self.process = None
        self.worker = threading.Thread(target=self._worker, daemon=True, name='history-thumbnails')
        self.worker.start()

    def _directory(self, jid):
        if not isinstance(jid, str) or not re.fullmatch(r'[A-Za-z0-9_-]{1,64}', jid):
            raise FileNotFoundError('Thumbnail not found.')
        directory = self.root / jid
        if directory.is_symlink() or directory.resolve() != directory or not directory.is_dir():
            raise FileNotFoundError('Thumbnail not found.')
        return directory

    def _path(self, jid, index):
        if index != 'source' and (isinstance(index, bool) or not isinstance(index, int) or not 0 <= index <= 100000):
            raise FileNotFoundError('Thumbnail not found.')
        return self._directory(jid) / f'thumbnail-{index}.jpg'

    @staticmethod
    def _valid(path):
        try:
            if path.is_symlink() or not path.is_file() or not 4 <= path.stat().st_size <= MAX_THUMBNAIL_BYTES:
                return False
            with path.open('rb') as source:
                if source.read(2) != b'\xff\xd8':
                    return False
                source.seek(-2, os.SEEK_END)
                return source.read(2) == b'\xff\xd9'
        except OSError:
            return False

    def _schedule(self, jid, index):
        key = (jid, index)
        with self.lock:
            if (self.closed.is_set() or key in self.pending
                    or self.retry_after.get(key, 0) > time.monotonic()):
                return
            try:
                self.queue.put_nowait(key)
            except queue.Full:
                return
            self.pending.add(key)

    def decorate(self, summaries):
        """Add ready thumbnail URLs without waiting for rendering or copying jobs."""
        decorated = []
        for summary in summaries:
            item = dict(summary, thumbnailUrl=None)
            index = item.get('thumbnailIndex')
            if item.get('status') in TERMINAL_STATES and index is not None:
                try:
                    path = self._path(item.get('id'), index)
                    if self._valid(path):
                        item['thumbnailUrl'] = f"/api/jobs/{item['id']}/thumbnail"
                    else:
                        self._schedule(item['id'], index)
                except (OSError, ValueError):
                    pass
            decorated.append(item)
        return decorated

    def file_for(self, jid):
        """Resolve only the chosen output's bounded JPEG, never the source GIF."""
        self._directory(jid)
        try:
            selected = self.engine.thumbnail_input(jid)
        except (KeyError, ValueError):
            selected = None
        if not selected or selected.get('status') not in TERMINAL_STATES:
            raise FileNotFoundError('Thumbnail not found.')
        path = self._path(jid, selected.get('index'))
        if not self._valid(path):
            raise FileNotFoundError('Thumbnail not found.')
        return path

    @staticmethod
    def _stop(process):
        if process is None:
            return
        try:
            if process.poll() is None:
                process.terminate()
            process.wait(timeout=.5)
        except subprocess.TimeoutExpired:
            process.kill()
            process.wait(timeout=1)
        except OSError:
            pass

    def _render(self, jid, index):
        selected = self.engine.thumbnail_input(jid)
        if (not selected or selected.get('status') not in TERMINAL_STATES
                or selected.get('index') != index):
            return
        target = self._path(jid, index)
        if self._valid(target):
            return
        source = Path(selected['path'])
        directory = target.parent
        seek = selected.get('seek', 0)
        if isinstance(seek, bool) or not isinstance(seek, (int, float)) or not math.isfinite(seek) or not 0 <= seek <= 8_640_000:
            raise ValueError('Invalid thumbnail position.')
        if index == 'source':
            # Only the engine's selected, local job source can be used here.
            # Never send its path to the browser or accept a path from the URL.
            raw = str(selected['path'])
            if (raw.startswith(('\\\\', '//')) or re.match(r'^[A-Za-z][A-Za-z0-9+.-]*://', raw)
                    or not source.is_absolute() or source.suffix.lower() not in SOURCE_SUFFIXES
                    or source.resolve() != source.absolute()):
                raise ValueError('Thumbnail source must be a local media file.')
        elif source.suffix.lower() != '.gif' or source.resolve().parent != directory:
            raise ValueError('Thumbnail source must be a completed GIF in its job folder.')
        if source.is_symlink() or not source.is_file():
            raise ValueError('Thumbnail source is missing or changed.')
        descriptor, raw_temp = tempfile.mkstemp(prefix='.thumbnail-', suffix='.jpg', dir=directory)
        os.close(descriptor)
        temporary = Path(raw_temp)
        process = None
        try:
            command = [self.engine._tool('ffmpeg'), '-hide_banner', '-loglevel', 'error',
                       '-nostdin', '-y', '-protocol_whitelist', 'file,pipe', '-threads', '1',
                       *(['-ss', f'{seek:g}'] if seek else []),
                       '-i', str(source), '-map', '0:v:0', '-an', '-sn', '-dn',
                       '-frames:v', '1', '-filter_threads', '1', '-threads', '1',
                       '-vf', "scale=w='min(160,iw)':h='min(90,ih)':force_original_aspect_ratio=decrease",
                       '-q:v', '4', '-pix_fmt', 'yuvj420p', '-fs', str(MAX_THUMBNAIL_BYTES + 1),
                       '-f', 'image2', '-update', '1', str(temporary)]
            if self.closed.is_set():
                return
            process = subprocess.Popen(command, stdin=subprocess.DEVNULL,
                                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            with self.lock:
                self.process = process
            deadline = time.monotonic() + THUMBNAIL_TIMEOUT
            while process.poll() is None:
                if self.closed.is_set():
                    return
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise TimeoutError('Thumbnail preparation timed out.')
                try:
                    process.wait(timeout=min(.1, remaining))
                except subprocess.TimeoutExpired:
                    pass
            if process.returncode or not self._valid(temporary):
                raise ValueError('Thumbnail preparation did not produce a bounded JPEG.')
            # Recheck containment immediately before replacing the cache file.
            if self._path(jid, index) != target or target.is_symlink():
                raise ValueError('Thumbnail destination changed.')
            os.replace(temporary, target)
        finally:
            self._stop(process)
            with self.lock:
                if self.process is process:
                    self.process = None
            temporary.unlink(missing_ok=True)

    def _worker(self):
        while not self.closed.is_set():
            try:
                key = self.queue.get(timeout=.2)
            except queue.Empty:
                continue
            acquired = False
            retry = 0
            try:
                while not self.closed.is_set():
                    acquired = self.operation_lock.acquire(timeout=.1)
                    if acquired:
                        break
                if acquired and not self.closed.is_set():
                    if self.can_generate():
                        self._render(*key)
                    else:
                        retry = 5
            except Exception:
                # Missing encoders, removed media, and bad previews are nonfatal.
                retry = 60
            finally:
                if acquired:
                    self.operation_lock.release()
                with self.lock:
                    self.pending.discard(key)
                    if retry:
                        self.retry_after[key] = time.monotonic() + retry
                        if len(self.retry_after) > 512:
                            oldest = min(self.retry_after, key=self.retry_after.get)
                            self.retry_after.pop(oldest, None)
                self.queue.task_done()

    def close(self):
        self.closed.set()
        with self.lock:
            process = self.process
        self._stop(process)
        self.worker.join(timeout=2)
