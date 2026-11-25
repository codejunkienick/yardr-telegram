"""Linked-yard mail through the native socket, Telegram poller and shared ledger."""
import json
import os
from pathlib import Path
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
import testyardr  # noqa: E402
from test_bridge import BridgeTestCase, write_settings  # noqa: E402
from test_topic_inbound import OPS, TOPICS, WEB, in_topic, record_topics  # noqa: E402
from test_yard_mail import DECLARATION  # noqa: E402


class PowerLoss(BaseException):
    pass


class PeerMailTests(BridgeTestCase):
    def test_settings_and_public_key_namespace(self):
        root = Path(self.tmp.name)
        path = write_settings(root)
        original = path.read_text()
        self.assertNotIn('yardr_peer_mail', b.load_settings(path).config)
        table = '[yardr.peer_mail]\ncrew = ["yardmaster"]\n'
        path.write_text(original + '\n' + table)
        with self.assertRaisesRegex(b.SettingsError, 'home and operator'):
            b.load_settings(path)
        native = original.replace('[yardr]\n', f'[yardr]\nhome = "{root}"\noperator = "pat"\ncards = false\n')
        for value in ('[]', '["yardmaster", "yardmaster"]', '["@peer"]', '[1]', '"yardmaster"'):
            with self.subTest(value=value):
                path.write_text(native + '\n' + table.replace('["yardmaster"]', value))
                with self.assertRaisesRegex(b.SettingsError, 'non-empty list of unique crew names'):
                    b.load_settings(path)
        topics = '\n[[topic]]\nname = "ops"\n'
        # A missing [[topic]] entry loads: the mirror pauses at send time.
        path.write_text(native + '\n' + table + 'topic = "ops"\n')
        self.assertEqual(b.load_settings(path).config['yardr_peer_mail_topic'], 'ops')
        path.write_text(native + '\n' + table + 'topic = "OPS"\n' + topics)
        self.assertEqual(b.load_settings(path).config['yardr_peer_mail_topic'], 'OPS')
        for value in ('""', '["ops"]', '1'):
            with self.subTest(value=value):
                path.write_text(native + '\n' + table + f'topic = {value}\n' + topics)
                with self.assertRaisesRegex(b.SettingsError, r'peer_mail\] topic must be'):
                    b.load_settings(path)
        path.write_text(native + '\n' + table + topics)
        settings = b.load_settings(path)
        self.assertEqual(settings.config['yardr_peer_mail'], ('yardmaster',))
        self.assertNotIn('yardr_peer_mail_topic', settings.config)
        path.write_text(native + '\n' + table + 'topic = "ops"\n' + topics)
        settings = b.load_settings(path)
        self.assertEqual((settings.config['yardr_peer_mail'], settings.config['yardr_peer_mail_topic']),
                         (('yardmaster',), 'ops'))

        key = 'peermail:42:0'
        for api, request in ((self.bridge.api_send, dict(text='synthetic')),
                             (self.bridge.api_tell, dict(title='synthetic')),
                             (self.bridge.api_posted, {}),
                             (self.bridge.api_siding_card, dict(depot='synthetic')),
                             (self.bridge.api_ask_card, dict(bead='synthetic', ask=1)),
                             (self.bridge.api_run_ask, dict(bead='synthetic', ask=1))):
            with self.subTest(api=api.__name__), self.assertRaisesRegex(b.ApiError, 'reserved for the peer mail'):
                api(request, key_header=key)
        result = self.bridge.publish(dict(conversation=self.bridge.conversation, idempotency_key=key, text='no'))
        self.assertEqual(result['metadata']['reason'], 'idempotency_key_conflict')
        self.assertIsNone(self.store.saved_receipt(key))
        self.assertEqual(self.calls, [])

    def test_same_link_wire_id_conflict_and_projection_rollback(self):
        mirror = self.bridge.peer_mail
        with self.store.connect() as db:
            db.execute('INSERT INTO peer_mail_cursor VALUES (1, 0, 0, ?, ?)', ('', 'fixture'))
            db.execute("INSERT INTO peer_mail_crew VALUES ('yardmaster', 0, 1)")
            db.execute("INSERT INTO peer_mail_links VALUES ('test')")
        at = '2026-11-24T12:00:00+00:00'
        mail = dict(to_peer='test', to='other', text='first', id='same')
        with patch.object(mirror, 'authorize'):
            mirror.project(dict(seq=1, id='1', kind='mail', actor='yardmaster', at=at, data=mail), 0)
            with self.assertRaisesRegex(b.peer_mail.MirrorError, 'sequence gap'):
                mirror.project(dict(seq=3, id='3', kind='mail', actor='yardmaster', at=at, data=mail), 1)
            mirror.project(dict(seq=2, id='2', kind='mail', actor='yardmaster', at=at, data=dict(mail, text='second')), 1)
        confirmation = dict(peer='test', id='same', kind='mail')
        mirror.project(dict(seq=3, id='3', kind='peer_message_sent', actor='yard', at=at, data=confirmation), 2)
        with self.assertRaisesRegex(b.peer_mail.MirrorError, 'invalid native event ID'):
            mirror.project(dict(seq=4, kind='peer_removed', actor='yard', at=at, data=dict(peer='test')), 3)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT cursor FROM peer_mail_cursor').fetchone()[0], 3)
            self.assertEqual(db.execute('SELECT name FROM peer_mail_links').fetchall(), [('test',)])
            self.assertEqual(db.execute('SELECT ready FROM peer_mail_work').fetchall(), [])
        self.assertEqual(self.store.offset(), 10)

    def topic_transport(self, url, body, *args, **kwargs):
        self.calls.append((url, body))
        message = dict(message_id=77, chat=dict(id=123, type='private'),
                       **{'from': dict(id=999, is_bot=True)})
        if 'message_thread_id' in body:
            message.update(is_topic_message=True, message_thread_id=body['message_thread_id'])
        return 200, dict(ok=True, result=message)

    def topic_bridge(self):
        record_topics(self.store)
        return b.Bridge(dict(self.config, topics=TOPICS), self.store, 'SECRET', 'CAPABILITY',
                        request=self.topic_transport, request_multipart=self.multipart_transport,
                        request_download=self.download_transport)

    def seed_part(self, key, seq, ref):
        with self.store.connect() as db:
            db.execute('INSERT INTO peer_mail_parts (key, letter_seq, ref, text, proof, bot, chat) VALUES (?, ?, ?, ?, ?, 999, 123)',
                       (key, seq, ref, 'synthetic', b.peer_mail.proof('synthetic')))

    def test_mirror_publish_to_a_recorded_topic_carries_its_thread(self):
        bridge = self.topic_bridge()
        conversation = bridge.topic_conversation(OPS)
        key = 'peermail:9:0'
        self.seed_part(key, 9, '00' * 16)
        request = dict(conversation=conversation, idempotency_key=key, text='synthetic', text_options=True)
        with patch.object(bridge.peer_mail, 'before_send'):
            receipt = bridge.publish(request, mirror=True)
        self.assertTrue(receipt['delivered'])
        self.assertEqual(receipt['conversation'], conversation)
        self.assertEqual(self.calls[-1][1]['message_thread_id'], OPS)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT message_id FROM peer_mail_parts WHERE key=?', (key,)).fetchone(), (77,))
        calls = len(self.calls)
        with patch.object(bridge.peer_mail, 'before_send'):
            again = bridge.publish(request, mirror=True)
        self.assertTrue(again['delivered'])
        self.assertEqual(len(self.calls), calls)
        with patch.object(bridge.peer_mail, 'before_send'):
            refused = bridge.publish(dict(request, conversation=bridge.topic_conversation(999)), mirror=True)
        self.assertEqual(refused['metadata']['reason'], 'recipient_not_authorized')
        self.assertEqual(len(self.calls), calls)

    def test_mirror_confirmation_requires_the_expected_thread(self):
        mirror = self.bridge.peer_mail
        result = dict(message_id=77, chat=dict(id=123, type='private'),
                      **{'from': dict(id=999, is_bot=True)})
        threaded = dict(result, is_topic_message=True, message_thread_id=OPS)
        self.assertTrue(mirror.confirmed(200, dict(ok=True, result=result)))
        self.assertFalse(mirror.confirmed(200, dict(ok=True, result=threaded)))
        self.assertTrue(mirror.confirmed(200, dict(ok=True, result=threaded), OPS))
        self.assertFalse(mirror.confirmed(200, dict(ok=True, result=result), OPS))
        self.assertFalse(mirror.confirmed(200, dict(ok=True, result=dict(threaded, message_thread_id=WEB)), OPS))

        def general_receipt(url, body, *args, **kwargs):
            self.calls.append((url, body))
            return 200, dict(ok=True, result=result)

        bridge = b.Bridge(dict(self.config, topics=TOPICS), self.store, 'SECRET', 'CAPABILITY',
                          request=general_receipt)
        record_topics(self.store)
        key = 'peermail:9:1'
        self.seed_part(key, 9, '11' * 16)
        with patch.object(bridge.peer_mail, 'before_send'):
            receipt = bridge.publish(dict(conversation=bridge.topic_conversation(OPS), idempotency_key=key,
                                          text='synthetic', text_options=True), mirror=True)
        self.assertEqual(self.calls[-1][1]['message_thread_id'], OPS)
        self.assertIsNot(receipt.get('delivered'), True)
        self.assertEqual(receipt['metadata']['reason'], b.yard_mail.AMBIGUOUS)
        # The saved receipt is the one reconciliation reads: it names the topic, not the paired chat.
        self.assertEqual(self.store.saved_receipt(key)['conversation'], bridge.topic_conversation(OPS))

    def test_mirror_replies_in_a_topic_are_consumed(self):
        mirror = self.bridge.peer_mail
        ref = 'ab' * 16
        text = f'synthetic one\nMirror ref {ref}'
        with self.store.connect() as db:
            db.execute('INSERT INTO peer_mail_parts (key, letter_seq, ref, text, proof, bot, chat, attempted, message_id)'
                       " VALUES ('peermail:9:0', 9, '00', 'stale', '00', 999, 123, 1, 501)")
            db.execute('INSERT INTO peer_mail_parts (key, letter_seq, ref, text, proof, bot, chat, attempted)'
                       ' VALUES (?, 10, ?, ?, ?, 999, 123, 1)', ('peermail:10:0', ref, text, b.peer_mail.proof(text)))

        def target(mid, body):
            return dict(message_id=mid, chat=dict(id=123, type='private'), text=body,
                        is_topic_message=True, message_thread_id=OPS, **{'from': dict(id=999, is_bot=True)})

        self.assertTrue(mirror.is_reply(dict(text='/status', reply_to_message=target(501, 'stale part'))))
        self.assertTrue(mirror.is_reply(dict(text='/status', reply_to_message=target(502, text))))
        self.assertFalse(mirror.is_reply(dict(text='normal reply', reply_to_message=target(503, 'ordinary topic post'))))
        self.assertFalse(mirror.is_reply(dict(text='ordinary copy',
                                              reply_to_message=target(504, text.replace('one', 'two')))))

    def queue_letter(self, seq, text='Hi.'):
        letter = dict(seq=seq, crew='yardmaster', remote='dispatch', peer='company', incoming=True,
                      text=text, files=[])
        with self.store.connect() as db:
            db.execute('INSERT INTO peer_mail_work (ready, letter_seq, crew, letter) VALUES (?, ?, ?, ?)',
                       (seq, seq, 'yardmaster', b.run_asks.canonical(letter)))
        return letter

    def test_missing_topic_entry_pauses_mirror_but_ordinary_sends_work(self):
        root = Path(self.tmp.name) / 'missing-entry'
        root.mkdir()
        path = write_settings(root)
        original = path.read_text()
        native = original.replace('[yardr]\n', f'[yardr]\nhome = "{root}"\noperator = "pat"\ncards = false\n')
        path.write_text(native + '\n[yardr.peer_mail]\ncrew = ["yardmaster"]\ntopic = "ops"\n'
                        '\n[[topic]]\nname = "web"\n')
        settings = b.load_settings(path)
        self.assertEqual(settings.config['yardr_peer_mail_topic'], 'ops')
        web_only = (dict(name='web', agent='ops-agent'),)
        self.store.record_topic('web', WEB, 'recorded')
        bridge = b.Bridge(dict(self.config, topics=web_only), self.store, 'SECRET', 'CAPABILITY',
                          request=self.topic_transport, request_multipart=self.multipart_transport,
                          request_download=self.download_transport)
        bridge.config['yardr_peer_mail_topic'] = 'ops'
        bridge.peer_mail.crew = frozenset(('yardmaster',))
        self.queue_letter(9201)
        with self.assertRaisesRegex(b.peer_mail.MirrorError, 'not configured and recorded'):
            bridge.peer_mail.deliver_next()
        self.assertEqual(self.calls, [])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM peer_mail_work').fetchone(), (1,))
            self.assertEqual(db.execute('SELECT count(*) FROM peer_mail_threads').fetchone(), (0,))
        general = bridge.publish(dict(conversation=bridge.conversation, idempotency_key='ordinary:1', text='Ordinary'))
        self.assertTrue(general['delivered'])
        self.assertNotIn('message_thread_id', self.calls[-1][1])
        web = bridge.publish(dict(conversation=bridge.topic_conversation(WEB), idempotency_key='ordinary:2',
                                  text='Web post'))
        self.assertTrue(web['delivered'])
        self.assertEqual(self.calls[-1][1]['message_thread_id'], WEB)

    def test_mirror_claim_ignores_unshadowed_auto_topic(self):
        record_topics(self.store)
        self.store.add_auto_topic(OPS, 'yardmaster')
        bridge = b.Bridge(dict(self.config, topics=TOPICS, auto_topics=dict(enabled=True)), self.store,
                          'SECRET', 'CAPABILITY', request=self.topic_transport,
                          request_multipart=self.multipart_transport,
                          request_download=self.download_transport)
        with self.store.connect() as db:
            db.execute("DELETE FROM topics WHERE name='ops'")
            db.execute("INSERT INTO topics (name, thread_id, source, recorded_at) VALUES ('ops', 77, 'recorded', '')")
        self.assertIsNone(bridge.topic_by_thread(OPS))
        key = 'peermail:9:0'
        self.seed_part(key, 9, '00' * 16)
        with patch.object(bridge.peer_mail, 'before_send'):
            refused = bridge.publish(dict(conversation=bridge.topic_conversation(OPS), idempotency_key=key,
                                          text='synthetic', text_options=True), mirror=True)
        self.assertEqual(refused['metadata']['reason'], 'recipient_not_authorized')
        calls = len(self.calls)
        ordinary = bridge.publish(dict(conversation=bridge.topic_conversation(OPS), idempotency_key='ordinary:auto',
                                       text='auto post'))
        self.assertTrue(ordinary['delivered'])
        self.assertEqual(len(self.calls), calls + 1)
        self.assertEqual(self.calls[-1][1]['message_thread_id'], OPS)

    def test_upgrade_started_letter_keeps_general(self):
        record_topics(self.store)

        def incrementing_transport(url, body, *args, **kwargs):
            self.calls.append((url, body))
            message = dict(message_id=700 + len(self.calls), chat=dict(id=123, type='private'),
                           **{'from': dict(id=999, is_bot=True)})
            if 'message_thread_id' in body:
                message.update(is_topic_message=True, message_thread_id=body['message_thread_id'])
            return 200, dict(ok=True, result=message)

        bridge = b.Bridge(dict(self.config, topics=TOPICS), self.store, 'SECRET', 'CAPABILITY',
                          request=incrementing_transport, request_multipart=self.multipart_transport,
                          request_download=self.download_transport)
        bridge.config['yardr_peer_mail_topic'] = 'ops'
        bridge.peer_mail.crew = frozenset(('yardmaster',))
        letter = dict(seq=9202, crew='yardmaster', remote='dispatch', peer='company', incoming=True,
                      text='Started before the topic.', files=[])
        with self.store.connect() as db:
            db.execute('INSERT INTO peer_mail_work (ready, letter_seq, crew, letter, parts) VALUES (?, ?, ?, ?, ?)',
                       (9202, 9202, 'yardmaster', b.run_asks.canonical(letter),
                        b.run_asks.canonical(['one', 'two'])))
        with patch.object(bridge.peer_mail, 'before_send'):
            self.assertIs(bridge.peer_mail.deliver_next(), True)
        self.assertNotIn('message_thread_id', self.calls[-1][1])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT thread FROM peer_mail_threads WHERE letter_seq=9202').fetchone(),
                             (None,))
        with patch.object(bridge.peer_mail, 'before_send'):
            self.assertIs(bridge.peer_mail.deliver_next(), True)
        self.assertNotIn('message_thread_id', self.calls[-1][1])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM peer_mail_work').fetchone(), (0,))
            self.assertEqual(db.execute('SELECT count(*) FROM peer_mail_threads').fetchone(), (0,))
        self.queue_letter(9203, 'Fresh after upgrade.')
        with patch.object(bridge.peer_mail, 'before_send'):
            self.assertIs(bridge.peer_mail.deliver_next(), True)
        self.assertEqual(self.calls[-1][1]['message_thread_id'], OPS)

    def test_stale_thread_at_telegram_pauses_and_resumes(self):
        record_topics(self.store)
        plans = []

        def stale_transport(url, body, *args, **kwargs):
            self.calls.append((url, body))
            if plans:
                return plans.pop(0)(body)
            message = dict(message_id=500 + len(self.calls), chat=dict(id=123, type='private'),
                           **{'from': dict(id=999, is_bot=True)})
            if 'message_thread_id' in body:
                message.update(is_topic_message=True, message_thread_id=body['message_thread_id'])
            return 200, dict(ok=True, result=message)

        bridge = b.Bridge(dict(self.config, topics=TOPICS), self.store, 'SECRET', 'CAPABILITY',
                          request=stale_transport, request_multipart=self.multipart_transport,
                          request_download=self.download_transport)
        bridge.config['yardr_peer_mail_topic'] = 'ops'
        bridge.peer_mail.crew = frozenset(('yardmaster',))
        letter = dict(seq=9101, crew='yardmaster', remote='dispatch', peer='company', incoming=True,
                      text='Two parts.', files=[])
        queued = dict(seq=9102, crew='yardmaster', remote='dispatch', peer='company', incoming=True,
                      text='Queued behind.', files=[])
        with self.store.connect() as db:
            db.execute('INSERT INTO peer_mail_work (ready, letter_seq, crew, letter, parts) VALUES (?, ?, ?, ?, ?)',
                       (9101, 9101, 'yardmaster', b.run_asks.canonical(letter),
                        b.run_asks.canonical(['one', 'two'])))
            db.execute('INSERT INTO peer_mail_threads VALUES (9101, ?)', (OPS,))
            db.execute('INSERT INTO peer_mail_work (ready, letter_seq, crew, letter) VALUES (?, ?, ?, ?)',
                       (9102, 9102, 'yardmaster', b.run_asks.canonical(queued)))
        with patch.object(bridge.peer_mail, 'before_send'):
            self.assertIs(bridge.peer_mail.deliver_next(), True)
        self.assertEqual(self.calls[-1][1]['message_thread_id'], OPS)
        self.assertIn('1/2', self.calls[-1][1]['text'])
        plans.append(lambda _body: (400, dict(ok=False, error_code=400,
                                              description='Bad Request: message thread not found')))
        with patch.object(bridge.peer_mail, 'before_send'):
            with self.assertRaisesRegex(b.peer_mail.MirrorError, f'thread {OPS} is not served'):
                bridge.peer_mail.deliver_next()
        self.assertEqual(self.calls[-1][1]['message_thread_id'], OPS)
        self.assertIn('2/2', self.calls[-1][1]['text'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT part FROM peer_mail_work WHERE ready=9101').fetchone(), (1,))
            self.assertEqual(db.execute('SELECT thread FROM peer_mail_threads WHERE letter_seq=9101').fetchone(),
                             (OPS,))
            self.assertEqual(db.execute('SELECT count(*) FROM peer_mail_work').fetchone(), (2,))
        self.assertTrue(b.resendable(self.store.saved_receipt('peermail:9101:1')))
        with patch.object(bridge.peer_mail, 'before_send'):
            self.assertIs(bridge.peer_mail.deliver_next(), True)
        self.assertEqual(self.calls[-1][1]['message_thread_id'], OPS)
        self.assertIn('2/2', self.calls[-1][1]['text'])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT ready FROM peer_mail_work').fetchall(), [(9102,)])
            self.assertEqual(db.execute('SELECT count(*) FROM peer_mail_threads WHERE letter_seq=9101').fetchone(),
                             (0,))
        plans.append(lambda _body: (400, dict(ok=False, error_code=400,
                                              description='Bad Request: TOPIC_DELETED')))
        with patch.object(bridge.peer_mail, 'before_send'):
            with self.assertRaisesRegex(b.peer_mail.MirrorError, f'thread {OPS} is not served'):
                bridge.peer_mail.deliver_next()
        self.assertEqual(self.calls[-1][1]['message_thread_id'], OPS)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT part FROM peer_mail_work WHERE ready=9102').fetchone(), (0,))
            self.assertEqual(db.execute('SELECT thread FROM peer_mail_threads WHERE letter_seq=9102').fetchone(),
                             (OPS,))
        self.assertTrue(b.resendable(self.store.saved_receipt('peermail:9102:0')))
        with patch.object(bridge.peer_mail, 'before_send'):
            self.assertIs(bridge.peer_mail.deliver_next(), True)
        self.assertEqual(self.calls[-1][1]['message_thread_id'], OPS)
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM peer_mail_work').fetchone(), (0,))

    def test_worker_retry_backoff_is_bounded_and_resets_after_progress(self):
        mirror = self.bridge.peer_mail
        mirror.active.set()
        waits = []

        def wait(seconds):
            waits.append(seconds)
            if len(waits) == 9:
                mirror.stopped.set()

        with patch.object(mirror, 'deliver_next', side_effect=[0] * 8 + [True, 0]), \
                patch.object(mirror.stopped, 'wait', side_effect=wait):
            mirror.deliver()
        self.assertEqual(waits, [1, 2, 4, 8, 16, 32, 60, 60, 1])


class RealPeerMailTests(BridgeTestCase):
    def eventually(self, description, predicate, seconds=12):
        deadline = time.monotonic() + seconds
        while time.monotonic() < deadline:
            if value := predicate():
                return value
            time.sleep(.02)
        self.fail(f'{description}; private diagnostics: {self.logs[-12:]}')

    def rows(self, query):
        with self.store.connect() as db:
            return db.execute(query).fetchall()

    def cli(self, *args):
        res = subprocess.run(self.command + list(args), env=self.env, cwd=self.root, capture_output=True,
                             text=True, timeout=25)
        self.assertEqual(res.returncode, 0, res.stdout + res.stderr)
        return res.stdout

    def advance_store(self, phase):
        testyardr.stop(self.server)
        self.server = None
        args = [str(self.fixture), str(self.home), phase]
        res = subprocess.run(args, env=self.env, cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(res.returncode, 0, res.stderr)
        self.server = testyardr.restart(self.root, self.binary, self.home, self.command, self.env)

    def stop_native(self):
        if self.server is not None:
            testyardr.stop(self.server)
            self.server = None

    def accept(self, request):
        self.assertEqual(request['chat_id'], 123)
        self.assertNotIn('reply_markup', request)
        self.assertNotIn('message_thread_id', request)
        self.assertNotIn('reply_parameters', request)
        self.assertLessEqual(b.utf16_units(request['text']), 4096)
        message = dict(message_id=501 + len(self.sent), chat=dict(id=123, type='private'),
                       text=request['text'], **{'from': dict(id=999, is_bot=True)})
        self.sent.append(message)
        return 200, dict(ok=True, result=message)

    def fake_telegram(self, url, body, *args, **kwargs):
        if url.endswith('/sendMessage'):
            self.attempts.append(body)
            return (self.send_plan.pop(0) if self.send_plan else self.accept)(body)
        if url.endswith('/getMe'):
            return 200, dict(ok=True, result=dict(id=999, is_bot=True))
        if url.endswith('/getWebhookInfo'):
            return 200, dict(ok=True, result=dict(url=''))
        if url.endswith('/getUpdates'):
            if not self.updates:
                time.sleep(.04)
                return 200, dict(ok=True, result=[])
            return 200, dict(ok=True, result=self.updates.pop(0))
        if url == self.config['hook_url']:
            self.hooks.append(body)
            return 200, dict(seq=len(self.hooks))
        raise AssertionError('unexpected synthetic Telegram operation: ' + url.rsplit('/', 1)[-1])

    def new_bridge(self, **config):
        bridge = b.Bridge(dict(self.config, **config), b.Store(self.path, 10), 'SYNTHETIC', 'SYNTHETIC',
                          request=self.fake_telegram, request_download=self.download_transport, port=0)
        bridge._log = self.logs.append
        return bridge

    def native_head(self):
        return self.client.call('Head', [])

    def through_head(self):
        head = self.native_head()
        self.eventually(f'mirror cursor through {head}', lambda: self.rows('SELECT cursor FROM peer_mail_cursor') == [(head,)])
        self.eventually('mirror work drained', lambda: self.rows('SELECT count(*) FROM peer_mail_work') == [(0,)])
        self.assertEqual(self.native_head(), head, 'the mirror changed native state')

    def direct_reply(self, update_id, target, text='/status'):
        update = self.update(update_id)
        update['message'].update(text=text, reply_to_message=target)
        return update

    def test_real_native_stream_order_crash_recovery_and_inert_replies(self):
        self.binary = testyardr.binary()
        if self.binary is None:
            self.skipTest('yardr required for the isolated peer mail stream')
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='pm-', dir='/tmp')))
        self.fixture = testyardr.store_fixture(root, 'peer_mail')
        if self.fixture is None:
            self.skipTest('TGBRIDGE_TEST_YARDR_SOURCE required for real linked-yard fixture seeding')
        self.root = root
        self.logs, self.sent, self.attempts, self.send_plan, self.hooks, self.updates = ([] for _ in range(6))
        self.server, self.home, self.command, self.env = testyardr.start(root, self.binary)
        self.addCleanup(self.stop_native)
        self.cli('init', '--harness', 'claude')
        self.cli('settings', 'set', 'operator', 'pat')
        source = root / 'tgbridge.yaml'
        source.write_text(DECLARATION)
        self.cli('source', 'add', str(source))
        self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
        self.advance_store('before')
        self.client = b.run_asks.YardSocketClient(self.home, 'tgbridge')
        self.config.update(yardr_home=str(self.home), yardr_operator='pat', yardr_source='tgbridge',
                           yardr_cards=False, yardr_peer_mail=('yardmaster',))

        # A constructed bridge, a foreign pairing, and a named human do not seal a Head.
        wrong_user = self.new_bridge(user_id=124, chat_id=124)
        with self.assertRaisesRegex(b.run_asks.NativeError, 'operator_not_authorized'):
            wrong_user.peer_mail.prepare()
        with self.assertRaisesRegex(b.run_asks.NativeError, 'GetCrew'):
            self.new_bridge(yardr_peer_mail=('pat',)).peer_mail.prepare()
        shadow = root / 'shadow'
        shadow.mkdir(mode=0o700)
        (shadow / 'yard.sock').symlink_to(self.home / 'yard.sock')
        with self.assertRaisesRegex(b.run_asks.NativeError, 'owner-held Unix socket'):
            self.new_bridge(yardr_home=str(shadow)).peer_mail.prepare()
        self.assertEqual(self.rows('SELECT cursor FROM peer_mail_cursor'), [])

        daemon = self.new_bridge()
        worker = daemon.peer_mail
        original_stream = worker.client.stream_events
        original_project = worker.project
        original_before_send = worker.before_send
        cut_before_stream, cut_after_commit = threading.Event(), threading.Event()
        order = []

        def disconnect_after_activation(after, *args):
            if not cut_before_stream.is_set():
                self.assertEqual(self.rows('SELECT activation, cursor FROM peer_mail_cursor'), [(after, after)])
                cut_before_stream.set()
                raise b.run_asks.RPCUnavailable('synthetic disconnect after activation')
            return original_stream(after, *args)

        def disconnect_after_commit(event, after):
            seq = original_project(event, after)
            if event['kind'] == 'mail' and event.get('data', {}).get('from_peer') and not cut_after_commit.is_set():
                self.assertTrue(self.rows(f'SELECT ready FROM peer_mail_work WHERE ready={seq}'))
                cut_after_commit.set()
                raise b.run_asks.RPCUnavailable('synthetic disconnect after commit')
            return seq

        def send_after_cut():
            self.assertTrue(cut_after_commit.wait(5))
            original_before_send()

        worker.client.stream_events = disconnect_after_activation
        worker.project = disconnect_after_commit
        worker.before_send = send_after_cut
        first = self.native_head()
        thread = threading.Thread(target=daemon.run, daemon=True)
        thread.start()
        try:
            self.eventually('disconnect after durable activation', cut_before_stream.is_set)
            self.advance_store('after')
            self.eventually('disconnect after cursor/work transaction', cut_after_commit.is_set)
            self.through_head()
            headings = [m['text'].splitlines()[0] for m in self.sent]
            self.assertEqual(headings, ['Incoming · dispatch@north → yardmaster · 1/1',
                                        'Incoming · dispatch@south → yardmaster · 1/1',
                                        'Outgoing · yardmaster → dispatch@company · 1/2',
                                        'Outgoing · yardmaster → dispatch@company · 2/2',
                                        'Incoming · dispatch@south → yardmaster · 1/1'])
            long = 'Synthetic handoff notes\n\n' + 'Checklist: check <marker> & **keep** each line.\n' * 102 + '\nEnd of handoff.'
            exported = [m['text'].rsplit('\n', 1)[0].split('\n', 1)[1] for m in self.sent]
            self.assertEqual(exported[0], 'North: please review the synthetic handoff.')
            self.assertEqual(exported[1], 'South: synthetic review complete.')
            self.assertTrue(exported[2].startswith('files: handoff.txt (in the yard)\n'))
            self.assertEqual(exported[2].split('\n', 1)[1] + exported[3], long)
            self.assertEqual(exported[4], 'Replacement: same wire ID, new linked yard.')
            self.assertEqual(self.hooks, [])
            self.assertEqual(self.store.offset(), 10)
            events = self.client.call('EventsSince', [first, 200])
            accepted = [e for e in events if e['kind'] == 'mail' and e['data'].get('text') in (exported[0], exported[1], exported[4])]
            queued = next(e for e in events if e['kind'] == 'mail' and e['data'].get('text') == long)
            confirmed = next(e for e in events if e['kind'] == 'peer_message_sent' and e['data']['id'] == queued['data']['id'])
            self.assertTrue(queued['seq'] < accepted[0]['seq'] < accepted[1]['seq'] < confirmed['seq'] < accepted[2]['seq'])
            self.assertEqual([e['data']['id'] for e in accepted], [queued['data']['id']] * 3)
            order = [accepted[0]['seq'], accepted[1]['seq'], confirmed['seq'], confirmed['seq'], accepted[2]['seq']]
            if capture := os.environ.get('TGBRIDGE_TEST_PEER_MAIL_CAPTURE'):
                Path(capture).write_text(json.dumps(dict(messages=self.sent, native_order=order, inbound_offset=self.store.offset()),
                                                    ensure_ascii=False))

            # Losing the current source identity pauses projection and export; restoring it resumes the same cursor.
            before = self.native_head()
            self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '456')
            self.advance_store('late')
            self.eventually('revoked identity reported', lambda: any('operator_not_authorized' in line for line in self.logs))
            self.assertLess(self.rows('SELECT cursor FROM peer_mail_cursor')[0][0], self.native_head())
            self.assertEqual(len(self.sent), 5)
            self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
            self.through_head()
            self.assertGreater(self.native_head(), before)
            self.assertIn('After restoring the operator binding.', self.sent[-1]['text'])
            with self.assertRaisesRegex(b.peer_mail.MirrorError, 'ledger identity changed'):
                self.new_bridge(yard='different-yard').peer_mail.prepare()
        finally:
            daemon.stop.set()
            thread.join(15)
        self.assertFalse(thread.is_alive())

        # Drive crash points on the same durable work using the production attempt path.
        self.advance_store('failure')
        mirror = self.new_bridge().peer_mail
        after = mirror.prepare()
        for event in self.client.call('EventsSince', [after, 200]):
            after = mirror.project(event, after)
        mirror.active.set()
        with patch.object(mirror, 'before_send', side_effect=PowerLoss), self.assertRaises(PowerLoss):
            mirror.deliver_next()
        key, text = self.rows('SELECT key, text FROM peer_mail_parts WHERE text IS NOT NULL')[0]
        self.assertTrue(b.resendable(self.store.saved_receipt(key)))
        self.assertEqual(len(self.attempts), 6)

        bridge = self.new_bridge()
        mirror = bridge.peer_mail
        mirror.active.set()

        def unavailable(_request):
            raise b.RequestNotSent('synthetic pre-send outage', 7, 'connect')
        self.send_plan.append(unavailable)
        self.assertEqual(mirror.deliver_next(), 0)
        self.assertEqual(self.attempts[-1]['text'], text)
        self.assertTrue(b.resendable(self.store.saved_receipt(key)))
        self.assertEqual(self.rows('SELECT part FROM peer_mail_work'), [(0,)])

        blocked, release = threading.Event(), threading.Event()

        def rate_limited(body):
            blocked.set()
            self.assertTrue(release.wait(5))
            return 429, dict(ok=False, error_code=429, parameters=dict(retry_after=2))

        self.send_plan.append(rate_limited)
        delivery = threading.Thread(target=mirror.deliver, daemon=True)
        delivery.start()
        try:
            self.eventually('Telegram send in flight', blocked.is_set)
            ordinary = self.update(10)
            self.updates.append([ordinary])
            bridge.poll_once()
            self.assertEqual(self.store.offset(), 11)
            self.assertEqual(len(self.hooks), 1)
            self.assertEqual(self.rows('SELECT part FROM peer_mail_work'), [(0,)])
            # Ordinary publishers can send while the mirror has a Telegram request in flight.
            reply = bridge.publish(dict(conversation=bridge.conversation, idempotency_key='ordinary:1', text='Ordinary post'))
            self.assertTrue(reply['delivered'])
            ordinary_post = self.sent[-1]
            release.set()
            self.eventually('retry wait saved', lambda: self.store.saved_receipt(key)['metadata'].get('retry_at'))
            self.assertEqual(self.rows('SELECT part FROM peer_mail_work'), [(0,)])
        finally:
            release.set()
            mirror.stop()
            delivery.join(5)
        self.assertFalse(delivery.is_alive())
        self.assertEqual(self.attempts[-2]['text'], text)
        self.assertEqual(self.rows(f"SELECT count(*) FROM deliveries WHERE key='{key}'"), [(1,)])

        def accepted_then_power_lost(request):
            self.accept(request)
            raise PowerLoss()

        mirror = self.new_bridge().peer_mail
        mirror.active.set()
        with patch.object(b.peer_mail.time, 'time', return_value=time.time() + 3):
            self.send_plan.append(accepted_then_power_lost)
            with self.assertRaises(PowerLoss):
                mirror.deliver_next()
        self.assertEqual(self.attempts[-1]['text'], text)
        self.assertEqual(self.store.saved_receipt(key)['metadata']['reason'], b.yard_mail.AMBIGUOUS)
        ambiguous = self.sent[-1]
        resumed = self.new_bridge()
        mirror = resumed.peer_mail
        mirror.active.set()
        self.assertIs(mirror.deliver_next(), True)  # ambiguous part remains a private diagnostic
        self.assertEqual(self.sent[-1], ambiguous)

        def died_after_receipt(key, receipt, **options):
            finish(key, receipt, **options)
            if receipt.get('delivered') is True:
                raise PowerLoss()

        finish = resumed.store.finish
        with patch.object(resumed.store, 'finish', side_effect=died_after_receipt), self.assertRaises(PowerLoss):
            mirror.deliver_next()
        confirmed_part = self.sent[-1]
        self.assertIn('an earlier part is unconfirmed', confirmed_part['text'])
        self.assertEqual(self.rows('SELECT part FROM peer_mail_work'), [(1,)])
        next_bridge = self.new_bridge()
        mirror = next_bridge.peer_mail
        mirror.active.set()
        self.assertIs(mirror.deliver_next(), True)
        self.assertEqual(self.sent[-1], confirmed_part)
        while mirror.deliver_next() is True:
            pass
        self.assertEqual(self.rows('SELECT count(*) FROM peer_mail_work'), [(0,)])

        # After restart and disable, receipt-backed and receiptless replies are both consumed before every inbound handler.
        disabled = self.new_bridge(yardr_peer_mail=())
        photo = self.photo_message(12)
        photo['message']['reply_to_message'] = confirmed_part
        altered = dict(ambiguous, text=ambiguous['text'].replace('Boundary', 'Different'))
        self.updates.append([self.direct_reply(10, ambiguous), self.direct_reply(11, ambiguous), photo,
                             self.direct_reply(13, confirmed_part), self.direct_reply(14, ordinary_post, 'normal reply'),
                             self.direct_reply(15, altered, 'ordinary copy')])
        with patch.object(disabled, '_match_command', wraps=disabled._match_command) as commands, \
                patch.object(disabled.siding_cards, 'reply', wraps=disabled.siding_cards.reply) as cards:
            disabled.poll_once()
        self.assertEqual([c.args[0] for c in commands.call_args_list], ['normal reply', 'ordinary copy'])
        self.assertEqual(cards.call_count, 2)
        self.assertEqual(self.download_calls, [])
        self.assertEqual(len(self.hooks), 3)
        self.assertEqual(self.store.offset(), 16)

        self.advance_store('reuse-crew')
        final = self.new_bridge()
        final.peer_mail.start()
        try:
            self.through_head()
        finally:
            final.peer_mail.stop()
        self.assertIn('Accepted by crew before its name was reused.', self.sent[-1]['text'])
        self.assertEqual(sum('Replacement human' in m['text'] for m in self.sent), 0)
        self.assertEqual(self.store.offset(), 16)
        self.assertEqual(len(self.hooks), 3)
        self.assertEqual(self.rows('SELECT count(*) FROM peer_mail_parts WHERE attempted=1 AND message_id IS NULL'), [(1,)])

        expanded = self.new_bridge(yardr_peer_mail=('yardmaster', 'spare')).peer_mail
        after = expanded.prepare()
        self.advance_store('conflict')
        for event in self.client.call('EventsSince', [after, 200]):
            after = expanded.project(event, after)
        [letter_seq] = self.rows('SELECT letter_seq FROM peer_mail_work')[0]
        conflicted_key = f'peermail:{letter_seq}:0'
        self.store.reserve(conflicted_key, 'synthetic-foreign-fingerprint', self.bridge.not_sent())
        expanded.active.set()
        attempts = len(self.attempts)
        with self.assertRaisesRegex(b.peer_mail.MirrorError, 'key or destination conflict'):
            expanded.deliver_next()
        self.assertEqual(len(self.attempts), attempts)
        self.assertEqual(self.rows('SELECT part FROM peer_mail_work'), [(0,)])
        with self.store.connect() as db:
            db.execute('DELETE FROM deliveries WHERE key=?', (conflicted_key,))
        self.send_plan.append(lambda _body: (403, dict(ok=False, error_code=403)))
        self.assertIs(expanded.deliver_next(), True)
        self.assertIsNone(expanded.deliver_next())
        self.assertEqual(self.store.saved_receipt(conflicted_key)['metadata']['reason'], 'telegram_rejected_not_sent_403')
        self.assertTrue(any(conflicted_key in line and '403' in line for line in self.logs))
        self.assertEqual(len(self.attempts), attempts + 1)
        print(f'Peer mail native order {order}; per-part messages {[item["message_id"] for item in self.sent[:5]]}; '
              f'inbound offset {self.store.offset()} after ordinary traffic and inert mirror replies')

    def accept_topic(self, request):
        self.assertEqual(request['chat_id'], 123)
        self.assertNotIn('reply_markup', request)
        self.assertNotIn('reply_parameters', request)
        self.assertLessEqual(b.utf16_units(request['text']), 4096)
        message = dict(message_id=501 + len(self.sent), chat=dict(id=123, type='private'),
                       text=request['text'], **{'from': dict(id=999, is_bot=True)})
        if 'message_thread_id' in request:
            message.update(is_topic_message=True, message_thread_id=request['message_thread_id'])
        self.sent.append(message)
        return 200, dict(ok=True, result=message)

    def threads_of(self, attempts):
        return [body.get('message_thread_id') for body in attempts]

    def test_real_native_topic_routing_claims_replies_and_pause(self):
        self.binary = testyardr.binary()
        if self.binary is None:
            self.skipTest('yardr required for the isolated peer mail stream')
        root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='pm-topic-', dir='/tmp')))
        self.fixture = testyardr.store_fixture(root, 'peer_mail')
        if self.fixture is None:
            self.skipTest('TGBRIDGE_TEST_YARDR_SOURCE required for real linked-yard fixture seeding')
        self.root = root
        self.logs, self.sent, self.attempts, self.send_plan, self.hooks, self.updates = ([] for _ in range(6))
        self.accept = self.accept_topic
        self.server, self.home, self.command, self.env = testyardr.start(root, self.binary)
        self.addCleanup(self.stop_native)
        self.cli('init', '--harness', 'claude')
        self.cli('settings', 'set', 'operator', 'pat')
        source = root / 'tgbridge.yaml'
        source.write_text(DECLARATION)
        self.cli('source', 'add', str(source))
        self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
        self.advance_store('before')
        self.client = b.run_asks.YardSocketClient(self.home, 'tgbridge')
        self.config.update(yardr_home=str(self.home), yardr_operator='pat', yardr_source='tgbridge',
                           yardr_cards=False, yardr_peer_mail=('yardmaster',), yardr_peer_mail_topic='ops',
                           topics=TOPICS)
        record_topics(self.store)

        daemon = self.new_bridge()
        thread = threading.Thread(target=daemon.run, daemon=True)
        thread.start()
        try:
            self.eventually('mirror activated', lambda: self.rows('SELECT activation FROM peer_mail_cursor'))
            self.advance_store('after')
            self.through_head()
            headings = [m['text'].splitlines()[0] for m in self.sent]
            self.assertEqual(headings, ['Incoming · dispatch@north → yardmaster · 1/1',
                                        'Incoming · dispatch@south → yardmaster · 1/1',
                                        'Outgoing · yardmaster → dispatch@company · 1/2',
                                        'Outgoing · yardmaster → dispatch@company · 2/2',
                                        'Incoming · dispatch@south → yardmaster · 1/1'])
            self.assertEqual(self.threads_of(self.attempts), [OPS] * 5)
            self.assertTrue(all(m.get('message_thread_id') == OPS and m.get('is_topic_message') is True
                                for m in self.sent))
            self.assertEqual(self.rows('SELECT count(*) FROM peer_mail_threads'), [(0,)])
            events = self.client.call('EventsSince', [0, 200])
            order = [e['seq'] for e in events if e['kind'] == 'mail' and e['data'].get('text') in
                     ('North: please review the synthetic handoff.', 'South: synthetic review complete.',
                      'Replacement: same wire ID, new linked yard.')]
            if capture := os.environ.get('TGBRIDGE_TEST_PEER_MAIL_TOPIC_CAPTURE'):
                Path(capture).write_text(json.dumps(dict(messages=self.sent, threads=self.threads_of(self.attempts),
                                                         native_order=order, inbound_offset=self.store.offset()),
                                                    ensure_ascii=False))
        finally:
            daemon.stop.set()
            thread.join(15)
        self.assertFalse(thread.is_alive())

        # A settings change between part receipts never moves the unfinished
        # letter; a fresh bridge on the same ledger is the restart.
        self.advance_store('failure')
        mirror = self.new_bridge().peer_mail
        after = mirror.prepare()
        for event in self.client.call('EventsSince', [after, 200]):
            after = mirror.project(event, after)
        mirror.active.set()
        self.assertIs(mirror.deliver_next(), True)
        self.assertEqual(self.rows('SELECT thread FROM peer_mail_threads'), [(OPS,)])
        self.assertEqual(self.threads_of(self.attempts[-1:]), [OPS])
        moved = self.new_bridge(yardr_peer_mail_topic='web').peer_mail
        moved.active.set()
        drained = 1
        while moved.deliver_next() is True:
            drained += 1
        self.assertGreater(drained, 1)
        self.assertEqual(self.threads_of(self.attempts[-drained:]), [OPS] * drained)
        self.assertEqual(self.rows('SELECT count(*) FROM peer_mail_work'), [(0,)])
        self.assertEqual(self.rows('SELECT count(*) FROM peer_mail_threads'), [(0,)])

        self.advance_store('late')
        after = moved.prepare()
        for event in self.client.call('EventsSince', [after, 200]):
            after = moved.project(event, after)
        self.assertIs(moved.deliver_next(), True)
        self.assertIsNone(moved.deliver_next())
        self.assertEqual(self.threads_of(self.attempts[-1:]), [WEB])
        self.assertIn('After restoring the operator binding.', self.sent[-1]['text'])

        # An unrecorded topic pauses delivery with a private status and sends
        # nothing, not even to General; recording it resumes the same work.
        expanded = self.new_bridge(yardr_peer_mail=('yardmaster', 'spare'),
                                   yardr_peer_mail_topic='lab').peer_mail
        after = expanded.prepare()
        self.advance_store('conflict')
        for event in self.client.call('EventsSince', [after, 200]):
            after = expanded.project(event, after)
        self.assertEqual(self.rows('SELECT crew FROM peer_mail_work'), [('spare',)])
        expanded.active.set()
        attempts = len(self.attempts)
        with self.assertRaisesRegex(b.peer_mail.MirrorError, 'not configured and recorded'):
            expanded.deliver_next()
        self.assertEqual(len(self.attempts), attempts)
        self.assertEqual(self.rows('SELECT crew FROM peer_mail_work'), [('spare',)])
        self.store.record_topic('lab', 21, 'recorded')
        self.assertIs(expanded.deliver_next(), True)
        self.assertEqual(self.threads_of(self.attempts[-1:]), [21])

        # A replaced thread pauses the claimed letter instead of switching it;
        # restoring the thread resumes the same conversation.
        letter = dict(seq=9001, crew='yardmaster', remote='dispatch', peer='company', incoming=True,
                      text='Replacement drill.\n' + 'x' * 6000, files=[])
        with self.store.connect() as db:
            db.execute('INSERT INTO peer_mail_work (ready, letter_seq, crew, letter) VALUES (9001, 9001, ?, ?)',
                       ('yardmaster', b.run_asks.canonical(letter)))
            db.execute('INSERT INTO peer_mail_threads VALUES (9001, ?)', (OPS,))
            db.execute("DELETE FROM topics WHERE name='ops'")
            db.execute("INSERT INTO topics (name, thread_id, source, recorded_at) VALUES ('ops', 77, 'recorded', '')")
        replaced = self.new_bridge().peer_mail
        replaced.active.set()
        attempts = len(self.attempts)
        with self.assertRaisesRegex(b.peer_mail.MirrorError, f'thread {OPS} is not served'):
            replaced.deliver_next()
        self.assertEqual(len(self.attempts), attempts)
        with self.store.connect() as db:
            db.execute("DELETE FROM topics WHERE name='ops'")
            db.execute("INSERT INTO topics (name, thread_id, source, recorded_at) VALUES ('ops', ?, 'recorded', '')",
                       (OPS,))
        drained = 0
        while replaced.deliver_next() is True:
            drained += 1
        self.assertEqual(drained, 3)
        self.assertEqual(self.threads_of(self.attempts[-3:]), [OPS, OPS, OPS])
        confirmed = len(self.attempts)
        self.assertIsNone(replaced.deliver_next())
        self.assertEqual(len(self.attempts), confirmed)

        # Ordinary posts still send on General and on an ordinary topic.
        bridge = self.new_bridge()
        general = bridge.publish(dict(conversation=bridge.conversation, idempotency_key='ordinary:1', text='Ordinary post'))
        self.assertTrue(general['delivered'])
        self.assertNotIn('message_thread_id', self.attempts[-1])
        ordinary_post = self.sent[-1]
        web = bridge.publish(dict(conversation=bridge.topic_conversation(WEB), idempotency_key='ordinary:2',
                                  text='Ordinary topic post'))
        self.assertTrue(web['delivered'])
        self.assertEqual(self.attempts[-1]['message_thread_id'], WEB)
        web_post = self.sent[-1]

        # After disable, replies to every mirror part in every thread are
        # consumed before every inbound handler; ordinary traffic still relays.
        parts = [m for m in self.sent if 'Mirror ref' in m['text']]
        self.assertEqual({m.get('message_thread_id') for m in parts}, {OPS, WEB, 21})
        disabled = self.new_bridge(yardr_peer_mail=())
        uid = 10
        batch = []
        for part in parts:
            batch.append(self.direct_reply(uid, part))
            uid += 1
        photo = self.photo_message(uid)
        photo['message']['reply_to_message'] = parts[0]
        batch.append(photo)
        uid += 1
        batch.append(self.direct_reply(uid, ordinary_post, 'normal reply'))
        uid += 1
        batch.append(self.direct_reply(uid, web_post, 'topic reply'))
        uid += 1
        batch.append(self.update(uid))
        uid += 1
        batch.append(in_topic(self.update(uid), WEB))
        self.updates.append(batch)
        with patch.object(disabled, '_match_command', wraps=disabled._match_command) as commands, \
                patch.object(disabled.siding_cards, 'reply', wraps=disabled.siding_cards.reply) as cards:
            disabled.poll_once()
        self.assertEqual([c.args[0] for c in commands.call_args_list],
                         ['normal reply', 'topic reply', 'hello', 'hello'])
        self.assertEqual(cards.call_count, 4)
        self.assertEqual(self.download_calls, [])
        self.assertEqual(len(self.hooks), 4)
        self.assertEqual([hook['conversation'] for hook in self.hooks],
                         ['123', '123', '123', '123:topic:9'])
        self.assertEqual(self.store.offset(), 10 + len(batch))
        print(f'Peer mail topic threads {sorted({m.get("message_thread_id") for m in parts})}; '
              f'{len(parts)} parts ignored in replies; inbound offset {self.store.offset()}')


if __name__ == '__main__':
    unittest.main()
