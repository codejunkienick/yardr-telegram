"""Missing cores skip optional native tests; incompatible supplied cores fail."""
import http.server
import json
from pathlib import Path
import socketserver
import tempfile
import threading
import unittest
from unittest.mock import patch

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
            thread = threading.Thread(target=server.serve_forever, daemon=True)
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


if __name__ == '__main__':
    unittest.main()
