"""Missing cores skip optional native tests; incompatible supplied cores fail."""
import http.server
import json
import os
from pathlib import Path
import socketserver
import tempfile
import threading
import unittest
from unittest.mock import Mock, patch

import test_model_switch as switch_tests
import testyardr
import yard_commands as y


class NativeFixtureTests(unittest.TestCase):
    def test_missing_or_incompatible_native_core(self):
        case = switch_tests.NativeSwitchTests(methodName='test_complete_real_native_poll_slice')
        with patch.object(testyardr, 'binary', return_value=None), patch.object(testyardr, 'start') as start:
            with self.assertRaisesRegex(unittest.SkipTest, 'crew_handoff_switch v1'):
                case.setUp()
            start.assert_not_called()
        for response in (b'{"crew_handoff_switch":0}', b'{"crew_handoff_switch":true}', b'PRIVATE'):
            with self.subTest(response=response), tempfile.TemporaryDirectory(prefix='mf') as root:
                case = switch_tests.NativeSwitchTests(methodName='test_complete_real_native_poll_slice')
                env = dict(HOME=root, PATH=root + ':/usr/bin:/bin')
                with patch.object(testyardr, 'binary', return_value='/provided/yardr'), \
                        patch.object(testyardr, 'start', return_value=(object(), Path(root), [], env)), \
                        patch.object(testyardr, 'stop'), \
                        patch.object(y, 'run_yardr', return_value=(0, response, b'PRIVATE', None)) as read, \
                        patch.object(case, 'cli', return_value='') as initialize:
                    try:
                        with self.assertRaisesRegex(AssertionError, 'supplied core requires crew_handoff_switch v1'):
                            case.setUp()
                        initialize.assert_called_once_with('init', '--harness', 'claude')
                        self.assertEqual(read.call_count, 1)
                        self.assertEqual(read.call_args.args[0][-4:], ['crew', 'switch', 'capabilities', '--json'])
                    finally:
                        case.doCleanups()

    def test_feature_probe_names_one_method_and_refuses_unknown_answers(self):
        answers = {}

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                self.rfile.read(int(self.headers['Content-Length']))
                raw = answers[self.path.removeprefix('/call/')]
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        with tempfile.TemporaryDirectory(prefix='fp') as root:
            server = Server(str(Path(root) / 'yard.sock'), Handler)
            thread = threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True)
            thread.start()
            try:
                answers.update(SkillDigest=json.dumps({'error': {'code': 'error', 'message': 'skill name "": invalid'}}).encode(),
                               Absent=json.dumps({'error': {'code': 'error', 'message': 'no such method Absent'}}).encode(),
                               Other=json.dumps({'error': {'code': 'error', 'message': 'no such method SkillDigest'}}).encode(),
                               Garbled=b'PRIVATE', Empty=b'{}')
                self.assertTrue(testyardr.serves(Path(root), 'SkillDigest'))
                self.assertFalse(testyardr.serves(Path(root), 'Absent'))
                # Another method's absence is no answer about this one.
                self.assertTrue(testyardr.serves(Path(root), 'Other'))
                for method in ('Garbled', 'Empty'):
                    with self.assertRaisesRegex(AssertionError, 'answered neither'):
                        testyardr.serves(Path(root), method)
            finally:
                server.shutdown()
                server.server_close()
                thread.join()


class CachedToolchainTests(unittest.TestCase):
    def _layout(self, toolchains, go_mod='module example.dev/x\n\ngo 1.27.4\n'):
        self._tmp.__enter__()
        root = Path(self._tmp.name)
        src = root / 'src'
        src.mkdir()
        (src / 'go.mod').write_text(go_mod)
        modcache = root / 'modcache'
        for name, runnable in toolchains:
            bindir = modcache / 'golang.org' / name / 'bin'
            bindir.mkdir(parents=True)
            exe = bindir / 'go'
            exe.write_text('#!/bin/sh\n')
            if runnable:
                exe.chmod(0o755)
        return src, {'GOMODCACHE': str(modcache)}

    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='ct')
        self.addCleanup(self._tmp.__exit__, None, None, None)
        self.platform = ('linux', 'amd64')

    def test_selects_minimum_satisfying_toolchain(self):
        src, env = self._layout([
            ('toolchain@v0.0.1-go1.27.3.linux-amd64', True),
            ('toolchain@v0.0.1-go1.27.4.linux-amd64', True),
            ('toolchain@v0.0.1-go1.28.0.linux-amd64', True),
        ])
        got = testyardr._cached_toolchain_go(src, env, self.platform)
        self.assertEqual(got, str(Path(env['GOMODCACHE']) / 'golang.org'
                                 / 'toolchain@v0.0.1-go1.27.4.linux-amd64' / 'bin' / 'go'))

    def test_exact_toolchain_line_wins_over_minimum(self):
        src, env = self._layout(
            [('toolchain@v0.0.1-go1.27.4.linux-amd64', True),
             ('toolchain@v0.0.1-go1.28.0.linux-amd64', True)],
            go_mod='module example.dev/x\n\ngo 1.27.4\ntoolchain go1.28.0\n')
        got = testyardr._cached_toolchain_go(src, env, self.platform)
        self.assertEqual(got, str(Path(env['GOMODCACHE']) / 'golang.org'
                                 / 'toolchain@v0.0.1-go1.28.0.linux-amd64' / 'bin' / 'go'))

    def test_exact_toolchain_line_without_match_returns_none(self):
        src, env = self._layout(
            [('toolchain@v0.0.1-go1.27.4.linux-amd64', True)],
            go_mod='module example.dev/x\n\ngo 1.27.4\ntoolchain go1.28.0\n')
        self.assertIsNone(testyardr._cached_toolchain_go(src, env, self.platform))

    def test_ignores_other_platforms_duds_and_missing_cache(self):
        src, env = self._layout([
            ('toolchain@v0.0.1-go9.99.9.darwin-arm64', True),
            ('toolchain@v0.0.1-go9.99.9.linux-amd64', False),
            ('toolchain@garbage.linux-amd64', True),
        ])
        self.assertIsNone(testyardr._cached_toolchain_go(src, env, self.platform))
        self.assertIsNone(testyardr._cached_toolchain_go(src, {}, self.platform))

    def test_nothing_satisfying_returns_none(self):
        src, env = self._layout([('toolchain@v0.0.1-go1.27.3.linux-amd64', True)])
        self.assertIsNone(testyardr._cached_toolchain_go(src, env, self.platform))


class FallbackGoTests(unittest.TestCase):
    def setUp(self):
        self._tmp = tempfile.TemporaryDirectory(prefix='fb')
        self.addCleanup(self._tmp.__exit__, None, None, None)
        root = Path(self._tmp.name)
        self.src = root / 'src'
        self.src.mkdir()
        (self.src / 'go.mod').write_text('module example.dev/x\n\ngo 1.27.4\n')
        goroot = root / 'goroot'
        (goroot / 'bin').mkdir(parents=True)
        self.go = goroot / 'bin' / 'go'
        self.go.write_text('#!/bin/sh\n')
        self.go.chmod(0o755)
        self.goroot = str(goroot)
        self.env = {'GOMODCACHE': '/warm/mod', 'GOCACHE': '/warm/build', 'GOPROXY': 'off'}

    def _probe(self, goversion, returncode=0):
        return Mock(returncode=returncode, stdout='%s\n%s\n' % (self.goroot, goversion))

    def test_satisfying_local_go_is_used(self):
        with patch.object(testyardr.subprocess, 'run', return_value=self._probe('go1.27.4')) as run:
            self.assertEqual(testyardr._fallback_go(self.src, self.env), str(self.go))
        _, kwargs = run.call_args
        self.assertEqual(kwargs['env']['GOTOOLCHAIN'], 'local')

    def test_too_old_local_go_returns_none_without_switching(self):
        with patch.object(testyardr.subprocess, 'run', return_value=self._probe('go1.27.3')) as run:
            self.assertIsNone(testyardr._fallback_go(self.src, self.env))
        for call in run.call_args_list:
            self.assertEqual(call.kwargs['env']['GOTOOLCHAIN'], 'local')

    def test_exact_toolchain_line_needs_exact_local(self):
        (self.src / 'go.mod').write_text('module example.dev/x\n\ngo 1.27.4\ntoolchain go1.28.0\n')
        with patch.object(testyardr.subprocess, 'run', return_value=self._probe('go1.28.1')):
            self.assertIsNone(testyardr._fallback_go(self.src, self.env))
        with patch.object(testyardr.subprocess, 'run', return_value=self._probe('go1.28.0')):
            self.assertEqual(testyardr._fallback_go(self.src, self.env), str(self.go))

    def test_probe_failure_returns_none(self):
        with patch.object(testyardr.subprocess, 'run', side_effect=OSError):
            self.assertIsNone(testyardr._fallback_go(self.src, self.env))
        with patch.object(testyardr.subprocess, 'run', return_value=self._probe('go1.27.4', returncode=1)):
            self.assertIsNone(testyardr._fallback_go(self.src, self.env))

    def test_missing_go_binary_returns_none(self):
        self.go.unlink()
        with patch.object(testyardr.subprocess, 'run', return_value=self._probe('go1.27.4')):
            self.assertIsNone(testyardr._fallback_go(self.src, self.env))

    def test_store_fixture_reports_missing_cache_without_building(self):
        with tempfile.TemporaryDirectory(prefix='fx') as tmp:
            root, src = Path(tmp) / 'root', Path(tmp) / 'src'
            root.mkdir()
            src.mkdir()
            (src / 'go.mod').write_text('module example.dev/x\n\ngo 9.99.9\n')
            (src / 'go.sum').write_text('')
            with patch.object(testyardr, '_cached_toolchain_go', return_value=None), \
                    patch.object(testyardr, '_fallback_go', return_value=None) as fallback, \
                    patch.object(testyardr.subprocess, 'run',
                                 side_effect=AssertionError('no build')) as run, \
                    patch.dict(os.environ, {'TGBRIDGE_TEST_YARDR_SOURCE': str(src),
                                            'GOMODCACHE': '/warm/mod', 'GOCACHE': '/warm/build'}):
                with self.assertRaisesRegex(AssertionError, 'warmed Go toolchain cache'):
                    testyardr.store_fixture(root, 'general')
            fallback.assert_called_once()
            run.assert_not_called()


if __name__ == '__main__':
    unittest.main()
