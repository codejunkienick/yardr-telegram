"""Read states: landed -> read -> done over fake Telegram and fake or real yardr."""
from contextlib import closing
import http.client
import http.server
import io
import json
from pathlib import Path
import re
import socketserver
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch
from urllib.parse import urlsplit

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
import testports  # noqa: E402
import testyardr  # noqa: E402
from test_bridge import BridgeTestCase, write_settings  # noqa: E402

PACK = Path(__file__).resolve().parents[1] / 'yardr'
DOVE = '\U0001F54A'
EYES = '\U0001F440'
OK_HAND = '\U0001F44C'
THUMBS_UP = '\U0001F44D'
REACTION_INVALID = (400, dict(ok=False, error_code=400, description='Bad Request: REACTION_INVALID'))


class FakeReadYard:
    """Minimal framed Unix RPC socket serving GetUser, EventsSince, GetCrew and Events."""

    def __init__(self, root):
        self.root = Path(root)
        self.calls = []
        self.hook_events = {}
        self.crew = {
            'yardmaster': dict(name='yardmaster', journal='yardmaster-journal', state='idle', status='', cursor=0),
        }
        self.journal_events = {'yardmaster-journal': []}
        self.override = {}
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            protocol_version = 'HTTP/1.1'

            def log_message(self, *args):
                pass

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
                if method == 'GetUser':
                    self._send(200, {'result': dict(name='pat', role='operator',
                                                    identities={'tgbridge': {'id': '123'}})})
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

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)


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


class ReadStateTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        self.yard_dir = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='tgrs-fake-', dir='/tmp')))
        self.native = FakeReadYard(self.yard_dir)
        self.addCleanup(self.native.close)
        self.mono = [1000.0]
        self.wall = [2000.0]
        self.next_seq = 10
        self.next_reply_seq = 500
        self.reaction_answer = (200, dict(ok=True, result=True))
        self.logs = []
        self.config.update(
            yardr_home=str(self.yard_dir),
            yardr_socket=str(self.yard_dir / 'yard.sock'),
            yardr_operator='pat',
            yardr_source='tgbridge',
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
                self.native.hook_events[seq] = hook_event(seq)
            return 200, dict(seq=seq)
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
        self.assertEqual(self.row(10)['crew'], 'yardmaster')
        self.assertEqual(self.row(10)['state'], 'landed')
        self.assertEqual(self.reaction_emojis(), [DOVE])

        # Round 2 before 60 s monotonic does nothing.
        self.mono[0] += 59.0
        self.native.calls.clear()
        self.native.crew['yardmaster']['cursor'] = 10
        self.poll()
        self.assertEqual(self.native_methods(), [])
        self.assertEqual(self.row(10)['state'], 'landed')

        # Round 2 at +60 s: EventsSince not called again, GetCrew moves landed -> read (eyes).
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
        self.native.journal_events['yardmaster-journal'].append(replied_event(501, 10))
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

    def test_real_core_transitions_races_and_degrades(self):
        with tempfile.TemporaryDirectory(prefix='tgrs-', dir='/tmp') as tmp:
            root = Path(tmp)
            server, home, command, env, port, hook_secret, callback, worker = self._start_yard(root)
            mono = [1000.0]
            logs = []
            rpc_calls = []
            lose_next_hook = [False]
            react_hook = [None]
            actions = []

            def cli(*args):
                result = subprocess.run(command + list(args), env=env, cwd=root, text=True,
                                        capture_output=True, timeout=60)
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
            finally:
                callback.shutdown()
                callback.server_close()
                worker.join(timeout=5)
                testyardr.stop(server)


if __name__ == '__main__':
    unittest.main()
