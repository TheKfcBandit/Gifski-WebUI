"""Local FFmpeg/Gifski execution, measured comparisons, and bounded size search.

Python 3.10+, standard library only. Child programs never run through a shell.
"""
from __future__ import annotations

import copy
import codecs
from concurrent.futures import ThreadPoolExecutor
import hashlib
import itertools
import json
import math
import os
from pathlib import Path
import queue
import re
import shutil
import struct
import subprocess
import threading
import time
import uuid

DEFAULTS = {
    'source': {'kind': 'video', 'path': '', 'paths': []},
    'trim': {'start': 0, 'end': None}, 'fps': 20, 'speed': 1,
    'width': 640, 'height': None, 'quality': 90, 'lossyQuality': None,
    'motionQuality': None, 'effort': 'normal', 'repeat': 0, 'bounce': False,
    'noSort': False, 'quiet': False, 'matte': None, 'fixedColors': [],
    'y4mColorOverride': None, 'outputName': 'animation.gif',
    'crop': {'enabled': False, 'x': 0, 'y': 0, 'width': 640, 'height': 360},
    'scaleFilter': 'lanczos', 'denoise': 0, 'sharpen': 0, 'reverse': False,
    'customFilter': '', 'pipeline': 'auto',
    'performance': {'threads': 0, 'hardwareDecode': 'none'},
    'sweep': {'qualities': [70, 80, 90], 'widths': [480, 640], 'fps': [15, 20]},
    'smart': {'targetMB': 20, 'effort': 'balanced', 'pinResolution': False, 'pinFps': False},
    'optimize': {'targetMiB': 8, 'minQuality': 40, 'maxQuality': 95,
                 'minWidth': 320, 'minFps': 10, 'allowResize': True,
                 'allowFps': True, 'maxTrials': 18},
}
COLOR_SPACES = ('bt709', 'fcc', 'bt470bg', 'bt601', 'smpte240', 'ycgco')
SCALE_FILTERS = ('lanczos', 'bicubic', 'bilinear', 'neighbor', 'spline', 'area')


def merged(default, values):
    out = copy.deepcopy(default)
    for k, v in values.items():
        out[k] = merged(out[k], v) if isinstance(v, dict) and isinstance(out.get(k), dict) else copy.deepcopy(v)
    return out


def number(value, label, lo, hi, integer=False, nullable=False):
    if nullable and value in (None, ''):
        return None
    if isinstance(value, bool):
        raise ValueError(f'{label} must be a number.')
    try:
        n = float(value)
    except (ValueError, TypeError):
        raise ValueError(f'{label} must be a number.') from None
    if not math.isfinite(n) or not lo <= n <= hi or (integer and n != int(n)):
        raise ValueError(f'{label} must be {"an integer " if integer else ""}between {lo} and {hi}.')
    return int(n) if integer else n


def color(value):
    value = str(value).strip().lstrip('#')
    if not re.fullmatch(r'[0-9a-fA-F]{6}', value):
        raise ValueError('Colors must contain six hexadecimal digits, such as #FFFFFF.')
    return value.upper()


def natural_key(value):
    return [int(x) if x.isdigit() else x.casefold() for x in re.split(r'(\d+)', str(value))]


def gif_dimensions(path):
    with Path(path).open('rb') as f:
        head = f.read(10)
    if len(head) != 10 or head[:6] not in (b'GIF87a', b'GIF89a'):
        raise RuntimeError('Encoder did not produce a valid GIF.')
    return struct.unpack('<HH', head[6:10])


class Cancelled(Exception):
    pass


class Engine:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.data = self.root / 'data'
        self.outputs = self.data / 'jobs'
        self.outputs.mkdir(parents=True, exist_ok=True)
        self.config_path = self.data / 'config.json'
        try:
            self.config = json.loads(self.config_path.read_text('utf-8'))
        except (OSError, ValueError):
            self.config = {}
        if not isinstance(self.config, dict):
            self.config = {}
        self.config = {k: v for k, v in self.config.items() if k in ('gifski', 'ffmpeg', 'ffprobe') and isinstance(v, str) and len(v) <= 4096 and '\x00' not in v}
        self.lock = threading.RLock()
        self.jobs = {}
        self.active = {}
        self.cancelled = set()
        self._status = None
        self._status_lock = threading.Lock()
        self._resolved_tools = {}
        self._status_revision = 0
        self._estimate_cache = {}
        self._frame_caches = {}
        self._queue = queue.Queue()
        for p in self.outputs.glob('*/job.json'):
            try:
                item = json.loads(p.read_text('utf-8'))
                if (not isinstance(item, dict) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', str(item.get('id', '')))
                        or item['id'] != p.parent.name or not isinstance(item.get('results', []), list)
                        or not isinstance(item.get('created'), (int, float)) or not math.isfinite(item['created'])):
                    continue
                if item.get('status') in ('queued', 'running'):
                    item.update(status='interrupted', stage='Server stopped', error='The app stopped before this job completed.')
                self.jobs[item['id']] = item
            except (OSError, ValueError, KeyError):
                pass
        self.jobs = dict(sorted(self.jobs.items(), key=lambda pair: pair[1]['created'], reverse=True)[:150])
        threading.Thread(target=self._worker, daemon=True, name='encode-queue').start()

    def _tool_candidates(self, name, config):
        configured = config.get(name)
        if configured:
            return [str(Path(configured).expanduser().resolve()) if Path(configured).expanduser().is_file() else (shutil.which(configured) or configured)]
        candidates = []
        if name == 'ffprobe' and config.get('ffmpeg'):
            ff = Path(config['ffmpeg']).expanduser()
            probe = ff.parent / ('ffprobe.exe' if ff.suffix.lower() == '.exe' else 'ffprobe')
            if probe.is_file():
                candidates.append(str(probe.resolve()))
        installed = shutil.which(name)
        if installed:
            candidates.append(installed)
        for directory in (self.root / 'bin', self.root):
            for ext in ('.exe', '') if os.name == 'nt' else ('', '.exe'):
                bundled = directory / (name + ext)
                if bundled.is_file():
                    candidates.append(str(bundled))
        return list(dict.fromkeys(candidates)) or [name]

    def _tool(self, name):
        with self.lock:
            return self._resolved_tools.get(name) or self._tool_candidates(name, self.config)[0]

    @staticmethod
    def _version_line(name, output):
        pattern = (r'^\s*gifski(?:\s+version)?\s+\d[^\r\n]*' if name == 'gifski'
                   else rf'^\s*{re.escape(name)}\s+version\s+\S+[^\r\n]*')
        match = re.search(pattern, output, re.IGNORECASE | re.MULTILINE)
        return match.group(0).strip()[:1000] if match else ''

    def status(self):
        # One probe batch at a time. The jobs lock remains free while programs run.
        with self.lock:
            if self._status is not None:
                return copy.deepcopy(self._status)
        with self._status_lock:
            with self.lock:
                if self._status is not None:
                    return copy.deepcopy(self._status)
                config_snapshot = copy.deepcopy(self.config)
                revision = self._status_revision
            def inspect(name):
                for path in self._tool_candidates(name, config_snapshot):
                    origin = ('config' if config_snapshot.get(name) or (name == 'ffprobe' and config_snapshot.get('ffmpeg') and path != shutil.which(name))
                              else 'path' if path == shutil.which(name) else 'local')
                    item = {'path': path, 'origin': origin, 'available': False, 'version': '', 'help': ''}
                    try:
                        r = subprocess.run([path, '--version' if name == 'gifski' else '-version'], capture_output=True, text=True, errors='replace', timeout=5)
                        combined = (r.stdout + '\n' + r.stderr).strip()
                        version = self._version_line(name, combined)
                        item.update(available=r.returncode == 0 and bool(version), version=version)
                        if not item['available']:
                            item['error'] = (combined[-1000:] or f'Exited with code {r.returncode}.') if r.returncode else f'Program did not identify itself as {name}.'
                            continue
                        if name == 'gifski':
                            try:
                                h = subprocess.run([path, '--help'], capture_output=True, text=True, errors='replace', timeout=5)
                                item['help'] = (h.stdout + h.stderr).strip()[:100000]
                                item['flags'] = sorted(set(re.findall(r'--[a-z][a-z-]+', item['help'])))
                            except (OSError, subprocess.TimeoutExpired) as e:
                                item['helpError'] = str(e)
                        break
                    except (OSError, subprocess.TimeoutExpired) as e:
                        item['error'] = str(e)
                return name, item
            with ThreadPoolExecutor(max_workers=3, thread_name_prefix='tool-version') as pool:
                tools = dict(pool.map(inspect, ('gifski', 'ffmpeg', 'ffprobe')))
            result = {'tools': tools, 'config': config_snapshot, 'defaults': copy.deepcopy(DEFAULTS), 'outputDirectory': str(self.outputs)}
            with self.lock:
                if self.config == config_snapshot and revision == self._status_revision:
                    self._resolved_tools = {name: item['path'] for name, item in tools.items()}
                    self._status = result
            return copy.deepcopy(result)

    def invalidate_status(self):
        with self.lock:
            self._status = None
            self._resolved_tools.clear()
            self._status_revision += 1

    def configure(self, values):
        patch = {}
        for k in ('gifski', 'ffmpeg', 'ffprobe'):
            if k in values:
                if not isinstance(values[k], str) or len(values[k]) > 4096 or '\x00' in values[k]:
                    raise ValueError(f'Invalid {k} executable path.')
                patch[k] = values[k].strip().strip('"')
        with self.lock:
            updated = dict(self.config, **patch)
            self._write_json(self.config_path, updated)
            self.config = updated
            self.invalidate_status()
        return self.status()

    @staticmethod
    def _write_json(path, value):
        tmp = Path(path).with_suffix('.tmp')
        tmp.write_text(json.dumps(value, indent=2, ensure_ascii=False), encoding='utf-8')
        os.replace(tmp, path)

    def _paths(self, source):
        kind = source.get('kind', 'video')
        if kind not in ('video', 'sequence', 'direct'):
            raise ValueError('Unknown source type.')
        if kind == 'sequence':
            raw = source.get('paths') or []
            if not isinstance(raw, list):
                raise ValueError('Sequence paths must be a list of PNG paths.')
            if not raw and source.get('path'):
                p = Path(source['path']).expanduser()
                if p.is_dir():
                    raw = [str(x) for x in p.iterdir() if x.suffix.lower() == '.png' and x.is_file()]
                else:
                    raw = [source['path']]
            if len(raw) < 2:
                raise ValueError('Select at least two PNG frames, or enter a folder containing PNG frames.')
            if len(raw) > 100000:
                raise ValueError('This app supports up to 100,000 PNG frames per job.')
        else:
            raw = [source.get('path', '')]
        paths = []
        for value in raw:
            if not isinstance(value, str) or not value.strip() or '\x00' in value:
                raise ValueError('Choose a source file first.')
            p = Path(value.strip().strip('"')).expanduser().resolve()
            if not p.is_file():
                raise ValueError(f'Source file does not exist: {p}')
            if kind == 'sequence' and p.suffix.lower() != '.png':
                raise ValueError('Image sequences must contain PNG files.')
            paths.append(p)
        return paths

    def probe(self, source, jid=None):
        paths = self._paths(source)
        path = paths[0]
        result = {'path': str(path), 'name': path.name, 'kind': source.get('kind', 'video'), 'bytes': sum(p.stat().st_size for p in paths), 'frameCount': len(paths) if source.get('kind') == 'sequence' else None}
        try:
            command = [self._tool('ffprobe'), '-protocol_whitelist', 'file,pipe', '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)]
            r = (self._capture_probe(jid, command) if jid is not None else
                 subprocess.run(command, capture_output=True, text=True, errors='replace', timeout=30))
            if r.returncode:
                raise ValueError(r.stderr[-2000:] or 'FFprobe could not read the source.')
            data = json.loads(r.stdout)
            stream = next((s for s in data.get('streams', []) if s.get('codec_type') == 'video'), None)
            if not stream:
                raise ValueError('The source has no video/image stream.')
            rate = stream.get('avg_frame_rate') or stream.get('r_frame_rate') or '0/1'
            try:
                n, d = map(float, rate.split('/'))
                fps = n / d if d else 0
            except (ValueError, ZeroDivisionError):
                fps = 0
            if not math.isfinite(fps) or fps < 0:
                fps = 0
            rotation = float(stream.get('tags', {}).get('rotate', 0))
            for side in stream.get('side_data_list', []):
                rotation = float(side.get('rotation', rotation))
            if not math.isfinite(rotation):
                rotation = 0
            w, h = stream.get('width'), stream.get('height')
            w = number(w, 'Source width', 1, 65535, integer=True)
            h = number(h, 'Source height', 1, 65535, integer=True)
            if abs(rotation) % 180 == 90:
                w, h = h, w
            duration = stream.get('duration') or data.get('format', {}).get('duration')
            duration = float(duration) if duration is not None else None
            if duration is not None and (not math.isfinite(duration) or duration <= 0):
                duration = None
            result.update(width=w, height=h, fps=fps, duration=duration,
                          codec=stream.get('codec_name'), pixelFormat=stream.get('pix_fmt'),
                          colorSpace=stream.get('color_space'), colorTransfer=stream.get('color_transfer'), rotation=rotation,
                          hasAlpha=(stream.get('pix_fmt') or '').startswith(('rgba', 'bgra', 'argb', 'abgr', 'yuva', 'gbrap', 'ya')) or stream.get('pix_fmt') == 'pal8',
                          streams=[{'codec_type': item.get('codec_type'), 'codec_name': item.get('codec_name')}
                                   for item in data.get('streams', []) if isinstance(item, dict)])
            if source.get('kind') != 'sequence' and stream.get('nb_frames', '').isdigit():
                result['frameCount'] = int(stream['nb_frames'])
        except FileNotFoundError:
            if path.suffix.lower() != '.png':
                raise ValueError('FFprobe was not found. Set its executable path in Settings.') from None
            with path.open('rb') as f:
                data = f.read(26)
            if len(data) < 26 or data[:8] != b'\x89PNG\r\n\x1a\n':
                raise ValueError('Invalid PNG source.')
            w, h = struct.unpack('>II', data[16:24])
            result.update(width=w, height=h, duration=None, fps=0, hasAlpha=data[25] in (4, 6), codec='png')
        except subprocess.TimeoutExpired:
            raise ValueError('FFprobe timed out while inspecting the source.') from None
        return result

    def _normalize(self, settings):
        if not isinstance(settings, dict):
            raise ValueError('Settings must be a JSON object.')
        s = merged(DEFAULTS, settings)
        for group in ('source', 'trim', 'crop', 'sweep', 'optimize', 'smart', 'performance'):
            if not isinstance(s[group], dict):
                raise ValueError(f'Invalid {group} settings.')
        for flag in ('bounce', 'noSort', 'quiet', 'reverse'):
            if not isinstance(s[flag], bool):
                raise ValueError(f'{flag} must be true or false.')
        if not isinstance(s['crop']['enabled'], bool):
            raise ValueError('Crop enabled must be true or false.')
        self._paths(s['source'])
        s['performance']['threads'] = number(s['performance']['threads'], 'FFmpeg threads', 0, 256, integer=True)
        if s['performance']['hardwareDecode'] not in ('none', 'auto'):
            raise ValueError('Unknown hardware decoding option.')
        s['smart']['targetMB'] = number(s['smart']['targetMB'], 'Target MB', .001, 100000)
        if s['smart']['effort'] not in ('quick', 'balanced', 'thorough'):
            raise ValueError('Unknown search effort.')
        for pin in ('pinResolution', 'pinFps'):
            if not isinstance(s['smart'][pin], bool):
                raise ValueError(f'{pin} must be true or false.')
        for k in ('quality', 'lossyQuality', 'motionQuality'):
            s[k] = number(s[k], k, 1, 100, integer=True, nullable=k != 'quality')
        for k in ('width', 'height'):
            s[k] = number(s[k], k, 1, 65535, integer=True, nullable=True)
        s['fps'] = number(s['fps'], 'Frame rate', 0.001, 100)
        s['speed'] = number(s['speed'], 'Speed', 0.001, 1000)
        if '_sequenceInputFps' in s:
            s['_sequenceInputFps'] = number(s['_sequenceInputFps'], 'Sequence input rate', .001, 100)
        s['repeat'] = number(s['repeat'], 'Repeat count', -1, 32767, integer=True)
        s['trim']['start'] = number(s['trim'].get('start', 0), 'Trim start', 0, 8640000)
        s['trim']['end'] = number(s['trim'].get('end'), 'Trim end', 0, 8640000, nullable=True)
        if s['trim']['end'] is not None and s['trim']['end'] <= s['trim']['start']:
            raise ValueError('Trim end must be later than trim start.')
        for k in ('x', 'y'):
            s['crop'][k] = number(s['crop'][k], f'Crop {k}', 0, 65535, integer=True)
        for k in ('width', 'height'):
            s['crop'][k] = number(s['crop'][k], f'Crop {k}', 1, 65535, integer=True)
        s['denoise'] = number(s['denoise'], 'Denoise', 0, 20)
        s['sharpen'] = number(s['sharpen'], 'Sharpen', 0, 5)
        if s['effort'] not in ('normal', 'fast', 'extra'):
            raise ValueError('Unknown encoding effort.')
        if s['pipeline'] not in ('auto', 'png', 'y4m', 'direct'):
            raise ValueError('Unknown input pipeline.')
        if s['scaleFilter'] not in SCALE_FILTERS:
            raise ValueError('Unknown FFmpeg scaling filter.')
        s['matte'] = color(s['matte']) if s['matte'] else None
        colors = s['fixedColors']
        if isinstance(colors, str):
            colors = re.split(r'[,\s]+', colors.strip()) if colors.strip() else []
        if not isinstance(colors, list) or len(colors) > 255:
            raise ValueError('Provide no more than 255 fixed colors.')
        s['fixedColors'] = [color(c) for c in colors]
        if s['y4mColorOverride'] and s['y4mColorOverride'] not in COLOR_SPACES:
            raise ValueError('Unknown Y4M color matrix override.')
        if not isinstance(s['customFilter'], str) or len(s['customFilter']) > 10000 or '\x00' in s['customFilter']:
            raise ValueError('Invalid FFmpeg filter expression.')
        s['outputName'] = re.sub(r'[<>:"/\\|?*\x00-\x1f]', '_', str(s['outputName']))[:160].strip(' .') or 'animation.gif'
        if not s['outputName'].lower().endswith('.gif'):
            s['outputName'] += '.gif'
        if s['source']['kind'] == 'sequence' and s['fps'] * s['speed'] > 100:
            raise ValueError('PNG sequence playback rate (FPS × speed) must not exceed 100 FPS.')
        return s

    def _pipeline(self, s):
        if s['pipeline'] == 'direct' or s['source']['kind'] == 'direct':
            return 'direct'
        if s['pipeline'] == 'y4m' and s['bounce']:
            raise ValueError('Bounce requires the PNG pipeline; choose Auto or PNG.')
        if s['pipeline'] != 'auto':
            return s['pipeline']
        if s['source']['kind'] == 'sequence' and not self._preprocess(s):
            return 'sequence'
        return 'png'

    @staticmethod
    def _preprocess(s):
        return bool(s['trim']['start'] or s['trim']['end'] is not None or s['crop']['enabled'] or s['reverse'] or s['denoise'] or s['sharpen'] or s['customFilter'])

    def _gifski(self, s, output, inputs, pipeline):
        cmd = [self._tool('gifski'), '--output', str(output), '--quality', str(s['quality']), '--repeat', str(s['repeat'])]
        for k, flag in (('width', '--width'), ('height', '--height'), ('lossyQuality', '--lossy-quality'), ('motionQuality', '--motion-quality')):
            if s[k] is not None:
                cmd += [flag, str(s[k])]
        fps = s['fps'] * s['speed'] if pipeline == 'sequence' else s['fps']
        cmd += ['--fps', f'{fps:g}']
        if pipeline == 'direct' and s['speed'] != 1:
            cmd += ['--fast-forward', f"{s['speed']:g}"]
        if s['effort'] != 'normal':
            cmd += ['--' + s['effort']]
        if s['bounce'] and pipeline in ('png', 'sequence'):
            cmd.append('--bounce')
        if s['noSort'] or pipeline == 'png':
            cmd.append('--no-sort')
        if s['quiet']:
            cmd.append('--quiet')
        if s['matte']:
            cmd += ['--matte', s['matte']]
        for c in s['fixedColors']:
            cmd += ['--fixed-color', c]
        if s['y4mColorOverride'] and pipeline in ('y4m', 'direct'):
            cmd += ['--y4m-color-override', s['y4mColorOverride']]
        return cmd + list(map(str, inputs))

    def _ffmpeg(self, s, source, dest, pipeline, sequence_pattern=None):
        performance = s['performance']
        threads = performance['threads'] or min(os.cpu_count() or 1, 8)
        cmd = [self._tool('ffmpeg'), '-hide_banner', '-nostdin', '-y', '-v', 'warning',
               '-nostats', '-stats_period', '0.5', '-progress', 'pipe:2',
               '-filter_threads', str(threads), '-protocol_whitelist', 'file,pipe']
        if performance['threads']:
            cmd += ['-threads', str(threads)]
        if performance['hardwareDecode'] == 'auto' and not sequence_pattern:
            cmd += ['-hwaccel', 'auto']
        if s['trim']['start']:
            cmd += ['-ss', f"{s['trim']['start']:g}"]
        if s['trim']['end'] is not None:
            cmd += ['-t', f"{s['trim']['end'] - s['trim']['start']:g}"]
        if sequence_pattern:
            cmd += ['-framerate', f"{s.get('_sequenceInputFps', s['fps']):g}", '-start_number', '0', '-i', str(sequence_pattern)]
        else:
            cmd += ['-i', str(source)]
        filters = []
        crop = s['crop']
        if crop['enabled']:
            filters.append(f"crop={crop['width']}:{crop['height']}:{crop['x']}:{crop['y']}")
        filters.append('settb=AVTB')
        filters.append(f"setpts=(PTS-STARTPTS)/{s['speed']:g}")
        if s['reverse']:
            filters.append('reverse')
        # Sequence FPS is playback FPS. Source frame retention is native unless filters require rasterization.
        filters.append(f"fps={s['fps'] * s['speed'] if sequence_pattern else s['fps']:g}")
        if s['denoise']:
            filters.append(f"hqdn3d={s['denoise']:g}")
        if s['width'] or s['height']:
            w = f"min(iw,{s['width']})" if s['width'] else '-1'
            h = f"min(ih,{s['height']})" if s['height'] else '-1'
            filters.append(f"scale='{w}':'{h}':flags={s['scaleFilter']}")
        if s['sharpen']:
            filters.append(f"unsharp=5:5:{s['sharpen']:g}:5:5:0")
        if s['customFilter'].strip():
            filters.append(s['customFilter'].strip())
        cmd += ['-map', '0:v:0', '-an', '-sn', '-dn', '-vf', ','.join(filters)]
        if pipeline == 'png':
            cmd += ['-frames:v', '100001', '-fps_mode', 'passthrough', '-pix_fmt', 'rgba', '-threads', str(threads), '-start_number', '0', str(dest)]
        else:
            cmd += ['-pix_fmt', 'yuv444p', '-strict', '-1', '-f', 'yuv4mpegpipe', str(dest)]
        return cmd

    def plan(self, settings):
        s = self._normalize(settings)
        paths = self._paths(s['source'])
        p = self._pipeline(s)
        warnings = []
        if p == 'direct':
            if self._preprocess(s):
                raise ValueError('Direct Gifski input cannot apply FFmpeg trim, crop, reverse or filters. Choose Auto/PNG instead.')
            if s['bounce']:
                raise ValueError('Bounce is supported only for PNG sequences. Choose Auto/PNG instead.')
        if p == 'y4m':
            warnings.append('Y4M has no alpha channel. Use PNG to preserve transparency.')
        if s['fps'] > 50:
            warnings.append('GIF delays are clamped to at least 20 ms by Gifski. Use 50 FPS or lower for reliable playback.')
        if s['effort'] == 'extra':
            warnings.append('Extra effort usually produces slightly better quality and a larger file, not a smaller file.')
        if s['width'] and s['height']:
            warnings.append('Explicit width and height can change the aspect ratio. Leave one blank to preserve it.')
        if s['width'] is None and s['height'] is None:
            warnings.append('Gifski applies its default automatic size limit when both dimensions are blank.')
        if s['matte']:
            warnings.append('Matte blends semitransparent pixels only; fully transparent pixels remain transparent.')
        if s['y4mColorOverride'] and p not in ('y4m', 'direct'):
            warnings.append('The Y4M color override does not apply to PNG input.')
        if s['source']['kind'] == 'sequence' and p == 'sequence':
            warnings.append('PNG sequences use Gifski native resizing; the FFmpeg scaling kernel applies only when PNG/FFmpeg preprocessing is selected.')
        out = '<job>/' + s['outputName']
        if p in ('direct', 'sequence'):
            inputs = paths if p == 'sequence' else [paths[0]]
            commands = [self._gifski(s, out, inputs, p)]
        else:
            seq = '<job>/input/f%06d.png' if s['source']['kind'] == 'sequence' else None
            ff = self._ffmpeg(s, paths[0], '<job>/frames/f%06d.png' if p == 'png' else '-', p, seq)
            gs = self._gifski(s, out, ['<numbered PNG frames>'] if p == 'png' else ['-'], p)
            if seq:
                gs[gs.index('--fps') + 1] = f"{s['fps'] * s['speed']:g}"
            commands = [ff, gs]
        return {'pipeline': p, 'commands': commands, 'warnings': warnings, 'settings': s,
                'notes': ['Commands are executed as argument arrays, never through a shell.', 'Smart searches use sampled estimates to guide complete, size-verified exports.']}

    def submit(self, settings, mode='encode'):
        if mode not in ('encode', 'sweep', 'optimize', 'estimate', 'discord', 'smart'):
            raise ValueError('Unknown job mode.')
        # Adaptive exports always use FFmpeg preprocessing so frame rate changes
        # resample the source rather than silently changing its duration.
        if mode in ('discord', 'smart'):
            if not isinstance(settings, dict):
                raise ValueError('Settings must be a JSON object.')
            settings = copy.deepcopy(settings)
            settings['pipeline'] = 'png'
            if isinstance(settings.get('source'), dict) and settings['source'].get('kind') == 'direct':
                settings['source']['kind'] = 'video'
        plan = self.plan(settings)
        s = plan['settings']
        if mode in ('smart', 'discord'):
            self._validate_smart_pins(s)
            managed_warnings = ('Gifski applies its default automatic size limit',
                                'Explicit width and height', 'GIF delays', 'Extra effort')
            plan['warnings'] = [warning for warning in plan['warnings'] if not warning.startswith(managed_warnings)]
        status = self.status()
        required = ['gifski'] + (['ffmpeg'] if plan['pipeline'] in ('png', 'y4m') else [])
        missing = [x for x in required if not status['tools'][x]['available']]
        if missing:
            raise ValueError('Missing executable(s): ' + ', '.join(missing) + '. Set the paths in Settings or place the executables in bin/.')
        if mode == 'sweep':
            self._sweep_settings(s)
        if mode == 'optimize':
            o = s['optimize']
            for k, lo, hi, integer in [('targetMiB', .001, 100000, False), ('minQuality', 1, 100, True), ('maxQuality', 1, 100, True), ('minWidth', 1, 65535, True), ('minFps', .001, 100, False), ('maxTrials', 1, 80, True)]:
                o[k] = number(o[k], k, lo, hi, integer=integer)
            if o['minQuality'] > o['maxQuality']:
                raise ValueError('Minimum quality must not exceed maximum quality.')
            if mode == 'optimize' and (s['lossyQuality'] is not None or s['motionQuality'] is not None):
                plan['warnings'].append('Quality overrides remain fixed during target search; this may reduce the effect of changing overall quality.')
        jid = uuid.uuid4().hex
        job = {'id': jid, 'status': 'queued', 'stage': 'Queued', 'progress': 0, 'created': time.time(), 'settings': s, 'mode': mode, 'results': [], 'logs': [], 'error': None, 'commands': [], 'warnings': plan['warnings'], 'bestIndex': None}
        if mode in ('discord', 'smart'):
            job['targetBytes'] = 20_000_000 if mode == 'discord' else round(s['smart']['targetMB'] * 1_000_000)
        (self.outputs / jid).mkdir()
        with self.lock:
            self.jobs[jid] = job
            self._persist(job)
        self._queue.put(jid)
        return copy.deepcopy(job)

    def list_jobs(self):
        with self.lock:
            return copy.deepcopy(sorted(self.jobs.values(), key=lambda j: j['created'], reverse=True))

    @staticmethod
    def _thumbnail_source(job):
        source = job.get('settings', {}).get('source', {})
        metadata = job.get('sourceMetadata', {})
        raw = metadata.get('path') or next(iter(source.get('paths') or []), '') or source.get('path')
        if not isinstance(raw, str) or len(raw) > 4096 or '\x00' in raw:
            return None
        # Apply the same paste-friendly spelling cleanup as _paths before
        # checking network paths, so quoted UNC paths never reach filesystem IO.
        raw = raw.strip().strip('"')
        if (not raw or raw != raw.strip().strip('"') or raw.startswith(('//', '\\\\')) or
                re.match(r'^[a-zA-Z][a-zA-Z0-9+.-]*://', raw)):
            return None
        seek = job.get('settings', {}).get('trim', {}).get('start', 0)
        if source.get('kind') == 'sequence' or Path(raw).suffix.lower() in ('.png', '.jpg', '.jpeg', '.bmp', '.tiff'):
            seek = 0
        if isinstance(seek, bool) or not isinstance(seek, (int, float)) or not math.isfinite(seek) or not 0 <= seek <= 8640000:
            return None
        return raw, seek

    @staticmethod
    def _thumbnail_index(job):
        if job.get('status') not in ('completed', 'cancelled', 'failed', 'interrupted'):
            return None
        results = job.get('results', [])
        valid = []
        for index, result in enumerate(results):
            if not isinstance(result, dict) or result.get('sample'):
                continue
            filename = result.get('filename')
            if (isinstance(filename, str) and filename.lower().endswith('.gif') and
                    not any(char in filename for char in ('/', '\\', '\x00'))):
                valid.append(index)
        chosen = job.get('bestIndex')
        if isinstance(chosen, int) and not isinstance(chosen, bool) and chosen in valid:
            return chosen
        return valid[0] if valid else 'source' if Engine._thumbnail_source(job) else None

    def thumbnail_input(self, jid):
        """A narrow snapshot of a terminal job's result, or its local source."""
        if not isinstance(jid, str) or not re.fullmatch(r'[a-zA-Z0-9_-]{1,64}', jid):
            return None
        with self.lock:
            job = self.jobs.get(jid)
            index = self._thumbnail_index(job) if job else None
            if index is None:
                return None
            status = job['status']
            if index == 'source':
                source = self._thumbnail_source(job)
            else:
                source = None
                filename = job['results'][index]['filename']
        if source is not None:
            try:
                path = self._paths({'kind': 'video', 'path': source[0]})[0]
            except (ValueError, OSError, RuntimeError):
                return None
            return {'id': jid, 'status': status, 'index': 'source', 'path': path, 'seek': source[1]}
        if index != 'source':
            try:
                root = self.outputs.resolve()
                directory = (root / jid).resolve()
                path = (directory / filename).resolve()
            except (OSError, RuntimeError):
                # Deleted folders and link loops are a missing preview, not a
                # reason for history polling to fail.
                return None
            if directory != root / jid or path.parent != directory:
                return None
            return {'id': jid, 'status': status, 'index': index, 'path': path, 'seek': 0}
        return None

    def list_job_summaries(self):
        with self.lock:
            summaries = []
            for job in sorted(self.jobs.values(), key=lambda j: j['created'], reverse=True):
                item = {key: job.get(key) for key in ('id', 'status', 'mode', 'created', 'started', 'finished',
                                                     'stage', 'progress', 'bestIndex', 'error')}
                source = job.get('settings', {}).get('source', {})
                name = source.get('path') or next(iter(source.get('paths') or []), '')
                item.update(sourceName=re.split(r'[/\\]', name)[-1], resultCount=len(job.get('results', [])),
                            thumbnailIndex=self._thumbnail_index(job))
                summaries.append(item)
            return summaries

    def has_active_jobs(self):
        with self.lock:
            return any(job.get('status') in ('queued', 'running') for job in self.jobs.values())

    def get_job(self, jid):
        with self.lock:
            if jid not in self.jobs:
                raise KeyError('Job not found.')
            return copy.deepcopy(self.jobs[jid])

    def cancel(self, jid):
        with self.lock:
            if jid not in self.jobs:
                raise KeyError('Job not found.')
            if self.jobs[jid]['status'] not in ('queued', 'running'):
                return copy.deepcopy(self.jobs[jid])
            self.cancelled.add(jid)
            for p in list(self.active.get(jid, [])):
                try:
                    p.terminate()
                except OSError:
                    pass
            self.jobs[jid]['stage'] = 'Cancelling'
            return copy.deepcopy(self.jobs[jid])

    def file_for(self, jid, index=0):
        job = self.get_job(jid)
        if not isinstance(index, int) or index < 0 or index >= len(job['results']):
            raise KeyError('Result not found.')
        return self.outputs / jid / job['results'][index]['filename']

    def _persist(self, job):
        self._write_json(self.outputs / job['id'] / 'job.json', job)

    def _log(self, jid, line):
        line = re.sub(r'\x1b(?:\[[0-?]*[ -/]*[@-~]|\][^\x07]*(?:\x07|\x1b\\))', '', str(line))
        line = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', line).strip()
        if not line:
            return
        with self.lock:
            self.jobs[jid].setdefault('logs', []).append(time.strftime('[%H:%M:%S] ') + line[-4000:])
            self.jobs[jid]['logs'] = self.jobs[jid]['logs'][-250:]

    def _check_cancel(self, jid):
        if jid in self.cancelled:
            raise Cancelled()

    def _track(self, jid, process):
        with self.lock:
            self.active.setdefault(jid, []).append(process)

    @staticmethod
    def _stop_process(process):
        if process.poll() is None:
            try:
                process.terminate()
                process.wait(timeout=2)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=3)
            except ProcessLookupError:
                pass

    def _start_process(self, jid, process, label):
        now = time.time()
        with self.lock:
            record = {'tool': label, 'pid': process.pid, 'status': 'running',
                      'startedAt': now, 'updatedAt': now, 'lastOutputAt': now,
                      'elapsedSeconds': 0, 'frame': None, 'fps': None, 'speed': None,
                      'outTimeSeconds': None, 'expectedSeconds': self.jobs[jid].get('stageExpectedSeconds'),
                      'percent': None, 'stalled': False}
            self.jobs[jid]['process'] = record
        self._log(jid, f'{label} started (process {process.pid}).')
        return record

    def _heartbeat(self, jid, record):
        now = time.time()
        with self.lock:
            record.update(updatedAt=now, elapsedSeconds=round(now - record['startedAt'], 1),
                          stalled=now - record['lastOutputAt'] >= 30)
        return now

    def _read_output(self, jid, pipe, label, record, outputs, ffmpeg=False, frame_progress=False):
        """Drain every pipe continuously, including CR-only terminal progress.

        A bounded incremental decoder avoids readline waiting forever on carriage
        returns, accumulating huge unterminated output, or splitting UTF-8 bytes.
        FFmpeg progress uses stderr so Y4M stdout remains binary and untouched.
        """
        decoder = codecs.getincrementaldecoder('utf-8')('replace')
        pending = ''
        last_redraw, last_line = 0, None
        progress_keys = {'frame', 'fps', 'bitrate', 'total_size', 'out_time_us', 'out_time_ms',
                         'out_time', 'dup_frames', 'drop_frames', 'speed', 'progress'}
        def accept(raw):
            nonlocal last_redraw, last_line
            line = re.sub(r'\x1b\[[0-?]*[ -/]*[@-~]', '', raw).strip()
            if not line:
                return
            with self.lock:
                record['lastOutputAt'] = time.time()
            key, separator, value = line.partition('=')
            if ffmpeg and separator and (key in progress_keys or re.fullmatch(r'stream_\d+_\d+_q', key)):
                with self.lock:
                    try:
                        if key in ('frame', 'fps', 'out_time_us', 'out_time_ms'):
                            value_number = float(value)
                            if math.isfinite(value_number) and value_number >= 0:
                                if key == 'frame':
                                    record['frame'] = int(value_number)
                                elif key == 'fps':
                                    record['fps'] = value_number
                                else:
                                    record['outTimeSeconds'] = value_number / 1_000_000
                        elif key == 'speed' and re.fullmatch(r'\s*\d+(?:\.\d+)?x', value):
                            record['speed'] = value.strip()
                        elif key == 'progress':
                            duration = record.get('expectedSeconds')
                            position = record.get('outTimeSeconds')
                            if duration and position is not None:
                                record['percent'] = round(min(99.9, max(0, position / duration * 100)), 1)
                    except (TypeError, ValueError, OverflowError):
                        pass
                return
            redraw = re.search(r'\bFrame\s+(\d+)\s*/\s*(\d+)', line) if label == 'Gifski' else None
            if redraw:
                current, total = map(int, redraw.groups())
                if frame_progress and total > 0:
                    with self.lock:
                        record.update(frame=current, totalFrames=total,
                                      percent=round(min(99.9, current / total * 100), 1))
                now = time.monotonic()
                if line == last_line or (now - last_redraw < .5 and current < total):
                    return
                last_redraw, last_line = now, line
            line = re.sub(r'[\x00-\x08\x0b-\x1f\x7f]', '', line)[-4000:]
            with self.lock:
                outputs.append(label + ': ' + line)
                del outputs[:-100]
            self._log(jid, label + ': ' + line)
        try:
            while True:
                raw = pipe.read1(4096) if hasattr(pipe, 'read1') else pipe.read(4096)
                if not raw:
                    pending += decoder.decode(b'', final=True)
                    break
                pending += decoder.decode(raw)
                pieces = re.split(r'[\r\n]', pending)
                pending = pieces.pop()
                for line in pieces:
                    accept(line)
                if len(pending) > 4096:
                    accept(pending[:4096])
                    pending = pending[4096:]
            if pending:
                accept(pending)
        except (OSError, ValueError) as error:
            # A pipe can be closed by process teardown. Keep other pipes draining.
            self._log(jid, f'{label} output stream closed: {error}')
        finally:
            pipe.close()

    def _finish_process(self, jid, process, record):
        with self.lock:
            self._heartbeat(jid, record)
            record.update(status='cancelled' if jid in self.cancelled else 'failed' if process.returncode else 'completed',
                          exitCode=process.returncode, stalled=False)
            if not process.returncode:
                record['percent'] = 100
            if process in self.active.get(jid, []):
                self.active[jid].remove(process)
        self._log(jid, f"{record['tool']} exited with code {process.returncode} after {record['elapsedSeconds']:g}s.")

    def _capture_probe(self, jid, command, timeout=30):
        """Capture probe JSON without logging it; job cancellation reaps the child.

        communicate() drains stdout and stderr concurrently, including on Windows.
        Retrying it after a short timeout preserves captured output and keeps the
        cancellation interval independent of the overall inspection timeout.
        """
        self._check_cancel(jid)
        with self.lock:
            self.jobs[jid].setdefault('commands', []).append(command)
        process = subprocess.Popen(command, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                   stderr=subprocess.PIPE, text=True, errors='replace')
        self._track(jid, process)
        record = self._start_process(jid, process, 'FFprobe')
        deadline = time.monotonic() + timeout
        try:
            while True:
                self._check_cancel(jid)
                remaining = deadline - time.monotonic()
                if remaining <= 0:
                    raise subprocess.TimeoutExpired(command, timeout)
                self._heartbeat(jid, record)
                try:
                    stdout, stderr = process.communicate(timeout=min(.1, remaining))
                    self._check_cancel(jid)
                    return subprocess.CompletedProcess(command, process.returncode, stdout, stderr)
                except subprocess.TimeoutExpired:
                    continue
        finally:
            self._stop_process(process)
            # Finish communicate's pipe-reader threads and close their handles
            # before Windows cleanup or the next queued job can begin.
            process.communicate(timeout=3)
            self._finish_process(jid, process, record)

    def _run(self, jid, cmd, cwd=None, stdin=None, stdout=None):
        self._check_cancel(jid)
        with self.lock:
            self.jobs[jid].setdefault('commands', []).append(cmd)
        p = subprocess.Popen(cmd, cwd=cwd, stdin=stdin if stdin is not None else subprocess.DEVNULL,
                             stdout=stdout if stdout is not None else subprocess.PIPE, stderr=subprocess.PIPE)
        self._track(jid, p)
        label = 'FFmpeg' if '-progress' in cmd else 'Gifski' if '--output' in cmd else Path(cmd[0]).name
        record = self._start_process(jid, p, label)
        outputs, threads = [], []
        try:
            for pipe in [p.stderr] + ([p.stdout] if stdout is None else []):
                thread = threading.Thread(target=self._read_output,
                                          args=(jid, pipe, label, record, outputs, label == 'FFmpeg',
                                                label == 'Gifski' and str(cmd[-1]).lower().endswith('.png')), daemon=True)
                thread.start()
                threads.append(thread)
            last_log = time.time()
            while p.poll() is None:
                if jid in self.cancelled:
                    self._stop_process(p)
                    break
                now = self._heartbeat(jid, record)
                if now - last_log >= 10:
                    details = f", {record['frame']:,} frames" if record['frame'] is not None else ''
                    if record['speed']:
                        details += f", {record['speed']} playback speed"
                    self._log(jid, f"{label} running for {record['elapsedSeconds']:g}s{details}.")
                    last_log = now
                time.sleep(.1)
            p.wait()
        finally:
            self._stop_process(p)
            for thread in threads:
                thread.join(timeout=3)
            self._finish_process(jid, p, record)
        self._check_cancel(jid)
        if p.returncode:
            raise RuntimeError(f'{label} exited with code {p.returncode}:\n' + '\n'.join(outputs[-12:]))

    def _run_pipe(self, jid, producer, consumer):
        self._check_cancel(jid)
        with self.lock:
            self.jobs[jid].setdefault('commands', []).extend([producer, consumer])
        ff = subprocess.Popen(producer, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._track(jid, ff)
        ff_record = self._start_process(jid, ff, 'FFmpeg')
        gs, gs_record = None, None
        outputs, threads = [], []
        try:
            gs = subprocess.Popen(consumer, stdin=ff.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
            self._track(jid, gs)
            gs_record = self._start_process(jid, gs, 'Gifski')
            ff.stdout.close()
            with self.lock:
                self.jobs[jid]['process'] = ff_record
            for pipe, label, record in ((ff.stderr, 'FFmpeg', ff_record),
                                        (gs.stdout, 'Gifski', gs_record), (gs.stderr, 'Gifski', gs_record)):
                thread = threading.Thread(target=self._read_output,
                                          args=(jid, pipe, label, record, outputs, label == 'FFmpeg'), daemon=True)
                thread.start()
                threads.append(thread)
            last_log = time.time()
            consumer_finished_at = None
            while ff.poll() is None or gs.poll() is None:
                # A failed producer must also terminate a consumer waiting for
                # more work. Conversely, stop a producer whose consumer exited.
                if gs.poll() == 0 and consumer_finished_at is None:
                    consumer_finished_at = time.monotonic()
                orphaned_producer = consumer_finished_at is not None and time.monotonic() - consumer_finished_at >= 2
                if jid in self.cancelled or ff.poll() not in (None, 0) or gs.poll() not in (None, 0) or orphaned_producer:
                    self._stop_process(ff)
                    self._stop_process(gs)
                    break
                current = ff_record if ff.poll() is None else gs_record
                with self.lock:
                    self.jobs[jid]['process'] = current
                now = self._heartbeat(jid, current)
                if now - last_log >= 10:
                    self._log(jid, f"{current['tool']} running for {current['elapsedSeconds']:g}s.")
                    last_log = now
                time.sleep(.1)
            ff.wait()
            gs.wait()
        finally:
            for proc in (ff, gs):
                if proc is not None:
                    self._stop_process(proc)
            for thread in threads:
                thread.join(timeout=3)
            for proc, record in ((ff, ff_record), (gs, gs_record)):
                if proc is not None:
                    # Also closes handles when consumer creation failed before
                    # reader threads were started (notably Windows file handles).
                    for pipe in (proc.stdout, proc.stderr):
                        if pipe is not None and not pipe.closed and not any(t.is_alive() for t in threads):
                            pipe.close()
                    self._finish_process(jid, proc, record)
        self._check_cancel(jid)
        if ff.returncode or gs.returncode:
            raise RuntimeError(f'Y4M pipeline failed (FFmpeg {ff.returncode}, Gifski {gs.returncode}):\n' + '\n'.join(outputs[-12:]))

    def _run_ffmpeg(self, jid, command, frames=None, consumer=None):
        def software_command():
            index = command.index('-hwaccel')
            return command[:index] + command[index + 2:]
        if '-hwaccel' in command and self.jobs[jid].get('hardwareDecodeFallback'):
            command = software_command()
        try:
            if consumer is None:
                self._run(jid, command)
            else:
                self._run_pipe(jid, command, consumer)
        except RuntimeError:
            self._check_cancel(jid)
            if '-hwaccel' not in command:
                raise
            self._log(jid, 'The hardware-assisted attempt failed. Retrying with CPU decoding.')
            with self.lock:
                self.jobs[jid]['hardwareDecodeFallback'] = True
            if frames is not None:
                for path in frames.glob('*.png'):
                    path.unlink()
            if consumer is None:
                self._run(jid, software_command())
            else:
                self._run_pipe(jid, software_command(), consumer)

    def _encode(self, jid, s, index, total, record=True):
        self._check_cancel(jid)
        directory = self.outputs / jid
        token = str(index) if record else 'sample-' + uuid.uuid4().hex[:10]
        work = directory / f'work-{token}'
        work.mkdir()
        base = Path(s['outputName']).stem
        output = directory / (f'{index + 1:02d}-{base}.gif' if record else f'{token}.gif')
        paths = self._paths(s['source'])
        pipeline = self._pipeline(s)
        started = time.monotonic()
        completed = False
        with self.lock:
            duration = self.jobs[jid].get('sourceMetadata', {}).get('duration')
            if s['source']['kind'] == 'sequence':
                duration = len(paths) / s.get('_sequenceInputFps', s['fps'])
            if s['trim']['end'] is not None:
                duration = min(duration, s['trim']['end']) if duration else s['trim']['end']
            self.jobs[jid]['stageExpectedSeconds'] = max(0, duration - s['trim']['start']) / s['speed'] if duration else None
            if record:
                adaptive = self.jobs[jid]['mode'] in ('smart', 'discord')
                progress = 40 + index / total * 55 if adaptive else index / total * 100
                self.jobs[jid].update(stage=f'Verifying full export {index + 1} of at most {total}', progress=round(progress, 1))
        try:
            if pipeline in ('sequence', 'direct'):
                cmd = self._gifski(s, output, paths if pipeline == 'sequence' else paths[:1], pipeline)
                arg_length = len(subprocess.list2cmdline(cmd))
                if pipeline == 'sequence' and (arg_length > (28000 if os.name == 'nt' else 500000)):
                    staged = work / 'input'
                    staged.mkdir()
                    if not s['noSort']:
                        paths.sort(key=natural_key)
                    names = []
                    for i, path in enumerate(paths):
                        self._check_cancel(jid)
                        dst = staged / f'f{i:06d}.png'
                        try:
                            os.link(path, dst)
                        except OSError:
                            shutil.copyfile(path, dst)
                        names.append(dst.name)
                    cmd = self._gifski(s, output, ['*.png'] if os.name == 'nt' else names, pipeline)
                    self._run(jid, cmd, cwd=staged)
                else:
                    self._run(jid, cmd)
            else:
                sequence = None
                if s['source']['kind'] == 'sequence':
                    staged = work / 'input'
                    staged.mkdir()
                    if not s['noSort']:
                        paths.sort(key=natural_key)
                    for i, path in enumerate(paths):
                        self._check_cancel(jid)
                        dst = staged / f'f{i:06d}.png'
                        try:
                            os.link(path, dst)
                        except OSError:
                            shutil.copyfile(path, dst)
                    sequence = staged / 'f%06d.png'
                if pipeline == 'png':
                    frame_key = self._recipe_key(s, frames_only=True)
                    cached = self._frame_caches.setdefault(jid, {})
                    frames = cached.get(frame_key, {}).get('path')
                    if frames is None or not frames.is_dir():
                        frames = work / 'frames'
                        frames.mkdir()
                        cmd = self._ffmpeg(s, paths[0], frames / 'f%06d.png', 'png', sequence)
                        with self.lock:
                            self.jobs[jid]['stage'] = ('Sampling source' if not record else f'Preparing full export {index + 1}')
                        self._run_ffmpeg(jid, cmd, frames=frames)
                        inputs = sorted(p.name for p in frames.glob('*.png'))
                        if len(inputs) < 2:
                            raise ValueError('The selected clip produced fewer than two frames. Increase its duration or FPS.')
                        if len(inputs) > 100000:
                            raise ValueError('The clip produced more than 100,000 frames. Shorten it or reduce FPS.')
                        cache_bytes = sum((frames / name).stat().st_size for name in inputs)
                        # Cache quality-independent preprocessing within a job only.
                        # Bound retained compressed PNG data and entries; the worker
                        # removes the entire cache on completion/cancellation/failure.
                        if cache_bytes <= 256 * 1024 * 1024:
                            while cached and (len(cached) >= 6 or sum(x['bytes'] for x in cached.values()) + cache_bytes > 512 * 1024 * 1024):
                                oldest = next(iter(cached))
                                shutil.rmtree(cached.pop(oldest)['path'], ignore_errors=True)
                            destination = directory / 'frame-cache' / frame_key
                            destination.parent.mkdir(exist_ok=True)
                            frames.rename(destination)
                            frames = destination
                            cached[frame_key] = {'path': frames, 'bytes': cache_bytes}
                    else:
                        inputs = sorted(p.name for p in frames.glob('*.png'))
                        with self.lock:
                            stats = self.jobs[jid].setdefault('search', {})
                            stats['frameCacheHits'] = stats.get('frameCacheHits', 0) + 1
                    actual = copy.deepcopy(s)
                    if sequence:
                        actual['fps'] *= actual['speed']
                    cmd = self._gifski(actual, output, ['*.png'] if os.name == 'nt' else inputs, 'png')
                    with self.lock:
                        self.jobs[jid]['stage'] = ('Measuring short sample' if not record else f'Compressing full export {index + 1}')
                    self._run(jid, cmd, cwd=frames)
                else:
                    ff = self._ffmpeg(s, paths[0], '-', 'y4m', sequence)
                    actual = copy.deepcopy(s)
                    if sequence:
                        actual['fps'] *= actual['speed']
                    gs = self._gifski(actual, output, ['-'], 'y4m')
                    self._run_ffmpeg(jid, ff, consumer=gs)
            w, h = gif_dimensions(output)
            result = {'index': index, 'filename': output.name, 'bytes': output.stat().st_size, 'width': w, 'height': h,
                      'fps': s['fps'] * s['speed'] if s['source']['kind'] == 'sequence' else s['fps'], 'quality': s['quality'],
                      'settings': copy.deepcopy(s), 'downloadUrl': f'/api/files/{jid}/{index}?download=1',
                      'previewUrl': f'/api/files/{jid}/{index}', 'fit': None, 'elapsed': round(time.monotonic() - started, 2)}
            if record and self.jobs[jid]['mode'] in ('optimize', 'discord', 'smart'):
                target = self.jobs[jid].get('targetBytes', s['optimize']['targetMiB'] * 1048576)
                result['fit'] = result['bytes'] <= target
            if not record:
                result['sample'] = True
            elif self.jobs[jid]['mode'] == 'estimate':
                result['sample'] = self.jobs[jid].get('sampling', True)
            if record:
                with self.lock:
                    self.jobs[jid]['results'].append(result)
                    self._persist(self.jobs[jid])
            completed = True
            return result
        finally:
            shutil.rmtree(work, ignore_errors=True)
            if not completed or not record:
                output.unlink(missing_ok=True)

    def _sweep_settings(self, s):
        sw = s['sweep']
        axes = []
        for key, low, high, integer in [('qualities', 1, 100, True), ('widths', 1, 65535, True), ('fps', .001, 100, False)]:
            items = sw.get(key)
            if not isinstance(items, list) or not items or len(items) > 20:
                raise ValueError(f'Sweep {key} must be a list of 1–20 numbers.')
            axes.append(list(dict.fromkeys(number(x, key, low, high, integer=integer) for x in items)))
        for key, setting in [('motionQualities', 'motionQuality'), ('lossyQualities', 'lossyQuality')]:
            items = sw.get(key) or [s[setting]]
            if not isinstance(items, list) or len(items) > 20:
                raise ValueError(f'Sweep {key} must be a list of up to 20 numbers.')
            axes.append(list(dict.fromkeys(number(x, key, 1, 100, integer=True, nullable=True) for x in items)))
        if math.prod(len(a) for a in axes) > 60:
            raise ValueError('Limit the comparison grid to 60 candidates or fewer.')
        out = []
        for q, w, fps, motion, lossy in itertools.product(*axes):
            item = copy.deepcopy(s)
            item.update(quality=q, width=w, fps=fps, motionQuality=motion, lossyQuality=lossy)
            out.append(self._normalize(item))
        return out

    def _optimize(self, jid, s):
        o = s['optimize']
        target = o['targetMiB'] * 1048576
        # Retain resolution first, then FPS, then the highest tested quality that fits.
        base_width = s['width']
        if base_width is None:
            base_width = (self.jobs[jid].get('sourceMetadata') or self.probe(s['source'], jid=jid))['width']
        widths = [base_width]
        if o['allowResize'] and o['minWidth'] < base_width:
            widths += [max(o['minWidth'], round(base_width * x)) for x in (.85, .70, .55)] + [o['minWidth']]
        rates = [s['fps']]
        if o['allowFps'] and o['minFps'] < s['fps']:
            rates += [max(o['minFps'], round(s['fps'] * .75, 3)), o['minFps']]
        widths = list(dict.fromkeys(widths))
        rates = list(dict.fromkeys(rates))
        count = 0
        for width, fps in itertools.product(widths, rates):
            if count >= o['maxTrials']:
                break
            low, high = o['minQuality'], o['maxQuality']
            q = high
            best_here = None
            tested = set()
            while count < o['maxTrials'] and low <= high and q not in tested:
                tested.add(q)
                trial = copy.deepcopy(s)
                trial.update(width=width, fps=fps, quality=q)
                if s['height'] and s['width']:
                    trial['height'] = max(1, round(s['height'] * width / s['width']))
                result = self._encode(jid, trial, count, o['maxTrials'])
                count += 1
                if result['bytes'] <= target:
                    best_here = result
                    low = q + 1
                else:
                    high = q - 1
                if q == o['maxQuality'] and best_here:
                    break
                if best_here is None and o['minQuality'] not in tested:
                    q = o['minQuality']
                else:
                    q = (low + high) // 2
            if best_here:
                with self.lock:
                    self.jobs[jid]['bestIndex'] = best_here['index']
                    self.jobs[jid]['searchNote'] = 'Best tested fit at the first feasible resolution/FPS tier. Search is bounded; GIF size is not strictly monotonic with quality.'
                return
        results = self.jobs[jid]['results']
        if results:
            with self.lock:
                self.jobs[jid]['bestIndex'] = min(results, key=lambda r: r['bytes'])['index']
                self.jobs[jid]['searchNote'] = 'No tested candidate met the target. Smallest measured result selected; lower the size/FPS limits or increase the trial budget.'

    def _source_interval(self, s, metadata):
        """Return the bounded source interval in seconds, before speed/bounce."""
        duration = (len(self._paths(s['source'])) / s.get('_sequenceInputFps', s['fps'])
                    if s['source']['kind'] == 'sequence' else metadata.get('duration'))
        if not duration and s['trim']['end'] is not None:
            duration = s['trim']['end']
        if not duration or not math.isfinite(duration) or duration <= 0:
            raise ValueError('The source duration is unknown. Enter a finite trim end before estimating.')
        start = s['trim']['start']
        end = min(s['trim']['end'], duration) if s['trim']['end'] is not None else duration
        if end <= start:
            raise ValueError('Trim start is beyond the end of the source.')
        return start, end

    def _recipe_key(self, s, frames_only=False):
        """Fingerprint recipe, source contents' metadata, and executable selection."""
        values = copy.deepcopy(s)
        for key in ('sweep', 'optimize', 'smart', 'outputName'):
            values.pop(key, None)
        if frames_only:
            for key in ('quality', 'motionQuality', 'lossyQuality', 'effort', 'repeat',
                        'bounce', 'quiet', 'matte', 'fixedColors', 'y4mColorOverride'):
                values.pop(key, None)
        signatures = []
        for path in self._paths(s['source']):
            stat = path.stat()
            signatures.append((str(path), stat.st_size, stat.st_mtime_ns))
        executables = []
        for name in ('ffmpeg', 'gifski'):
            path = Path(self._tool(name))
            try:
                stat = path.stat()
                executables.append((str(path), stat.st_size, stat.st_mtime_ns))
            except OSError:
                executables.append((str(path), None, None))
        raw = json.dumps([values, signatures, executables], sort_keys=True, separators=(',', ':'), allow_nan=False)
        return hashlib.sha256(raw.encode()).hexdigest()

    def _sample_summary(self, jid, s, metadata, cache=True):
        """Three short, separated samples; no sample is presented as a size fit."""
        self._check_cancel(jid)
        key = self._recipe_key(s)
        cached = self._estimate_cache.get(key) if cache else None
        if cached and time.monotonic() - cached[0] < 300:
            with self.lock:
                stats = self.jobs[jid].setdefault('search', {})
                stats['cacheHits'] = stats.get('cacheHits', 0) + 1
            return dict(copy.deepcopy(cached[1]), cacheHit=True)
        start, end = self._source_interval(s, metadata)
        duration = end - start
        factor = 2 if s['bounce'] else 1
        output_seconds = duration / s['speed'] * factor
        effective_fps = s['fps'] * s['speed'] if s['source']['kind'] == 'sequence' else s['fps']
        # Four frames avoid tiny-fragment overhead; half a second is enough for
        # first-pass screening. Never sample so sparsely that GIF timing vanishes.
        span = max(.5 * s['speed'] / factor, 4 * s['speed'] / effective_fps)
        if span / s['speed'] > 6:
            raise ValueError('Frame rate is too low for quick sampling. Use a full export or a higher frame rate.')
        if duration <= span * 3:
            samples = [(start, end)]
        else:
            samples = [(start, start + span), ((start + end - span) / 2, (start + end + span) / 2), (end - span, end)]
        measured = []
        for a, b in samples:
            self._check_cancel(jid)
            trial = copy.deepcopy(s)
            if s['source']['kind'] == 'sequence':
                paths = self._paths(s['source'])
                if not s['noSort']:
                    paths.sort(key=natural_key)
                input_rate = s.get('_sequenceInputFps', s['fps'])
                first = max(0, int(a * input_rate))
                last = min(len(paths), max(first + 2, math.ceil(b * input_rate)))
                trial['source'] = {'kind': 'sequence', 'paths': list(map(str, paths[first:last]))}
                trial['_sequenceInputFps'] = input_rate
                trial['trim'] = {'start': 0, 'end': None}
                a, b = first / input_rate, last / input_rate
            else:
                trial['trim'] = {'start': a, 'end': b}
            result = self._encode(jid, trial, 0, 1, record=False)
            sample_seconds = (b - a) / s['speed'] * factor
            measured.append((result['bytes'], sample_seconds))
        rates = [size / seconds for size, seconds in measured]
        estimate = round(sum(size for size, _ in measured) / sum(seconds for _, seconds in measured) * output_seconds)
        summary = {'estimatedBytes': estimate,
                   'lowerBytes': round(min(rates) * output_seconds * .6),
                   'upperBytes': round(max(rates) * output_seconds * 1.6),
                   'exact': False, 'sampleCount': len(samples), 'cacheHit': False,
                   'sampledSeconds': round(sum(seconds for _, seconds in measured), 3),
                   'outputSeconds': round(output_seconds, 3),
                   'note': 'Fast estimate from short separated samples. The range is heuristic; unseen scenes, frame reuse and filters may put the final size outside it. A full export verifies the limit.'}
        if cache:
            self._estimate_cache[key] = (time.monotonic(), copy.deepcopy(summary))
            while len(self._estimate_cache) > 48:
                self._estimate_cache.pop(next(iter(self._estimate_cache)))
        return summary

    def _estimate(self, jid, s):
        metadata = self.jobs[jid].get('sourceMetadata') or self.probe(s['source'], jid=jid)
        with self.lock:
            self.jobs[jid]['sourceMetadata'] = metadata
        if not metadata.get('duration') and s['trim']['end'] is not None:
            metadata['duration'] = s['trim']['end']
        start, end = self._source_interval(s, metadata)
        output_seconds = (end - start) / s['speed'] * (2 if s['bounce'] else 1)
        exact = output_seconds <= 2
        if self._pipeline(s) == 'direct' and not exact:
            raise ValueError('Quick estimation of long native inputs requires Auto, PNG or Y4M processing.')
        with self.lock:
            self.jobs[jid]['sampling'] = not exact
        if exact:
            result = self._encode(jid, s, 0, 1)
            size = result['bytes']
            estimate = {'estimatedBytes': size, 'lowerBytes': size, 'upperBytes': size,
                        'exact': True, 'sampleCount': 1, 'cacheHit': False,
                        'sampledSeconds': round(output_seconds, 3), 'outputSeconds': round(output_seconds, 3),
                        'note': 'Measured complete output for this short clip.'}
        else:
            estimate = self._sample_summary(jid, s, metadata)
        with self.lock:
            job = self.jobs[jid]
            job['estimate'] = estimate
            job['bestIndex'] = 0 if exact else None
            job['searchNote'] = estimate['note']

    def _discord_settings(self, s, metadata):
        """Automatic source baseline or fixed requested axes, without upscaling."""
        self._validate_smart_pins(s)
        trial = copy.deepcopy(s)
        width = number(metadata.get('width'), 'Source width', 1, 65535, integer=True)
        height = number(metadata.get('height'), 'Source height', 1, 65535, integer=True)
        if s['crop']['enabled']:
            width = min(width - s['crop']['x'], s['crop']['width'])
            height = min(height - s['crop']['y'], s['crop']['height'])
        if width < 1 or height < 1:
            raise ValueError('Crop lies outside the source.')
        # Source-sized output is allowed; the cap is a resource safety bound,
        # not an arbitrary export preset resolution.
        if s['smart']['pinResolution']:
            pinned_width = min(width, s['width']) if s['width'] is not None else None
            pinned_height = min(height, s['height']) if s['height'] is not None else None
            actual_width = pinned_width or max(1, round(width * pinned_height / height))
            actual_height = pinned_height or max(1, round(height * pinned_width / width))
            if actual_width * actual_height > 16_000_000:
                raise ValueError('Locked resolution exceeds the 16-megapixel Smart limit. Choose smaller dimensions or use Advanced export.')
            base_width = actual_width
        else:
            scale = min(1.0, math.sqrt(16_000_000 / (width * height)))
            base_width = max(1, int(width * scale))
            pinned_width, pinned_height = base_width, None
        if s['source']['kind'] == 'sequence':
            input_rate = s.get('_sequenceInputFps', s['fps'])
            source_rate = input_rate * s['speed']
            trial['_sequenceInputFps'] = input_rate
        else:
            source_rate = float(metadata.get('fps') or s['fps']) * s['speed']
        if not math.isfinite(source_rate) or source_rate <= 0:
            source_rate = s['fps']
        base_fps = ((s['fps'] * s['speed'] if s['source']['kind'] == 'sequence' else s['fps'])
                    if s['smart']['pinFps'] else max(.001, min(50, source_rate)))
        trial.update(width=pinned_width, height=pinned_height, quality=100, motionQuality=None,
                     lossyQuality=None, effort='normal', pipeline='png')
        return trial, base_width, base_fps

    @staticmethod
    def _validate_smart_pins(s):
        if s['smart']['pinResolution'] and s['width'] is None and s['height'] is None:
            raise ValueError('Locking resolution requires a width or height.')
        if s['smart']['pinFps']:
            effective_fps = s['fps'] * s['speed'] if s['source']['kind'] == 'sequence' else s['fps']
            if effective_fps > 50:
                raise ValueError('Locked playback FPS must not exceed 50; GIF frame delays cannot preserve a higher rate reliably.')

    @staticmethod
    def _fidelity_score(width, fps, quality, base_width, base_fps):
        # A transparent preference model, not a claimed perceptual metric.
        # Quality's small upper-end gains have diminishing returns: Q90-100
        # should not routinely buy a large loss of spatial or temporal detail.
        quality_value = max(.05, 1 - .65 * ((100 - quality) / 65) ** 1.6)
        return (width / base_width) ** .75 * (fps / base_fps) ** .30 * quality_value

    def _discord(self, jid, s):
        """Shared Smart/Discord search: cheap screening, then measured exports."""
        job = self.jobs[jid]
        target = 20_000_000 if job['mode'] == 'discord' else round(s['smart']['targetMB'] * 1_000_000)
        metadata = self.jobs[jid].get('sourceMetadata') or self.probe(s['source'], jid=jid)
        with self.lock:
            self.jobs[jid]['sourceMetadata'] = metadata
        if not metadata.get('duration') and s['trim']['end'] is not None:
            metadata['duration'] = s['trim']['end']
        base, base_width, base_fps = self._discord_settings(s, metadata)
        pin_resolution, pin_fps = s['smart']['pinResolution'], s['smart']['pinFps']
        sample_budget, full_budget = {'quick': (5, 4), 'balanced': (9, 8), 'thorough': (15, 12)}[s['smart']['effort']]
        with self.lock:
            job['targetBytes'] = target
            job['search'] = {'sampleTrials': 0, 'fullTrials': 0, 'cacheHits': 0,
                             'frameCacheHits': 0, 'phase': 'screening', 'reason': '',
                             'pinResolution': pin_resolution, 'pinFps': pin_fps}
            if pin_resolution and (base['width'] != s['width'] or base['height'] != s['height']):
                job.setdefault('warnings', []).append('Locked dimensions are limited to the cropped source bounds; Smart never upscales.')
            if pin_resolution and base['width'] and base['height']:
                job.setdefault('warnings', []).append('Both dimensions are locked independently. Leave one blank to preserve the source aspect ratio.')
            if not pin_resolution and metadata['width'] * metadata['height'] > 16_000_000 and not s['crop']['enabled']:
                job.setdefault('warnings', []).append('Adaptive exports limit each frame to 16 megapixels for memory safety.')
        try:
            a, b = self._source_interval(base, metadata)
            output_seconds = (b - a) / s['speed'] * (2 if s['bounce'] else 1)
            can_sample = output_seconds > 2 and base_fps >= 1
        except ValueError as e:
            if 'duration is unknown' not in str(e):
                raise
            can_sample = False
        screened, verified = {}, {}
        model = {'width': 1.8, 'fps': .8, 'quality': 1.4, 'correction': 1.0}
        reference = None

        def recipe(key):
            width, fps, quality = key
            trial = copy.deepcopy(base)
            trial.update(fps=s['fps'] if pin_fps else fps / s['speed'] if s['source']['kind'] == 'sequence' else fps, quality=quality)
            if not pin_resolution:
                trial['width'] = width
            return trial

        def score(key):
            return self._fidelity_score(*key, base_width, base_fps)

        def predicted(key):
            if key in screened:
                return screened[key] * model['correction']
            width, fps, quality = key
            return reference * (width / base_width) ** model['width'] * (fps / base_fps) ** model['fps'] * (quality / 100) ** model['quality'] * model['correction']

        def screen(key):
            self._check_cancel(jid)
            if key in screened:
                return screened[key]
            summary = self._sample_summary(jid, recipe(key), metadata)
            screened[key] = max(1, summary['estimatedBytes'])
            with self.lock:
                job['search']['sampleTrials'] += 1
                job['estimate'] = summary
                job['progress'] = max(job.get('progress', 0), min(40, job['search']['sampleTrials'] / sample_budget * 40))
                job['stage'] = 'Learning which settings save the most bytes'
            return screened[key]

        def verify(key):
            self._check_cancel(jid)
            trial = recipe(key)
            result = self._encode(jid, trial, len(verified), full_budget)
            result['fit'] = result['bytes'] <= target
            result['preferenceScore'] = round(score(key), 6)
            verified[key] = result
            with self.lock:
                job['search']['fullTrials'] = len(verified)
                job['search']['phase'] = 'verifying'
                job['progress'] = 40 + len(verified) / full_budget * 55
                fits = [r for r in job['results'] if r.get('fit')]
                job['bestIndex'] = max(fits, key=lambda r: (r['preferenceScore'], -r['bytes']))['index'] if fits else min(job['results'], key=lambda r: r['bytes'])['index']
                if 'id' in job:
                    self._persist(job)
            return result

        def extra_if_requested(key):
            if s['smart']['effort'] != 'thorough':
                return
            trial = recipe(key)
            trial['effort'] = 'extra'
            result = self._encode(jid, trial, len(job['results']), full_budget)
            result['fit'] = result['bytes'] <= target
            result['preferenceScore'] = round(score(key) * 1.005, 6)
            with self.lock:
                job['search']['fullTrials'] = len(job['results'])
                job['search']['phase'] = 'verifying'
                fits = [r for r in job['results'] if r.get('fit')]
                if fits:
                    job['bestIndex'] = max(fits, key=lambda r: (r['preferenceScore'], -r['bytes']))['index']
                if 'id' in job:
                    self._persist(job)

        top = (base_width, base_fps, 100)
        if can_sample:
            reference = screen(top)
            if reference <= target * .90:
                best = verify(top)
                if best['fit']:
                    extra_if_requested(top)
                    self._finish_smart(jid, target, 'Quality 100 fits within the selected resolution and frame-rate constraints.')
                    return
                model['correction'] = max(.1, best['bytes'] / reference)
            # Isolated probes learn the actual clip's quality, pixel and motion
            # response before a model directs the more expensive full exports.
            probes = [(base_width, base_fps, 85),
                      (max(1, round(base_width * .75)), base_fps, 100),
                      (base_width, max(.001, round(base_fps * .70, 3)), 100)]
            for key, axis, ratio, bounds in zip(probes, ('quality', 'width', 'fps'),
                                                (.85, probes[1][0] / base_width, probes[2][1] / base_fps),
                                                ((.2, 4), (.5, 3.5), (.1, 1.5))):
                if key == top or ratio >= 1 or (axis == 'width' and pin_resolution) or (axis == 'fps' and pin_fps):
                    continue
                value = screen(key)
                exponent = math.log(max(.001, value / reference)) / math.log(ratio)
                model[axis] = max(bounds[0], min(bounds[1], exponent))
        else:
            first = verify(top)
            reference = max(1, first['bytes'])
            if first['fit']:
                extra_if_requested(top)
                self._finish_smart(jid, target, 'Quality 100 fits within the selected resolution and frame-rate constraints.')
                return

        # Pixel and FPS candidates are paired with quality choices. Search
        # follows measured response, not a Cartesian series of full encodes.
        widths = [base_width] if pin_resolution else list(dict.fromkeys(max(1, round(base_width * x)) for x in (1, .93, .85, .75, .65, .55, .45, .35, .25, .16, .10, .05)))
        rates = [base_fps] if pin_fps else list(dict.fromkeys(max(min(5, base_fps), round(base_fps * x, 3)) for x in (1, .85, .70, .55, .4)))
        qualities = (100, 95, 90, 85, 80, 70, 60, 50, 40)
        if pin_resolution or pin_fps:
            # Pins remove size controls. Include the full lower quality range
            # before reporting no fit, rather than breaking a fixed setting.
            qualities += (30, 20, 10, 1)
        candidates = list(itertools.product(widths, rates, qualities))
        if can_sample:
            while len(screened) < sample_budget:
                remaining = [k for k in candidates if k not in screened and k not in verified]
                plausible = [k for k in remaining if predicted(k) <= target * .93]
                if not remaining:
                    break
                key = max(plausible, key=score) if plausible else min(remaining, key=predicted)
                before = predicted(key)
                measured = screen(key)
                # Correct a poor global model without assuming strict quality/
                # size monotonicity. Keep actual sample measurements per recipe.
                ratio = measured * model['correction'] / max(1, before)
                model['correction'] *= max(.7, min(1.4, math.sqrt(ratio)))
                if len(screened) >= 5 and measured * model['correction'] <= target * .95:
                    fitted = [k for k in screened if predicted(k) <= target * .95]
                    ceiling = max((score(k) for k in plausible), default=0)
                    if fitted and ceiling <= max(map(score, fitted)) * 1.008:
                        break

        reason = 'Search budget reached.'
        while len(verified) < full_budget:
            self._check_cancel(jid)
            fits = [k for k, result in verified.items() if result['fit']]
            best_score = max(map(score, fits), default=0)
            remaining = [k for k in candidates if k not in verified]
            # Leave headroom for sampling uncertainty, then calibrate from real
            # exports. No estimated candidate is ever labeled as fitting.
            plausible = [k for k in remaining if predicted(k) <= target * .97]
            improving = [k for k in plausible if score(k) > best_score * 1.008]
            if fits and not improving:
                reason = 'Stopped at diminishing returns: no predicted improvement above 0.8% in the settings preference model.'
                break
            if not remaining:
                reason = 'All useful candidates checked.'
                break
            key = max(improving or plausible, key=score) if (improving or plausible) else min(remaining, key=predicted)
            estimate = max(1, predicted(key))
            result = verify(key)
            ratio = result['bytes'] / estimate
            # Full measured bytes override the sample model. Use bounded steps
            # so one unusual scene does not abruptly discard every candidate.
            model['correction'] *= max(.35, min(3.0, ratio))
            if result['fit'] and key == top:
                reason = 'Maximum quality within the selected constraints fits.'
                break
        if len(job['results']) < full_budget:
            fits = [key for key, result in verified.items() if result['fit']]
            if fits:
                extra_if_requested(max(fits, key=score))
        self._finish_smart(jid, target, reason)

    def _finish_smart(self, jid, target, reason):
        with self.lock:
            job = self.jobs[jid]
            results = job['results']
            fits = [r for r in results if r.get('fit')]
            if fits:
                chosen = max(fits, key=lambda r: (r['preferenceScore'], -r['bytes']))
                job['bestIndex'] = chosen['index']
                job['searchNote'] = (f'Best verified fit under {target:,} bytes. {reason} '
                                     'Ranking balances retained detail, motion and diminishing gains near quality 100. '
                                     'This is a settings preference model, not a perceptual metric or a guarantee of a global optimum.')
            elif results:
                job['bestIndex'] = min(results, key=lambda r: r['bytes'])['index']
                pins = job['search'].get('pinResolution') or job['search'].get('pinFps')
                job['searchNote'] = (f'No tested candidate fits {target:,} bytes. The smallest measured result is shown without a fit claim. '
                                     + ('Locked settings were preserved. Unlock a setting, shorten the clip or increase the limit.' if pins else
                                        'Shorten the clip or increase the limit.'))
            job['search']['reason'] = reason
            job['search']['phase'] = 'complete'

    def _worker(self):
        while True:
            jid = self._queue.get()
            try:
                self._check_cancel(jid)
                with self.lock:
                    job = self.jobs[jid]
                    job.update(status='running', started=time.time())
                    settings = copy.deepcopy(job['settings'])
                self._log(jid, f"Started {job['mode']} job.")
                with self.lock:
                    job['stage'] = 'Inspecting source'
                try:
                    metadata = self.probe(settings['source'], jid=jid)
                    with self.lock:
                        job['sourceMetadata'] = metadata
                except (ValueError, OSError):
                    # Direct Gifski decoding does not require FFprobe. Unknown
                    # duration leaves progress indeterminate, never fabricated.
                    self._log(jid, 'Source duration unavailable; showing live frame counts and elapsed time.')
                self._check_cancel(jid)
                if job['mode'] == 'optimize':
                    self._optimize(jid, settings)
                elif job['mode'] == 'estimate':
                    self._estimate(jid, settings)
                elif job['mode'] in ('discord', 'smart'):
                    self._discord(jid, settings)
                else:
                    variants = self._sweep_settings(settings) if job['mode'] == 'sweep' else [settings]
                    for i, s in enumerate(variants):
                        self._encode(jid, s, i, len(variants))
                    with self.lock:
                        job['bestIndex'] = 0
                self._check_cancel(jid)
                with self.lock:
                    job.update(status='completed', stage='Complete', progress=100, finished=time.time())
                self._log(jid, 'Job complete.')
            except Cancelled:
                with self.lock:
                    self.jobs[jid].update(status='cancelled', stage='Cancelled', finished=time.time())
                self._log(jid, 'Job cancelled.')
            except Exception as e:
                with self.lock:
                    self.jobs[jid].update(status='failed', stage='Failed', error=str(e), finished=time.time())
                self._log(jid, str(e))
            finally:
                with self.lock:
                    self.active.pop(jid, None)
                    self._frame_caches.pop(jid, None)
                    self.cancelled.discard(jid)
                    shutil.rmtree(self.outputs / jid / 'frame-cache', ignore_errors=True)
                    self._persist(self.jobs[jid])
                self._queue.task_done()
