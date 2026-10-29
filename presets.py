"""Validated, app-local recipes with atomic replacement and revision checks."""
from __future__ import annotations

import copy
import hashlib
import json
import math
import os
from pathlib import Path
import re
import tempfile
import threading

MAX_PRESETS = 100
MAX_STORE_BYTES = 1024 * 1024


class PresetConflict(ValueError):
    pass


class PresetStoreUnavailable(RuntimeError):
    pass


RANGES = {
    'quality': (1, 100, True, False), 'lossyQuality': (1, 100, True, True),
    'motionQuality': (1, 100, True, True), 'width': (1, 65535, True, True),
    'height': (1, 65535, True, True), 'fps': (.001, 100, False, False),
    'speed': (.001, 1000, False, False), 'repeat': (-1, 32767, True, False),
    'denoise': (0, 20, False, False), 'sharpen': (0, 5, False, False),
    'crop.x': (0, 65535, True, False), 'crop.y': (0, 65535, True, False),
    'crop.width': (1, 65535, True, False), 'crop.height': (1, 65535, True, False),
    'smart.targetMB': (.01, 100000, False, False),
    'performance.threads': (0, 256, True, False),
}
ENUMS = {
    'effort': ('normal', 'fast', 'extra'), 'pipeline': ('auto', 'png', 'y4m', 'direct'),
    'scaleFilter': ('lanczos', 'bicubic', 'bilinear', 'neighbor', 'spline', 'area'),
    'smart.effort': ('quick', 'balanced', 'thorough'),
    'performance.hardwareDecode': ('none', 'auto'),
    'y4mColorOverride': (None, '', 'bt709', 'fcc', 'bt470bg', 'bt601', 'smpte240', 'ycgco'),
}
BOOLEANS = {'bounce', 'noSort', 'quiet', 'reverse', 'crop.enabled', 'smart.pinResolution', 'smart.pinFps'}
GROUPS = {'crop': {'enabled', 'x', 'y', 'width', 'height'},
          'smart': {'targetMB', 'effort', 'pinResolution', 'pinFps'},
          'performance': {'threads', 'hardwareDecode'}}
SIMPLE_KEYS = {key for key in set(RANGES) | set(ENUMS) | BOOLEANS if '.' not in key} | {'matte', 'fixedColors', 'outputName', 'customFilter'}
COLOR = re.compile(r'#?[0-9a-fA-F]{6}')


def _value(key, value):
    if key in RANGES:
        low, high, integer, nullable = RANGES[key]
        if value is None and nullable:
            return None
        if (isinstance(value, bool) or not isinstance(value, (int, float))
                or not low <= value <= high or (isinstance(value, float) and not math.isfinite(value))
                or (integer and value != int(value))):
            raise ValueError(f'Invalid recipe value for {key}.')
    elif key in ENUMS:
        if value not in ENUMS[key]:
            raise ValueError(f'Unknown recipe option for {key}.')
    elif key in BOOLEANS:
        if not isinstance(value, bool):
            raise ValueError(f'Recipe {key} must be true or false.')
    elif key == 'matte':
        if value is not None and (not isinstance(value, str) or not COLOR.fullmatch(value)):
            raise ValueError('Recipe matte must be a six-digit RGB color or null.')
    elif key == 'fixedColors':
        if (not isinstance(value, list) or len(value) > 255
                or any(not isinstance(item, str) or not COLOR.fullmatch(item) for item in value)):
            raise ValueError('Recipe fixed colors must contain at most 255 RGB colors.')
    elif key == 'outputName':
        if (not isinstance(value, str) or not 1 <= len(value) <= 180 or value in ('.', '..')
                or any(char in value for char in '/\\:') or any(ord(char) < 32 for char in value)):
            raise ValueError('Recipe output name must be a filename without a path.')
    elif key == 'customFilter':
        if not isinstance(value, str) or len(value) > 10000 or '\x00' in value:
            raise ValueError('Invalid recipe custom filter.')
    else:
        raise ValueError(f'Unsupported recipe field: {key}.')
    return copy.deepcopy(value)


def validate_presets(values):
    if not isinstance(values, list) or len(values) > MAX_PRESETS:
        raise ValueError('Save at most 100 recipes.')
    validated, ids = [], set()
    for item in values:
        if not isinstance(item, dict) or set(item) != {'id', 'name', 'mode', 'settings'}:
            raise ValueError('Each recipe must contain only id, name, mode, and settings.')
        ident, name, mode, settings = (item[key] for key in ('id', 'name', 'mode', 'settings'))
        if not isinstance(ident, str) or not re.fullmatch(r'custom-[a-zA-Z0-9-]{1,100}', ident) or ident in ids:
            raise ValueError('Recipe IDs must be valid and unique.')
        if (not isinstance(name, str) or not 1 <= len(name.strip()) <= 60
                or any(ord(char) < 32 for char in name)):
            raise ValueError('Recipe names must contain 1–60 characters.')
        if mode not in ('default', 'smart', 'advanced'):
            raise ValueError('Unknown recipe mode.')
        if not isinstance(settings, dict) or set(settings) - SIMPLE_KEYS - set(GROUPS):
            raise ValueError('Recipe settings contain an unsupported field; source paths and trim are not saved.')
        clean = {}
        for key, value in settings.items():
            if key in GROUPS:
                if not isinstance(value, dict) or set(value) - GROUPS[key]:
                    raise ValueError(f'Invalid recipe {key} settings.')
                clean[key] = {subkey: _value(f'{key}.{subkey}', subvalue) for subkey, subvalue in value.items()}
            else:
                clean[key] = _value(key, value)
        validated.append({'id': ident, 'name': name.strip(), 'mode': mode, 'settings': clean})
        ids.add(ident)
    return validated


def validate_preferences(value):
    if not isinstance(value, dict) or set(value) != {'keepAwake'} or not isinstance(value['keepAwake'], bool):
        raise ValueError('Preferences must contain a boolean keepAwake value.')
    return dict(value)


def _unique_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError('Duplicate JSON field.')
        result[key] = value
    return result


class PresetStore:
    def __init__(self, root):
        self.root = Path(root).resolve()
        self.directory = self.root / 'data'
        self.path = self.directory / 'presets.json'
        self.lock = threading.Lock()

    def _check_location(self):
        if self.directory.is_symlink() or self.path.is_symlink():
            raise PresetStoreUnavailable('The app recipe store must not be a symbolic link.')

    def _read(self):
        self._check_location()
        try:
            with self.path.open('rb') as source:
                raw = source.read(MAX_STORE_BYTES + 1)
        except FileNotFoundError:
            return {'presets': [], 'preferences': {'keepAwake': False}, 'revision': 'missing'}
        except OSError:
            raise PresetStoreUnavailable('The app recipe store cannot be read. Existing recipes were not changed.') from None
        try:
            if len(raw) > MAX_STORE_BYTES:
                raise ValueError('Recipe store too large.')
            data = json.loads(raw, object_pairs_hook=_unique_object,
                              parse_constant=lambda _: (_ for _ in ()).throw(ValueError('Nonfinite number.')))
            if not isinstance(data, dict) or not {'version', 'presets'} <= set(data) or set(data) - {'version', 'presets', 'preferences'} or type(data['version']) is not int or data['version'] != 1:
                raise ValueError('Unknown recipe store format.')
            presets = validate_presets(data['presets'])
            preferences = validate_preferences(data.get('preferences', {'keepAwake': False}))
        except (ValueError, TypeError, RecursionError, UnicodeDecodeError, OverflowError):
            raise PresetStoreUnavailable('The app recipe store is invalid. Back up and repair data/presets.json before saving; existing bytes were not changed.') from None
        return {'presets': presets, 'preferences': preferences, 'revision': hashlib.sha256(raw).hexdigest()}

    def read(self):
        with self.lock:
            return self._read()

    def replace(self, payload):
        return self._replace(payload, 'presets', validate_presets)

    def replace_preferences(self, payload):
        return self._replace(payload, 'preferences', validate_preferences)

    def _replace(self, payload, section, validate):
        if not isinstance(payload, dict) or set(payload) != {section, 'revision'}:
            raise ValueError(f'Provide {section} and the last loaded revision.')
        revision = payload['revision']
        if not isinstance(revision, str) or (revision != 'missing' and not re.fullmatch(r'[0-9a-f]{64}', revision)):
            raise ValueError('Invalid recipe store revision.')
        replacement = validate(payload[section])
        with self.lock:
            current = self._read()
            if current['revision'] != revision:
                raise PresetConflict('App settings changed in another window. Reload saved settings and retry.')
            data = {'version': 1, 'presets': current['presets'], 'preferences': current['preferences']}
            data[section] = replacement
            raw = (json.dumps(data, ensure_ascii=False, indent=2, allow_nan=False) + '\n').encode('utf-8')
            if len(raw) > MAX_STORE_BYTES:
                raise ValueError('Saved recipes must occupy less than 1 MiB in total.')
            temporary = None
            try:
                self.directory.mkdir(parents=True, exist_ok=True)
                self._check_location()
                descriptor, name = tempfile.mkstemp(prefix='.presets-', suffix='.tmp', dir=self.directory)
                temporary = Path(name)
                with os.fdopen(descriptor, 'wb') as output:
                    output.write(raw)
                    output.flush()
                    os.fsync(output.fileno())
                self._check_location()
                os.replace(temporary, self.path)
            except OSError:
                raise PresetStoreUnavailable('Could not save settings in this app folder. Existing settings were not changed.') from None
            finally:
                if temporary is not None:
                    temporary.unlink(missing_ok=True)
            return {'presets': data['presets'], 'preferences': data['preferences'],
                    'revision': hashlib.sha256(raw).hexdigest()}
