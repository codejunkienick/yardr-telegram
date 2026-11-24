"""Unit tests for scripts/run_tests.py: discover pattern, serial env, worker scaling."""
import contextlib
import importlib.util
import io
import os
from pathlib import Path
import shutil
import tempfile
import unittest
from unittest import mock

ROOT = Path(__file__).resolve().parents[1]
_SPEC = importlib.util.spec_from_file_location('tgrun_tests', ROOT / 'scripts' / 'run_tests.py')
run_tests = importlib.util.module_from_spec(_SPEC)
_SPEC.loader.exec_module(run_tests)


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
        with mock.patch.object(run_tests.subprocess, 'run') as fake:
            fake.return_value = mock.Mock(returncode=0)
            rc = run_tests._run_target([target], [], serial=True)
        self.assertEqual(rc, 0)
        env = fake.call_args.kwargs.get('env')
        self.assertIsNotNone(env)
        self.assertEqual(env.get('TGBRIDGE_TEST_JOBS'), '1')

    def test_target_without_serial_inherits_env(self):
        target = str(ROOT / 'bridge' / 'test_testports.py')
        with mock.patch.object(run_tests.subprocess, 'run') as fake:
            fake.return_value = mock.Mock(returncode=0)
            run_tests._run_target([target], [], serial=False)
        self.assertIsNone(fake.call_args.kwargs.get('env'))


class MultiFileTargetTests(unittest.TestCase):
    def _run_mocked(self, names, returncodes=(0,)):
        targets = [str(ROOT / 'bridge' / name) for name in names]
        with mock.patch.object(run_tests.subprocess, 'run') as fake:
            if len(returncodes) == 1:
                fake.return_value = mock.Mock(returncode=returncodes[0])
            else:
                fake.side_effect = [mock.Mock(returncode=c) for c in returncodes]
            rc = run_tests._run_target(targets, [], serial=False)
        return rc, [call.args[0] for call in fake.call_args_list]

    def _discover_cmd(self, name):
        return [run_tests.sys.executable, '-B', '-m', 'unittest',
                'discover', '-s', 'bridge', '-p', name]

    def test_two_files_run_two_discovers(self):
        rc, commands = self._run_mocked(['test_testports.py', 'test_topics.py'])
        self.assertEqual(rc, 0)
        self.assertEqual(commands, [
            self._discover_cmd('test_testports.py'),
            self._discover_cmd('test_topics.py'),
        ])

    def test_multi_file_returns_first_failure(self):
        rc, _ = self._run_mocked(['test_testports.py', 'test_topics.py'], (0, 3))
        self.assertEqual(rc, 3)
        rc, _ = self._run_mocked(['test_testports.py', 'test_topics.py'], (2, 3))
        self.assertEqual(rc, 2)

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


if __name__ == '__main__':
    unittest.main()
