"""App-local recipe persistence, privacy boundaries, and lost-update protection."""
import copy
import json
from pathlib import Path
import tempfile
import threading
import unittest
from unittest.mock import patch

from presets import MAX_STORE_BYTES, PresetConflict, PresetStore, PresetStoreUnavailable


def recipe(ident='custom-one'):
    return {'id': ident, 'name': 'Pinned Smart', 'mode': 'smart', 'settings': {
        'width': 640, 'height': None, 'fps': 24, 'outputName': 'result.gif',
        'smart': {'targetMB': 10, 'effort': 'thorough', 'pinResolution': True, 'pinFps': True},
        'performance': {'threads': 0, 'hardwareDecode': 'none'}, 'fixedColors': ['#112233']}}


class PresetTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name).resolve()
        self.store = PresetStore(self.root)

    def tearDown(self):
        self.temp.cleanup()

    def save(self, values=None, revision='missing'):
        return self.store.replace({'presets': [recipe()] if values is None else values, 'revision': revision})

    def test_fresh_folder_is_empty_and_roundtrip_survives_restart(self):
        self.assertEqual(self.store.read(), {'presets': [], 'preferences': {'keepAwake': False}, 'revision': 'missing'})
        saved = self.save()
        self.assertEqual(PresetStore(self.root).read(), saved)
        other = self.root / 'another-copy'
        other.mkdir()
        self.assertEqual(PresetStore(other).read()['presets'], [])
        self.assertEqual(saved['presets'], [recipe()])

    def test_preference_and_preset_writes_preserve_other_section_and_share_revision(self):
        saved = self.save()
        updated = self.store.replace_preferences({'preferences': {'keepAwake': True}, 'revision': saved['revision']})
        self.assertEqual(updated['presets'], saved['presets'])
        with self.assertRaises(PresetConflict):
            self.save([], saved['revision'])
        deleted = self.save([], updated['revision'])
        self.assertEqual(deleted['preferences'], {'keepAwake': True})
        self.assertEqual(PresetStore(self.root).read(), deleted)

    def test_invalid_or_private_fields_never_replace_existing_bytes(self):
        saved = self.save()
        before = self.store.path.read_bytes()
        invalid = [dict(recipe(), source='/private'), dict(recipe(), mode='discord')]
        settings = [dict(recipe()['settings'], source={'path': '/private'}),
                    dict(recipe()['settings'], path='/private'), dict(recipe()['settings'], trim={'start': 0}),
                    dict(recipe()['settings'], outputName='../private.gif'),
                    dict(recipe()['settings'], smart={'pinFps': 'true'}),
                    dict(recipe()['settings'], performance={'threads': True}),
                    dict(recipe()['settings'], quality=float('nan')),
                    dict(recipe()['settings'], quality=10**1000),
                    dict(recipe()['settings'], customFilter='x' * 10001)]
        invalid += [dict(recipe(), settings=value) for value in settings]
        for value in invalid:
            with self.subTest(value=list(value)), self.assertRaises(ValueError):
                self.save([value], saved['revision'])
            self.assertEqual(self.store.path.read_bytes(), before)
        for values in ([recipe(), recipe()], [recipe('custom-' + str(i)) for i in range(101)]):
            with self.assertRaises(ValueError):
                self.save(values, saved['revision'])

    def test_corrupt_store_fails_closed_and_is_not_replaced(self):
        self.store.directory.mkdir()
        for raw in (b'{broken', b'{"version":1,"version":1,"presets":[]}', b'[]',
                    json.dumps({'version': 1, 'presets': [dict(recipe(), settings={'source': 'private'})]}).encode(),
                    b'x' * (MAX_STORE_BYTES + 1)):
            self.store.path.write_bytes(raw)
            with self.assertRaises(PresetStoreUnavailable):
                self.store.read()
            with self.assertRaises(PresetStoreUnavailable):
                self.save()
            self.assertEqual(self.store.path.read_bytes(), raw)

    def test_atomic_failure_preserves_original_and_cleans_temporary_file(self):
        saved = self.save()
        before = self.store.path.read_bytes()
        with patch('presets.os.replace', side_effect=OSError('disk full')):
            with self.assertRaises(PresetStoreUnavailable):
                self.save([], saved['revision'])
        self.assertEqual(self.store.path.read_bytes(), before)
        self.assertEqual(list(self.store.directory.glob('.presets-*.tmp')), [])

    def test_concurrent_same_revision_has_one_winner(self):
        saved = self.save()
        outcomes, barrier = [], threading.Barrier(2)
        def replace(index):
            barrier.wait()
            try:
                self.save([recipe('custom-' + str(index))], saved['revision'])
                outcomes.append('saved')
            except PresetConflict:
                outcomes.append('conflict')
        threads = [threading.Thread(target=replace, args=(index,)) for index in range(2)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join(3)
        self.assertEqual(sorted(outcomes), ['conflict', 'saved'])
        self.assertEqual(len(self.store.read()['presets']), 1)

    def test_symlink_store_is_not_followed(self):
        self.store.directory.mkdir()
        external = self.root / 'external.json'
        external.write_text('private')
        try:
            self.store.path.symlink_to(external)
        except OSError:
            self.skipTest('Symlink creation requires platform privileges.')
        with self.assertRaises(PresetStoreUnavailable):
            self.store.read()
        with self.assertRaises(PresetStoreUnavailable):
            self.save()
        self.assertEqual(external.read_text(), 'private')

    def test_invalid_preferences_do_not_touch_file(self):
        for preferences in ({'keepAwake': 1}, {'keepAwake': True, 'path': '/private'}, {}):
            with self.assertRaises(ValueError):
                self.store.replace_preferences({'preferences': preferences, 'revision': 'missing'})
        self.assertFalse(self.store.path.exists())


if __name__ == '__main__':
    unittest.main()
