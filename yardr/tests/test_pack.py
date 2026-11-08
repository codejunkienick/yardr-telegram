"""The yardr pack's two programs, run as the yard runs them: one JSON
object on stdin, one on stdout, with only the environment the yard hands a
source's command. thread talks to a fake bridge on loopback; nothing
reaches the network."""
import http.server
import runpy
import json
import os
import shutil
import subprocess
import sys
import tempfile
import time
import threading
import unittest
from pathlib import Path

PACK = Path(__file__).resolve().parent.parent
BRIDGE = PACK.parent / 'bridge' / 'bridge.py'
NORMALIZE = PACK / 'tgbridge' / 'normalize'
THREAD = PACK / 'tgbridge' / 'thread'
SECRET = 'test-callback-secret'


def run(program, args, stdin, env=None):
    base_env = {'PATH': os.environ.get('PATH', '/usr/bin:/bin'), 'HOME': os.environ.get('HOME', '/tmp')}
    base_env.update(env or {})
    return subprocess.run([str(program), *args], input=json.dumps(stdin), capture_output=True, text=True,
                          env=base_env, timeout=30)


def entry(**overrides):
    body = {
        'id': '999:123:topic:7:41', 'kind': 'message', 'chat': '123', 'conversation': '123:topic:7',
        'topic': 'homelab', 'thread_id': 7, 'message_id': '41', 'reply_to': None,
        'from': {'id': '123', 'name': 'Pat'}, 'text': '[topic: homelab]\nIs the NAS up?',
        'forwarded': False, 'received_at': '2026-11-06T10:00:00+00:00',
    }
    body.update(overrides)
    return {'headers': {'X-Tgbridge-Secret': ['never-handed-to-normalize']}, 'body': body}


class NormalizeTests(unittest.TestCase):
    normalize = NORMALIZE

    def test_topic_entry_becomes_the_envelope(self):
        result = run(self.normalize, [], entry())
        self.assertEqual(result.returncode, 0, result.stderr)
        envelope = json.loads(result.stdout)
        self.assertEqual(envelope, {
            'type': 'message', 'action': 'create', 'actor': {'id': '123', 'name': 'Pat'},
            'thread': {'id': '123:topic:7', 'title': 'Telegram: homelab'},
            'text': '[topic: homelab]\nIs the NAS up?', 'mentions': [], 'message': '41', 'parent': '',
            'fields': {'chat': ['123'], 'topic': ['homelab'], 'kind': ['message'], 'sender': ['123'],
                       'thread': ['7']}})

    def test_general_transcript_with_a_reply(self):
        result = run(self.normalize, [], entry(conversation='123', topic='general', thread_id=None, kind='transcript',
                                          message_id='41-transcript', reply_to='40'))
        envelope = json.loads(result.stdout)
        self.assertEqual(envelope['thread'], {'id': '123', 'title': 'Telegram'})
        self.assertEqual(envelope['fields']['kind'], ['transcript'])
        self.assertEqual(envelope['parent'], '40')
        self.assertEqual(envelope['message'], '41-transcript')
        self.assertNotIn('thread', envelope['fields'])  # General has no thread id to route by

    def test_refuses_without_a_body_and_never_echoes_it(self):
        for given in ({'headers': {}}, {'body': 'text'}, {'body': {'text': 'secret words'}}):
            result = run(self.normalize, [], given)
            self.assertEqual(result.returncode, 1)
            self.assertNotIn('secret words', result.stderr)
            self.assertEqual(result.stdout, '')


class FakeBridge:
    """A loopback stand-in for the bridge's yard API: records each request
    and answers from `answers` by path."""

    def __init__(self):
        self.requests, self.answers = [], {}
        fake = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                body = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                fake.requests.append((self.path, dict(self.headers), body))
                status, answer = fake.answers.get(self.path, (200, {}))
                payload = json.dumps(answer).encode()
                self.send_response(status)
                self.send_header('Content-Type', 'application/json')
                self.send_header('Content-Length', str(len(payload)))
                self.end_headers()
                self.wfile.write(payload)

        self.server = http.server.ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self.base = f'http://127.0.0.1:{self.server.server_address[1]}'
        threading.Thread(target=self.server.serve_forever, daemon=True).start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()


class ThreadTests(unittest.TestCase):
    thread = THREAD

    def setUp(self):
        self.bridge = FakeBridge()
        self.addCleanup(self.bridge.close)
        self.env = {'TGBRIDGE_SECRET': SECRET, 'YARDR_SOURCE_BASE': self.bridge.base}

    def test_reply_sends_auto_format_with_the_key_and_answers_the_id(self):
        self.bridge.answers['/v1/send'] = (200, {'delivered': True, 'message_id': '77', 'conversation': '123'})
        result = run(self.thread, ['reply'], {'thread': '123:topic:7', 'parent': '41', 'body': '<b>Up</b>', 'key': 'k1'},
                     self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(json.loads(result.stdout), {'id': '77'})
        [(path, headers, body)] = self.bridge.requests
        self.assertEqual(path, '/v1/send')
        self.assertEqual(headers['Authorization'], 'Bearer ' + SECRET)
        self.assertEqual(body, {'conversation': '123:topic:7', 'text': '<b>Up</b>', 'key': 'k1', 'format': 'auto'})

    def test_reply_carries_files_as_attachments(self):
        self.bridge.answers['/v1/send'] = (200, {'delivered': True, 'message_id': '78'})
        result = run(self.thread, ['reply'], {'thread': '123', 'parent': '', 'body': 'shot', 'key': 'k2',
                                         'files': ['/tmp/a/shot.png']}, self.env)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(self.bridge.requests[0][2]['attachments'], ['/tmp/a/shot.png'])

    def test_reply_not_delivered_fails_with_the_reason(self):
        self.bridge.answers['/v1/send'] = (200, {'delivered': False, 'reason': 'options_need_keyboard',
                                                 'failure_kind': 'permanent'})
        result = run(self.thread, ['reply'], {'thread': '123', 'body': 'A: x\nB: y', 'key': 'k3'}, self.env)
        self.assertEqual(result.returncode, 1)
        self.assertIn('options_need_keyboard', result.stderr)
        self.assertEqual(result.stdout, '')

    def test_reply_refused_names_the_bridge_error(self):
        self.bridge.answers['/v1/send'] = (400, {'error': 'conversation ID is not the paired private chat'})
        result = run(self.thread, ['reply'], {'thread': '9', 'body': 'x', 'key': 'k4'}, self.env)
        self.assertEqual(result.returncode, 1)
        self.assertIn('HTTP 400: conversation ID is not the paired private chat', result.stderr)

    def test_posted_finds_the_ledger_send_or_nothing(self):
        self.bridge.answers['/v1/posted'] = (200, {'message_id': '77'})
        found = run(self.thread, ['posted'], {'thread': '123', 'key': 'k1'}, self.env)
        self.assertEqual(json.loads(found.stdout), {'id': '77'})
        self.bridge.answers['/v1/posted'] = (200, {})
        none = run(self.thread, ['posted'], {'thread': '123', 'key': 'k9'}, self.env)
        self.assertEqual(json.loads(none.stdout), {})
        self.assertEqual([r[2] for r in self.bridge.requests], [{'key': 'k1'}, {'key': 'k9'}])

    def test_read_maps_the_log_to_messages(self):
        self.bridge.answers['/v1/read'] = (200, {'messages': [
            {'id': '41', 'direction': 'in', 'from': 'Pat', 'text': 'long ' * 200, 'at': '2026-11-06T10:00:00+00:00'},
            {'id': '77', 'direction': 'out', 'from': 'test_bot', 'text': 'Up', 'at': '2026-11-06T10:01:00+00:00'}]})
        result = run(self.thread, ['read'], {'thread': '123:topic:7'}, self.env)
        messages = json.loads(result.stdout)['messages']
        self.assertEqual([m['id'] for m in messages], ['41', '77'])
        self.assertEqual(messages[0]['body'], 'long ' * 200)
        self.assertEqual(messages[1]['author'], {'id': 'test_bot', 'name': 'test_bot'})
        self.assertEqual(self.bridge.requests[0][2], {'conversation': '123:topic:7', 'limit': 50})

    def test_refuses_a_base_off_this_machine_and_a_missing_secret(self):
        for base in ('https://127.0.0.1:8941', 'http://example.com:8941', 'http://10.0.0.2:8941'):
            result = run(self.thread, ['posted'], {'thread': '1', 'key': 'k'}, dict(self.env, YARDR_SOURCE_BASE=base))
            self.assertEqual(result.returncode, 1, base)
            self.assertIn('loopback', result.stderr)
        result = run(self.thread, ['posted'], {'thread': '1', 'key': 'k'}, dict(self.env, TGBRIDGE_SECRET=''))
        self.assertEqual(result.returncode, 1)
        self.assertIn('TGBRIDGE_SECRET', result.stderr)
        self.assertEqual(self.bridge.requests, [])

    def test_a_bridge_that_is_down_is_named(self):
        self.bridge.close()
        result = run(self.thread, ['read'], {'thread': '123'}, self.env)
        self.assertEqual(result.returncode, 1)
        self.assertIn('is its unit running', result.stderr)


class RenderedPackTests(unittest.TestCase):
    """An instance's pack as install.sh renders it from a release's yardr/:
    the declaration's one base line names the instance's port, and every
    other byte, the legacy bases included, is the release's."""

    @classmethod
    def setUpClass(cls):
        cls.b = runpy.run_path(str(BRIDGE))

    def test_two_instances_differ_from_the_release_only_in_the_base_line(self):
        release = self.b['pack_tree'](PACK)
        bases = b'bases:\n  odyssey: http://127.0.0.1:8941\n  imd: http://127.0.0.1:8942\n'
        self.assertIn(bases, release['tgbridge.yaml'][0])
        digests = set()
        for port in (18941, 28942):
            with self.subTest(port=port):
                rendered = self.b['render_pack'](PACK, port)
                self.assertEqual(sorted(rendered), sorted(release))
                self.assertEqual({k: v for k, v in rendered.items() if k != 'tgbridge.yaml'},
                                 {k: v for k, v in release.items() if k != 'tgbridge.yaml'})
                old, new = release['tgbridge.yaml'][0], rendered['tgbridge.yaml'][0]
                self.assertEqual([(a, b) for a, b in zip(old.splitlines(True), new.splitlines(True)) if a != b],
                                 [(b'base: http://127.0.0.1:8941\n', f'base: http://127.0.0.1:{port}\n'.encode())])
                self.assertEqual(len(old.splitlines()), len(new.splitlines()))
                self.assertIn(bases, new)
                digests.add(self.b['pack_digest'](rendered))
        self.assertEqual(len(digests), 2)

    def test_a_declaration_without_exactly_one_base_line_is_refused_and_nothing_is_written(self):
        line = 'base: http://127.0.0.1:8941\n'
        text = (PACK / 'tgbridge.yaml').read_text()
        for broken, has in ((text.replace(line, ''), 'it has 0 top-level base lines'),
                            (text.replace(line, line + line), 'it has 2 top-level base lines'),
                            (text.replace(line, 'base: https://127.0.0.1:8941\n'), 'its one is another form')):
            with self.subTest(has=has), tempfile.TemporaryDirectory() as tmp:
                copy, root = Path(tmp) / 'yardr', Path(tmp) / 'root'
                shutil.copytree(PACK, copy, ignore=shutil.ignore_patterns('__pycache__'))
                (copy / 'tgbridge.yaml').write_text(broken)
                with self.assertRaises(ValueError) as caught:
                    self.b['pack_plan'](root, copy, 18941)
                self.assertEqual(str(caught.exception), f'{copy}/tgbridge.yaml must have exactly one top-level '
                                 f'line base: http://127.0.0.1:<port>; {has}')
                self.assertFalse(root.exists())

    def test_a_link_in_the_release_is_refused(self):
        with tempfile.TemporaryDirectory() as tmp:
            copy = Path(tmp) / 'yardr'
            shutil.copytree(PACK, copy, ignore=shutil.ignore_patterns('__pycache__'))
            (copy / 'tgbridge' / 'elsewhere').symlink_to('/etc/hostname')
            with self.assertRaises(ValueError) as caught:
                self.b['render_pack'](copy, 18941)
            self.assertEqual(str(caught.exception), f'{copy}/tgbridge/elsewhere is not a regular file')


def rendered_copy(cls, port):
    """cls's pack directory: this pack rendered for port into a temporary
    directory removed after the class, as install.sh stages one."""
    b = runpy.run_path(str(BRIDGE))
    tmp = tempfile.TemporaryDirectory(prefix='rendered-')
    cls.addClassCleanup(tmp.cleanup)
    b['stage_pack'](b['render_pack'](PACK, port), tmp.name)
    return Path(tmp.name)


class RenderedNormalizeTests(NormalizeTests):
    @classmethod
    def setUpClass(cls):
        cls.normalize = rendered_copy(cls, 18941) / 'tgbridge' / 'normalize'


class RenderedThreadTests(ThreadTests):
    @classmethod
    def setUpClass(cls):
        cls.thread = rendered_copy(cls, 28942) / 'tgbridge' / 'thread'


class DeclarationTests(unittest.TestCase):
    @unittest.skipUnless(shutil.which('yardr'), 'yardr is not on PATH')
    def test_yardr_reads_the_pack(self):
        binary = str(Path(shutil.which('yardr')).resolve())
        with tempfile.TemporaryDirectory(prefix='pack-') as tmp:
            root = Path(tmp)
            home = root / 'yard'
            home.mkdir(mode=0o700)
            env = dict(HOME=tmp, XDG_CONFIG_HOME=str(root / 'config'), XDG_STATE_HOME=str(root / 'state'),
                       PATH=tmp + ':/usr/local/bin:/usr/bin:/bin', USER='operator')
            command = [binary, '--home', str(home), '--actor', 'operator']
            server = subprocess.Popen(command + ['server', 'run'], env=env, cwd=root,
                                      stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 15
                while not (home / 'yard.sock').exists():
                    if server.poll() is not None or time.monotonic() > deadline:
                        self.fail('isolated pack-check server did not start')
                    time.sleep(.02)
                setup = subprocess.run(command + ['settings', 'set', 'default_kind', 'codex'],
                                       env=env, cwd=root, capture_output=True, text=True, timeout=20)
                self.assertEqual(setup.returncode, 0, setup.stderr)
                result = subprocess.run(command + ['pack', 'check', str(PACK)], env=env, cwd=root,
                                        capture_output=True, text=True, timeout=60)
            finally:
                server.terminate()
                server.wait(timeout=10)
        self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
        self.assertIn('declared source tgbridge', result.stdout)
        self.assertIn('runs 2 programs of the pack as you: tgbridge/normalize, tgbridge/thread', result.stdout)


if __name__ == '__main__':
    unittest.main()
