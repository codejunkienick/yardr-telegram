"""Offline pack adoption through a real native hook, store and reply caller."""
import http.client
import http.server
import io
import json
from pathlib import Path
import re
import subprocess
import tempfile
import threading
import time
from urllib.parse import urlsplit

import bridge as b
import testports
import testyardr
from test_bridge import BridgeTestCase


PACK = Path(__file__).resolve().parents[1] / 'yardr'


class NativeAdoptionTests(BridgeTestCase):
    def test_rendered_pack_hook_reply_retry_markdown_and_full_thread(self):
        binary = testyardr.binary()
        if binary is None:
            self.skipTest('yardr required for isolated pack adoption')
        with tempfile.TemporaryDirectory(prefix='tga-', dir='/tmp') as tmp:
            root = Path(tmp)
            server, home, command, env = testyardr.start(root, binary)

            def cli(*args, ok=True, stdin=None):
                result = subprocess.run(command + list(args), env=env, cwd=root, input=stdin,
                                        text=True, capture_output=True, timeout=60)
                if ok:
                    self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result

            try:
                cli('init', '--harness', 'claude')
                cli('settings', 'set', 'operator', 'pat')
                requests, lose_send_response = [], [True]

                class Handler(b._callback_handler(self.bridge)):
                    def _answer(handler, status, value):
                        if handler.path == '/v1/send' and status == 200 and lose_send_response[0]:
                            lose_send_response[0] = False
                            handler.close_connection = True
                            return
                        super()._answer(status, value)

                    def do_POST(handler):
                        original = handler.rfile
                        raw = original.read(int(handler.headers['Content-Length']))
                        requests.append((handler.path, json.loads(raw)))
                        handler.rfile = io.BytesIO(raw)
                        try:
                            super().do_POST()
                        finally:
                            handler.rfile = original

                callback = testports.bind_test_server(Handler)
                worker = threading.Thread(target=callback.serve_forever, daemon=True)
                worker.start()
                try:
                    pack = root / 'pack'
                    b.stage_pack(b.render_pack(PACK, callback.server_address[1]), pack)
                    checked = cli('pack', 'check', str(pack), ok=False)
                    if checked.returncode:
                        self.assertEqual(checked.returncode, 1, checked.stdout + checked.stderr)
                        self.assertIn('thread.formats: unknown key', checked.stderr)
                        missing = cli('source', 'show', 'tgbridge', ok=False)
                        self.assertNotEqual(missing.returncode, 0, missing.stdout)
                        self.assertFalse((home / 'sources' / 'tgbridge.yaml').exists())
                        self.skipTest('pack check exit 1: thread.formats: unknown key; nothing applied')
                    self.assertIn('runs 2 programs of the pack as you: tgbridge/normalize, tgbridge/thread',
                                  checked.stdout)
                    cli('pack', 'apply', str(pack), '--yes')
                    cli('source', 'key', 'tgbridge', stdin='TGBRIDGE_SECRET=' + self.bridge.secret + '\n')
                    cli('source', 'set', 'tgbridge', 'updates_per_hour=100')
                    cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
                    credential = root / 'writer.env'
                    credential.write_text('TGBRIDGE_SECRET=' + self.bridge.secret + '\n')
                    credential.chmod(0o600)
                    cli('crew', 'add', 'writer', '--dir', str(root), '--set', 'kind=codex',
                        '--set', 'env_file=' + str(credential))
                    cli('subscribe', 'writer', 'tgbridge', '--scope', 'all', '--where', 'type=message')
                    secret = root / 'hook.secret'
                    secret.write_text('synthetic-hook-secret')
                    secret.chmod(0o600)
                    cli('hook', 'add', 'tgbridge', '--source', 'tgbridge', '--secret-file', str(secret),
                        '--unknown', 'ignore')
                    # Core rejects hook_addr port 0; reserve a kernel-selected test port before restart.
                    probe = testports.bind_test_server(http.server.BaseHTTPRequestHandler)
                    port = probe.server_address[1]
                    probe.server_close()
                    cli('settings', 'set', 'hook_addr', f'127.0.0.1:{port}')
                    testyardr.stop(server)
                    log_path = root / 'server.log'
                    with log_path.open('w') as log:
                        server = subprocess.Popen(command + ['server', 'run'], env=env, cwd=root,
                                                  stdout=log, stderr=log)
                    deadline = time.monotonic() + 15
                    while not re.search(r'hook receiver ready.*127\.0\.0\.1:' + str(port), log_path.read_text()):
                        if server.poll() is not None or time.monotonic() > deadline:
                            self.fail('isolated hook receiver did not start: ' + log_path.read_text())
                        time.sleep(.02)
                    self.bridge.hook_url = f'http://127.0.0.1:{port}/hooks/tgbridge'
                    self.bridge.hook_secret = secret.read_text()
                    self.bridge.reactions['on_accept'], self.bridge.typing_seconds = False, 0
                    accepted, lost = [], [False]

                    def request(url, body, *args, **kwargs):
                        if url != self.bridge.hook_url:
                            return self.transport(url, body, *args, **kwargs)
                        target = urlsplit(url)
                        connection = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
                        try:
                            connection.request('POST', target.path, json.dumps(body),
                                               kwargs['headers'] | {'Content-Type': 'application/json'})
                            response = connection.getresponse()
                            status, answer = response.status, json.load(response)
                        finally:
                            connection.close()
                        accepted.append((body, answer))
                        if not lost[0]:
                            lost[0] = True
                            raise b.TransportError('synthetic lost response after native accept')
                        return status, answer

                    self.bridge.request = request
                    update = self.update(40)
                    full_text = 'Synthetic long inbound ' + 'whole thread body ' * 40
                    self.assertGreater(len(full_text), 500)
                    update['message']['text'] = full_text
                    offset = self.store.offset()
                    with self.assertRaisesRegex(b.TransportError, 'lost response'):
                        self.bridge.drain([update])
                    self.assertEqual(self.store.offset(), offset)
                    self.bridge.drain([update])
                    self.assertEqual(accepted[0][0]['id'], accepted[1][0]['id'])
                    seq = accepted[0][1]['seq']
                    self.assertIs(type(seq), int)
                    self.assertGreater(seq, 0)
                    self.assertEqual(accepted[1][1]['seq'], seq)
                    self.assertEqual(self.store.offset(), 41)
                    # The paired Telegram user is unknown to core until their source identity is restored.
                    cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '456')
                    with self.assertRaisesRegex(b.TransportError, 'sender is not heard'):
                        self.bridge.drain([self.update(41)])
                    self.assertEqual(accepted[-1][1], {'dropped': True})
                    self.assertEqual(self.store.offset(), 41)
                    cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
                    self.bridge.drain([self.update(41)])
                    self.assertGreater(accepted[-1][1]['seq'], seq)
                    self.assertEqual(self.store.offset(), 42)

                    env['TGBRIDGE_SECRET'] = self.bridge.secret
                    self.calls.clear()
                    interrupted = cli('reply', str(seq), '--crew', 'writer', 'Adoption reply', ok=False)
                    self.assertNotEqual(interrupted.returncode, 0)
                    for _ in range(2):
                        cli('reply', str(seq), '--crew', 'writer', 'Adoption reply')
                    sends = [body for url, body in self.calls if url.endswith('/sendMessage')]
                    self.assertEqual(len(sends), 1)
                    self.assertEqual(sends[0]['chat_id'], 123)
                    self.assertEqual(sends[0]['text'], 'Adoption reply')
                    [(path, sent)] = [(p, body) for p, body in requests if p == '/v1/send']
                    self.assertEqual(sent['conversation'], '123')
                    self.assertEqual(self.store.saved_receipt(sent['key'])['message_id'], '77')
                    self.assertIn('/v1/posted', [path for path, _ in requests])

                    markdown = '**Ready** `<raw>`'
                    cli('reply', str(seq), '--crew', 'writer', '--format', 'markdown', markdown)
                    sends = [body for url, body in self.calls if url.endswith('/sendMessage')]
                    self.assertEqual(len(sends), 2)
                    self.assertEqual(sends[-1]['chat_id'], 123)
                    self.assertEqual(sends[-1]['parse_mode'], 'HTML')
                    self.assertEqual(sends[-1]['text'], '<b>Ready</b> <code>&lt;raw&gt;</code>')
                    self.assertEqual([body for path, body in requests if path == '/v1/send'][-1]['format'],
                                     'markdown')
                    shown = cli('thread', 'show', str(seq)).stdout
                    self.assertIn(full_text, shown)
                    self.assertIn('Adoption reply', shown)
                    self.assertIn('/v1/read', [path for path, _ in requests])
                    self.assertEqual(sum(m['text'] == full_text for m in self.store.read_messages('123', 50)), 1)

                    declaration = pack / 'tgbridge.yaml'
                    original = declaration.read_text()
                    declaration.write_text(original.replace('  formats: [markdown]\n', ''))
                    try:
                        cli('pack', 'apply', str(pack), '--yes')
                        before = len(requests), len(self.calls)
                        refused = cli('reply', str(seq), '--crew', 'writer', '--format', 'markdown',
                                      '**Declaration required**', ok=False)
                        self.assertNotEqual(refused.returncode, 0)
                        self.assertIn('thread.formats does not support the selected reply format', refused.stderr)
                        self.assertEqual((len(requests), len(self.calls)), before)
                    finally:
                        declaration.write_text(original)
                        cli('pack', 'apply', str(pack), '--yes')
                finally:
                    callback.shutdown()
                    callback.server_close()
                    worker.join(timeout=10)
                    self.assertFalse(worker.is_alive())
            finally:
                testyardr.stop(server)
