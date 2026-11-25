"""Unit tests for scripts/run_tests.py: discover pattern, serial env, worker scaling."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import json
import shutil
import subprocess
import sys
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location('tgrun_tests', ROOT / 'scripts' / 'run_tests.py')
run_tests = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_tests)


def _fake_child(*records):
    """subprocess.run stand-in: each call writes the next record to the result path."""
    pending = list(records)

    def fake(cmd, **kwargs):
        record = pending.pop(0)
        if record is not None:
            Path(cmd[cmd.index('--file-child') + 3]).write_text(json.dumps(record), encoding='utf-8')
        return mock.Mock(returncode=0)
    return fake


def _record(**over):
    base = {'selected': 1, 'ran': 1, 'failures': 0, 'errors': 0, 'skipped': 0,
            'unexpected_successes': 0}
    base.update(over)
    return base


class PatternTests(unittest.TestCase):
    def test_discover_pattern_filters_to_matching_file(self):
        ids = run_tests._discover_suite('bridge', None, 'test_testports.py')
        self.assertGreater(len(ids), 0)
        self.assertTrue(all(tid.startswith('test_testports.') for tid in ids))

    def test_build_units_with_pattern_only_matching_module(self):
        units = run_tests._build_units(None, 'test_testports.py')
        self.assertEqual(len(units), 1)
        suite_dir, label, test_ids = units[0]
        self.assertEqual((suite_dir, label), ('bridge', 'test_testports'))
        self.assertGreater(len(test_ids), 0)

    def test_no_match_pattern_builds_no_units(self):
        self.assertEqual(run_tests._build_units(None, 'zzz_no_match_*.py'), [])

    def test_empty_pattern_discovers_zero_tests(self):
        with self.subTest(mode='parallel'):
            self.assertEqual(run_tests._build_units(None, ''), [])
        with self.subTest(mode='serial'):
            tmp = Path(tempfile.mkdtemp(prefix='yt-empty-pattern-'))
            try:
                (tmp / 'test_dummy.py').write_text(
                    'import unittest\n'
                    'class DummyTests(unittest.TestCase):\n'
                    '    def test_dummy(self):\n'
                    '        pass\n',
                    encoding='utf-8',
                )
                with mock.patch.object(run_tests, 'SUITES', (str(tmp),)):
                    rc = run_tests._run_serial([], True, '')
            finally:
                shutil.rmtree(tmp, ignore_errors=True)
            self.assertEqual(rc, 5)

    def test_serial_forwards_pattern_to_discover(self):
        with mock.patch.object(run_tests.subprocess, 'run') as fake:
            fake.return_value = mock.Mock(returncode=0)
            rc = run_tests._run_serial([], True, 'test_*.py')
        self.assertEqual(rc, 0)
        for call in fake.call_args_list:
            args = call.args[0]
            self.assertIn('-p', args)
            self.assertEqual(args[args.index('-p') + 1], 'test_*.py')

    def test_pattern_with_targets_refused(self):
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            rc = run_tests.main(['-p', 'test_*.py', 'bridge/test_bridge.py'])
        self.assertEqual(rc, 2)
        self.assertIn('-p/--pattern cannot be combined', err.getvalue())


class UnknownFlagTests(unittest.TestCase):
    def test_unsupported_discover_flags_refused(self):
        for argv in (['-s', 'bridge'], ['-t', '.'], ['--durations', '5']):
            with self.subTest(argv=argv):
                err = io.StringIO()
                with contextlib.redirect_stderr(err):
                    rc = run_tests.main(argv)
                self.assertEqual(rc, 2)
                self.assertIn('supported options', err.getvalue())


class SerialEnvTests(unittest.TestCase):
    def test_serial_sets_jobs_one_for_both_suites(self):
        with mock.patch.object(run_tests.subprocess, 'run') as fake:
            fake.return_value = mock.Mock(returncode=0)
            rc = run_tests._run_serial([], False)
        self.assertEqual(rc, 0)
        self.assertEqual(fake.call_count, 2)
        for call in fake.call_args_list:
            env = call.kwargs.get('env')
            self.assertIsNotNone(env)
            self.assertEqual(env.get('TGBRIDGE_TEST_JOBS'), '1')

    def test_target_serial_sets_jobs_one(self):
        target = str(ROOT / 'bridge' / 'test_testports.py')
        with mock.patch.object(run_tests.subprocess, 'run', side_effect=_fake_child(_record())) as fake:
            rc = run_tests._run_target([target], [], serial=True)
        self.assertEqual(rc, 0)
        env = fake.call_args.kwargs.get('env')
        self.assertIsNotNone(env)
        self.assertEqual(env.get('TGBRIDGE_TEST_JOBS'), '1')

    def test_target_without_serial_inherits_env(self):
        target = str(ROOT / 'bridge' / 'test_testports.py')
        with mock.patch.object(run_tests.subprocess, 'run', side_effect=_fake_child(_record())) as fake:
            run_tests._run_target([target], [], serial=False)
        self.assertIsNone(fake.call_args.kwargs.get('env'))


class MultiFileTargetTests(unittest.TestCase):
    def test_two_files_run_two_children(self):
        targets = [str(ROOT / 'bridge' / n) for n in ('test_testports.py', 'test_topics.py')]
        with mock.patch.object(run_tests.subprocess, 'run',
                               side_effect=_fake_child(_record(), _record())) as fake:
            rc = run_tests._run_target(targets, ['-k', 'x'], serial=False)
        self.assertEqual(rc, 0)
        commands = [call.args[0] for call in fake.call_args_list]
        self.assertEqual([c[c.index('--file-child') + 1:c.index('--file-child') + 3] for c in commands],
                         [['bridge', 'test_testports.py'], ['bridge', 'test_topics.py']])
        for command in commands:
            self.assertEqual(command[-2:], ['-k', 'x'])

    def test_missing_or_malformed_record_fails(self):
        target = [str(ROOT / 'bridge' / 'test_testports.py')]
        for bad in (None, {'selected': 1}, _record(ran='1'), _record(errors=-1)):
            with self.subTest(record=bad):
                with mock.patch.object(run_tests.subprocess, 'run', side_effect=_fake_child(bad)):
                    self.assertNotEqual(run_tests._run_target(target, [], serial=False), 0)

    def test_non_file_targets_keep_unittest_fallback(self):
        with mock.patch.object(run_tests.subprocess, 'run') as fake:
            fake.return_value = mock.Mock(returncode=0)
            rc = run_tests._run_target(['test_testports.NoSuchTest'], [], serial=False)
        self.assertEqual(rc, 0)
        self.assertEqual(fake.call_count, 1)
        self.assertNotIn('discover', fake.call_args.args[0])


class DefaultJobsTests(unittest.TestCase):
    def _jobs(self, cpus, load1, env_value='ABSENT'):
        with mock.patch.object(os, 'cpu_count', return_value=cpus), \
                mock.patch.object(os, 'getloadavg', return_value=(load1, 0, 0)), \
                mock.patch.dict(os.environ):
            if env_value == 'ABSENT':
                os.environ.pop('TGBRIDGE_TEST_JOBS', None)
            else:
                os.environ['TGBRIDGE_TEST_JOBS'] = env_value
            return run_tests._default_jobs()

    def test_low_load_keeps_cap(self):
        self.assertEqual(self._jobs(16, 0.5), 8)

    def test_high_load_scales_down(self):
        self.assertEqual(self._jobs(16, 40.0), 2)

    def test_env_override_wins_under_load(self):
        self.assertEqual(self._jobs(16, 40.0, '8'), 8)

    def test_getloadavg_failure_falls_back(self):
        with mock.patch.object(os, 'cpu_count', return_value=16), \
                mock.patch.object(os, 'getloadavg', side_effect=OSError), \
                mock.patch.dict(os.environ):
            os.environ.pop('TGBRIDGE_TEST_JOBS', None)
            self.assertEqual(run_tests._default_jobs(), 8)


class WorkerEnvTests(unittest.TestCase):
    def _env(self, exported):
        with mock.patch.dict(os.environ, {}, clear=True):
            os.environ.update(exported)
            return run_tests._worker_env(
                '/w/home', '/w/tmp',
                {'GOMODCACHE': '/warm/mod', 'GOCACHE': '/warm/build'},
                '9000-9007')

    def test_blank_cache_vars_inherit_probed_paths(self):
        env = self._env({'GOMODCACHE': '', 'GOCACHE': '   '})
        self.assertEqual(env['GOMODCACHE'], '/warm/mod')
        self.assertEqual(env['GOCACHE'], '/warm/build')

    def test_unset_cache_vars_inherit_probed_paths(self):
        env = self._env({})
        self.assertEqual(env['GOMODCACHE'], '/warm/mod')
        self.assertEqual(env['GOCACHE'], '/warm/build')

    def test_explicit_cache_vars_win(self):
        env = self._env({'GOMODCACHE': '/mine/mod', 'GOCACHE': '/mine/build'})
        self.assertEqual(env['GOMODCACHE'], '/mine/mod')
        self.assertEqual(env['GOCACHE'], '/mine/build')

    def test_isolates_home_and_tmpdir(self):
        env = self._env({})
        self.assertEqual((env['HOME'], env['TMPDIR']), ('/w/home', '/w/tmp'))
        self.assertEqual(env['TEST_PORT_RANGE'], '9000-9007')


if __name__ == '__main__':
    unittest.main()


def _suite_src(body):
    return 'import unittest\n' + body


class FileTargetRunTests(unittest.TestCase):
    """Real subprocesses against a scratch tree with its own copy of the runner."""

    def setUp(self):
        self.tmp = Path(tempfile.mkdtemp(prefix='yt-targets-'))
        self.addCleanup(shutil.rmtree, self.tmp, True)
        (self.tmp / 'scripts').mkdir()
        (self.tmp / 'bridge').mkdir()
        shutil.copy(ROOT / 'scripts' / 'run_tests.py', self.tmp / 'scripts' / 'run_tests.py')

    def _write(self, name, body):
        (self.tmp / 'bridge' / name).write_text(_suite_src(body), encoding='utf-8')
        return f'bridge/{name}'

    def _run(self, *args, env=None):
        full = {k: v for k, v in os.environ.items() if k != 'FORCE_COLOR'}
        full.update(env or {})
        proc = subprocess.run([sys.executable, '-B', 'scripts/run_tests.py', *args], cwd=self.tmp,
                              env=full, capture_output=True, text=True, timeout=120)
        return proc

    PASS = 'class T(unittest.TestCase):\n    def test_alpha(self):\n        pass\n'

    def test_k_miss_in_one_of_two_files_passes(self):
        a = self._write('test_a.py', self.PASS)
        b = self._write('test_b.py', 'class U(unittest.TestCase):\n    def test_beta(self):\n        pass\n')
        proc = self._run(a, b, '-k', 'alpha')
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_k_miss_in_all_files_fails(self):
        a = self._write('test_a.py', self.PASS)
        b = self._write('test_b.py', self.PASS)
        self.assertNotEqual(self._run(a, b, '-k', 'nothing_matches').returncode, 0)

    def test_k_miss_does_not_hide_a_failure(self):
        a = self._write('test_a.py', 'class T(unittest.TestCase):\n    def test_alpha(self):\n        self.fail("x")\n')
        b = self._write('test_b.py', 'class U(unittest.TestCase):\n    def test_beta(self):\n        pass\n')
        self.assertEqual(self._run(a, b, '-k', 'alpha').returncode, 1)

    def test_setupclass_error_with_failfast_skips_later_file(self):
        a = self._write('test_a.py', 'class T(unittest.TestCase):\n    @classmethod\n'
                        '    def setUpClass(cls):\n        raise RuntimeError("boom")\n'
                        '    def test_alpha(self):\n        pass\n')
        marker = self.tmp / 'ran-b'
        b = self._write('test_b.py', 'from pathlib import Path\nclass U(unittest.TestCase):\n'
                        f'    def test_beta(self):\n        Path({str(marker)!r}).write_text("x")\n')
        proc = self._run(a, b, '-f')
        self.assertEqual(proc.returncode, 1, proc.stderr)
        self.assertFalse(marker.exists())
        proc = self._run(a, b)
        self.assertEqual(proc.returncode, 1)
        self.assertTrue(marker.exists())

    def test_setupclass_skip_counts_as_selected(self):
        a = self._write('test_a.py', 'class T(unittest.TestCase):\n    @classmethod\n'
                        '    def setUpClass(cls):\n        raise unittest.SkipTest("no")\n'
                        '    def test_alpha(self):\n        pass\n')
        self.assertEqual(self._run(a).returncode, 0)

    def test_nested_unittest_run_inside_a_test(self):
        a = self._write('test_a.py', 'import io\nclass T(unittest.TestCase):\n'
                        '    def test_outer(self):\n'
                        '        class Inner(unittest.TestCase):\n'
                        '            def test_in(self):\n                self.fail("inner")\n'
                        '        suite = unittest.defaultTestLoader.loadTestsFromTestCase(Inner)\n'
                        '        res = unittest.TextTestRunner(stream=io.StringIO()).run(suite)\n'
                        '        self.assertEqual(len(res.failures), 1)\n')
        proc = self._run(a)
        self.assertEqual(proc.returncode, 0, proc.stderr)

    def test_passing_test_printing_failed_is_not_a_failure(self):
        a = self._write('test_a.py', 'class T(unittest.TestCase):\n    def test_alpha(self):\n'
                        '        print("FAILED (failures=3)")\n'
                        '        import sys\n        print("FAILED", file=sys.stderr)\n')
        proc = self._run(a)
        self.assertEqual(proc.returncode, 0, proc.stderr)
        self.assertIn('FAILED (failures=3)', proc.stdout)

    def test_ansi_colour_forced_on(self):
        a = self._write('test_a.py', 'class T(unittest.TestCase):\n    @classmethod\n'
                        '    def setUpClass(cls):\n        raise RuntimeError("boom")\n'
                        '    def test_alpha(self):\n        pass\n')
        self.assertEqual(self._run(a, env={'FORCE_COLOR': '1'}).returncode, 1)
        b = self._write('test_b.py', self.PASS)
        self.assertEqual(self._run(b, env={'FORCE_COLOR': '1'}).returncode, 0)

    def test_child_output_passes_through_on_its_own_stream(self):
        a = self._write('test_a.py', 'class T(unittest.TestCase):\n    def test_alpha(self):\n'
                        '        print("to-stdout")\n')
        proc = self._run(a)
        self.assertIn('to-stdout', proc.stdout)
        self.assertNotIn('to-stdout', proc.stderr)

    def test_child_that_dies_without_a_record_fails(self):
        a = self._write('test_a.py', 'class T(unittest.TestCase):\n    def test_alpha(self):\n'
                        '        import os\n        os._exit(0)\n')
        self.assertNotEqual(self._run(a).returncode, 0)
