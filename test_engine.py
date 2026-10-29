"""Meaningful standard-library regression tests; run: python -m unittest -v test_engine."""
import base64
import copy
import json
from pathlib import Path
import tempfile
import unittest
from unittest.mock import patch

from engine import Cancelled, Engine, DEFAULTS, gif_dimensions

# A valid 1 × 1 transparent GIF is sufficient for path/command validation.
GIF = base64.b64decode('R0lGODlhAQABAIAAAAAAAP///yH5BAEAAAAALAAAAAABAAEAAAIBRAA7')


class EngineTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.root = Path(self.temp.name)
        self.source = self.root / 'source.gif'
        self.source.write_bytes(GIF)
        self.engine = Engine(self.root)
        self.settings = {'source': {'kind': 'video', 'path': str(self.source)}}

    def tearDown(self):
        self.temp.cleanup()

    def test_complete_gifski_flags_and_safe_arguments(self):
        s = dict(self.settings, quality=73, lossyQuality=68, motionQuality=84,
                 effort='extra', width=480, height=270, repeat=-1, quiet=True,
                 noSort=True, bounce=True, matte='#112233', fixedColors=['#FF0000', '00FF00'])
        plan = self.engine.plan(s)
        args = plan['commands'][-1]
        for flag in ['--quality', '--lossy-quality', '--motion-quality', '--extra', '--width', '--height', '--repeat', '--quiet', '--no-sort', '--bounce', '--matte']:
            self.assertIn(flag, args)
        self.assertEqual(args.count('--fixed-color'), 2)
        self.assertNotIn('--fast', args)
        self.assertTrue(all(isinstance(arg, str) for cmd in plan['commands'] for arg in cmd))

    def test_invalid_numbers_and_color_fail_before_execution(self):
        for values in [{'quality': 0}, {'fps': 101}, {'fps': float('nan')},
                       {'speed': 0}, {'repeat': -2}, {'width': 0},
                       {'fixedColors': ['#xyz']}, {'trim': {'start': 5, 'end': 4}}]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.engine.plan(dict(self.settings, **values))

    def test_direct_input_rejects_silent_filter_loss(self):
        for values in [{'crop': {'enabled': True}}, {'reverse': True}, {'trim': {'start': 1}}, {'bounce': True}]:
            with self.subTest(values=values), self.assertRaises(ValueError):
                self.engine.plan(dict(self.settings, pipeline='direct', **values))

    def test_y4m_flags_and_bounce_restriction(self):
        p = self.engine.plan(dict(self.settings, pipeline='y4m', y4mColorOverride='bt709'))
        self.assertEqual(p['commands'][0][-1], '-')
        self.assertEqual(p['commands'][1][-1], '-')
        self.assertIn('--y4m-color-override', p['commands'][1])
        with self.assertRaises(ValueError):
            self.engine.plan(dict(self.settings, pipeline='y4m', bounce=True))

    def test_preprocessing_speed_not_applied_twice(self):
        p = self.engine.plan(dict(self.settings, speed=2, fps=15))
        self.assertIn('setpts=(PTS-STARTPTS)/2', p['commands'][0][p['commands'][0].index('-vf') + 1])
        self.assertNotIn('--fast-forward', p['commands'][1])
        self.assertEqual(p['commands'][1][p['commands'][1].index('--fps') + 1], '15')

    def test_native_sequence_speed_and_order(self):
        paths = []
        for name in ('frame10.png', 'frame2.png'):
            p = self.root / name
            p.write_bytes(b'png')  # No decoder is run by command planning.
            paths.append(str(p))
        s = dict(self.settings, source={'kind': 'sequence', 'paths': paths}, fps=20, speed=2, noSort=True)
        p = self.engine.plan(s)
        self.assertEqual(p['pipeline'], 'sequence')
        self.assertEqual(p['commands'][0][-2:], [str(Path(x).resolve()) for x in paths])
        self.assertEqual(p['commands'][0][p['commands'][0].index('--fps') + 1], '40')
        self.assertNotIn('--fast-forward', p['commands'][0])

    def test_sweep_limit_and_product(self):
        s = self.engine._normalize(self.settings)
        self.assertEqual(len(self.engine._sweep_settings(s)), 12)
        s['sweep'] = {'qualities': list(range(1, 11)), 'widths': [100, 200, 300, 400], 'fps': [10, 20]}
        with self.assertRaises(ValueError):
            self.engine._sweep_settings(s)

    def test_measured_size_search_selects_best_tested_fit(self):
        s = self.engine._normalize(self.settings)
        s['optimize'].update(targetMiB=0.00006, minQuality=40, maxQuality=90, maxTrials=12)
        jid = 'test-search'
        self.engine.jobs[jid] = {'results': [], 'mode': 'optimize'}
        def encode(jid, trial, index, total):
            result = {'index': index, 'quality': trial['quality'], 'bytes': trial['quality'], 'settings': copy.deepcopy(trial)}
            self.engine.jobs[jid]['results'].append(result)
            return result
        with patch.object(self.engine, '_encode', side_effect=encode):
            self.engine._optimize(jid, s)
        job = self.engine.jobs[jid]
        chosen = job['results'][job['bestIndex']]
        self.assertEqual(chosen['quality'], 62)
        self.assertLessEqual(chosen['bytes'], s['optimize']['targetMiB'] * 1048576)
        self.assertLessEqual(len(job['results']), 12)

    def test_target_failure_selects_smallest_without_claiming_fit(self):
        s = self.engine._normalize(self.settings)
        s['optimize'].update(targetMiB=0.000001, maxTrials=3)
        jid = 'failed-search'
        self.engine.jobs[jid] = {'results': [], 'mode': 'optimize'}
        def encode(jid, trial, index, total):
            result = {'index': index, 'bytes': 100 - index, 'settings': trial}
            self.engine.jobs[jid]['results'].append(result)
            return result
        with patch.object(self.engine, '_encode', side_effect=encode):
            self.engine._optimize(jid, s)
        job = self.engine.jobs[jid]
        self.assertEqual(len(job['results']), 3)
        self.assertEqual(job['bestIndex'], 2)
        self.assertIn('No tested candidate', job['searchNote'])

    def test_sampled_estimate_extrapolates_and_does_not_change_recipe(self):
        settings = self.engine._normalize(dict(self.settings, trim={'start': 4, 'end': 64}, speed=2))
        original = copy.deepcopy(settings)
        jid = 'estimated'
        self.engine.jobs[jid] = {'results': [], 'mode': 'estimate'}
        trials = []
        def encode(jid, trial, index, total, record=True):
            self.assertFalse(record)
            trials.append(copy.deepcopy(trial))
            span = (trial['trim']['end'] - trial['trim']['start']) / trial['speed']
            return {'index': index, 'bytes': int(span * 1000), 'settings': copy.deepcopy(trial)}
        with patch.object(self.engine, 'probe', return_value={'duration': 100}), patch.object(self.engine, '_encode', side_effect=encode):
            self.engine._estimate(jid, settings)
        job = self.engine.jobs[jid]
        estimate = job['estimate']
        self.assertEqual(estimate['estimatedBytes'], 30000)
        self.assertEqual(estimate['sampleCount'], 3)
        self.assertFalse(estimate['exact'])
        self.assertLess(estimate['lowerBytes'], 30000)
        self.assertGreater(estimate['upperBytes'], 30000)
        self.assertEqual(estimate['sampledSeconds'], 1.5)
        self.assertIsNone(job['bestIndex'])
        self.assertEqual(job['results'], [])
        self.assertEqual(settings, original)
        ranges = [(r['trim']['start'], r['trim']['end']) for r in trials]
        self.assertEqual(ranges, [(4, 5), (33.5, 34.5), (63, 64)])

    def test_estimate_cache_reuses_matching_recipe_and_invalidates_source_change(self):
        settings = self.engine._normalize(self.settings)
        jid = 'cache'
        self.engine.jobs[jid] = {'results': [], 'mode': 'estimate'}
        with patch.object(self.engine, '_encode', return_value={'bytes': 1000}) as encode:
            first = self.engine._sample_summary(jid, settings, {'duration': 30})
            second = self.engine._sample_summary(jid, settings, {'duration': 30})
            self.assertEqual(encode.call_count, 3)
            self.assertFalse(first['cacheHit'])
            self.assertTrue(second['cacheHit'])
            self.source.write_bytes(GIF + b'changed')
            self.engine._sample_summary(jid, settings, {'duration': 30})
            self.assertEqual(encode.call_count, 6)

    def test_short_estimate_is_exact_complete_encode(self):
        settings = self.engine._normalize(dict(self.settings, trim={'start': 1, 'end': 3}))
        jid = 'exact-estimate'
        self.engine.jobs[jid] = {'results': [], 'mode': 'estimate'}
        with patch.object(self.engine, 'probe', return_value={'duration': 20}), patch.object(self.engine, '_encode', return_value={'bytes': 12345}):
            self.engine._estimate(jid, settings)
        estimate = self.engine.jobs[jid]['estimate']
        self.assertTrue(estimate['exact'])
        self.assertEqual(estimate['sampleCount'], 1)
        self.assertEqual(estimate['lowerBytes'], 12345)
        self.assertEqual(estimate['upperBytes'], 12345)

    def test_discord_baseline_keeps_aspect_and_caps_source_rate(self):
        settings = self.engine._normalize(self.settings)
        base, width, fps = self.engine._discord_settings(settings, {'width': 800, 'height': 1600, 'fps': 120})
        self.assertEqual(width, 800)
        self.assertIsNone(base['height'])
        self.assertEqual(fps, 50)
        self.assertEqual(base['pipeline'], 'png')
        _, width, fps = self.engine._discord_settings(settings, {'width': 160, 'height': 90, 'fps': 12})
        self.assertEqual(width, 160)
        self.assertEqual(fps, 12)

    def test_discord_budget_is_decimal_and_measured(self):
        settings = self.engine._normalize(self.settings)
        settings['optimize']['maxTrials'] = 8
        jid = 'discord-fit'
        self.engine.jobs[jid] = {'results': [], 'mode': 'discord'}
        def encode(jid, trial, index, total):
            result = {'index': index, 'bytes': 20_000_000 if index == 0 else 20_000_001,
                      'settings': copy.deepcopy(trial)}
            self.engine.jobs[jid]['results'].append(result)
            return result
        with patch.object(self.engine, 'probe', return_value={'width': 1920, 'height': 1080, 'fps': 60}), patch.object(self.engine, '_encode', side_effect=encode):
            self.engine._discord(jid, settings)
        job = self.engine.jobs[jid]
        self.assertEqual(job['targetBytes'], 20_000_000)
        self.assertEqual(job['bestIndex'], 0)
        self.assertTrue(job['results'][0]['fit'])
        self.assertEqual(len(job['results']), 1)  # Maximum source-quality fit needs no more trials.
        self.assertTrue(all(r['settings']['fps'] <= 50 for r in job['results']))

    def test_discord_sequence_resamples_without_changing_input_timing(self):
        paths = []
        for name in ('one.png', 'two.png'):
            p = self.root / name
            p.write_bytes(b'png')
            paths.append(str(p))
        settings = self.engine._normalize(dict(self.settings, source={'kind': 'sequence', 'paths': paths}, fps=40, speed=2))
        base, width, fps = self.engine._discord_settings(settings, {'width': 320, 'height': 180})
        base['fps'] = 12.5  # Actual output 25 FPS after 2x playback.
        args = self.engine._ffmpeg(base, paths[0], 'output.png', 'png', 'frame%06d.png')
        self.assertEqual(args[args.index('-framerate') + 1], '40')
        self.assertIn('fps=25', args[args.index('-vf') + 1])

    def test_smart_samples_never_substitute_for_full_size_verification(self):
        settings = self.engine._normalize(dict(self.settings, smart={'targetMB': 1, 'effort': 'quick'}))
        jid = 'smart-fit'
        self.engine.jobs[jid] = {'results': [], 'mode': 'smart'}
        def estimate(jid, trial, metadata):
            # Deliberately optimistic estimates: first full export must fail.
            return {'estimatedBytes': int(700_000 * (trial['width'] / 1000) ** 2 * (trial['fps'] / 30) * trial['quality'] / 100)}
        def encode(jid, trial, index, total):
            size = int(1_200_000 * (trial['width'] / 1000) ** 2 * (trial['fps'] / 30) * trial['quality'] / 100)
            result = {'index': index, 'bytes': size, 'settings': copy.deepcopy(trial)}
            self.engine.jobs[jid]['results'].append(result)
            return result
        with patch.object(self.engine, 'probe', return_value={'width': 1000, 'height': 600, 'fps': 30, 'duration': 30}), patch.object(self.engine, '_sample_summary', side_effect=estimate), patch.object(self.engine, '_encode', side_effect=encode):
            self.engine._discord(jid, settings)
        job = self.engine.jobs[jid]
        self.assertEqual(job['targetBytes'], 1_000_000)
        self.assertFalse(job['results'][0]['fit'])
        best = job['results'][job['bestIndex']]
        self.assertTrue(best['fit'])
        self.assertLessEqual(best['bytes'], 1_000_000)
        self.assertLessEqual(len(job['results']), 4)
        self.assertLessEqual(job['search']['sampleTrials'], 5)
        self.assertNotIn('global optimum.', job['searchNote'].split('not')[0])

    def test_smart_no_fit_does_not_report_success(self):
        settings = self.engine._normalize(dict(self.settings, smart={'targetMB': .001, 'effort': 'quick'}))
        jid = 'no-fit'
        self.engine.jobs[jid] = {'results': [], 'mode': 'smart'}
        def encode(jid, trial, index, total):
            result = {'index': index, 'bytes': 2000 - index, 'settings': copy.deepcopy(trial)}
            self.engine.jobs[jid]['results'].append(result)
            return result
        with patch.object(self.engine, 'probe', return_value={'width': 1000, 'height': 600, 'fps': 30}), patch.object(self.engine, '_encode', side_effect=encode):
            self.engine._discord(jid, settings)
        job = self.engine.jobs[jid]
        self.assertTrue(all(not result['fit'] for result in job['results']))
        self.assertIn('No tested candidate fits', job['searchNote'])
        self.assertEqual(job['bestIndex'], len(job['results']) - 1)

    def test_smart_rejects_nonfinite_limits_and_invalid_effort(self):
        for smart in ({'targetMB': float('nan')}, {'targetMB': float('inf')}, {'targetMB': 0}, {'effort': 'forever'}):
            with self.subTest(smart=smart), self.assertRaises(ValueError):
                self.engine._normalize(dict(self.settings, smart=smart))

    def test_diminishing_quality_returns_preserve_detail(self):
        score = self.engine._fidelity_score
        self.assertGreater(score(1000, 30, 90, 1000, 30), score(850, 30, 100, 1000, 30))
        self.assertGreater(score(1000, 30, 100, 1000, 30), score(1000, 30, 90, 1000, 30))

    def test_quality_refinement_reuses_preprocessed_frames(self):
        settings = self.engine._normalize(self.settings)
        jid = 'cached-frames'
        (self.engine.outputs / jid).mkdir()
        self.engine.jobs[jid] = {'id': jid, 'results': [], 'mode': 'encode'}
        calls = []
        def run(jid, command, cwd=None):
            calls.append(command)
            if '--output' in command:
                Path(command[command.index('--output') + 1]).write_bytes(GIF)
            else:
                for i in range(2):
                    Path(command[-1].replace('%06d', f'{i:06d}')).write_bytes(b'png')
        with patch.object(self.engine, '_run', side_effect=run):
            self.engine._encode(jid, settings, 0, 2)
            settings['quality'] = 75
            self.engine._encode(jid, settings, 1, 2)
        self.assertEqual(sum('--output' not in cmd for cmd in calls), 1)
        self.assertEqual(sum('--output' in cmd for cmd in calls), 2)
        self.assertEqual(self.engine.jobs[jid]['search']['frameCacheHits'], 1)

    def test_cached_estimation_still_honors_cancellation(self):
        settings = self.engine._normalize(self.settings)
        jid = 'cancel-estimate'
        self.engine.jobs[jid] = {'results': [], 'mode': 'estimate'}
        with patch.object(self.engine, '_encode', return_value={'bytes': 1000}) as encode:
            self.engine._sample_summary(jid, settings, {'duration': 30})
            self.engine.cancelled.add(jid)
            with self.assertRaises(Cancelled):
                self.engine._sample_summary(jid, settings, {'duration': 30})
            self.assertEqual(encode.call_count, 3)

    def test_thorough_extra_effort_must_also_meet_measured_limit(self):
        settings = self.engine._normalize(dict(self.settings, smart={'targetMB': 1, 'effort': 'thorough'}))
        jid = 'extra-check'
        self.engine.jobs[jid] = {'results': [], 'mode': 'smart'}
        def encode(jid, trial, index, total):
            size = 1_000_001 if trial['effort'] == 'extra' else 999_999
            result = {'index': index, 'bytes': size, 'settings': copy.deepcopy(trial)}
            self.engine.jobs[jid]['results'].append(result)
            return result
        with patch.object(self.engine, 'probe', return_value={'width': 320, 'height': 180, 'fps': 20, 'duration': 1}), patch.object(self.engine, '_encode', side_effect=encode):
            self.engine._discord(jid, settings)
        job = self.engine.jobs[jid]
        self.assertEqual(len(job['results']), 2)
        self.assertTrue(job['results'][0]['fit'])
        self.assertFalse(job['results'][1]['fit'])
        self.assertEqual(job['bestIndex'], 0)

    def test_input_protocols_and_frame_count_are_bounded(self):
        command = self.engine.plan(self.settings)['commands'][0]
        self.assertEqual(command[command.index('-protocol_whitelist') + 1], 'file,pipe')
        self.assertEqual(command[command.index('-frames:v') + 1], '100001')

    def test_malformed_persisted_config_and_jobs_are_ignored(self):
        self.engine.config_path.write_text('[]')
        folder = self.engine.outputs / 'broken'
        folder.mkdir()
        (folder / 'job.json').write_text('[]')
        reopened = Engine(self.root)
        self.assertEqual(reopened.config, {})
        self.assertEqual(reopened.jobs, {})

    def test_restart_marks_unfinished_jobs_interrupted(self):
        folder = self.root / 'data' / 'jobs' / 'abc'
        folder.mkdir()
        (folder / 'job.json').write_text(json.dumps({'id': 'abc', 'status': 'running', 'created': 1}), encoding='utf8')
        reopened = Engine(self.root)
        self.assertEqual(reopened.get_job('abc')['status'], 'interrupted')

    def test_actual_gif_dimensions(self):
        self.assertEqual(gif_dimensions(self.source), (1, 1))
        bad = self.root / 'bad.gif'
        bad.write_text('not GIF')
        with self.assertRaises(RuntimeError):
            gif_dimensions(bad)

    def test_missing_sources_fail(self):
        with self.assertRaises(ValueError):
            self.engine.plan({'source': {'kind': 'video', 'path': str(self.root / 'missing.mp4')}})


if __name__ == '__main__':
    unittest.main()
