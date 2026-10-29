"""Local FFmpeg/Gifski execution, measured comparisons, and bounded size search.

Python 3.10+, standard library only. Child programs never run through a shell.
"""
from __future__ import annotations

import copy
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
    'sweep': {'qualities': [70, 80, 90], 'widths': [480, 640], 'fps': [15, 20]},
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
        self.lock = threading.RLock()
        self.jobs = {}
        self.active = {}
        self.cancelled = set()
        self._status = None
        self._queue = queue.Queue()
        for p in sorted(self.outputs.glob('*/job.json'))[-150:]:
            try:
                item = json.loads(p.read_text('utf-8'))
                if item.get('status') in ('queued', 'running'):
                    item.update(status='interrupted', stage='Server stopped', error='The app stopped before this job completed.')
                self.jobs[item['id']] = item
            except (OSError, ValueError, KeyError):
                pass
        threading.Thread(target=self._worker, daemon=True, name='encode-queue').start()

    def _tool(self, name):
        configured = self.config.get(name)
        if configured:
            return str(Path(configured).expanduser().resolve()) if Path(configured).expanduser().is_file() else (shutil.which(configured) or configured)
        if name == 'ffprobe' and self.config.get('ffmpeg'):
            ff = Path(self.config['ffmpeg']).expanduser()
            probe = ff.parent / ('ffprobe.exe' if ff.suffix.lower() == '.exe' else 'ffprobe')
            if probe.is_file():
                return str(probe.resolve())
        for directory in (self.root / 'bin', self.root):
            for ext in ('.exe', '') if os.name == 'nt' else ('', '.exe'):
                bundled = directory / (name + ext)
                if bundled.is_file():
                    return str(bundled)
        return shutil.which(name) or name

    def status(self):
        with self.lock:
            if self._status is not None:
                return copy.deepcopy(self._status)
        tools = {}
        for name in ('gifski', 'ffmpeg', 'ffprobe'):
            path = self._tool(name)
            item = {'path': path, 'available': False, 'version': '', 'help': ''}
            try:
                r = subprocess.run([path, '--version' if name == 'gifski' else '-version'], capture_output=True, text=True, errors='replace', timeout=10)
                combined = (r.stdout + r.stderr).strip()
                item.update(available=r.returncode == 0, version=combined.splitlines()[0] if combined else '')
                if name == 'gifski' and item['available']:
                    h = subprocess.run([path, '--help'], capture_output=True, text=True, errors='replace', timeout=10)
                    item['help'] = (h.stdout + h.stderr).strip()
                    item['flags'] = sorted(set(re.findall(r'--[a-z][a-z-]+', item['help'])))
            except (OSError, subprocess.TimeoutExpired) as e:
                item['error'] = str(e)
            tools[name] = item
        result = {'tools': tools, 'config': copy.deepcopy(self.config), 'defaults': copy.deepcopy(DEFAULTS), 'outputDirectory': str(self.outputs)}
        with self.lock:
            self._status = result
        return copy.deepcopy(result)

    def configure(self, values):
        with self.lock:
            for k in ('gifski', 'ffmpeg', 'ffprobe'):
                if k in values:
                    if not isinstance(values[k], str) or len(values[k]) > 4096 or '\x00' in values[k]:
                        raise ValueError(f'Invalid {k} executable path.')
                    self.config[k] = values[k].strip().strip('"')
            self._write_json(self.config_path, self.config)
            self._status = None
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

    def probe(self, source):
        paths = self._paths(source)
        path = paths[0]
        result = {'path': str(path), 'name': path.name, 'kind': source.get('kind', 'video'), 'bytes': sum(p.stat().st_size for p in paths), 'frameCount': len(paths) if source.get('kind') == 'sequence' else None}
        try:
            r = subprocess.run([self._tool('ffprobe'), '-v', 'error', '-show_streams', '-show_format', '-of', 'json', str(path)], capture_output=True, text=True, errors='replace', timeout=30)
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
            rotation = float(stream.get('tags', {}).get('rotate', 0))
            for side in stream.get('side_data_list', []):
                rotation = float(side.get('rotation', rotation))
            w, h = stream.get('width'), stream.get('height')
            if abs(rotation) % 180 == 90:
                w, h = h, w
            duration = stream.get('duration') or data.get('format', {}).get('duration')
            result.update(width=w, height=h, fps=fps, duration=float(duration) if duration is not None else None,
                          codec=stream.get('codec_name'), pixelFormat=stream.get('pix_fmt'),
                          colorSpace=stream.get('color_space'), colorTransfer=stream.get('color_transfer'), rotation=rotation,
                          hasAlpha=(stream.get('pix_fmt') or '').startswith(('rgba', 'bgra', 'argb', 'abgr', 'yuva', 'gbrap', 'ya')) or stream.get('pix_fmt') == 'pal8',
                          streams=data.get('streams', []))
            if source.get('kind') != 'sequence' and stream.get('nb_frames', '').isdigit():
                result['frameCount'] = int(stream['nb_frames'])
        except FileNotFoundError:
            if path.suffix.lower() != '.png':
                raise ValueError('FFprobe was not found. Set its executable path in Tools.') from None
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
        for group in ('source', 'trim', 'crop', 'sweep', 'optimize'):
            if not isinstance(s[group], dict):
                raise ValueError(f'Invalid {group} settings.')
        for flag in ('bounce', 'noSort', 'quiet', 'reverse'):
            if not isinstance(s[flag], bool):
                raise ValueError(f'{flag} must be true or false.')
        if not isinstance(s['crop']['enabled'], bool):
            raise ValueError('Crop enabled must be true or false.')
        self._paths(s['source'])
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
        cmd = [self._tool('ffmpeg'), '-hide_banner', '-nostdin', '-y', '-v', 'warning']
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
            cmd += ['-fps_mode', 'passthrough', '-pix_fmt', 'rgba', '-threads', '1', '-start_number', '0', str(dest)]
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
                'notes': ['Commands are executed as argument arrays, never through a shell.', 'Every search candidate is a complete encode; file sizes are measured, not estimated.']}

    def submit(self, settings, mode='encode'):
        if mode not in ('encode', 'sweep', 'optimize', 'estimate', 'discord'):
            raise ValueError('Unknown job mode.')
        # Adaptive exports always use FFmpeg preprocessing so frame rate changes
        # resample the source rather than silently changing its duration.
        if mode == 'discord':
            settings = copy.deepcopy(settings)
            settings['pipeline'] = 'png'
            if settings.get('source', {}).get('kind') == 'direct':
                settings['source']['kind'] = 'video'
        plan = self.plan(settings)
        s = plan['settings']
        status = self.status()
        required = ['gifski'] + (['ffmpeg'] if plan['pipeline'] in ('png', 'y4m') else [])
        missing = [x for x in required if not status['tools'][x]['available']]
        if missing:
            raise ValueError('Missing executable(s): ' + ', '.join(missing) + '. Set the paths in Tools or place the executables in bin/.')
        if mode == 'sweep':
            self._sweep_settings(s)
        if mode in ('optimize', 'discord'):
            o = s['optimize']
            for k, lo, hi, integer in [('targetMiB', .001, 100000, False), ('minQuality', 1, 100, True), ('maxQuality', 1, 100, True), ('minWidth', 1, 65535, True), ('minFps', .001, 100, False), ('maxTrials', 1, 80, True)]:
                o[k] = number(o[k], k, lo, hi, integer=integer)
            if o['minQuality'] > o['maxQuality']:
                raise ValueError('Minimum quality must not exceed maximum quality.')
            if mode == 'optimize' and (s['lossyQuality'] is not None or s['motionQuality'] is not None):
                plan['warnings'].append('Quality overrides remain fixed during target search; this may reduce the effect of changing overall quality.')
        jid = uuid.uuid4().hex
        job = {'id': jid, 'status': 'queued', 'stage': 'Queued', 'progress': 0, 'created': time.time(), 'settings': s, 'mode': mode, 'results': [], 'logs': [], 'error': None, 'commands': [], 'warnings': plan['warnings'], 'bestIndex': None}
        if mode == 'discord':
            job['targetBytes'] = 20_000_000
            job['warnings'].append('Discord adapts dimensions and FPS to the source, and searches quality with normal effort and inherited motion/lossy quality. The 20 MB cap is 20,000,000 bytes.')
        (self.outputs / jid).mkdir()
        with self.lock:
            self.jobs[jid] = job
            self._persist(job)
        self._queue.put(jid)
        return copy.deepcopy(job)

    def list_jobs(self):
        with self.lock:
            return copy.deepcopy(sorted(self.jobs.values(), key=lambda j: j['created'], reverse=True))

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
        with self.lock:
            self.jobs[jid]['logs'].append(str(line)[-4000:])
            self.jobs[jid]['logs'] = self.jobs[jid]['logs'][-250:]

    def _check_cancel(self, jid):
        if jid in self.cancelled:
            raise Cancelled()

    def _track(self, jid, process):
        with self.lock:
            self.active.setdefault(jid, []).append(process)

    def _run(self, jid, cmd, cwd=None, stdin=None, stdout=None):
        self._check_cancel(jid)
        with self.lock:
            self.jobs[jid]['commands'].append(cmd)
        p = subprocess.Popen(cmd, cwd=cwd, stdin=stdin or subprocess.DEVNULL, stdout=stdout if stdout is not None else subprocess.PIPE, stderr=subprocess.PIPE)
        self._track(jid, p)
        outputs = []
        def reader(pipe):
            try:
                for raw in iter(pipe.readline, b''):
                    line = raw.decode('utf-8', 'replace').strip()
                    if line:
                        outputs.append(line)
                        if len(outputs) > 100:
                            del outputs[:-100]
                        self._log(jid, line)
            finally:
                pipe.close()
        threads = [threading.Thread(target=reader, args=(p.stderr,), daemon=True)]
        if stdout is None:
            threads.append(threading.Thread(target=reader, args=(p.stdout,), daemon=True))
        for t in threads:
            t.start()
        while p.poll() is None:
            if jid in self.cancelled:
                p.terminate()
                try:
                    p.wait(timeout=3)
                except subprocess.TimeoutExpired:
                    p.kill()
                    p.wait()
                break
            time.sleep(.12)
        for t in threads:
            t.join(timeout=3)
        with self.lock:
            self.active[jid].remove(p)
        self._check_cancel(jid)
        if p.returncode:
            raise RuntimeError(f'{Path(cmd[0]).name} exited with code {p.returncode}:\n' + '\n'.join(outputs[-12:]))

    def _run_pipe(self, jid, producer, consumer):
        self._check_cancel(jid)
        with self.lock:
            self.jobs[jid]['commands'].extend([producer, consumer])
        ff = subprocess.Popen(producer, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        self._track(jid, ff)
        try:
            gs = subprocess.Popen(consumer, stdin=ff.stdout, stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        except Exception:
            ff.terminate()
            ff.wait()
            raise
        self._track(jid, gs)
        ff.stdout.close()
        outputs = []
        def read(pipe, label):
            try:
                for raw in iter(pipe.readline, b''):
                    line = raw.decode('utf-8', 'replace').strip()
                    if line:
                        outputs.append(label + ': ' + line)
                        if len(outputs) > 100:
                            del outputs[:-100]
                        self._log(jid, label + ': ' + line)
            finally:
                pipe.close()
        threads = [threading.Thread(target=read, args=(pipe, label), daemon=True)
                   for pipe, label in [(ff.stderr, 'FFmpeg'), (gs.stdout, 'Gifski'), (gs.stderr, 'Gifski')]]
        for t in threads:
            t.start()
        try:
            while ff.poll() is None or gs.poll() is None:
                if jid in self.cancelled or (gs.poll() not in (None, 0)):
                    for proc in (ff, gs):
                        if proc.poll() is None:
                            proc.terminate()
                            try:
                                proc.wait(timeout=3)
                            except subprocess.TimeoutExpired:
                                proc.kill()
                    break
                time.sleep(.12)
            for proc in (ff, gs):
                proc.wait()
            for t in threads:
                t.join(timeout=3)
            self._check_cancel(jid)
            if ff.returncode or gs.returncode:
                raise RuntimeError(f'Y4M pipeline failed (FFmpeg {ff.returncode}, Gifski {gs.returncode}):\n' + '\n'.join(outputs[-12:]))
        finally:
            for proc in (ff, gs):
                if proc.poll() is None:
                    proc.kill()
                    proc.wait()
            with self.lock:
                for proc in (ff, gs):
                    self.active[jid].remove(proc)

    def _encode(self, jid, s, index, total):
        self._check_cancel(jid)
        directory = self.outputs / jid
        work = directory / f'work-{index}'
        work.mkdir()
        base = Path(s['outputName']).stem
        output = directory / f'{index + 1:02d}-{base}.gif'
        paths = self._paths(s['source'])
        pipeline = self._pipeline(s)
        started = time.monotonic()
        completed = False
        with self.lock:
            self.jobs[jid].update(stage=f'Encoding trial {index + 1} of {total}', progress=round(index / total * 100, 1))
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
                    frames = work / 'frames'
                    frames.mkdir()
                    cmd = self._ffmpeg(s, paths[0], frames / 'f%06d.png', 'png', sequence)
                    with self.lock:
                        self.jobs[jid]['stage'] = f'Extracting frames for trial {index + 1} of {total}'
                    self._run(jid, cmd)
                    inputs = sorted(p.name for p in frames.glob('*.png'))
                    if len(inputs) < 2:
                        raise ValueError('The selected clip produced fewer than two frames. Increase its duration or FPS.')
                    if len(inputs) > 100000:
                        raise ValueError('The clip produced more than 100,000 frames. Shorten it or reduce FPS.')
                    actual = copy.deepcopy(s)
                    if sequence:
                        actual['fps'] *= actual['speed']
                    cmd = self._gifski(actual, output, ['*.png'] if os.name == 'nt' else inputs, 'png')
                    with self.lock:
                        self.jobs[jid]['stage'] = f'Compressing trial {index + 1} of {total}'
                    self._run(jid, cmd, cwd=frames)
                else:
                    ff = self._ffmpeg(s, paths[0], '-', 'y4m', sequence)
                    actual = copy.deepcopy(s)
                    if sequence:
                        actual['fps'] *= actual['speed']
                    gs = self._gifski(actual, output, ['-'], 'y4m')
                    self._run_pipe(jid, ff, gs)
            w, h = gif_dimensions(output)
            result = {'index': index, 'filename': output.name, 'bytes': output.stat().st_size, 'width': w, 'height': h,
                      'fps': s['fps'] * s['speed'] if s['source']['kind'] == 'sequence' else s['fps'], 'quality': s['quality'],
                      'settings': copy.deepcopy(s), 'downloadUrl': f'/api/files/{jid}/{index}?download=1',
                      'previewUrl': f'/api/files/{jid}/{index}', 'fit': None, 'elapsed': round(time.monotonic() - started, 2)}
            if self.jobs[jid]['mode'] in ('optimize', 'discord'):
                target = self.jobs[jid].get('targetBytes', s['optimize']['targetMiB'] * 1048576)
                result['fit'] = result['bytes'] <= target
            if self.jobs[jid]['mode'] == 'estimate':
                result['sample'] = self.jobs[jid].get('sampling', True)
            with self.lock:
                self.jobs[jid]['results'].append(result)
                self._persist(self.jobs[jid])
            completed = True
            return result
        finally:
            shutil.rmtree(work, ignore_errors=True)
            if not completed:
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
            base_width = self.probe(s['source'])['width']
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
        if not duration or not math.isfinite(duration) or duration <= 0:
            raise ValueError('The source duration is unknown. Enter a finite trim end before estimating.')
        start = s['trim']['start']
        end = min(s['trim']['end'], duration) if s['trim']['end'] is not None else duration
        if end <= start:
            raise ValueError('Trim start is beyond the end of the source.')
        return start, end

    def _estimate(self, jid, s):
        """Encode three separated samples, then extrapolate measured bytes/time."""
        metadata = self.probe(s['source'])
        # A supplied finite end also permits formats without duration metadata.
        if not metadata.get('duration') and s['trim']['end'] is not None:
            metadata['duration'] = s['trim']['end']
        start, end = self._source_interval(s, metadata)
        duration = end - start
        output_seconds = duration / s['speed'] * (2 if s['bounce'] else 1)
        exact = output_seconds <= 6
        if self._pipeline(s) == 'direct' and not exact:
            raise ValueError('Fast estimation of long inputs requires Auto, PNG or Y4M processing. Direct mode can measure clips up to six seconds.')
        # Very low frame rates need a longer sample to yield at least two frames.
        span = max(1.5 * s['speed'], 3 * s['speed'] / s['fps'])
        if span / s['speed'] > 6 and not exact:
            raise ValueError('Frame rate is too low for a quick sample. Use a complete encode to measure this clip.')
        if duration <= span * 3:
            exact = True
        if exact:
            samples = [(start, end)]
        else:
            samples = [(start, start + span), ((start + end - span) / 2, (start + end + span) / 2), (end - span, end)]
        with self.lock:
            self.jobs[jid]['sampling'] = not exact
        measured = []
        for i, (a, b) in enumerate(samples):
            self._check_cancel(jid)
            trial = copy.deepcopy(s)
            if not exact:
                if s['source']['kind'] == 'sequence':
                    paths = self._paths(s['source'])
                    if not s['noSort']:
                        paths.sort(key=natural_key)
                    input_rate = s.get('_sequenceInputFps', s['fps'])
                    first = max(0, int(a * input_rate))
                    last = min(len(paths), max(first + 2, math.ceil(b * input_rate)))
                    trial['source'] = {'kind': 'sequence', 'paths': list(map(str, paths[first:last]))}
                    trial['trim'] = {'start': 0, 'end': None}
                    a, b = first / input_rate, last / input_rate
                else:
                    trial['trim'] = {'start': a, 'end': b}
            result = self._encode(jid, trial, i, len(samples))
            sample_seconds = (b - a) / s['speed'] * (2 if s['bounce'] else 1)
            measured.append((result['bytes'], sample_seconds))
        if exact:
            estimate = low = high = measured[0][0]
            note = 'Measured complete output for this short clip. This is an actual encode, not an extrapolation.'
        else:
            rates = [size / seconds for size, seconds in measured]
            estimate = round(sum(size for size, _ in measured) / sum(seconds for _, seconds in measured) * output_seconds)
            # Deliberately wide heuristic bounds. These are not statistical confidence
            # intervals: unseen scene changes can fall outside the displayed range.
            low = round(min(rates) * output_seconds * .65)
            high = round(max(rates) * output_seconds * 1.5)
            note = 'Estimate from three separated samples. Range is a heuristic, not a confidence interval or size guarantee; scene changes, GIF frame reuse and custom filters can put the final file outside it.'
        with self.lock:
            job = self.jobs[jid]
            job['estimate'] = {'estimatedBytes': estimate, 'lowerBytes': low, 'upperBytes': high,
                               'exact': exact, 'sampleCount': len(samples),
                               'sampledSeconds': round(sum(seconds for _, seconds in measured), 3),
                               'outputSeconds': round(output_seconds, 3), 'note': note}
            job['bestIndex'] = 0 if exact else None
            job['searchNote'] = note

    def _discord_settings(self, s, metadata):
        """Derive an uncropped-aspect baseline without enlarging the source."""
        trial = copy.deepcopy(s)
        width = int(metadata.get('width') or 0)
        height = int(metadata.get('height') or 0)
        if width < 1 or height < 1:
            raise ValueError('Source dimensions are required for the Discord preset.')
        if s['crop']['enabled']:
            width = min(width - s['crop']['x'], s['crop']['width'])
            height = min(height - s['crop']['y'], s['crop']['height'])
        if width < 1 or height < 1:
            raise ValueError('Crop lies outside the source.')
        # Limit both axes for portrait sources while keeping the source aspect ratio.
        scale = min(1.0, 1280 / width, 1280 / height)
        base_width = max(1, round(width * scale))
        if s['source']['kind'] == 'sequence':
            input_rate = s.get('_sequenceInputFps', s['fps'])
            source_rate = input_rate * s['speed']
            trial['_sequenceInputFps'] = input_rate
        else:
            source_rate = float(metadata.get('fps') or s['fps']) * s['speed']
        base_fps = max(.001, min(50, source_rate))
        trial.update(width=base_width, height=None, quality=95, motionQuality=None,
                     lossyQuality=None, effort='normal', pipeline='png')
        return trial, base_width, base_fps

    def _discord(self, jid, s):
        """Compare measured full exports under a hard 20,000,000-byte budget."""
        target = 20_000_000
        metadata = self.probe(s['source'])
        base, base_width, base_fps = self._discord_settings(s, metadata)
        budget = int(s['optimize']['maxTrials'])
        # Test a broad ladder before spending remaining trials refining quality.
        # Include the smallest tier even with a small trial budget.
        tiers = []
        for ratio, frame_ratio in [(1, 1), (.85, 1), (.7, .8), (.55, .6), (.4, .5), (.25, .4)]:
            pair = (max(1, round(base_width * ratio)), max(.001, round(base_fps * frame_ratio, 3)))
            if pair not in tiers:
                tiers.append(pair)
        picks = tiers if budget >= len(tiers) else [tiers[round(i * (len(tiers) - 1) / max(1, budget - 1))] for i in range(budget)]
        tested = set()
        def encode(width, fps, quality):
            key = (width, fps, quality)
            if key in tested or len(tested) >= budget:
                return None
            tested.add(key)
            trial = copy.deepcopy(base)
            trial.update(width=width, fps=fps / s['speed'] if s['source']['kind'] == 'sequence' else fps, quality=quality)
            result = self._encode(jid, trial, len(tested) - 1, budget)
            # The score ranks settings only; it is not a perceptual image metric.
            result['preferenceScore'] = round((width / base_width) ** .65 * (fps / base_fps) ** .2 * (quality / 100) ** 1.4, 6)
            result['fit'] = result['bytes'] <= target
            return result
        for width, fps in picks:
            encode(width, fps, 100)
        for quality in (75, 55):
            for width, fps in picks:
                if len(tested) >= budget:
                    break
                encode(width, fps, quality)
        # If duplicate tiers left room, use midpoints around successful candidates.
        fits = [r for r in self.jobs[jid]['results'] if r.get('fit')]
        for r in sorted(fits, key=lambda r: r['preferenceScore'], reverse=True):
            trial = r['settings']
            fps = trial['fps'] * s['speed'] if s['source']['kind'] == 'sequence' else trial['fps']
            encode(trial['width'], fps, min(100, trial['quality'] + 10))
        results = self.jobs[jid]['results']
        fits = [r for r in results if r.get('fit')]
        with self.lock:
            job = self.jobs[jid]
            job['targetBytes'] = target
            if fits:
                chosen = max(fits, key=lambda r: (r['preferenceScore'], -r['bytes']))
                job['bestIndex'] = chosen['index']
                job['searchNote'] = 'Best tested fit under 20,000,000 bytes. Ranking balances retained width (weight 0.65), FPS (0.20) and configured quality (1.40); it is a settings preference, not a perceptual score or global optimum. Review the alternatives visually.'
            elif results:
                job['bestIndex'] = min(results, key=lambda r: r['bytes'])['index']
                job['searchNote'] = 'No tested candidate fits 20,000,000 bytes. The smallest result is shown, not approved as a fit. Shorten the trim or crop unused content and retry.'

    def _worker(self):
        while True:
            jid = self._queue.get()
            try:
                self._check_cancel(jid)
                with self.lock:
                    job = self.jobs[jid]
                    job.update(status='running', started=time.time())
                    settings = copy.deepcopy(job['settings'])
                if job['mode'] == 'optimize':
                    self._optimize(jid, settings)
                elif job['mode'] == 'estimate':
                    self._estimate(jid, settings)
                elif job['mode'] == 'discord':
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
            except Cancelled:
                with self.lock:
                    self.jobs[jid].update(status='cancelled', stage='Cancelled', finished=time.time())
            except Exception as e:
                with self.lock:
                    self.jobs[jid].update(status='failed', stage='Failed', error=str(e), finished=time.time())
                self._log(jid, str(e))
            finally:
                with self.lock:
                    self.active.pop(jid, None)
                    self._persist(self.jobs[jid])
                self._queue.task_done()
