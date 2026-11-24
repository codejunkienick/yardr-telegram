"""Read states: landed -> read -> done over fake Telegram and fake or real yardr."""
from contextlib import closing
from html import unescape
import http.client
import http.server
import io
import json
from pathlib import Path
import queue
import re
import socket
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import parse_qs, urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
import testports  # noqa: E402
import testyardr  # noqa: E402
import yard_commands as y  # noqa: E402
from test_bridge import BridgeTestCase, write_settings  # noqa: E402
from test_typed_nudge import nudged  # noqa: E402

PACK = Path(__file__).resolve().parents[1] / 'yardr'
DOVE = '\U0001F54A'
EYES = '\U0001F440'
OK_HAND = '\U0001F44C'
THUMBS_UP = '\U0001F44D'
REACTION_INVALID = (400, dict(ok=False, error_code=400, description='Bad Request: REACTION_INVALID'))


class FakeReadYard:
    """Minimal framed Unix RPC socket serving Head, GetUser, EventsSince, GetCrew, Events, NudgeSession and GET /events."""

    def __init__(self, root, crew='yardmaster', source='tgbridge'):
        self.root = Path(root)
        self.calls = []
        self.hook_events = {}
        self.crew = {
            crew: dict(name=crew, journal=crew + '-journal', state='idle', status='', cursor=0),
        }
        self.journal_events = {crew + '-journal': []}
        self.nudges = {}
        self.nudge_mode = None
        self.override = {}
        self.head_seq = 0
        self.stream_events = []
        self.stream_requests = []
        self.stream_mode = None
        self._stream_lock = threading.Lock()
        self._stream_cv = threading.Condition(self._stream_lock)
        self._stream_sockets = set()
        self._closed = False
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

            def do_GET(self):
                parsed = urlsplit(self.path)
                if parsed.path != '/events':
                    self._send(404, {'error': {'code': 'not-found', 'message': 'not found'}})
                    return
                qs = parse_qs(parsed.query)
                after = int(qs.get('after', ['0'])[0])
                owner.stream_requests.append(after)
                if owner.stream_mode == 'http_404':
                    self._send(404, b'not found')
                    return
                if owner.stream_mode == 'http_503':
                    self._send(503, b'replacing')
                    return
                self.send_response(200)
                self.send_header('Content-Type', 'application/x-ndjson')
                self.send_header('Transfer-Encoding', 'chunked')
                self.end_headers()
                self.wfile.flush()
                with owner._stream_lock:
                    owner._stream_sockets.add(self.connection)
                sent_seq = after
                try:
                    while not owner._closed:
                        with owner._stream_cv:
                            batch = [e for e in owner.stream_events if e['seq'] > sent_seq]
                            if not batch and not owner._closed:
                                owner._stream_cv.wait(timeout=0.5)
                                batch = [e for e in owner.stream_events if e['seq'] > sent_seq]
                        for ev in batch:
                            line = (json.dumps(ev) + '\n').encode()
                            chunk = f'{len(line):x}\r\n'.encode() + line + b'\r\n'
                            self.wfile.write(chunk)
                            self.wfile.flush()
                            sent_seq = ev['seq']
                except OSError:
                    pass
                finally:
                    with owner._stream_lock:
                        owner._stream_sockets.discard(self.connection)

            def do_POST(self):
                length = int(self.headers.get('Content-Length', '0'))
                args = json.loads(self.rfile.read(length))
                method = self.path.removeprefix('/call/')
                owner.calls.append((method, args))
                handler = owner.override.get(method)
                if handler is not None:
                    outcome = handler(args, self)
                    if outcome is None:
                        return
                    status, payload = outcome
                    self._send(status, payload)
                    return
                if method == 'Head':
                    with owner._stream_lock:
                        head = max([owner.head_seq, *owner.hook_events.keys(),
                                    *(e['seq'] for e in owner.stream_events)], default=0)
                    self._send(200, {'result': head})
                    return
                if method == 'GetUser':
                    self._send(200, {'result': dict(name='pat', role='operator',
                                                    identities={source: {'id': '123'}})})
                    return
                if method == 'EventsSince':
                    after, limit = args[0], args[1]
                    seqs = sorted(s for s in owner.hook_events if s > after)[:limit]
                    self._send(200, {'result': [owner.hook_events[s] for s in seqs]})
                    return
                if method == 'GetCrew':
                    name = args.get('name') if isinstance(args, dict) else None
                    member = owner.crew.get(name)
                    if member is None:
                        self._send(200, {'error': {'code': 'not-found', 'message': 'no such crew member'}})
                        return
                    self._send(200, {'result': dict(member)})
                    return
                if method == 'Events':
                    bead, limit = args[0], args[1]
                    events = owner.journal_events.get(bead, [])
                    self._send(200, {'result': events[-limit:]})
                    return
                if method == 'NudgeSession':
                    if owner.nudge_mode == 'no-nudge-method':
                        self._send(200, {'error': {'code': 'error', 'message': 'no such method NudgeSession'}})
                        return
                    target = args.get('target') if isinstance(args, dict) else None
                    if not target:
                        self._send(200, {'error': {'code': 'error', 'message': 'target required'}})
                        return
                    member = owner.crew.get(target)
                    if member is None:
                        self._send(200, {'error': {'code': 'not-found', 'message': 'no such session, crew member or bead'}})
                        return
                    if member.get('state') != 'running':
                        self._send(200, {'error': {'code': 'error', 'message': 'only a running session is nudged'}})
                        return
                    user = args.get('user', '') if isinstance(args, dict) else ''
                    key = 'crew:' + target + (':for:' + user if user else '')
                    if key in owner.nudges:
                        self._send(200, {'error': {'code': 'conflict', 'message': 'a nudge stands already'}})
                        return
                    ident = f'nudge-{len(owner.nudges) + 1}'
                    owner.nudges[key] = dict(
                        id=ident, target=target, user=user, text=args.get('text', ''),
                        clear=args.get('clear'), actor=args.get('actor'),
                    )
                    self._send(200, {'result': dict(
                        id=ident, crew=target, user=user, text=args.get('text', ''), state='asked',
                    )})
                    return
                self._send(200, {'error': {'code': 'error', 'message': 'no such method ' + method}})

            def _send(self, status, payload):
                raw = json.dumps(payload).encode() if not isinstance(payload, bytes) else payload
                self.send_response(status)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.server = Server(str(self.root / 'yard.sock'), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def push_event(self, event):
        with self._stream_cv:
            self.stream_events.append(event)
            self.head_seq = max(self.head_seq, event['seq'])
            self._stream_cv.notify_all()

    def drop_streams(self):
        with self._stream_lock:
            sockets = list(self._stream_sockets)
        for s in sockets:
            try:
                s.shutdown(socket.SHUT_RDWR)
            except OSError:
                pass
            try:
                s.close()
            except OSError:
                pass

    def close(self):
        with self._stream_cv:
            self._closed = True
            self._stream_cv.notify_all()
        self.drop_streams()
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


def inbox_read_event(seq, reader='yardmaster', through=10, session=''):
    return dict(
        seq=seq, kind='inbox_read', actor=reader, at='2026-11-24T09:30:00Z',
        data=dict(reader=reader, through=through, session=session),
    )


def hook_event(seq, source='tgbridge', deliveries=None):
    return dict(
        seq=seq, id=str(seq), kind='hook', actor='hook:tgbridge', at='2026-11-21T16:00:00Z',
        data=dict(hook='tgbridge', source=source, deliveries={'yardmaster': 'all'} if deliveries is None else deliveries),
    )


def replied_event(seq, target_seq, crew='yardmaster', source='tgbridge', journal='yardmaster-journal'):
    return dict(
        seq=seq, id=str(seq), bead=journal, kind='replied', actor=crew, at='2026-11-21T16:01:00Z',
        data=dict(crew=crew, source=source, thread='123', url='http://127.0.0.1/reply', seq=target_seq,
                  message=f'reply-{target_seq}'),
    )


class ReadStateCase(BridgeTestCase):
    CREW = 'yardmaster'
    SOURCE = 'tgbridge'
    CARDS = True
    BOT_ID = 999

    def setUp(self):
        super().setUp()
        self.yard_dir = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='tgrs-fake-', dir='/tmp')))
        self.native = FakeReadYard(self.yard_dir, self.CREW, self.SOURCE)
        self.addCleanup(self.native.close)
        self.mono = [1000.0]
        self.wall = [2000.0]
        self.next_seq = 10
        self.next_reply_seq = 500
        self.next_sent_id = 77
        self.reaction_answer = (200, dict(ok=True, result=True))
        self.send_answer = None
        self.logs = []
        self.config.update(
            yardr_home=str(self.yard_dir),
            yardr_socket=str(self.yard_dir / 'yard.sock'),
            yardr_operator='pat',
            agent=self.CREW, yardr_source=self.SOURCE, yardr_cards=self.CARDS, bot_id=self.BOT_ID,
            reactions=dict(on_accept=True, seen=DOVE, read=EYES, done=OK_HAND),
        )
        self.restart()

    def restart(self, **overrides):
        config = dict(self.config, **overrides)
        self.bridge = b.Bridge(
            config, self.store, 'SECRET', 'CAPABILITY',
            request=self.transport, request_multipart=self.multipart_transport,
            request_download=self.download_transport, sleep=lambda s: None,
            monotonic=lambda: self.mono[0],
        )
        self.bridge.run_asks.now = lambda: self.wall[0]

    def transport(self, url, body, *args, **kwargs):
        if url == self.bridge.hook_url:
            self.calls.append((url, body))
            seq = self.next_seq
            self.next_seq += 1
            if seq not in self.native.hook_events:
                ev = hook_event(seq, source=self.SOURCE, deliveries={self.CREW: "all"})
                self.native.hook_events[seq] = ev
                self.native.push_event(ev)
            return 200, dict(seq=seq)
        if url.endswith('/sendMessage'):
            self.calls.append((url, body))
            if isinstance(self.send_answer, Exception):
                raise self.send_answer
            if callable(self.send_answer):
                return self.send_answer(body)
            if self.send_answer is not None:
                return self.send_answer
            mid = self.next_sent_id
            self.next_sent_id += 1
            return 200, dict(ok=True, result=dict(message_id=mid, chat=dict(id=123)))
        if url.endswith('/setMessageReaction'):
            self.calls.append((url, body))
            if isinstance(self.reaction_answer, Exception):
                raise self.reaction_answer
            if callable(self.reaction_answer):
                return self.reaction_answer(body)
            return self.reaction_answer
        if url.endswith('/answerCallbackQuery'):
            self.calls.append((url, body))
            return 200, dict(ok=True, result=True)
        if url.endswith('/editMessageText'):
            self.calls.append((url, body))
            return 200, dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=123)))
        return super().transport(url, body, *args, **kwargs)

    def poll(self, *updates):
        telegram = self.bridge.telegram

        def fetched(method, body, timeout=20):
            return (200, dict(ok=True, result=list(updates))) if method == 'getUpdates' else telegram(method, body, timeout)

        with patch.object(self.bridge, 'telegram', side_effect=fetched), \
                patch.object(self.bridge, '_log', side_effect=self.logs.append):
            self.bridge.poll_once()
        self.bridge.reaction_queue.join()

    def drain(self, *updates):
        with patch.object(self.bridge, '_log', side_effect=self.logs.append):
            self.bridge.drain(list(updates))
        self.bridge.reaction_queue.join()

    def reaction_emojis(self):
        bodies = [body for url, body in self.calls if url.endswith('/setMessageReaction')]
        for body in bodies:
            self.assertLessEqual(len(body['reaction']), 1)
            if body['reaction']:
                self.assertEqual(body['reaction'][0]['type'], 'emoji')
        return [body['reaction'][0]['emoji'] if body['reaction'] else None for body in bodies]

    def row(self, mid):
        return self.bridge.read_states.get(mid)

    def native_methods(self):
        return [method for method, _ in self.native.calls
                if method in ('EventsSince', 'GetCrew', 'Events')]

    def _sends(self):
        return [body for url, body in self.calls if url.endswith('/sendMessage')]

    def _edits(self):
        return [body for url, body in self.calls if url.endswith('/editMessageText')]

    def _answers(self):
        return [body for url, body in self.calls if url.endswith('/answerCallbackQuery')]

    def _nudge_writes(self):
        return [args for m, args in self.native.calls if m == 'NudgeSession' and args.get('target')]

    def _tap(self, token, action='nudge', uid=40, **message):
        tn_row = self.bridge.typed_nudge.get(token)
        base = dict(message_id=tn_row['message'], chat=dict(id=123, type='private'),
                    **{'from': dict(id=self.bridge.config['bot_id'], is_bot=True)})
        base.update(message)
        return dict(update_id=uid, callback_query=dict(
            id=f'q{uid}', data=f'nudge:{token}:{action}', message=base,
            **{'from': dict(id=123, is_bot=False)},
        ))

    def _enable_offers(self, nudge_after=300, status='idle'):
        self.native.crew[self.CREW].update(kind='claude', model='opus', state='running', status=status)
        self.restart(
            commands=(y.NUDGE_COMMAND,),
            reactions=dict(self.config['reactions'], nudge_after=nudge_after),
        )

    def _wait_until(self, predicate, timeout=5.0, message='condition not met'):
        deadline = time.monotonic() + timeout
        while time.monotonic() < deadline:
            if predicate():
                return
            time.sleep(0.02)
        self.fail(message)



class ReadStateTests(ReadStateCase):
    def test_settings_without_read_makes_no_rows_and_no_native_calls(self):
        self.restart(reactions=dict(on_accept=True, seen=EYES, done=OK_HAND))
        self.drain(self.update(uid=10))
        self.assertEqual(self.reaction_emojis(), [EYES])
        self.assertIsNone(self.row(10))
        self.mono[0] += 120
        self.poll()
        self.assertEqual(self.native_methods(), [])

    def test_settings_read_without_yardr_home_refuses_naming_reactions_read(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = write_settings(Path(tmp))
            path.write_text(path.read_text() + '\n[reactions]\nread = "' + EYES + '"\n')
            with self.assertRaisesRegex(b.SettingsError, r'reactions\.read'):
                b.load_settings(path)

    def test_full_lifecycle_replace_not_stack_and_idle_call_counts(self):
        # Idle poll with no open row makes 0 native calls.
        self.poll()
        self.assertEqual(self.native_methods(), [])

        # Accept inserts row in landed and sets dove.
        self.drain(self.update(uid=10))
        row = self.row(10)
        self.assertEqual(row['state'], 'landed')
        self.assertEqual(row['seq'], 10)
        self.assertEqual(row['conversation_id'], '123')
        self.assertIsNone(row['thread_id'])
        self.assertIsNone(row['crew'])
        self.assertEqual(self.reaction_emojis(), [DOVE])

        # Replayed update keeps one row (INSERT OR IGNORE) and sends no second dove.
        self.bridge.inbound(self.update(uid=10))
        self.bridge.reaction_queue.join()
        with closing(sqlite3.connect(self.path)) as db:
            count = db.execute('SELECT count(*) FROM read_states WHERE message_id=10').fetchone()[0]
        self.assertEqual(count, 1)
        self.assertEqual(self.reaction_emojis(), [DOVE])

        # Round 1: EventsSince resolves crew, GetCrew cursor 0 < 10 stays landed, 0 Events calls.
        self.poll()
        self.assertEqual(self.native_methods(), ['EventsSince', 'GetCrew'])
        self.assertEqual(self.row(10)['crew'], self.CREW)
        self.assertEqual(self.row(10)['state'], 'landed')
        self.assertEqual(self.reaction_emojis(), [DOVE])

        # Round 2 before 5 s monotonic does nothing.
        self.mono[0] += 4.0
        self.native.calls.clear()
        self.native.crew[self.CREW]['cursor'] = 10
        self.poll()
        self.assertEqual(self.native_methods(), [])
        self.assertEqual(self.row(10)['state'], 'landed')

        # Round 2 at +5 s: EventsSince not called again, GetCrew moves landed -> read (eyes).
        self.mono[0] += 1.0
        self.poll()
        self.assertEqual(self.native_methods(), ['GetCrew'])
        self.assertEqual(self.row(10)['state'], 'read')
        self.assertIsNotNone(self.row(10)['read_at'])
        self.assertEqual(self.reaction_emojis(), [DOVE, EYES])

        # With only `read` row and no send, next round makes 0 native calls.
        self.mono[0] += 60.0
        self.native.calls.clear()
        self.poll()
        self.assertEqual(self.native_methods(), [])

        # A reply send inserts a non-reaction delivery row; reply event logged after send.
        self.assertTrue(self.bridge.publish(self.publish(key='reply-1'))['delivered'])
        self.native.journal_events[self.CREW + '-journal'].append(replied_event(501, 10, crew=self.CREW, source=self.SOURCE, journal=self.CREW + "-journal"))
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.native_methods(), ['GetCrew', 'Events'])
        self.assertEqual(self.row(10)['state'], 'done')
        self.assertIsNotNone(self.row(10)['done_at'])
        self.assertEqual(self.reaction_emojis(), [DOVE, EYES, OK_HAND])

        # Idle again after done: 0 native calls.
        self.mono[0] += 60.0
        self.native.calls.clear()
        self.poll()
        self.assertEqual(self.native_methods(), [])

    def test_accept_only_for_messages_never_commands_asks_or_transcripts(self):
        command = dict(name='deploy', description='Deploy', handler='/bin/true', menu=True,
                       callback_prefix='deploy:', timeout=10)
        self.restart(commands=(command,))
        cmd_update = self.update(uid=10)
        cmd_update['message']['text'] = '/deploy'
        with patch.object(self.bridge, 'run_command'):
            self.drain(cmd_update)
        self.assertIsNone(self.row(10))

        ask_id = b.Bridge._ask_id_for_key('ask-1')
        request = dict(self.publish(key='ask-1', text='Ship?'),
                       keyboard=[[dict(text='Yes', callback_data=f'ask:{ask_id}:yes')]])
        self.assertTrue(self.bridge.publish(request)['delivered'])
        press = dict(update_id=11, callback_query=dict(
            id='q11', data=f'ask:{ask_id}:yes', chat_instance='x', **{'from': dict(id=123, is_bot=False)},
            message=dict(message_id=77, chat=dict(id=123, type='private'))))
        self.drain(press)
        self.assertIsNone(self.row(77))

        # Transcript relay calls _relay(..., kind='transcript') directly without _accepted.
        self.bridge._relay(12, '12-transcript', 'transcript text', kind='transcript')
        self.assertIsNone(self.row(12))

        # _accepted never raises and cursor advances even if read_states.record fails.
        with patch.object(self.bridge.read_states, 'record', side_effect=sqlite3.OperationalError('locked')):
            self.drain(self.update(uid=13))
        self.assertEqual(self.store.offset(), 14)
        self.assertEqual(self.reaction_emojis(), [DOVE])

    def test_call_counts_one_getcrew_per_crew_and_events_two_round_mark_and_pagination(self):
        # Configure a topic routed to `secretary`.
        self.native.crew['secretary'] = dict(name='secretary', journal='secretary-journal',
                                             state='idle', status='', cursor=0)
        self.native.journal_events['secretary-journal'] = []
        self.restart(topics=(dict(name='ops', agent='secretary'),))
        self.store.record_topic('ops', 44, 'created')

        def hook(url, body, *args, **kwargs):
            if url == self.bridge.hook_url:
                seq = self.next_seq
                self.next_seq += 1
                deliveries = {'secretary': 'all'} if body.get('topic') == 'ops' else {'yardmaster': 'all'}
                self.native.hook_events[seq] = hook_event(seq, deliveries=deliveries)
                return 200, dict(seq=seq)
            return self.transport(url, body, *args, **kwargs)

        self.bridge.request = hook
        # Two messages to yardmaster, one to secretary.
        u1 = self.update(uid=10)
        u2 = self.update(uid=11)
        u3 = dict(update_id=12, message=dict(message_id=12, text='ops', message_thread_id=44,
                                             is_topic_message=True, chat=dict(id=123, type='private'),
                                             **{'from': dict(id=123, is_bot=False)}))
        self.drain(u1, u2, u3)
        self.assertEqual(self.row(12)['thread_id'], 44)
        self.poll()
        self.assertEqual(self.native_methods().count('EventsSince'), 3)
        self.assertEqual(sorted(args['name'] for m, args in self.native.calls if m == 'GetCrew'),
                         ['secretary', 'yardmaster'])

        # Acknowledge only yardmaster message 10 (not 11); round does 1 GetCrew per crew.
        self.native.calls.clear()
        self.native.crew['yardmaster']['cursor'] = 10
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(sorted(args['name'] for m, args in self.native.calls if m == 'GetCrew'),
                         ['secretary', 'yardmaster'])
        self.assertEqual(self.row(10)['state'], 'read')
        self.assertEqual(self.row(11)['state'], 'landed')

        # Build > 64 journal events on yardmaster-journal so Events doubles from 64 to 128 to find seq 10.
        self.native.journal_events['yardmaster-journal'] = (
            [replied_event(5, 999)]  # seq 5 <= oldest open seq (10)
            + [dict(seq=100 + i, id=str(100 + i), bead='yardmaster-journal', kind='noted',
                    actor='yardmaster', data={}, at='2026-11-21T16:00:00Z') for i in range(70)]
        )
        # Send happens, replied not logged yet -> Round 1 calls Events (64, 128), mark stays.
        self.assertTrue(self.bridge.publish(self.publish(key='send-1'))['delivered'])
        self.native.calls.clear()
        self.mono[0] += 60.0
        self.poll()
        limits = [args[1] for m, args in self.native.calls if m == 'Events' and args[0] == 'yardmaster-journal']
        self.assertEqual(limits, [64, 128])
        self.assertEqual(self.row(10)['state'], 'read')

        # Replied event logged after send returned -> Round 2 (second round with no newer send) finds it and moves mark.
        self.native.journal_events['yardmaster-journal'].append(replied_event(200, 10))
        self.native.calls.clear()
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(10)['state'], 'done')
        self.assertIn('Events', self.native_methods())

        # Round 3 with no newer send: mark already moved -> 0 Events calls!
        self.native.calls.clear()
        self.mono[0] += 60.0
        self.poll()
        self.assertNotIn('Events', self.native_methods())

    def test_reply_before_ack_moves_landed_directly_to_done(self):
        self.drain(self.update(uid=10))
        self.assertTrue(self.bridge.publish(self.publish(key='reply-1'))['delivered'])
        self.native.journal_events['yardmaster-journal'].append(replied_event(501, 10))
        self.poll()
        self.assertEqual(self.row(10)['state'], 'done')
        self.assertEqual(self.reaction_emojis(), [DOVE, OK_HAND])

    def test_react_overrides_and_non_row_message(self):
        # Message with no row: react does nothing.
        self.bridge.set_reaction(999, OK_HAND, 'reaction:999:k0')
        self.assertIsNone(self.row(999))

        self.drain(self.update(uid=10), self.update(uid=11), self.update(uid=12), self.update(uid=13))
        # seen emoji changes nothing.
        self.bridge.set_reaction(10, DOVE, 'reaction:10:seen')
        self.assertEqual(self.row(10)['state'], 'landed')

        # read emoji moves landed -> read.
        self.bridge.set_reaction(10, EYES, 'reaction:10:read')
        self.assertEqual(self.row(10)['state'], 'read')

        # done emoji moves landed or read -> done.
        self.bridge.set_reaction(10, OK_HAND, 'reaction:10:done')
        self.assertEqual(self.row(10)['state'], 'done')
        self.bridge.set_reaction(11, OK_HAND, 'reaction:11:done')
        self.assertEqual(self.row(11)['state'], 'done')

        # other emoji or none moves to manual and stops tracking.
        self.bridge.api_react(dict(reaction=THUMBS_UP, message_id=12, key='api-1'))
        self.assertEqual(self.row(12)['state'], 'manual')
        b._react(self.bridge, b._build_parser().parse_args(['react', '--reaction', 'none', '--message-id', '13']))
        self.assertEqual(self.row(13)['state'], 'manual')

        # Future rounds make 0 native calls since all rows are terminal.
        self.native.calls.clear()
        self.native.crew['yardmaster']['cursor'] = 999
        self.poll()
        self.assertEqual(self.native_methods(), [])

    def test_api_send_with_reply_to_moves_row_to_done(self):
        self.drain(self.update(uid=10))
        answer = self.bridge.api_send(dict(text='All set.', reply_to='10', key='reply-send-1'))
        self.assertTrue(answer['delivered'])
        self.assertEqual(self.row(10)['state'], 'done')
        self.assertEqual(self.reaction_emojis(), [DOVE, OK_HAND])

    def test_24h_expiry_moves_landed_to_expired_without_reaction(self):
        self.drain(self.update(uid=10))
        self.wall[0] += 24 * 3600 + 1
        self.native.crew['yardmaster']['cursor'] = 10
        self.poll()
        self.assertEqual(self.row(10)['state'], 'expired')
        self.assertEqual(self.reaction_emojis(), [DOVE])
        self.assertEqual(self.native_methods(), [])

    def test_transient_not_sent_retries_same_key_and_rejected_advances_once(self):
        self.drain(self.update(uid=10), self.update(uid=11))
        self.native.crew['yardmaster']['cursor'] = 99
        # Transient RequestNotSent on msg 10 leaves it landed and retries next round with same key.
        self.reaction_answer = lambda body: (_ for _ in ()).throw(b.RequestNotSent('connect failed', 7, 'connect')) \
            if body['message_id'] == 10 else (200, dict(ok=True, result=True))
        self.poll()
        self.assertEqual(self.row(10)['state'], 'landed')
        self.assertEqual(self.row(11)['state'], 'read')
        self.assertTrue(any('reaction not set (msg 10): transport_unavailable_not_sent' in line for line in self.logs))

        # Recovery delivers msg 10 under the same key readstate:10:read.
        self.reaction_answer = (200, dict(ok=True, result=True))
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(10)['state'], 'read')

        # 400 rejection on done advances row, logs once, never resends.
        self.reaction_answer = REACTION_INVALID
        self.assertTrue(self.bridge.publish(self.publish(key='s1'))['delivered'])
        self.native.journal_events['yardmaster-journal'].append(replied_event(501, 10))
        self.logs.clear()
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(10)['state'], 'done')
        self.assertEqual(self.logs.count('reaction not set (msg 10): telegram_rejected_not_sent_400'), 1)

    def test_early_reply_in_accept_gap_is_not_retired(self):
        # 1. First open message: reply arrives after hook acceptance before _accepted/record runs.
        relay = self.bridge._relay

        def reply_before_record(*args, **kwargs):
            result = relay(*args, **kwargs)
            self.assertTrue(self.bridge.publish(self.publish(key='reply-gap-1'))['delivered'])
            self.native.journal_events['yardmaster-journal'].append(replied_event(501, result[1]))
            return result

        self.native.crew['yardmaster']['cursor'] = 10
        with patch.object(self.bridge, '_relay', side_effect=reply_before_record):
            self.drain(self.update(uid=10))
        self.poll()
        self.assertEqual(self.row(10)['state'], 'done')
        self.assertIn('Events', self.native_methods())
        self.assertEqual(self.reaction_emojis(), [DOVE, OK_HAND])

        # 2. After earlier rows became terminal: lost hook response after acceptance + reply, then retry.
        self.calls.clear()
        self.native.calls.clear()
        base_transport = self.bridge.request

        def lose_after_reply(url, body, *args, **kwargs):
            status, answer = base_transport(url, body, *args, **kwargs)
            if url == self.bridge.hook_url and answer.get('seq') == 11:
                self.assertTrue(self.bridge.publish(self.publish(key='reply-gap-2'))['delivered'])
                self.native.journal_events['yardmaster-journal'].append(replied_event(502, 11))

                def duplicate_hook(u, b_body, *a, **kw):
                    if u == self.bridge.hook_url:
                        return 200, dict(seq=11)
                    return base_transport(u, b_body, *a, **kw)

                self.bridge.request = duplicate_hook
                raise b.TransportError('lost hook answer after accept')
            return status, answer

        self.bridge.request = lose_after_reply
        with self.assertRaises(b.TransportError):
            self.drain(self.update(uid=11))
        self.native.crew['yardmaster']['cursor'] = 11
        self.drain(self.update(uid=11))
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(11)['state'], 'done')
        self.assertIn('Events', self.native_methods())
        self.assertEqual(self.reaction_emojis(), [DOVE, OK_HAND])

    def test_transient_done_retried_across_restart_for_both_triggers(self):
        # 1. Journal replied event: RequestNotSent for 2 rounds advances mark, restart + recovery still completes done.
        self.drain(self.update(uid=10))
        self.native.crew['yardmaster']['cursor'] = 10
        self.poll()
        self.assertEqual(self.row(10)['state'], 'read')
        self.assertTrue(self.bridge.publish(self.publish(key='reply-s1'))['delivered'])
        self.native.journal_events['yardmaster-journal'].append(replied_event(501, 10))
        self.reaction_answer = b.RequestNotSent('connect failed', 7, 'connect')
        for _ in range(2):
            self.mono[0] += 60.0
            self.poll()
        self.assertEqual(self.row(10)['state'], 'read')
        self.assertTrue(b.resendable(self.store.saved_receipt('readstate:10:done')))

        self.restart()
        self.reaction_answer = (200, dict(ok=True, result=True))
        self.native.calls.clear()
        self.poll()
        self.assertEqual(self.row(10)['state'], 'done')
        self.assertEqual(self.native_methods(), [])

        # 2. /v1/send reply_to: RequestNotSent, restart, then recovery retries done.
        self.calls.clear()
        self.drain(self.update(uid=11))
        self.native.crew['yardmaster']['cursor'] = 11
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(11)['state'], 'read')
        self.reaction_answer = b.RequestNotSent('connect failed', 7, 'connect')
        answer = self.bridge.api_send(dict(text='All set.', reply_to='11', key='reply-to-transient'))
        self.assertTrue(answer['delivered'])
        self.assertEqual(self.row(11)['state'], 'read')

        self.restart()
        self.reaction_answer = (200, dict(ok=True, result=True))
        self.native.calls.clear()
        self.poll()
        self.assertEqual(self.row(11)['state'], 'done')
        self.assertEqual(self.native_methods(), [])
        self.assertEqual(self.reaction_emojis().count(OK_HAND) > 0, True)
        self.assertEqual([e for e in self.reaction_emojis() if e == EYES], [EYES])

        # 3. Landed row with pending_done + cursor >= seq never overwrites with eyes.
        self.calls.clear()
        self.drain(self.update(uid=12))
        self.reaction_answer = b.RequestNotSent('connect failed', 7, 'connect')
        self.assertTrue(self.bridge.api_send(dict(text='Done.', reply_to='12', key='reply-to-landed'))['delivered'])
        self.native.crew['yardmaster']['cursor'] = 99
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(12)['state'], 'landed')
        self.assertNotIn(EYES, self.reaction_emojis())
        self.reaction_answer = (200, dict(ok=True, result=True))
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(12)['state'], 'done')
        self.assertEqual([e for e in self.reaction_emojis() if e in (DOVE, EYES, OK_HAND) and e == EYES], [])

        # Untracked reply_to does nothing.
        self.calls.clear()
        self.assertTrue(self.bridge.api_send(dict(text='Old.', reply_to='999', key='reply-to-untracked'))['delivered'])
        self.assertIsNone(self.row(999))
        self.assertEqual(self.reaction_emojis(), [])

    def test_successful_reply_retry_after_transient_outage_is_not_retired(self):
        # A same-key retry that delivers after a transient outage still drives
        # done: the send mark never retires the undelivered first attempt.
        self.drain(self.update(uid=10))
        self.native.crew['yardmaster']['cursor'] = 10
        self.poll()
        self.assertEqual(self.row(10)['state'], 'read')
        original = self.bridge.request

        def unavailable(url, body, *args, **kwargs):
            if url.endswith('/sendMessage'):
                raise b.RequestNotSent('synthetic connect failure', 7, 'connect')
            return original(url, body, *args, **kwargs)

        self.bridge.request = unavailable
        request = dict(text='Reply after outage', key='native-reply-stable-key')
        self.assertFalse(self.bridge.api_send(request)['delivered'])
        for _ in range(2):
            self.mono[0] += 60.0
            self.poll()
        self.assertEqual(self.row(10)['state'], 'read')
        self.bridge.request = original
        self.assertTrue(self.bridge.api_send(request)['delivered'])
        self.native.journal_events['yardmaster-journal'].append(replied_event(501, 10))
        self.native.calls.clear()
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(10)['state'], 'done')
        self.assertIn('Events', self.native_methods())
        self.assertEqual(self.reaction_emojis()[-1], OK_HAND)

    def test_transient_recipient_check_does_not_retire_reply(self):
        # While one row's recipient check stays transiently unavailable, the
        # send mark holds so its already-delivered reply is found on recovery.
        self.drain(self.update(uid=10), self.update(uid=11))
        self.native.crew['yardmaster']['cursor'] = 99
        self.assertTrue(self.bridge.publish(self.publish(key='reply-for-unresolved-recipient'))['delivered'])
        self.native.journal_events['yardmaster-journal'].append(replied_event(501, 11))
        original_call = self.bridge.run_asks.client.call

        def temporarily_unavailable(method, arguments):
            if method == 'EventsSince' and arguments == [10, 1]:
                raise b.run_asks.RPCUnavailable('synthetic recipient read unavailable before send')
            return original_call(method, arguments)

        with patch.object(self.bridge.run_asks.client, 'call', side_effect=temporarily_unavailable):
            for _ in range(2):
                self.mono[0] += 60.0
                self.poll()
        self.assertEqual(self.row(10)['state'], 'read')
        self.assertEqual(self.row(11)['state'], 'landed')
        self.assertIsNone(self.row(11)['crew'])
        self.native.calls.clear()
        for _ in range(3):
            self.mono[0] += 60.0
            self.poll()
        self.assertEqual(self.row(11)['crew'], 'yardmaster')
        self.assertEqual(self.row(11)['state'], 'done')
        self.assertEqual(self.reaction_emojis()[-1], OK_HAND)

    def test_pre_record_react_overrides_and_pre_record_reply_to(self):
        relay = self.bridge._relay

        # 1. Pre-record react done preserves done and never sends dove or eyes after inbox ack.
        def done_before_record(*args, **kwargs):
            result = relay(*args, **kwargs)
            self.assertTrue(self.bridge.set_reaction(10, OK_HAND, 'reaction:10:pre-done')['delivered'])
            return result

        self.native.crew['yardmaster']['cursor'] = 99
        with patch.object(self.bridge, '_relay', side_effect=done_before_record):
            self.drain(self.update(uid=10))
        self.native.calls.clear()
        self.poll()
        self.assertEqual(self.row(10)['state'], 'done')
        self.assertIsNotNone(self.row(10)['done_at'])
        self.assertEqual(self.native_methods(), [])
        self.assertEqual(self.reaction_emojis(), [OK_HAND])

        # 2. Pre-record react read starts at read (skips dove) and still advances to done on reply.
        self.calls.clear()

        def read_before_record(*args, **kwargs):
            result = relay(*args, **kwargs)
            self.assertTrue(self.bridge.set_reaction(11, EYES, 'reaction:11:pre-read')['delivered'])
            return result

        with patch.object(self.bridge, '_relay', side_effect=read_before_record):
            self.drain(self.update(uid=11))
        self.assertEqual(self.row(11)['state'], 'read')
        self.assertTrue(self.bridge.publish(self.publish(key='reply-pre-read'))['delivered'])
        self.native.journal_events['yardmaster-journal'].append(replied_event(505, 11))
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(11)['state'], 'done')
        self.assertEqual(self.reaction_emojis(), [EYES, OK_HAND])

        # 3. Pre-record other emoji starts at manual and stops tracking.
        self.calls.clear()

        def manual_before_record(*args, **kwargs):
            result = relay(*args, **kwargs)
            self.assertTrue(self.bridge.set_reaction(12, THUMBS_UP, 'reaction:12:pre-manual')['delivered'])
            return result

        with patch.object(self.bridge, '_relay', side_effect=manual_before_record):
            self.drain(self.update(uid=12))
        self.native.calls.clear()
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(12)['state'], 'manual')
        self.assertEqual(self.native_methods(), [])
        self.assertEqual(self.reaction_emojis(), [THUMBS_UP])

        # 4. Pre-record /v1/send reply_to preserves pending_done and finishes next round.
        self.calls.clear()

        def reply_to_before_record(*args, **kwargs):
            result = relay(*args, **kwargs)
            self.assertTrue(self.bridge.api_send(dict(text='Early.', reply_to='13', key='early-rt'))['delivered'])
            return result

        with patch.object(self.bridge, '_relay', side_effect=reply_to_before_record):
            self.drain(self.update(uid=13))
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(13)['state'], 'done')
        self.assertEqual(self.reaction_emojis(), [DOVE, OK_HAND])

    def test_typing_survives_read_and_done_rounds(self):
        self.restart(typing=dict(seconds=300))
        self.drain(self.update(uid=10))
        mark = self.bridge.typing_windows[None]['mark']
        self.native.crew['yardmaster']['cursor'] = 10
        self.poll()
        self.assertEqual(self.row(10)['state'], 'read')
        self.assertFalse(self.store.sent_since(mark))

    def test_degrade_cursor_missing_events_since_unsupported_and_socket_absent(self):
        # 1. Cursor missing -> row goes off.
        self.drain(self.update(uid=10))
        self.native.crew['yardmaster'].pop('cursor')
        self.poll()
        self.assertEqual(self.row(10)['state'], 'off')
        self.assertEqual(self.row(10)['reason'], 'invalid_crew_cursor')

        # 2. EventsSince no such method -> row goes off, no GetCrew.
        self.native.calls.clear()
        self.native.override['EventsSince'] = lambda args, h: (
            200, {'error': {'code': 'error', 'message': 'no such method EventsSince'}})
        self.drain(self.update(uid=11))
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(11)['state'], 'off')
        self.assertNotIn('GetCrew', self.native_methods())
        self.native.override.pop('EventsSince')

        # 3. Socket absent: inbound cursor advances, round logs once and retries when socket returns.
        self.native.crew['yardmaster']['cursor'] = 99
        (self.yard_dir / 'yard.sock').unlink()
        self.drain(self.update(uid=12))
        self.assertEqual(self.store.offset(), 13)
        self.logs.clear()
        self.mono[0] += 60.0
        self.poll()
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(12)['state'], 'landed')
        self.assertEqual(len([l for l in self.logs if 'read states recipient check' in l]), 1)

        # Socket returns -> next round advances msg 12 to read.
        self.native.close()
        self.native = FakeReadYard(self.yard_dir)
        self.native.hook_events[12] = hook_event(12)
        self.native.crew['yardmaster']['cursor'] = 12
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(12)['state'], 'read')

    def test_degrade_bool_cursor_source_mismatch_and_invalid_events_shape(self):
        # 1. Bool cursor -> invalid_crew_cursor -> off.
        self.drain(self.update(uid=10))
        self.native.crew['yardmaster']['cursor'] = True
        self.poll()
        self.assertEqual(self.row(10)['state'], 'off')
        self.assertEqual(self.row(10)['reason'], 'invalid_crew_cursor')

        # 2. Hook event with another source -> hook_source_mismatch -> off.
        self.native.crew['yardmaster']['cursor'] = 99
        self.native.hook_events[11] = hook_event(11, source='other')
        self.drain(self.update(uid=11))
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(11)['state'], 'off')
        self.assertEqual(self.row(11)['reason'], 'hook_source_mismatch')

        # 3. Events invalid shape degrades cleanly, logs once, keeps poll loop working.
        self.drain(self.update(uid=12))
        self.assertTrue(self.bridge.publish(self.publish(key='s-bad-ev'))['delivered'])
        self.native.override['Events'] = lambda args, h: (200, {'result': {'bad': True}})
        self.logs.clear()
        self.mono[0] += 60.0
        self.poll()
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(12)['state'], 'read')
        self.assertEqual(len([l for l in self.logs if 'read states events yardmaster:' in l]), 1)

    def test_preview_script_renders_synthetic_states(self):
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'preview_read_states.py'
        result = subprocess.run([sys.executable, '-B', str(script)], capture_output=True, text=True, timeout=30)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn(DOVE, result.stdout)
        self.assertIn(EYES, result.stdout)
        self.assertIn(OK_HAND, result.stdout)

        secondary = subprocess.run([sys.executable, '-B', str(script), '--instance', 'secondary'],
                                   capture_output=True, text=True, timeout=30)
        self.assertEqual(secondary.returncode, 0, secondary.stderr)
        self.assertIn('crew secretary · native cards not owned here', secondary.stdout)
        self.assertIn('✓ Nudge typed', secondary.stdout)
        self.assertNotIn('outcome unreadable', secondary.stdout)
        for emoji in (DOVE, EYES, OK_HAND):
            self.assertIn(emoji, secondary.stdout)

    def test_nudge_after_settings_validation(self):
        with tempfile.TemporaryDirectory() as tmp:
            base_path = write_settings(Path(tmp))
            base_text = base_path.read_text()

            # nudge_after = 0 without read loads cleanly.
            p0 = Path(tmp) / 'zero.toml'
            p0.write_text(base_text + '\n[reactions]\nnudge_after = 0\n')
            p0.chmod(0o600)
            self.assertEqual(b.load_settings(p0).config['reactions']['nudge_after'], 0)

            # nudge_after > 0 without read refuses naming reactions.nudge_after.
            p_noread = Path(tmp) / 'noread.toml'
            p_noread.write_text(base_text + '\n[reactions]\nnudge_after = 300\n')
            p_noread.chmod(0o600)
            with self.assertRaisesRegex(b.SettingsError, r'reactions\.nudge_after'):
                b.load_settings(p_noread)

            # Out of range or non-int refuses naming reactions.nudge_after.
            for bad in (-1, 3601, 'true', '"300"'):
                p_bad = Path(tmp) / f'bad-{bad}.toml'
                p_bad.write_text(base_text + f'\n[reactions]\nnudge_after = {bad}\n')
                p_bad.chmod(0o600)
                with self.assertRaisesRegex(b.SettingsError, r'reactions\.nudge_after'):
                    b.load_settings(p_bad)

            # Valid nudge_after with read and [yardr] home/operator loads.
            yard_home = Path(tmp) / 'yard'
            yard_home.mkdir(mode=0o700)
            p_ok = Path(tmp) / 'ok.toml'
            p_ok.write_text(
                base_text.replace('[yardr]\n', f'[yardr]\nhome = "{yard_home}"\noperator = "pat"\n')
                + f'\n[reactions]\nread = "{EYES}"\nnudge_after = 300\n'
            )
            p_ok.chmod(0o600)
            loaded = b.load_settings(p_ok)
            self.assertEqual(loaded.config['reactions']['nudge_after'], 300)

    def test_offer_threshold_boundary_crew_states_one_per_crew_and_unserved_topic(self):
        self._enable_offers(nudge_after=300, status='idle')
        u1 = self.update(uid=10)
        u1['message']['text'] = 'secret operator payload'
        self.drain(u1)

        # At nudge_after - 1 s: no offer card.
        self.wall[0] += 299
        self.poll()
        self.assertEqual(self._sends(), [])
        self.assertIsNone(self.row(10)['offer_token'])

        # At nudge_after while working, stopped, or failed: no offer card.
        self.wall[0] += 1
        for state, status in (('running', 'working'), ('stopped', 'idle'), ('failed', 'idle'), ('running', 'unknown')):
            self.native.crew['yardmaster']['state'] = state
            self.native.crew['yardmaster']['status'] = status
            self.mono[0] += 60.0
            self.poll()
            self.assertEqual(self._sends(), [])
            self.assertIsNone(self.row(10)['offer_token'])

        # Once running + idle: exactly one silent offer card replying to msg 10.
        self.native.crew['yardmaster']['state'] = 'running'
        self.native.crew['yardmaster']['status'] = 'idle'
        self.mono[0] += 60.0
        self.poll()
        sends = self._sends()
        self.assertEqual(len(sends), 1)
        card = sends[0]
        self.assertTrue(card.get('disable_notification'))
        self.assertEqual(card.get('reply_parameters'), {'message_id': 10})
        self.assertIn('<b>Nudge crew member yardmaster?</b>', card['text'])
        self.assertIn('Unread for 5m', card['text'])
        self.assertIn('running', card['text'])
        self.assertIn('idle', card['text'])
        expected_line = b.read_states.OFFER_LINE.format(seq=10)
        self.assertIn(expected_line, unescape(card['text']))
        self.assertLessEqual(len(expected_line), b.typed_nudge.WORDS_MAX)
        self.assertNotIn('secret operator payload', card['text'])
        buttons = [btn for r in card['reply_markup']['inline_keyboard'] for btn in r]
        self.assertEqual([btn['text'] for btn in buttons], ['Nudge', 'Cancel'])
        token1 = self.row(10)['offer_token']
        self.assertIsNotNone(token1)

        # Second unread message for same crew while offer 1 is open: no second card.
        self.drain(self.update(uid=11))
        self.wall[0] += 300
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(len(self._sends()), 1)
        self.assertIsNone(self.row(11)['offer_token'])

        # Unserved topic (decide_conversation is None) gets no offer even for an eligible crew.
        self.native.crew['secretary'] = dict(name='secretary', journal='secretary-journal',
                                             kind='claude', model='opus', state='running', status='blocked', cursor=0)
        self.native.hook_events[12] = hook_event(12, deliveries={'yardmaster': 'all'})
        u_unserved = dict(update_id=12, message=dict(
            message_id=12, text='unserved topic msg', message_thread_id=99, is_topic_message=True,
            chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)},
        ))
        self.drain(u_unserved)
        self.wall[0] += 300
        self.mono[0] += 60.0
        self.poll()
        self.assertIsNone(self.row(12)['offer_token'])

        # Blocked status on another crew is eligible and opens its own offer.
        self.restart(
            commands=(y.NUDGE_COMMAND,),
            reactions=dict(self.config['reactions'], nudge_after=300),
            topics=(dict(name='ops', agent='secretary'),),
        )
        self.store.record_topic('ops', 44, 'created')
        self.native.hook_events[13] = hook_event(13, deliveries={'secretary': 'all'})
        u_topic = dict(update_id=13, message=dict(
            message_id=13, text='topic msg', message_thread_id=44, is_topic_message=True,
            chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)},
        ))
        self.drain(u_topic)
        self.wall[0] += 300
        self.mono[0] += 60.0
        self.poll()
        self.assertIsNotNone(self.row(13)['offer_token'])
        self.assertEqual(self._sends()[-1].get('message_thread_id'), 44)
        self.assertIn('blocked', self._sends()[-1]['text'])

    def test_offer_tap_double_tap_replay_cancel_and_read_after_nudge(self):
        self._enable_offers(nudge_after=300, status='idle')
        self.drain(self.update(uid=10))
        self.wall[0] += 300
        self.poll()
        token = self.row(10)['offer_token']
        offer_mid = self.bridge.typed_nudge.get(token)['message']

        # Unauthorized tap writes nothing.
        bad_tap = self._tap(token, uid=39)
        bad_tap['callback_query']['from'] = dict(id=124, is_bot=False)
        self.poll(bad_tap)
        self.assertEqual(self._nudge_writes(), [])

        # Verified tap calls NudgeSession once with the fixed line; replay and double tap write nothing more.
        self.poll(self._tap(token, uid=40))
        self.poll(self._tap(token, uid=40), self._tap(token, uid=41))
        writes = self._nudge_writes()
        self.assertEqual(len(writes), 1)
        expected_line = b.read_states.OFFER_LINE.format(seq=10)
        self.assertEqual(writes[0], dict(target='yardmaster', user='', text=expected_line, clear=False, actor='pat'))

        # Outcome event relays on the offer card.
        ident = self.native.nudges['crew:yardmaster']['id']
        self.native.journal_events['yardmaster-journal'].append(nudged(501, ident))
        self.poll()
        reply = self._sends()[-1]
        self.assertEqual(reply['text'], '\u2713 Nudge typed')
        self.assertEqual(reply['reply_parameters']['message_id'], offer_mid)

        # Still landed after nudge -> no re-offer; then ack moves it to eyes.
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(10)['offer_token'], token)
        self.assertEqual(len(self._sends()), 2)
        self.native.crew['yardmaster']['cursor'] = 10
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(10)['state'], 'read')
        self.assertEqual(self.reaction_emojis(), [DOVE, EYES])

        # Cancel writes nothing and never re-offers.
        self.drain(self.update(uid=45))
        self.wall[0] += 300
        self.mono[0] += 60.0
        self.poll()
        token2 = self.row(45)['offer_token']
        self.assertIsNotNone(token2)
        self.poll(self._tap(token2, action='cancel', uid=50))
        self.assertEqual(len(self._nudge_writes()), 1)
        self.assertEqual(self.bridge.typed_nudge.get(token2)['state'], 'cancelled')
        self.assertEqual(self._edits()[-1]['reply_markup'], {'inline_keyboard': []})
        self.mono[0] += 60.0
        self.wall[0] += 300
        self.poll()
        self.assertEqual(self.row(45)['offer_token'], token2)
        self.assertEqual(len(self._sends()), 3)

    def test_offer_close_on_read_done_manual_and_expired(self):
        self._enable_offers(nudge_after=300, status='idle')

        # 1. Read before tap closes card with notice and removes buttons; late tap writes nothing.
        self.drain(self.update(uid=10))
        self.wall[0] += 300
        self.poll()
        tok1 = self.row(10)['offer_token']
        self.native.crew['yardmaster']['cursor'] = 10
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(10)['state'], 'read')
        tn1 = self.bridge.typed_nudge.get(tok1)
        self.assertEqual(tn1['state'], 'cancelled')
        self.assertIn('read', tn1['notice'].lower())
        self.assertEqual(self._edits()[-1]['reply_markup'], {'inline_keyboard': []})
        self.poll(self._tap(tok1, uid=40))
        self.assertEqual(self._nudge_writes(), [])
        self.assertEqual(self._answers()[-1]['text'], b.typed_nudge.ALREADY)

        # 2. Done via /v1/send reply_to closes card immediately.
        self.drain(self.update(uid=45))
        self.wall[0] += 300
        self.mono[0] += 60.0
        self.poll()
        tok2 = self.row(45)['offer_token']
        self.assertTrue(self.bridge.api_send(dict(text='On it.', reply_to='45', key='close-done'))['delivered'])
        tn2 = self.bridge.typed_nudge.get(tok2)
        self.assertEqual(tn2['state'], 'cancelled')
        self.assertIn('answered', tn2['notice'].lower())
        self.assertEqual(self._edits()[-1]['reply_markup'], {'inline_keyboard': []})
        self.poll(self._tap(tok2, uid=46))
        self.assertEqual(self._nudge_writes(), [])

        # 3. Manual via react closes card even when no open rows remain.
        self.drain(self.update(uid=50))
        self.wall[0] += 300
        self.mono[0] += 60.0
        self.poll()
        tok3 = self.row(50)['offer_token']
        self.assertTrue(self.bridge.api_react(dict(reaction=THUMBS_UP, message_id=50, key='close-manual'))['delivered'])
        tn3 = self.bridge.typed_nudge.get(tok3)
        self.assertEqual(tn3['state'], 'cancelled')
        self.assertIn('tracking ended', tn3['notice'].lower())
        self.assertEqual(self._edits()[-1]['reply_markup'], {'inline_keyboard': []})

        # 4. 24h expiry closes open offer without reaction.
        self.drain(self.update(uid=55))
        self.wall[0] += 300
        self.mono[0] += 60.0
        self.poll()
        tok4 = self.row(55)['offer_token']
        self.wall[0] += 24 * 3600 + 1
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.row(55)['state'], 'expired')
        tn4 = self.bridge.typed_nudge.get(tok4)
        self.assertEqual(tn4['state'], 'cancelled')
        self.assertIn('tracking ended', tn4['notice'].lower())
        self.assertEqual(self._edits()[-1]['reply_markup'], {'inline_keyboard': []})

    def test_offer_save_before_send_restart_replay_retry_and_ambiguous(self):
        self._enable_offers(nudge_after=300, status='idle')

        # 1. RequestNotSent leaves offer_token + typed_nudge (message=None) saved; next round sends once.
        self.drain(self.update(uid=10))
        self.wall[0] += 300
        self.send_answer = b.RequestNotSent('connect failed', 7, 'connect')
        self.poll()
        tok1 = self.row(10)['offer_token']
        self.assertIsNotNone(tok1)
        self.assertIsNone(self.bridge.typed_nudge.get(tok1)['message'])
        self.assertEqual(self.bridge.typed_nudge.get(tok1)['state'], 'open')

        # Restart and recover Telegram: sends once under the same key.
        self.send_answer = None
        self._enable_offers(nudge_after=300, status='idle')
        self.calls.clear()
        self.poll()
        self.assertEqual(len(self._sends()), 1)
        self.assertIsNotNone(self.bridge.typed_nudge.get(tok1)['message'])
        # Close msg 10 so crew is free for case 2 & 3.
        self.native.crew['yardmaster']['cursor'] = 10
        self.mono[0] += 60.0
        self.poll()

        # 2. Crash after confirmed delivery before message_id saved: restart replays ledger with 0 Telegram sends.
        self.drain(self.update(uid=11))
        self.wall[0] += 300
        self.calls.clear()
        orig_save = self.bridge.typed_nudge.save
        calls_to_save = [0]

        def crash_after_send(row, db=None):
            calls_to_save[0] += 1
            if row.get('message') is not None:
                raise RuntimeError('simulated crash before message_id saved')
            return orig_save(row, db=db)

        with patch.object(self.bridge.typed_nudge, 'save', side_effect=crash_after_send):
            self.mono[0] += 60.0
            self.poll()
        tok2 = self.row(11)['offer_token']
        self.assertIsNotNone(tok2)
        self.assertIsNone(self.bridge.typed_nudge.get(tok2)['message'])
        self.assertEqual(len(self._sends()), 1)
        # Restart and poll: ledger replays confirmed send with 0 new sendMessage calls.
        self._enable_offers(nudge_after=300, status='idle')
        self.calls.clear()
        self.poll()
        self.assertEqual(self._sends(), [])
        self.assertIsNotNone(self.bridge.typed_nudge.get(tok2)['message'])
        self.native.crew['yardmaster']['cursor'] = 11
        self.mono[0] += 60.0
        self.poll()

        # 3. Ambiguous send counts as offered, logs once, and never retries or re-offers.
        self.drain(self.update(uid=12))
        self.wall[0] += 300
        self.send_answer = b.TransportError('lost answer after send')
        self.logs.clear()
        self.mono[0] += 60.0
        self.poll()
        tok3 = self.row(12)['offer_token']
        self.assertIsNotNone(tok3)
        self.assertEqual(self.bridge.typed_nudge.get(tok3)['state'], 'unknown')
        self.assertTrue(any(f'read states offer {tok3}: nudge card not sent:' in line for line in self.logs))
        self.send_answer = None
        self.calls.clear()
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self._sends(), [])
        self.assertEqual(self.row(12)['offer_token'], tok3)

        # 4. RequestNotSent saved before restart on a core without NudgeSession:
        #    probes once on restart and publishes zero cards; sends once if restored to fork.
        self.drain(self.update(uid=13))
        self.wall[0] += 300
        self.send_answer = b.RequestNotSent('connect failed', 7, 'connect')
        self.mono[0] += 60.0
        self.poll()
        tok4 = self.row(13)['offer_token']
        self.assertIsNotNone(tok4)
        self.assertIsNone(self.bridge.typed_nudge.get(tok4)['message'])

        self.send_answer = None
        self.native.nudge_mode = 'no-nudge-method'
        self._enable_offers(nudge_after=300, status='idle')
        self.calls.clear()
        self.native.calls.clear()
        self.logs.clear()
        self.poll()
        self.assertEqual(self._sends(), [])
        self.assertEqual([args for m, args in self.native.calls if m == 'NudgeSession'], [{}])
        self.assertIsNone(self.bridge.typed_nudge.get(tok4)['message'])
        self.assertEqual(len([l for l in self.logs if 'nudge offers off' in l]), 1)
        self.native.calls.clear()
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self._sends(), [])
        self.assertEqual([args for m, args in self.native.calls if m == 'NudgeSession'], [])

        # Restoring a core with NudgeSession delivers the pending offer once; then mark read.
        self.native.nudge_mode = None
        self._enable_offers(nudge_after=300, status='idle')
        self.calls.clear()
        self.poll()
        self.assertEqual(len(self._sends()), 1)
        self.assertIsNotNone(self.bridge.typed_nudge.get(tok4)['message'])
        self.native.crew['yardmaster']['cursor'] = 13
        self.mono[0] += 60.0
        self.poll()

        # 5. Confirmed send saved in ledger before crash replays without duplicate send even after rollback.
        self.drain(self.update(uid=14))
        self.wall[0] += 300
        self.calls.clear()
        with patch.object(self.bridge.typed_nudge, 'save', side_effect=crash_after_send):
            self.mono[0] += 60.0
            self.poll()
        tok5 = self.row(14)['offer_token']
        self.assertIsNotNone(tok5)
        self.assertIsNone(self.bridge.typed_nudge.get(tok5)['message'])
        self.assertEqual(len(self._sends()), 1)
        self.native.nudge_mode = 'no-nudge-method'
        self._enable_offers(nudge_after=300, status='idle')
        self.calls.clear()
        self.poll()
        self.assertEqual(self._sends(), [])
        self.assertIsNotNone(self.bridge.typed_nudge.get(tok5)['message'])
        self.native.crew['yardmaster']['cursor'] = 14
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self.bridge.typed_nudge.get(tok5)['state'], 'cancelled')
        self.assertEqual(self._edits()[-1]['reply_markup'], {'inline_keyboard': []})

        # 6. Confirmed send saved in ledger before crash, message becomes read before recovery:
        #    recovers card message id, cancels offer, removes buttons, refuses late tap.
        self.native.nudge_mode = None
        self._enable_offers(nudge_after=300, status='idle')
        self.drain(self.update(uid=15))
        self.wall[0] += 300
        self.calls.clear()
        with patch.object(self.bridge.typed_nudge, 'save', side_effect=crash_after_send):
            self.mono[0] += 60.0
            self.poll()
        tok6 = self.row(15)['offer_token']
        self.assertIsNotNone(tok6)
        self.assertIsNone(self.bridge.typed_nudge.get(tok6)['message'])
        self.assertEqual(len(self._sends()), 1)
        card_mid6 = int(self.store.saved_receipt(f'typed-nudge:{tok6}:send')['message_id'])
        self.native.crew['yardmaster']['cursor'] = self.row(15)['seq']
        self._enable_offers(nudge_after=300, status='idle')
        self.calls.clear()
        self.poll()
        self.assertEqual(self._sends(), [])
        self.assertEqual(self.row(15)['state'], 'read')
        tn6 = self.bridge.typed_nudge.get(tok6)
        self.assertEqual(tn6['message'], card_mid6)
        self.assertEqual(tn6['state'], 'cancelled')
        self.assertIn('read', tn6['notice'].lower())
        self.assertEqual(len(self._edits()), 1)
        self.assertEqual(self._edits()[0]['message_id'], card_mid6)
        self.assertEqual(self._edits()[0]['reply_markup'], {'inline_keyboard': []})
        self.poll(self._tap(tok6, uid=51))
        self.assertEqual(self._nudge_writes(), [])
        self.assertEqual(self._answers()[-1]['text'], b.typed_nudge.ALREADY)

        # 7. Confirmed send saved in ledger before crash, message becomes done before recovery:
        #    recovers card message id, cancels offer, removes buttons, refuses late tap.
        self.drain(self.update(uid=60))
        self.wall[0] += 300
        self.calls.clear()
        with patch.object(self.bridge.typed_nudge, 'save', side_effect=crash_after_send):
            self.mono[0] += 60.0
            self.poll()
        tok7 = self.row(60)['offer_token']
        self.assertIsNotNone(tok7)
        self.assertIsNone(self.bridge.typed_nudge.get(tok7)['message'])
        self.assertEqual(len(self._sends()), 1)
        card_mid7 = int(self.store.saved_receipt(f'typed-nudge:{tok7}:send')['message_id'])
        self.native.journal_events['yardmaster-journal'].append(replied_event(502, self.row(60)['seq']))
        self._enable_offers(nudge_after=300, status='idle')
        self.calls.clear()
        self.poll()
        self.assertEqual(self._sends(), [])
        self.assertEqual(self.row(60)['state'], 'done')
        tn7 = self.bridge.typed_nudge.get(tok7)
        self.assertEqual(tn7['message'], card_mid7)
        self.assertEqual(tn7['state'], 'cancelled')
        self.assertIn('answered', tn7['notice'].lower())
        self.assertEqual(len(self._edits()), 1)
        self.assertEqual(self._edits()[0]['message_id'], card_mid7)
        self.assertEqual(self._edits()[0]['reply_markup'], {'inline_keyboard': []})
        self.poll(self._tap(tok7, uid=61))
        self.assertEqual(self._nudge_writes(), [])
        self.assertEqual(self._answers()[-1]['text'], b.typed_nudge.ALREADY)

    def test_offer_disabled_when_no_nudge_method_no_native_nudge_or_no_operator(self):
        # 1. NudgeSession unsupported -> probed once, cached, logged once, no card, no write.
        self.native.nudge_mode = 'no-nudge-method'
        self._enable_offers(nudge_after=300, status='idle')
        self.drain(self.update(uid=10))
        self.wall[0] += 300
        self.native.calls.clear()
        self.logs.clear()
        self.poll()
        self.assertEqual(self._sends(), [])
        self.assertIsNone(self.row(10)['offer_token'])
        self.assertEqual([args for m, args in self.native.calls if m == 'NudgeSession'], [{}])
        self.assertEqual(len([l for l in self.logs if 'nudge offers off' in l]), 1)
        self.native.calls.clear()
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual([args for m, args in self.native.calls if m == 'NudgeSession'], [])
        self.assertEqual(len([l for l in self.logs if 'nudge offers off' in l]), 1)

        # 2. Built-in /nudge off (native_nudge() False) -> logged once, no NudgeSession, no card.
        self.native.nudge_mode = None
        self.native.crew['yardmaster'].update(kind='claude', model='opus', state='running', status='idle')
        self.restart(commands=(), reactions=dict(self.config['reactions'], nudge_after=300))
        self.native.calls.clear()
        self.logs.clear()
        self.mono[0] += 60.0
        self.poll()
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual(self._sends(), [])
        self.assertEqual([m for m, _ in self.native.calls if m == 'NudgeSession'], [])
        self.assertEqual(len([l for l in self.logs if 'nudge offers off' in l]), 1)

        # 3. No [yardr] operator -> logged once, no NudgeSession, no card.
        self.restart(commands=(y.NUDGE_COMMAND,), yardr_operator=None,
                     reactions=dict(self.config['reactions'], nudge_after=300))
        self.native.calls.clear()
        self.logs.clear()
        with patch.object(self.bridge, '_log', side_effect=self.logs.append):
            self.mono[0] += 60.0
            self.bridge.read_states.refresh()
            self.mono[0] += 60.0
            self.bridge.read_states.refresh()
        self.assertEqual(self._sends(), [])
        self.assertEqual([m for m, _ in self.native.calls if m == 'NudgeSession'], [])
        self.assertEqual(len([l for l in self.logs if 'nudge offers off' in l]), 1)

    def test_offer_probe_cached_when_first_offer_refuses_crew_read(self):
        self.native.crew['secretary'] = dict(name='secretary', journal='secretary-journal',
                                             kind='claude', model='opus', state='running', status='idle', cursor=0)
        self.native.crew['yardmaster'].update(kind='claude', model='opus', state='running', status='idle')
        self.restart(
            commands=(y.NUDGE_COMMAND,),
            reactions=dict(self.config['reactions'], nudge_after=300),
            topics=(dict(name='ops', agent='secretary'),),
        )
        self.store.record_topic('ops', 44, 'created')
        self.native.hook_events[10] = hook_event(10, deliveries={'yardmaster': 'all'})
        self.native.hook_events[11] = hook_event(11, deliveries={'secretary': 'all'})
        self.drain(
            self.update(uid=10),
            dict(update_id=11, message=dict(
                message_id=11, text='topic msg', message_thread_id=44, is_topic_message=True,
                chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)},
            )),
        )
        self.wall[0] += 300
        yardmaster_reads = [0]

        def get_crew(args, _handler):
            name = args.get('name')
            if name == 'yardmaster':
                yardmaster_reads[0] += 1
                if yardmaster_reads[0] == 2:
                    return 200, {'error': {'code': 'denied', 'message': 'crew read refused'}}
            return 200, {'result': dict(self.native.crew[name])}

        self.native.override['GetCrew'] = get_crew
        self.native.calls.clear()
        self.calls.clear()
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual([args for m, args in self.native.calls if m == 'NudgeSession'], [{}])
        self.assertIsNone(self.row(10)['offer_token'])
        self.assertIsNotNone(self.row(11)['offer_token'])
        self.assertEqual(len(self._sends()), 1)

        # Next round stays cached (no probe) and offers to yardmaster once GetCrew succeeds.
        self.native.override.pop('GetCrew')
        self.native.calls.clear()
        self.mono[0] += 60.0
        self.poll()
        self.assertEqual([args for m, args in self.native.calls if m == 'NudgeSession'], [])
        self.assertIsNotNone(self.row(10)['offer_token'])
        self.assertEqual(len(self._sends()), 2)

    def test_nudge_after_zero_makes_only_read_bead_calls(self):
        self.native.crew['yardmaster'].update(kind='claude', model='opus', state='running', status='idle')
        self.restart(commands=(y.NUDGE_COMMAND,), reactions=dict(self.config['reactions'], nudge_after=0))
        self.drain(self.update(uid=10))
        self.wall[0] += 600
        self.native.calls.clear()
        self.poll()
        self.assertEqual(self.native_methods(), ['EventsSince', 'GetCrew'])
        self.assertNotIn('NudgeSession', [m for m, _ in self.native.calls])
        self.assertEqual(self._sends(), [])
        self.assertIsNone(self.row(10)['offer_token'])

    def test_stream_inbox_read_and_replied_act_at_once_and_ignore_other_session_or_source(self):
        with patch.object(self.bridge, '_log', side_effect=self.logs.append):
            self.bridge.read_states.start()
            self.addCleanup(self.bridge.read_states.stop)
            self._wait_until(lambda: self.bridge.read_states._stream_connected, message='stream did not connect')

            self.drain(self.update(uid=10))
            self.assertEqual(self.reaction_emojis(), [DOVE])

            # Non-line session slice inbox_read must not mark a hook delivery read.
            self.native.push_event(inbox_read_event(20, reader=self.CREW, through=10, session='hal'))
            self._wait_until(lambda: (self.bridge.read_states._last_seq or 0) >= 20)
            self.assertEqual(self.row(10)['state'], 'landed')

            # Line session inbox_read transitions landed -> read at once without poll().
            t0 = time.monotonic()
            self.native.push_event(inbox_read_event(21, reader=self.CREW, through=10, session=''))
            self._wait_until(lambda: (self.row(10) or {}).get('state') == 'read', timeout=2.0,
                             message='inbox_read did not move row to read')
            self.assertLess(time.monotonic() - t0, 2.0)
            self.assertEqual(self.reaction_emojis(), [DOVE, EYES])

            # Replied for another source is ignored; replied for our source moves read -> done at once.
            self.native.push_event(replied_event(22, 10, crew=self.CREW, source='other-source',
                                                 journal=self.CREW + '-journal'))
            self._wait_until(lambda: (self.bridge.read_states._last_seq or 0) >= 22)
            self.assertEqual(self.row(10)['state'], 'read')

            self.native.push_event(replied_event(23, 10, crew=self.CREW, source=self.SOURCE,
                                                 journal=self.CREW + '-journal'))
            self._wait_until(lambda: (self.row(10) or {}).get('state') == 'done', timeout=2.0,
                             message='replied did not move row to done')
            self.assertEqual(self.reaction_emojis(), [DOVE, EYES, OK_HAND])

    def test_stream_drop_resumes_from_last_seq_without_lost_or_duplicate_reaction(self):
        with patch.object(self.bridge, '_log', side_effect=self.logs.append):
            self.bridge.read_states.start()
            self.addCleanup(self.bridge.read_states.stop)
            self._wait_until(lambda: self.bridge.read_states._stream_connected, message='stream did not connect')

            self.drain(self.update(uid=10), self.update(uid=11))
            self.native.push_event(inbox_read_event(20, reader=self.CREW, through=10, session=''))
            self._wait_until(lambda: (self.row(10) or {}).get('state') == 'read', timeout=2.0)

            # Drop active stream mid-run, queue events while disconnected, and verify resume after=20.
            self.native.drop_streams()
            self._wait_until(lambda: not self.bridge.read_states._stream_connected, timeout=2.0)
            self.native.push_event(inbox_read_event(21, reader=self.CREW, through=11, session=''))
            self.native.push_event(replied_event(22, 10, crew=self.CREW, source=self.SOURCE,
                                                 journal=self.CREW + '-journal'))

            self._wait_until(
                lambda: (self.row(10) or {}).get('state') == 'done'
                and (self.row(11) or {}).get('state') == 'read',
                timeout=3.0, message='stream did not resume missed events',
            )
            self.assertIn(20, self.native.stream_requests[1:])
            msg10_emojis = [body['reaction'][0]['emoji'] for url, body in self.calls
                            if url.endswith('/setMessageReaction') and body['message_id'] == 10]
            msg11_emojis = [body['reaction'][0]['emoji'] for url, body in self.calls
                            if url.endswith('/setMessageReaction') and body['message_id'] == 11]
            self.assertEqual(msg10_emojis, [DOVE, EYES, OK_HAND])
            self.assertEqual(msg11_emojis, [DOVE, EYES])

    def test_stream_fallback_when_events_refused_or_inbox_read_absent(self):
        # 1. HTTP 404 on /events logs fallback reason once and polls within 5 s.
        self.native.stream_mode = 'http_404'
        with patch.object(self.bridge, '_log', side_effect=self.logs.append):
            self.bridge.read_states.start()
            self.addCleanup(self.bridge.read_states.stop)
            self._wait_until(lambda: any('read states stream fallback:' in l for l in self.logs), timeout=2.0)
            self.drain(self.update(uid=10))
            self.native.crew[self.CREW]['cursor'] = 10
            self.mono[0] += 5.0
            self.poll()
            self.assertEqual(self.row(10)['state'], 'read')
            self.assertEqual(len([l for l in self.logs if 'read states stream fallback:' in l]), 1)
        self.bridge.read_states.stop()

        # 2. Stream open without inbox_read detects absence once when GetCrew cursor advances.
        self.logs.clear()
        self.native.stream_mode = None
        self.restart()
        with patch.object(self.bridge, '_log', side_effect=self.logs.append):
            self.bridge.read_states.start()
            self.addCleanup(self.bridge.read_states.stop)
            self._wait_until(lambda: self.bridge.read_states._stream_connected, timeout=2.0)
            self.drain(self.update(uid=11), self.update(uid=12))
            self.native.crew[self.CREW]['cursor'] = 12
            self.mono[0] += 5.0
            self.poll()
            self.assertEqual(self.row(11)['state'], 'read')
            self.assertEqual(self.row(12)['state'], 'read')
            fallback_logs = [l for l in self.logs if 'did not emit inbox_read' in l]
            self.assertEqual(len(fallback_logs), 1)

    def test_bridge_run_starts_and_stops_read_states_stream(self):
        ready = threading.Event()

        def transport(url, body, *args, **kwargs):
            if url.endswith('/getUpdates'):
                if not ready.is_set():
                    ready.set()
                    return 200, dict(ok=True, result=[self.update(uid=10)])
                self.bridge.stop.wait(0.05)
                return 200, dict(ok=True, result=[])
            return self.transport(url, body, *args, **kwargs)

        self.bridge.request = transport
        run_thread = threading.Thread(target=testports.run_bridge, args=(self.bridge,), daemon=True)
        with patch.object(self.bridge, 'probe_with_retry', return_value=True), \
                patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO):
            run_thread.start()
            self._wait_until(lambda: (self.row(10) or {}).get('state') == 'landed', timeout=3.0)
            self._wait_until(lambda: self.bridge.read_states._stream_connected, timeout=3.0)
            self.native.push_event(inbox_read_event(20, reader=self.CREW, through=10, session=''))
            self._wait_until(lambda: (self.row(10) or {}).get('state') == 'read', timeout=2.0)
            self.bridge.stop.set()
            run_thread.join(timeout=5)
        self.assertFalse(run_thread.is_alive())
        self.assertFalse(self.bridge.read_states._stream_started)


class SecondaryReadStateTests(ReadStateCase):
    CREW = 'secretary'
    SOURCE = 'tgsecretary'
    CARDS = False
    BOT_ID = 998

    test_full_lifecycle_replace_not_stack_and_idle_call_counts = ReadStateTests.test_full_lifecycle_replace_not_stack_and_idle_call_counts
    test_stream_inbox_read_and_replied_act_at_once_and_ignore_other_session_or_source = (
        ReadStateTests.test_stream_inbox_read_and_replied_act_at_once_and_ignore_other_session_or_source
    )
    test_stream_drop_resumes_from_last_seq_without_lost_or_duplicate_reaction = (
        ReadStateTests.test_stream_drop_resumes_from_last_seq_without_lost_or_duplicate_reaction
    )
    test_stream_fallback_when_events_refused_or_inbox_read_absent = (
        ReadStateTests.test_stream_fallback_when_events_refused_or_inbox_read_absent
    )
    test_bridge_run_starts_and_stops_read_states_stream = (
        ReadStateTests.test_bridge_run_starts_and_stops_read_states_stream
    )

    def test_secondary_offer_and_read_only_degrade(self):
        self.assertFalse(self.bridge.native_decide())
        self.assertIsNone(self.bridge.run_asks.client)
        self._enable_offers()
        self.drain(self.update(uid=10))
        self.wall[0] += 300
        self.poll()
        token = self.row(10)['offer_token']
        self.assertIsNotNone(token)
        self.poll(self._tap(token))
        self.assertEqual(self._nudge_writes(), [dict(target=self.CREW, user='',
            text=b.read_states.OFFER_LINE.format(seq=10), clear=False, actor='pat')])
        ident = self.native.nudges['crew:' + self.CREW]['id']
        self.native.journal_events[self.CREW + '-journal'].append(
            dict(nudged(502, ident, crew=self.CREW), bead=self.CREW + '-journal'))
        self.poll()
        self.assertEqual(self._sends()[-1]['text'], '✓ Nudge typed')
        self.assertLessEqual(set(self.native_methods()),
                            {'GetUser', 'GetCrew', 'EventsSince', 'Events', 'ListSessions', 'NudgeSession'})

        self.native.calls.clear()
        self.restart(yardr_operator=None)
        self.drain(self.update(uid=50))
        self.native.crew[self.CREW]['cursor'] = 11
        self.poll()
        self.assertEqual(self.row(50)['state'], 'read')
        self.assertNotIn('GetUser', self.native_methods())
        self.assertNotIn('NudgeSession', self.native_methods())

        self.native.nudge_mode = 'no-nudge-method'
        self.calls.clear()
        self.native.calls.clear()
        self.logs.clear()
        self.restart(commands=(y.NUDGE_COMMAND,), reactions=dict(self.config['reactions'], nudge_after=300))
        self.drain(self.update(uid=51))
        self.wall[0] += 300
        self.poll()
        self.mono[0] += 60
        self.poll()
        self.assertEqual(self._sends(), [])
        self.assertEqual([args for m, args in self.native.calls if m == 'NudgeSession'], [{}])
        self.assertEqual(len([line for line in self.logs if 'nudge offers off' in line]), 1)


class RealReadStateTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        self.binary = testyardr.binary()
        if self.binary is None:
            self.skipTest('yardr required for isolated real read-state tests')

    def _start_yard(self, root):
        server, home, command, env = testyardr.start(root, self.binary)

        def cli(*args, ok=True, stdin=None):
            result = subprocess.run(command + list(args), env=env, cwd=root, input=stdin,
                                    text=True, capture_output=True, timeout=60)
            if ok:
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result

        try:
            cli('init', '--harness', 'claude')
            cli('settings', 'set', 'operator', 'pat')
            requests = []

            test_case = self

            class BridgeProxy:
                def __getattr__(self, name):
                    return getattr(test_case.bridge, name)

            class Handler(b._callback_handler(BridgeProxy())):
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
            pack = root / 'pack'
            b.stage_pack(b.render_pack(PACK, callback.server_address[1]), pack)
            checked = cli('pack', 'check', str(pack), ok=False)
            if checked.returncode:
                callback.shutdown()
                callback.server_close()
                worker.join(timeout=5)
                self.skipTest('pack check refused: ' + checked.stderr.strip())
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
            probe = testports.bind_test_server(http.server.BaseHTTPRequestHandler)
            port = probe.server_address[1]
            probe.server_close()
            cli('settings', 'set', 'hook_addr', f'127.0.0.1:{port}')
            testyardr.stop(server)
            log_path = root / 'server.log'
            with log_path.open('w') as log:
                server = subprocess.Popen(command + ['server', 'run'], env=env, cwd=root, stdout=log, stderr=log)
            deadline = time.monotonic() + 15
            while not re.search(r'hook receiver ready.*127\.0\.0\.1:' + str(port), log_path.read_text()):
                if server.poll() is not None or time.monotonic() > deadline:
                    self.fail('isolated hook receiver did not start: ' + log_path.read_text())
                time.sleep(.02)
            env['TGBRIDGE_SECRET'] = self.bridge.secret
            return server, home, command, env, port, secret.read_text(), callback, worker
        except Exception:
            testyardr.stop(server)
            raise

    def second_instance(self, root, cli, home, port, mono, wall):
        state = root / 'secondary-state'
        state.mkdir(mode=0o700)
        store = b.Store(state / 'ledger.sqlite3', 0)
        config = dict(self.config, agent='secretary', bot_id=998, account_id='secretary_bot', yardr_source='tgsecretary',
                      yardr_home=str(home), yardr_operator='pat', yardr_cards=False,
                      hook_url=f'http://127.0.0.1:{port}/hooks/tgsecretary', hook_secret='secondary-hook',
                      commands=(y.NUDGE_COMMAND,),
                      reactions=dict(on_accept=True, seen=DOVE, read=EYES, done=OK_HAND, nudge_after=300))
        telegram = self.bridge.request
        def request(url, body, *args, **kwargs):
            if url == config['hook_url']:
                conn = http.client.HTTPConnection('127.0.0.1', port, timeout=10)
                try:
                    conn.request('POST', '/hooks/tgsecretary', json.dumps(body),
                                 kwargs['headers'] | {'Content-Type': 'application/json'})
                    response = conn.getresponse()
                    return response.status, json.load(response)
                finally:
                    conn.close()
            return telegram(url, body, *args, **kwargs)
        br = b.Bridge(config, store, 'SECOND', 'SECOND_CAPABILITY', request=request,
                      monotonic=lambda: mono[0], sleep=lambda _: None)
        br.run_asks.now = lambda: wall[0]
        case = self
        class Proxy:
            def __getattr__(self, name):
                return getattr(case.bridge, name)
        server = testports.bind_test_server(b._callback_handler(Proxy()))
        worker = threading.Thread(target=server.serve_forever, daemon=True)
        worker.start()
        try:
            pack = root / 'secondary-pack'
            b.stage_pack(b.render_pack(PACK, server.server_address[1], 'tgsecretary'), pack)
            cli('pack', 'apply', str(pack), '--yes')
            cli('source', 'key', 'tgsecretary', stdin='TGBRIDGE_SECRET=SECOND_CAPABILITY\n')
            cli('source', 'set', 'tgsecretary', 'updates_per_hour=300')
            cli('user', 'identity', 'pat', '--source', 'tgsecretary', '--id', '123')
            credential = root / 'secretary.env'
            credential.write_text('TGBRIDGE_SECRET=SECOND_CAPABILITY\n')
            credential.chmod(0o600)
            cli('crew', 'add', 'secretary', '--dir', str(root), '--set', 'kind=codex',
                '--set', 'env_file=' + str(credential))
            cli('subscribe', 'secretary', 'tgsecretary', '--scope', 'all', '--where', 'type=message')
            secret = root / 'secondary-hook.secret'
            secret.write_text('secondary-hook')
            secret.chmod(0o600)
            cli('hook', 'add', 'tgsecretary', '--source', 'tgsecretary', '--secret-file', str(secret), '--unknown', 'ignore')
        except BaseException:
            server.shutdown()
            server.server_close()
            worker.join(timeout=5)
            raise
        return br, server, worker

    def test_real_core_transitions_races_and_degrades(self):
        with tempfile.TemporaryDirectory(prefix='tgrs-', dir='/tmp') as tmp:
            root = Path(tmp)
            server, home, command, env, port, hook_secret, callback, worker = self._start_yard(root)
            mono = [1000.0]
            wall = [2000.0]
            next_sent_id = [200]
            logs = []
            rpc_calls = []
            lose_next_hook = [False]
            react_hook = [None]
            actions = []

            def cli(*args, stdin=None):
                result = subprocess.run(command + list(args), env=env, cwd=root, text=True,
                                        capture_output=True, input=stdin, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result

            def make_transport():
                base = self.transport

                def request(url, body, *args, **kwargs):
                    if url == f'http://127.0.0.1:{port}/hooks/tgbridge':
                        target = urlsplit(url)
                        conn = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
                        try:
                            conn.request('POST', target.path, json.dumps(body),
                                         kwargs['headers'] | {'Content-Type': 'application/json'})
                            resp = conn.getresponse()
                            status, answer = resp.status, json.load(resp)
                        finally:
                            conn.close()
                        if lose_next_hook[0]:
                            lose_next_hook[0] = False
                            raise b.TransportError('synthetic lost hook answer')
                        return status, answer
                    if url.endswith('/sendMessage'):
                        self.calls.append((url, body))
                        mid = next_sent_id[0]
                        next_sent_id[0] += 1
                        return 200, dict(ok=True, result=dict(message_id=mid, chat=dict(id=123)))
                    if url.endswith('/editMessageText'):
                        self.calls.append((url, body))
                        return 200, dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=123)))
                    if url.endswith('/answerCallbackQuery'):
                        self.calls.append((url, body))
                        return 200, dict(ok=True, result=True)
                    if url.endswith('/setMessageReaction'):
                        self.calls.append((url, body))
                        if react_hook[0] is not None:
                            react_hook[0](body)
                        return 200, dict(ok=True, result=True)
                    if url.endswith('/sendChatAction'):
                        actions.append(body)
                        return 200, dict(ok=True, result=True)
                    return base(url, body, *args, **kwargs)

                return request

            def make_bridge(**extra):
                cfg = dict(
                    self.config,
                    agent='writer',
                    yardr_home=str(home),
                    yardr_socket=str(home / 'yard.sock'),
                    yardr_operator='pat',
                    yardr_source='tgbridge',
                    hook_url=f'http://127.0.0.1:{port}/hooks/tgbridge',
                    hook_secret=hook_secret,
                    reactions=dict(on_accept=True, seen=DOVE, read=EYES, done=OK_HAND),
                    typing=dict(seconds=300),
                    **extra,
                )
                br = b.Bridge(cfg, self.store, 'SECRET', 'CAPABILITY', request=make_transport(),
                              request_multipart=self.multipart_transport, request_download=self.download_transport,
                              sleep=lambda s: None, monotonic=lambda: mono[0])
                br.run_asks.now = lambda: wall[0]
                orig_call = br.run_asks.client.call

                def spy(method, arguments):
                    rpc_calls.append((method, arguments))
                    return orig_call(method, arguments)

                br.run_asks.client.call = spy
                return br

            def poll(br):
                telegram = br.telegram

                def fetched(method, body, timeout=20):
                    return (200, dict(ok=True, result=[])) if method == 'getUpdates' else telegram(method, body, timeout)

                with patch.object(br, 'telegram', side_effect=fetched), \
                        patch.object(br, '_log', side_effect=logs.append):
                    br.poll_once()
                br.reaction_queue.join()

            def emojis(mid):
                return [body['reaction'][0]['emoji'] for url, body in self.calls
                        if url.endswith('/setMessageReaction') and body['message_id'] == mid]

            try:
                self.bridge = make_bridge()
                # Ensure handler inside callback server uses the active bridge.
                callback.RequestHandlerClass.bridge = self.bridge

                # 1. Duplicate delivery -> one row and one dove.
                lose_next_hook[0] = True
                with self.assertRaises(b.TransportError):
                    self.bridge.drain([self.update(40)])
                self.bridge.drain([self.update(40)])
                self.bridge.reaction_queue.join()
                row1 = self.bridge.read_states.get(40)
                self.assertEqual(row1['state'], 'landed')
                self.assertEqual(emojis(40), [DOVE])
                seq1 = row1['seq']

                # 2. Cursor below seq stays dove.
                poll(self.bridge)
                self.assertEqual(self.bridge.read_states.get(40)['state'], 'landed')
                self.assertEqual(emojis(40), [DOVE])
                mark = self.bridge.typing_windows[None]['mark']

                # 3. yardr inbox --crew writer --ack <seq> -> eyes next round; typing window survives read.
                cli('inbox', '--crew', 'writer', '--ack', str(seq1))
                mono[0] += 60.0
                poll(self.bridge)
                self.assertEqual(self.bridge.read_states.get(40)['state'], 'read')
                self.assertEqual(emojis(40), [DOVE, EYES])
                self.assertFalse(self.store.sent_since(mark))

                # 4. yardr reply <seq> --crew writer -> ok_hand.
                cli('reply', str(seq1), '--crew', 'writer', 'Real core reply 1')
                mono[0] += 60.0
                poll(self.bridge)
                self.assertEqual(self.bridge.read_states.get(40)['state'], 'done')
                self.assertEqual(emojis(40), [DOVE, EYES, OK_HAND])

                # 5. Reply before any ack -> dove -> ok_hand directly.
                self.bridge.drain([self.update(41)])
                self.bridge.reaction_queue.join()
                seq2 = self.bridge.read_states.get(41)['seq']
                cli('reply', str(seq2), '--crew', 'writer', 'Direct reply without ack')
                mono[0] += 60.0
                poll(self.bridge)
                self.assertEqual(self.bridge.read_states.get(41)['state'], 'done')
                self.assertEqual(emojis(41), [DOVE, OK_HAND])

                # 6. Configured crew not in deliveries goes off with no GetCrew.
                cli('crew', 'add', 'observer', '--dir', str(root), '--set', 'kind=codex')
                self.bridge = make_bridge(topics=(dict(name='silent', agent='observer'),))
                callback.RequestHandlerClass.bridge = self.bridge
                self.store.record_topic('silent', 77, 'created')
                rpc_calls.clear()
                topic_msg = dict(update_id=42, message=dict(
                    message_id=42, text='unsubscribed topic', message_thread_id=77, is_topic_message=True,
                    chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)}))
                self.bridge.drain([topic_msg])
                self.bridge.reaction_queue.join()
                mono[0] += 60.0
                poll(self.bridge)
                self.assertEqual(self.bridge.read_states.get(42)['state'], 'off')
                self.assertEqual(self.bridge.read_states.get(42)['reason'], 'crew_not_in_deliveries')
                self.assertNotIn('GetCrew', [m for m, _ in rpc_calls])

                # 7. Restart after reserved read key replays saved receipt with no second setMessageReaction.
                self.bridge = make_bridge()
                callback.RequestHandlerClass.bridge = self.bridge
                self.bridge.drain([self.update(43)])
                self.bridge.reaction_queue.join()
                seq4 = self.bridge.read_states.get(43)['seq']
                cli('inbox', '--crew', 'writer', '--ack', str(seq4))
                # Pre-reserve readstate:43:read as delivered before row update, simulating a crash.
                body = dict(chat_id=123, message_id=43, reaction=[dict(type='emoji', emoji=EYES)])
                self.store.reserve('readstate:43:read', b.Bridge.delivery_fingerprint(body),
                                   self.bridge.receipt(True, message_id='43'))
                self.bridge = make_bridge()
                callback.RequestHandlerClass.bridge = self.bridge
                before_calls = len(emojis(43))
                poll(self.bridge)
                self.assertEqual(self.bridge.read_states.get(43)['state'], 'read')
                self.assertEqual(len(emojis(43)), before_calls)

                # 8. react done racing a read round ends at ok_hand, never eyes after it.
                self.bridge.drain([self.update(44)])
                self.bridge.reaction_queue.join()
                seq5 = self.bridge.read_states.get(44)['seq']
                cli('inbox', '--crew', 'writer', '--ack', str(seq5))
                entered, release = threading.Event(), threading.Event()
                cli_bridge = make_bridge()

                def block_read(body):
                    if body['message_id'] == 44 and body['reaction'][0]['emoji'] == EYES and not entered.is_set():
                        entered.set()
                        self.assertTrue(release.wait(10))

                react_hook[0] = block_read
                mono[0] += 60.0
                t_round = threading.Thread(target=poll, args=(self.bridge,))
                t_round.start()
                self.assertTrue(entered.wait(10))
                t_done = threading.Thread(
                    target=lambda: cli_bridge.set_reaction(44, OK_HAND, 'reaction:44:race-done'))
                t_done.start()
                t_done.join(0.3)
                self.assertTrue(t_done.is_alive())
                release.set()
                t_round.join(10)
                t_done.join(10)
                react_hook[0] = None
                self.assertEqual(self.bridge.read_states.get(44)['state'], 'done')
                self.assertEqual(emojis(44), [DOVE, EYES, OK_HAND])

                # 9. Real-core reply + ack during acceptance gap (no open rows) still moves to done.
                relay = self.bridge._relay

                def real_reply_before_record(*args, **kwargs):
                    result = relay(*args, **kwargs)
                    cli('reply', str(result[1]), '--crew', 'writer', 'Reply in accept gap')
                    cli('inbox', '--crew', 'writer', '--ack', str(result[1]))
                    return result

                with patch.object(self.bridge, '_relay', side_effect=real_reply_before_record):
                    self.bridge.drain([self.update(45)])
                self.bridge.reaction_queue.join()
                mono[0] += 60.0
                poll(self.bridge)
                self.assertEqual(self.bridge.read_states.get(45)['state'], 'done')
                self.assertEqual(emojis(45), [DOVE, OK_HAND])
                # Finish the second quiet round for Case 9's send mark.
                mono[0] += 60.0
                self.bridge.read_states.refresh()

                # 10. Real-core react done + ack during acceptance gap stays done and never sends eyes.
                def real_react_before_record(*args, **kwargs):
                    result = relay(*args, **kwargs)
                    cli('call', 'tgbridge', 'react', 'reaction=done', 'message_id=46')
                    cli('inbox', '--crew', 'writer', '--ack', str(result[1]))
                    return result

                with patch.object(self.bridge, '_relay', side_effect=real_react_before_record):
                    self.bridge.drain([self.update(46)])
                self.bridge.reaction_queue.join()
                mono[0] += 60.0
                rpc_calls.clear()
                self.bridge.read_states.refresh()
                self.assertEqual(rpc_calls, [])
                mono[0] += 60.0
                poll(self.bridge)
                self.assertEqual(self.bridge.read_states.get(46)['state'], 'done')
                self.assertEqual(emojis(46), [OK_HAND])

                primary = self.bridge
                secondary, second_server, second_worker = self.second_instance(root, cli, home, port, mono, wall)
                try:
                    self.assertTrue(primary.native_decide())
                    self.assertFalse(secondary.native_decide())
                    self.assertIsNone(secondary.run_asks.client)
                    self.bridge = secondary
                    calls = []
                    real = secondary.reader.call
                    def observed(method, args):
                        calls.append((method, args))
                        return real(method, args)
                    secondary.reader.call = observed
                    secondary.drain([self.update(47)])
                    secondary.reaction_queue.join()
                    seq = secondary.read_states.get(47)['seq']
                    self.assertEqual(emojis(47), [DOVE])
                    cli('inbox', '--crew', 'writer', '--ack', str(seq))
                    poll(secondary)
                    self.assertEqual(secondary.read_states.get(47)['state'], 'landed')
                    cli('inbox', '--crew', 'secretary', '--ack', str(seq))
                    mono[0] += 60
                    poll(secondary)
                    self.assertEqual(emojis(47), [DOVE, EYES])
                    env['TGBRIDGE_SECRET'] = 'SECOND_CAPABILITY'
                    cli('reply', str(seq), '--crew', 'secretary', 'Secondary reply')
                    mono[0] += 60
                    poll(secondary)
                    self.assertEqual(emojis(47), [DOVE, EYES, OK_HAND])

                    # Home-only reopens the same ledger with reads, without an operator lookup.
                    config = dict(secondary.config)
                    config.pop('yardr_operator')
                    self.bridge = b.Bridge(config, secondary.store, 'SECOND', 'SECOND_CAPABILITY', request=secondary.request,
                                           monotonic=lambda: mono[0])
                    calls.clear()
                    home_only_call = self.bridge.reader.call
                    def observed_home_only(method, args):
                        calls.append((method, args))
                        return home_only_call(method, args)
                    self.bridge.reader.call = observed_home_only
                    self.bridge.drain([self.update(48)])
                    self.bridge.reaction_queue.join()
                    seq = self.bridge.read_states.get(48)['seq']
                    cli('inbox', '--crew', 'secretary', '--ack', str(seq))
                    poll(self.bridge)
                    self.assertEqual(self.bridge.read_states.get(48)['state'], 'read')
                    cli('reply', str(seq), '--crew', 'secretary', 'Read-only instance reply')
                    mono[0] += 60
                    poll(self.bridge)
                    self.assertEqual(emojis(48), [DOVE, EYES, OK_HAND])
                    self.assertNotIn('GetUser', [m for m, _ in calls])
                    self.assertNotIn('NudgeSession', [m for m, _ in calls])
                    self.assertLessEqual({m for m, _ in calls},
                                        {'GetCrew', 'EventsSince', 'Events', 'GetUser', 'ListSessions', 'NudgeSession'})
                    for call, fields in [('run_ask', ['bead=missing', 'ask=7']),
                                         ('ask_card', ['bead=missing', 'ask=7']),
                                         ('siding_card', ['depot=missing'])]:
                        refused = json.loads(cli('call', 'tgsecretary', call, *fields, 'key=second-' + call, '--json').stdout)
                        self.assertFalse(refused['delivered'])
                        self.assertIn('not_configured', refused['reason'])
                    ordinary = self.update(49)
                    ordinary['message']['text'] = '/decide'
                    self.bridge.drain([ordinary])
                    self.assertIsNotNone(self.bridge.read_states.get(49))
                    self.bridge.inbound(dict(update_id=50, callback_query=dict(id='second-decide', data='decide:o:1',
                        message=dict(message_id=1234, chat=dict(id=123, type='private'),
                                     **{'from': dict(id=998, is_bot=True)}), **{'from': dict(id=123, is_bot=False)})))
                    print('Two real native crews: owner writer and non-owner secretary; landed/read/done, '
                          'cross-crew ack isolation, home-only reads, native API refusals')
                finally:
                    second_server.shutdown()
                    second_server.server_close()
                    second_worker.join(timeout=5)
                    self.bridge = primary

            finally:
                callback.shutdown()
                callback.server_close()
                worker.join(timeout=5)
                testyardr.stop(server)

    def test_real_core_nudge_offer_on_fork_and_silent_on_upstream(self):
        for secondary in (False, True):
            with self.subTest(secondary=secondary):
                self.exercise_real_core_nudge_offer(secondary)

    def exercise_real_core_nudge_offer(self, secondary):
        self.calls.clear()
        with tempfile.TemporaryDirectory(prefix='tgrs-nudge-', dir='/tmp') as tmp:
            root = Path(tmp)
            server, home, command, env, port, hook_secret, callback, worker = self._start_yard(root)
            mono = [1000.0]
            wall = [2000.0]
            next_sent_id = [300]
            logs = []
            rpc_calls = []

            def cli(*args, stdin=None):
                result = subprocess.run(command + list(args), env=env, cwd=root, text=True,
                                        capture_output=True, input=stdin, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result

            def make_transport():
                base = self.transport

                def request(url, body, *args, **kwargs):
                    if url == f'http://127.0.0.1:{port}/hooks/tgbridge':
                        target = urlsplit(url)
                        conn = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
                        try:
                            conn.request('POST', target.path, json.dumps(body),
                                         kwargs['headers'] | {'Content-Type': 'application/json'})
                            resp = conn.getresponse()
                            return resp.status, json.load(resp)
                        finally:
                            conn.close()
                    if url.endswith('/sendMessage'):
                        self.calls.append((url, body))
                        mid = next_sent_id[0]
                        next_sent_id[0] += 1
                        return 200, dict(ok=True, result=dict(message_id=mid, chat=dict(id=123)))
                    if url.endswith('/editMessageText'):
                        self.calls.append((url, body))
                        return 200, dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=123)))
                    if url.endswith('/answerCallbackQuery'):
                        self.calls.append((url, body))
                        return 200, dict(ok=True, result=True)
                    if url.endswith('/setMessageReaction'):
                        self.calls.append((url, body))
                        return 200, dict(ok=True, result=True)
                    return base(url, body, *args, **kwargs)

                return request

            def make_bridge():
                cfg = dict(
                    self.config,
                    agent='writer',
                    yardr_home=str(home),
                    yardr_socket=str(home / 'yard.sock'),
                    yardr_operator='pat',
                    yardr_source='tgbridge',
                    hook_url=f'http://127.0.0.1:{port}/hooks/tgbridge',
                    hook_secret=hook_secret,
                    commands=(y.NUDGE_COMMAND,),
                    reactions=dict(on_accept=True, seen=DOVE, read=EYES, done=OK_HAND, nudge_after=300),
                )
                br = b.Bridge(cfg, self.store, 'SECRET', 'CAPABILITY', request=make_transport(),
                              request_multipart=self.multipart_transport, request_download=self.download_transport,
                              sleep=lambda s: None, monotonic=lambda: mono[0])
                br.run_asks.now = lambda: wall[0]
                orig_call = br.yard_client().call

                def spy(method, arguments):
                    rpc_calls.append((method, arguments))
                    return orig_call(method, arguments)

                br.yard_client().call = spy
                return br

            def poll(br, *updates):
                telegram = br.telegram

                def fetched(method, body, timeout=20):
                    return (200, dict(ok=True, result=list(updates))) if method == 'getUpdates' else telegram(method, body, timeout)

                with patch.object(br, 'telegram', side_effect=fetched), \
                        patch.object(br, '_log', side_effect=logs.append):
                    br.poll_once()
                br.reaction_queue.join()

            crew = 'secretary' if secondary else 'writer'
            second_server = second_worker = None
            try:
                has_nudge = testyardr.serves(home, 'NudgeSession')
                br = make_bridge()
                self.bridge = br
                if secondary:
                    br, second_server, second_worker = self.second_instance(root, cli, home, port, mono, wall)
                    self.bridge = br
                    secondary_call = br.reader.call
                    def observed(method, arguments):
                        rpc_calls.append((method, arguments))
                        return secondary_call(method, arguments)
                    br.reader.call = observed
                callback.RequestHandlerClass.bridge = br

                if not has_nudge:
                    u = self.update(uid=50)
                    u['message']['text'] = 'upstream unread message'
                    br.drain([u])
                    br.reaction_queue.join()
                    wall[0] += 300
                    mono[0] += 60.0
                    poll(br)
                    sends = [body for url, body in self.calls if url.endswith('/sendMessage')]
                    self.assertEqual(sends, [])
                    self.assertIsNone(br.read_states.get(50)['offer_token'])
                    self.assertEqual([args for m, args in rpc_calls if m == 'NudgeSession'], [])
                    # Even when GetCrew reports running+idle, real U refuses NudgeSession as unsupported.
                    real_call = br.yard_client().call

                    def idle_get_crew(method, arguments):
                        res = real_call(method, arguments)
                        if method == 'GetCrew' and isinstance(res, dict):
                            res = dict(res, state='running', status='idle')
                        return res

                    br.yard_client().call = idle_get_crew
                    mono[0] += 60.0
                    poll(br)
                    sends = [body for url, body in self.calls if url.endswith('/sendMessage')]
                    self.assertEqual(sends, [])
                    self.assertIsNone(br.read_states.get(50)['offer_token'])
                    self.assertEqual([args for m, args in rpc_calls if m == 'NudgeSession'], [{}])
                    self.assertEqual(len([l for l in logs if 'nudge offers off' in l]), 1)
                    rpc_calls.clear()
                    mono[0] += 60.0
                    poll(br)
                    self.assertEqual([args for m, args in rpc_calls if m == 'NudgeSession'], [])
                    print('Real core U (no NudgeSession):', self.binary, '0 offer cards, 0 NudgeSession writes')
                    return

                fixture = testyardr.store_fixture(root, 'nudge')
                if fixture is None:
                    self.skipTest('TGBRIDGE_TEST_YARDR_SOURCE names no candidate source for the daemon-write fixture')

                def daemon(*args):
                    nonlocal server
                    testyardr.stop(server)
                    try:
                        res = subprocess.run([str(fixture), str(home), *args], capture_output=True, text=True, timeout=60)
                        self.assertEqual(res.returncode, 0, res.stderr)
                    finally:
                        server = testyardr.restart(root, self.binary, home, command, env)
                    return res.stdout.split()

                # Real hook delivery while crew is stopped, then started+working, then idle.
                u = self.update(uid=50)
                u['message']['text'] = 'real fork secret text'
                br.drain([u])
                br.reaction_queue.join()
                seq = br.read_states.get(50)['seq']

                wall[0] += 300
                poll(br)
                self.assertEqual([body for url, body in self.calls if url.endswith('/sendMessage')], [])

                daemon('start', crew)
                daemon('status', crew, 'working')
                mono[0] += 60.0
                poll(br)
                self.assertEqual([body for url, body in self.calls if url.endswith('/sendMessage')], [])

                daemon('status', crew, 'idle')
                mono[0] += 60.0
                poll(br)
                sends = [body for url, body in self.calls if url.endswith('/sendMessage')]
                self.assertEqual(len(sends), 1)
                card = sends[0]
                self.assertTrue(card.get('disable_notification'))
                self.assertEqual(card.get('reply_parameters'), {'message_id': 50})
                self.assertIn('Unread for 5m', card['text'])
                self.assertIn('idle', card['text'])
                expected_line = b.read_states.OFFER_LINE.format(seq=seq)
                self.assertIn(expected_line, unescape(card['text']))
                self.assertNotIn('real fork secret text', card['text'])

                token = br.read_states.get(50)['offer_token']
                self.assertIsNotNone(token)
                tn_row = br.typed_nudge.get(token)
                tap = dict(update_id=60, callback_query=dict(
                    id='q60', data=f'nudge:{token}:nudge',
                    message=dict(message_id=tn_row['message'], chat=dict(id=123, type='private'),
                                 **{'from': dict(id=br.config['bot_id'], is_bot=True)}),
                    **{'from': dict(id=123, is_bot=False)},
                ))
                poll(br, tap)
                nudge_writes = [args for m, args in rpc_calls if m == 'NudgeSession' and args.get('target')]
                self.assertEqual(len(nudge_writes), 1)
                self.assertEqual(nudge_writes[0], dict(target=crew, user='', text=expected_line,
                                                       clear=False, actor='pat'))
                journal = br.typed_nudge.get(token)['journal']
                ident = br.typed_nudge.get(token)['request']
                requested = [e for e in br.yard_client().call('Events', [journal, 200])
                             if e['kind'] == 'session_nudge_requested' and e['data'].get('id') == ident]
                self.assertEqual(len(requested), 1)
                self.assertEqual(requested[0]['data']['line'], expected_line)

                daemon('finish', crew, 'typed', '')
                poll(br)
                sends = [body for url, body in self.calls if url.endswith('/sendMessage')]
                self.assertEqual(len(sends), 2)
                self.assertEqual(sends[-1]['text'], '\u2713 Nudge typed')
                self.assertEqual(sends[-1]['reply_parameters']['message_id'], tn_row['message'])

                # Ack moves landed -> read (eyes) as usual.
                cli('inbox', '--crew', crew, '--ack', str(seq))
                mono[0] += 60.0
                poll(br)
                self.assertEqual(br.read_states.get(50)['state'], 'read')
                emojis = [body['reaction'][0]['emoji'] for url, body in self.calls
                          if url.endswith('/setMessageReaction') and body['message_id'] == 50]
                self.assertEqual(emojis, [DOVE, EYES])
                print('Real core F (NudgeSession), crew', crew, 'cards', br.native_decide(), ':', self.binary,
                      'offer -> tap -> session_nudged -> outcome -> read')
            finally:
                if second_server is not None:
                    second_server.shutdown()
                    second_server.server_close()
                    second_worker.join(timeout=5)
                callback.shutdown()
                callback.server_close()
                worker.join(timeout=5)
                testyardr.stop(server)

    def test_real_core_stream_ndfm_fallback_and_drop_resume(self):
        class _UnixProxy:
            def __init__(self, proxy_home, target_sock):
                self.target_sock = str(target_sock)
                self.drop_inbox_read = False
                self.stream_afters = []
                self._lock = threading.Lock()
                self._active = []
                owner = self

                class Handler(socketserver.BaseRequestHandler):
                    def handle(self):
                        up = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
                        up.connect(owner.target_sock)
                        buf = b''
                        while b'\r\n\r\n' not in buf:
                            chunk = self.request.recv(4096)
                            if not chunk:
                                up.close()
                                return
                            buf += chunk
                        head, rest = buf.split(b'\r\n\r\n', 1)
                        req_line = head.split(b'\r\n', 1)[0].decode('ascii', 'replace')
                        parts = req_line.split()
                        is_stream = len(parts) >= 2 and parts[0] == 'GET' and parts[1].startswith('/events')
                        if is_stream:
                            qs = parse_qs(urlsplit(parts[1]).query)
                            owner.stream_afters.append(int(qs.get('after', ['0'])[0]))
                            with owner._lock:
                                owner._active.append((self.request, up))
                        up.sendall(head + b'\r\n\r\n' + rest)
                        try:
                            if is_stream and owner.drop_inbox_read:
                                resp = http.client.HTTPResponse(up)
                                resp.begin()
                                self.request.sendall(
                                    f'HTTP/1.1 {resp.status} {resp.reason}\r\n'
                                    'Content-Type: application/x-ndjson\r\n'
                                    'Transfer-Encoding: chunked\r\n\r\n'.encode()
                                )
                                while True:
                                    line = resp.readline(1024 * 1024)
                                    if not line:
                                        break
                                    try:
                                        parsed = json.loads(line)
                                        if parsed.get('kind') == 'inbox_read':
                                            line = (json.dumps({
                                                'seq': parsed['seq'], 'kind': 'proxy_filtered',
                                                'actor': 'yard', 'at': parsed['at'],
                                            }) + '\n').encode()
                                    except ValueError:
                                        pass
                                    self.request.sendall(f'{len(line):x}\r\n'.encode() + line + b'\r\n')
                                self.request.sendall(b'0\r\n\r\n')
                                return

                            def pump(src, dst):
                                try:
                                    while True:
                                        data = src.recv(65536)
                                        if not data:
                                            break
                                        dst.sendall(data)
                                except OSError:
                                    pass
                                finally:
                                    try:
                                        dst.shutdown(socket.SHUT_WR)
                                    except OSError:
                                        pass

                            t1 = threading.Thread(target=pump, args=(self.request, up), daemon=True)
                            t1.start()
                            pump(up, self.request)
                            t1.join(timeout=2)
                        except OSError:
                            pass
                        finally:
                            if is_stream:
                                with owner._lock:
                                    if (self.request, up) in owner._active:
                                        owner._active.remove((self.request, up))
                            up.close()

                class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
                    daemon_threads = True

                proxy_home.mkdir(mode=0o700, exist_ok=True)
                self.server = Server(str(proxy_home / 'yard.sock'), Handler)
                self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
                self.thread.start()

            def drop_streams(self):
                with self._lock:
                    pairs = list(self._active)
                for cli_sock, up_sock in pairs:
                    for s in (cli_sock, up_sock):
                        try:
                            s.shutdown(socket.SHUT_RDWR)
                        except OSError:
                            pass
                        try:
                            s.close()
                        except OSError:
                            pass

            def close(self):
                self.drop_streams()
                self.server.shutdown()
                self.server.server_close()
                self.thread.join(timeout=5)

        def wait_until(pred, timeout=10.0, msg='condition not met'):
            deadline = time.monotonic() + timeout
            while time.monotonic() < deadline:
                if pred():
                    return
                time.sleep(0.02)
            self.fail(msg)

        with tempfile.TemporaryDirectory(prefix='tgrs-stream-', dir='/tmp') as tmp:
            root = Path(tmp)
            server, home, command, env, port, hook_secret, callback, worker = self._start_yard(root)
            mono = [1000.0]
            wall = [2000.0]
            next_sent_id = [500]
            logs = []
            second_server = second_worker = proxy = None

            def cli(*args, stdin=None):
                result = subprocess.run(command + list(args), env=env, cwd=root, text=True,
                                        capture_output=True, input=stdin, timeout=60)
                self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
                return result

            def make_transport():
                base = self.transport

                def request(url, body, *args, **kwargs):
                    if url.startswith(f'http://127.0.0.1:{port}/hooks/'):
                        target = urlsplit(url)
                        conn = http.client.HTTPConnection(target.hostname, target.port, timeout=10)
                        try:
                            conn.request('POST', target.path, json.dumps(body),
                                         kwargs['headers'] | {'Content-Type': 'application/json'})
                            resp = conn.getresponse()
                            return resp.status, json.load(resp)
                        finally:
                            conn.close()
                    if url.endswith('/sendMessage'):
                        self.calls.append((url, body))
                        mid = next_sent_id[0]
                        next_sent_id[0] += 1
                        return 200, dict(ok=True, result=dict(message_id=mid, chat=dict(id=123)))
                    if url.endswith('/setMessageReaction'):
                        self.calls.append((url, body))
                        return 200, dict(ok=True, result=True)
                    return base(url, body, *args, **kwargs)

                return request

            def emojis(mid):
                return [body['reaction'][0]['emoji'] for url, body in self.calls
                        if url.endswith('/setMessageReaction') and body['message_id'] == mid]

            try:
                cfg = dict(
                    self.config, agent='writer', yardr_home=str(home), yardr_socket=str(home / 'yard.sock'),
                    yardr_operator='pat', yardr_source='tgbridge',
                    hook_url=f'http://127.0.0.1:{port}/hooks/tgbridge', hook_secret=hook_secret,
                    reactions=dict(on_accept=True, seen=DOVE, read=EYES, done=OK_HAND),
                )
                self.bridge = b.Bridge(cfg, self.store, 'SECRET', 'CAPABILITY', request=make_transport(),
                                       request_multipart=self.multipart_transport,
                                       request_download=self.download_transport, sleep=lambda s: None)
                callback.RequestHandlerClass.bridge = self.bridge
                secondary, second_server, second_worker = self.second_instance(root, cli, home, port, mono, wall)
                secondary.sleep = lambda s: None
                secondary.monotonic = time.monotonic

                # Probe whether this real yardr binary emits inbox_read on GET /events.
                probe_client = b.run_asks.YardSocketClient(home, 'tgbridge')
                probe_head = probe_client.call('Head', [])
                probe_stop = threading.Event()
                probe_seen = []

                def probe_watch():
                    try:
                        for ev in probe_client.stream_events(probe_head, stop_event=probe_stop):
                            probe_seen.append(ev)
                            if ev['kind'] == 'inbox_read':
                                probe_stop.set()
                                break
                    except Exception:
                        pass

                t_probe = threading.Thread(target=probe_watch, daemon=True)
                t_probe.start()
                self.bridge.drain([self.update(99)])
                self.bridge.reaction_queue.join()
                probe_seq = self.bridge.read_states.get(99)['seq']
                cli('inbox', '--crew', 'writer', '--ack', str(probe_seq))
                deadline = time.monotonic() + 0.8
                while time.monotonic() < deadline and not any(e['kind'] == 'inbox_read' for e in probe_seen):
                    time.sleep(0.02)
                probe_stop.set()
                has_inbox_read = any(e['kind'] == 'inbox_read' for e in probe_seen)
                # Finish msg 99 so open rows start empty.
                cli('reply', str(probe_seq), '--crew', 'writer', 'probe done')
                self.bridge.read_states._set_terminal(99, b.read_states.OPEN_STATES, 'done')

                # 1. Start live stream on both primary (owner) and secondary (cards=False) instances.
                with patch.object(self.bridge, '_log', side_effect=logs.append), \
                        patch.object(secondary, '_log', side_effect=logs.append):
                    self.bridge.read_states.start()
                    secondary.read_states.start()
                    self.addCleanup(self.bridge.read_states.stop)
                    self.addCleanup(secondary.read_states.stop)
                    wait_until(lambda: self.bridge.read_states._stream_connected
                               and secondary.read_states._stream_connected,
                               timeout=5.0, msg='real streams did not connect')

                    self.bridge.drain([self.update(100)])
                    self.bridge.reaction_queue.join()
                    seq100 = self.bridge.read_states.get(100)['seq']
                    self.assertEqual(emojis(100), [DOVE])

                    t_ack = time.monotonic()
                    cli('inbox', '--crew', 'writer', '--ack', str(seq100))
                    limit = 2.0 if has_inbox_read else b.read_states.ROUND_SECONDS + 2.0
                    wait_until(lambda: self.bridge.read_states.get(100)['state'] == 'read'
                               and EYES in emojis(100), timeout=limit, msg='primary ack did not turn to eyes')
                    primary_ack_elapsed = time.monotonic() - t_ack
                    if has_inbox_read:
                        self.assertLess(primary_ack_elapsed, 2.0)
                    else:
                        self.assertLessEqual(primary_ack_elapsed, b.read_states.ROUND_SECONDS + 2.0)
                        wait_until(lambda: any('did not emit inbox_read' in l for l in logs), timeout=2.0)

                    # replied moves read -> done at once (< 2 s) on both ndfm and upstream cores.
                    t_rep = time.monotonic()
                    cli('reply', str(seq100), '--crew', 'writer', 'Immediate stream reply')
                    wait_until(lambda: self.bridge.read_states.get(100)['state'] == 'done'
                               and OK_HAND in emojis(100), timeout=2.0, msg='replied did not turn to ok_hand')
                    replied_elapsed = time.monotonic() - t_rep
                    self.assertLess(replied_elapsed, 2.0)
                    self.assertEqual(emojis(100), [DOVE, EYES, OK_HAND])

                    # Secondary non-owner instance works identically for its crew.
                    secondary.drain([self.update(101)])
                    secondary.reaction_queue.join()
                    seq101 = secondary.read_states.get(101)['seq']
                    t_sec = time.monotonic()
                    cli('inbox', '--crew', 'secretary', '--ack', str(seq101))
                    wait_until(lambda: secondary.read_states.get(101)['state'] == 'read'
                               and EYES in emojis(101), timeout=limit, msg='secondary ack did not turn to eyes')
                    sec_elapsed = time.monotonic() - t_sec
                    if has_inbox_read:
                        self.assertLess(sec_elapsed, 2.0)
                    env['TGBRIDGE_SECRET'] = 'SECOND_CAPABILITY'
                    primary = self.bridge
                    self.bridge = secondary
                    try:
                        cli('reply', str(seq101), '--crew', 'secretary', 'Secondary stream reply')
                    finally:
                        self.bridge = primary
                    wait_until(lambda: secondary.read_states.get(101)['state'] == 'done'
                               and OK_HAND in emojis(101), timeout=2.0)
                    self.assertEqual(emojis(101), [DOVE, EYES, OK_HAND])

                    self.bridge.read_states.stop()
                    secondary.read_states.stop()

                # 2. Stream drop mid-run through proxy against the real server -> resume with no lost/dup reaction.
                proxy_home = root / 'proxy-home'
                proxy = _UnixProxy(proxy_home, home / 'yard.sock')
                proxy_store = b.Store(root / 'proxy.sqlite3', 0)
                proxy_cfg = dict(cfg, yardr_home=str(proxy_home), yardr_socket=str(proxy_home / 'yard.sock'))
                br_proxy = b.Bridge(proxy_cfg, proxy_store, 'SECRET', 'CAPABILITY', request=make_transport(),
                                    sleep=lambda s: None)
                proxy_logs = []
                with patch.object(br_proxy, '_log', side_effect=proxy_logs.append):
                    br_proxy.read_states.start()
                    try:
                        wait_until(lambda: br_proxy.read_states._stream_connected, timeout=5.0)
                        br_proxy.drain([self.update(102)])
                        br_proxy.reaction_queue.join()
                        seq102 = br_proxy.read_states.get(102)['seq']
                        wait_until(lambda: (br_proxy.read_states._last_seq or 0) >= seq102, timeout=2.0)
                        last_before_drop = br_proxy.read_states._last_seq

                        proxy.drop_streams()
                        wait_until(lambda: not br_proxy.read_states._stream_connected, timeout=2.0)
                        cli('inbox', '--crew', 'writer', '--ack', str(seq102))
                        env['TGBRIDGE_SECRET'] = self.bridge.secret
                        cli('reply', str(seq102), '--crew', 'writer', 'Reply while disconnected')

                        wait_until(lambda: br_proxy.read_states.get(102)['state'] == 'done'
                                   and OK_HAND in emojis(102), timeout=3.0,
                                   msg='proxy stream did not resume missed events')
                        self.assertIn(last_before_drop, proxy.stream_afters[1:])
                        self.assertEqual(emojis(102).count(DOVE), 1)
                        self.assertEqual(emojis(102).count(OK_HAND), 1)

                        # 3. On an ndfm core, verify real-server fallback when inbox_read is stripped from stream.
                        if has_inbox_read:
                            proxy.drop_inbox_read = True
                            br_fb = b.Bridge(proxy_cfg, b.Store(root / 'proxy-fb.sqlite3', 0), 'SECRET', 'CAPABILITY',
                                             request=make_transport(), sleep=lambda s: None)
                            fb_logs = []
                            with patch.object(br_fb, '_log', side_effect=fb_logs.append):
                                br_fb.read_states.start()
                                try:
                                    wait_until(lambda: br_fb.read_states._stream_connected, timeout=5.0)
                                    br_fb.drain([self.update(103)])
                                    br_fb.reaction_queue.join()
                                    seq103 = br_fb.read_states.get(103)['seq']
                                    t_fb = time.monotonic()
                                    cli('inbox', '--crew', 'writer', '--ack', str(seq103))
                                    wait_until(lambda: br_fb.read_states.get(103)['state'] == 'read'
                                               and EYES in emojis(103),
                                               timeout=b.read_states.ROUND_SECONDS + 2.0,
                                               msg='real fallback poll did not turn to eyes within 5 s')
                                    fb_elapsed = time.monotonic() - t_fb
                                    self.assertLessEqual(fb_elapsed, b.read_states.ROUND_SECONDS + 2.0)
                                    self.assertEqual(len([l for l in fb_logs if 'did not emit inbox_read' in l]), 1)
                                finally:
                                    br_fb.read_states.stop()
                    finally:
                        br_proxy.read_states.stop()
                print(
                    f'Real core stream ({self.binary}, ndfm={has_inbox_read}): '
                    f'ack={primary_ack_elapsed:.3f}s, replied={replied_elapsed:.3f}s, '
                    f'secondary_ack={sec_elapsed:.3f}s'
                )
            finally:
                if proxy is not None:
                    proxy.close()
                if second_server is not None:
                    second_server.shutdown()
                    second_server.server_close()
                    second_worker.join(timeout=5)
                callback.shutdown()
                callback.server_close()
                worker.join(timeout=5)
                testyardr.stop(server)


if __name__ == '__main__':
    unittest.main()
