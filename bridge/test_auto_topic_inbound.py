"""Inbound in topics the paired user creates ([auto_topics], OPERATIONS.md,
"Topics the user creates"): a new thread is recorded, named from Telegram's
service data and relayed to the yard on its own conversation under its topic
line, its body naming the topic, so the yard routes it by subscription. A
delivery the yard does not confirm keeps the cursor and never falls back to
the paired chat. General, the settings topics and a file without
[auto_topics] relay exactly as before. Fake Telegram, yard hook and
transcriber; temporary directories; no network."""
import io
import json
from pathlib import Path
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
import testports  # noqa: E402
import test_topic_inbound  # noqa: E402
from test_commands import CommandCase  # noqa: E402
from test_topic_inbound import OPS, WEB, TopicInboundCase, in_topic  # noqa: E402
from test_topics import GET_ME, MODE_OFF, USERS_ALLOWED  # noqa: E402

AUTO_ON = {'enabled': True}
AUTO_OFF = {'enabled': False}
NEW, OTHER = 601, 602  # threads the paired user created; no row of either kind yet
HOOK_URL = 'http://127.0.0.1:8797/hooks/tgbridge'
# How the fake yard answers a delivery it does not confirm: each keeps the cursor.
REFUSALS = ('error', 'forbidden', 'dropped', 'no_seq', 'zero_seq', 'text_seq', 'not_object', 'unconfirmed',
            'not_sent')


def service(uid, thread_id, key, **fields):
    """A forum topic service message (no text) in `thread_id`."""
    update = in_topic(dict(update_id=uid, message=dict(message_id=uid, chat=dict(id=123, type='private'),
                                                       **{'from': dict(id=123, is_bot=False)})), thread_id)
    update['message'][key] = fields
    return update


def topic_id(thread_id):
    return f'123:topic:{thread_id}'


class AutoTopicInboundCase(TopicInboundCase):
    """The topic inbound fixture with [auto_topics] on and a fake yard hook
    of its own: every delivery is recorded with its headers and accepted
    with a seq, unless `refusals` names how the yard answers one on its
    conversation (REFUSALS)."""

    def setUp(self):
        super().setUp()
        self.config['auto_topics'] = AUTO_ON
        self.bridge = self.make_bridge(self.config)
        self.refusals = {}  # conversation id -> how the yard answers a delivery on it
        self.hook_headers = []

    def transport(self, url, body, *args, **kwargs):
        if url != HOOK_URL:
            return super().transport(url, body, *args, **kwargs)
        self.calls.append((url, body))
        self.hook_headers.append(kwargs.get('headers'))
        how = self.refusals.get(body['conversation'])
        if how == 'not_sent':
            raise b.RequestNotSent('network request not sent', 7, 'connect')
        if how == 'unconfirmed':
            raise b.RequestUnconfirmed('network request failed; outcome unconfirmed', 52, 'after-send')
        answers = dict(error=(500, dict(error='internal')), forbidden=(403, dict(error='bad secret')),
                       dropped=(200, dict(dropped=True)), no_seq=(200, {}), zero_seq=(200, dict(seq=0)),
                       text_seq=(200, dict(seq='1')), not_object=(200, ['recorded']))
        if how in answers:
            return answers[how]
        return 200, dict(seq=len(self.deliveries()))

    def deliveries(self):
        """The body of every delivery to the yard's hook, in order."""
        return [body for url, body in self.calls if url == HOOK_URL]

    def relays(self):
        """(conversation id, message id, reply id, text) of every delivery, in order."""
        return [(body['conversation'], body['message_id'], body['reply_to'], body['text'])
                for body in self.deliveries()]

    def row(self, thread_id):
        return next((row for row in self.store.auto_topic_rows() if row['thread_id'] == thread_id), None)


class AutoTopicRelayTests(AutoTopicInboundCase):
    def test_a_message_in_a_new_thread_is_recorded_then_relayed_on_its_own_conversation(self):
        log = self.drain(in_topic(self.update(10), NEW))
        self.assertEqual(self.relays(), [(topic_id(NEW), '10', None, f'[topic: thread {NEW}]\nhello')])
        [body] = self.deliveries()
        self.assertEqual({k: body[k] for k in ('id', 'kind', 'chat', 'topic', 'thread_id', 'forwarded')},
                         dict(id=f'999:{topic_id(NEW)}:10', kind='message', chat='123', topic=f'thread {NEW}',
                              thread_id=NEW, forwarded=False))
        self.assertEqual(self.hook_headers, [{'X-Tgbridge-Secret': 'HOOKSECRET'}])
        self.assertEqual(self.store.offset(), 11)
        row = self.row(NEW)
        self.assertEqual((row['name'], row['agent']), (None, 'yardmaster'))
        self.assertEqual([topic['thread_id'] for topic in self.bridge.served_auto_topics()], [NEW])
        self.assertEqual(log, '')
        self.assertEqual(self.store.last_accepted_in(topic_id(NEW)), 10)

    def test_a_second_message_relays_on_the_same_conversation_with_its_reply(self):
        self.drain(in_topic(self.update(10), NEW), in_topic(self.update(11), NEW, reply_to_message=dict(message_id=10)))
        self.assertEqual(self.relays(), [(topic_id(NEW), '10', None, f'[topic: thread {NEW}]\nhello'),
                                         (topic_id(NEW), '11', '10', f'[topic: thread {NEW}]\nhello')])
        self.assertEqual(len(self.store.auto_topic_rows()), 1)
        self.assertEqual(self.store.offset(), 12)

    def test_a_reply_to_the_topic_root_is_no_reply(self):
        self.drain(in_topic(self.update(10), NEW, reply_to_message=dict(message_id=NEW)))
        self.assertEqual(self.relays(), [(topic_id(NEW), '10', None, f'[topic: thread {NEW}]\nhello')])

    def test_the_creation_message_records_and_names_and_relays_nothing(self):
        log = self.drain(service(10, NEW, 'forum_topic_created', name='Weekly plan', icon_color=7322096))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.offset(), 11)
        self.assertEqual(log, '')
        row = self.row(NEW)
        self.assertEqual((row['name'], row['name_implicit']), ('Weekly plan', False))
        self.drain(in_topic(self.update(11), NEW))
        self.assertEqual(self.relays(), [(topic_id(NEW), '11', None, '[topic: Weekly plan]\nhello')])
        self.assertEqual(self.deliveries()[0]['topic'], 'Weekly plan')

    def test_an_implicit_name_is_recorded_as_implicit(self):
        self.drain(service(10, NEW, 'forum_topic_created', name='hello', is_name_implicit=True),
                   service(11, OTHER, 'forum_topic_created', name='Plan', is_name_implicit='yes'))
        self.assertEqual([(r['name'], r['name_implicit']) for r in self.store.auto_topic_rows()],
                         [('hello', True), ('Plan', False)])

    def test_a_creation_message_without_a_name_records_the_thread_unnamed(self):
        self.drain(service(10, NEW, 'forum_topic_created', icon_color=7322096))
        self.assertIsNone(self.row(NEW)['name'])
        self.drain(in_topic(self.update(11), NEW))
        self.assertEqual(self.relays()[0][3], f'[topic: thread {NEW}]\nhello')

    def test_a_reply_carrying_the_creation_names_a_row_without_a_name(self):
        root = dict(message_id=NEW, forum_topic_created=dict(name='Plan', icon_color=7322096))
        self.drain(in_topic(self.update(10), NEW, reply_to_message=root))
        self.assertEqual(self.relays(), [(topic_id(NEW), '10', None, '[topic: Plan]\nhello')])
        # A row that already has a name keeps it.
        self.drain(service(11, NEW, 'forum_topic_edited', name='Roadmap'))
        self.drain(in_topic(self.update(12), NEW, reply_to_message=root))
        self.assertEqual(self.relays()[1][3], '[topic: Roadmap]\nhello')
        self.assertEqual(self.row(NEW)['name'], 'Roadmap')

    def test_a_name_with_delimiters_relays_sanitized_and_a_settings_name_is_suffixed(self):
        self.drain(service(10, NEW, 'forum_topic_created', name='a [b|c] d'),
                   service(11, OTHER, 'forum_topic_created', name='OPS'),
                   in_topic(self.update(12), NEW), in_topic(self.update(13), OTHER))
        self.assertEqual([text for *_, text in self.relays()],
                         ['[topic: a b c d]\nhello', f'[topic: OPS #{OTHER}]\nhello'])
        self.assertEqual([body['topic'] for body in self.deliveries()], ['a b c d', f'OPS #{OTHER}'])

    def test_an_edit_renames_and_an_icon_only_edit_does_not(self):
        self.drain(service(10, NEW, 'forum_topic_created', name='New topic', is_name_implicit=True),
                   service(11, NEW, 'forum_topic_edited', name='Roadmap'),
                   in_topic(self.update(12), NEW),
                   service(13, NEW, 'forum_topic_edited', icon_custom_emoji_id='5312536423851630001'),
                   in_topic(self.update(14), NEW))
        self.assertEqual([text for *_, text in self.relays()], ['[topic: Roadmap]\nhello'] * 2)
        self.assertEqual([body['topic'] for body in self.deliveries()], ['Roadmap'] * 2)
        self.assertEqual((self.row(NEW)['name'], self.row(NEW)['name_implicit']), ('Roadmap', False))
        self.assertEqual(self.store.offset(), 15)

    def test_an_edit_in_a_settings_topic_a_leftover_row_or_an_unknown_thread_changes_nothing(self):
        self.drain(service(10, OPS, 'forum_topic_edited', name='Renamed'),
                   service(11, 13, 'forum_topic_edited', name='Renamed'),
                   service(12, NEW, 'forum_topic_edited', name='Renamed'),
                   service(13, OPS, 'forum_topic_created', name='ops'),
                   service(14, 13, 'forum_topic_created', name='gone'))
        self.assertEqual(self.calls, [])
        self.assertEqual(self.store.auto_topic_rows(), [])
        self.assertEqual(self.store.offset(), 15)

    def test_a_first_update_that_relays_nothing_records_its_topic(self):
        sticker = in_topic(self.update(11), OTHER)
        del sticker['message']['text']
        sticker['message']['sticker'] = dict(file_id='sticker-1')
        start = in_topic(self.update(10), NEW)
        start['message']['text'] = '/start'
        log = self.drain(start, sticker,
                         service(12, NEW + 2, 'forum_topic_closed'))
        self.assertEqual(self.calls, [])
        self.assertEqual([(row['thread_id'], row['name']) for row in self.store.auto_topic_rows()],
                         [(NEW, None), (OTHER, None), (NEW + 2, None)])
        self.assertEqual([topic['thread_id'] for topic in self.bridge.served_auto_topics()], [NEW, OTHER, NEW + 2])
        self.assertEqual((log, self.store.offset()), ('', 13))
        self.drain(in_topic(self.update(13), NEW))
        self.assertEqual(self.relays(), [(topic_id(NEW), '13', None, f'[topic: thread {NEW}]\nhello')])

    def test_a_service_message_from_anyone_else_changes_nothing(self):
        update = service(10, NEW, 'forum_topic_created', name='Plan')
        update['message']['from'] = dict(id=124, is_bot=False)
        self.drain(update)
        self.assertEqual((self.calls, self.store.auto_topic_rows()), ([], []))


class AutoTopicRefusalTests(AutoTopicInboundCase):
    """The yard does not confirm a delivery on an auto topic: the topic stays
    recorded and served, the cursor stays, and nothing reaches the paired chat."""

    def test_every_refusal_retains_the_cursor_and_never_reaches_the_paired_chat(self):
        for index, how in enumerate(REFUSALS):
            thread, uid = NEW + index, 100 + 2 * index
            with self.subTest(how):
                del self.calls[:]
                self.refusals[topic_id(thread)] = how
                with self.assertRaises(b.TransportError):
                    self.drain(service(uid, thread, 'forum_topic_created', name=f'Plan {index}'),
                               in_topic(self.update(uid + 1), thread))
                self.assertEqual(self.relays(),
                                 [(topic_id(thread), str(uid + 1), None, f'[topic: Plan {index}]\nhello')])
                self.assertEqual(self.store.offset(), uid + 1)
                self.assertIsNone(self.store.last_accepted_in(topic_id(thread)))
                self.assertIn(thread, [topic['thread_id'] for topic in self.bridge.served_auto_topics()])
                # Once the yard confirms it, the same message relays again under the same id.
                del self.refusals[topic_id(thread)], self.calls[:]
                self.drain(in_topic(self.update(uid + 1), thread))
                self.assertEqual([body['id'] for body in self.deliveries()], [f'999:{topic_id(thread)}:{uid + 1}'])
                self.assertEqual(self.store.offset(), uid + 2)
        self.assertIsNone(self.store.last_accepted_in('123'))

    def test_a_duplicate_the_yard_already_recorded_is_accepted(self):
        def duplicate(url, body, *args, **kwargs):
            if url != HOOK_URL:
                return self.transport(url, body, *args, **kwargs)
            self.calls.append((url, body))
            return 200, dict(seq=7, duplicate=True)
        self.bridge.request = duplicate
        self.drain(in_topic(self.update(10), NEW))
        self.assertEqual(self.relays(), [(topic_id(NEW), '10', None, f'[topic: thread {NEW}]\nhello')])
        self.assertEqual((self.store.offset(), self.store.last_accepted_in(topic_id(NEW))), (11, 10))

    def test_a_row_keeps_the_crew_member_it_was_recorded_for(self):
        # The settings' crew when the bridge first saw the thread; a row
        # recorded under another keeps it, whatever the settings say now.
        self.store.add_auto_topic(OTHER, 'helper')
        self.drain(in_topic(self.update(10), OTHER))
        self.assertEqual(self.relays(), [(topic_id(OTHER), '10', None, f'[topic: thread {OTHER}]\nhello')])
        self.assertEqual(self.row(OTHER)['agent'], 'helper')
        self.assertEqual([(topic['thread_id'], topic['agent']) for topic in self.bridge.served_auto_topics()],
                         [(OTHER, 'helper')])


class AutoTopicIdentityTests(AutoTopicInboundCase):
    """Where [auto_topics] changes nothing."""

    UPDATES = (
        ('general', lambda case: case.update(10)),
        ('settings topic', lambda case: in_topic(case.update(11), OPS)),
        ('settings topic with its own crew', lambda case: in_topic(case.update(12), WEB)),
        ('settings topic, reply', lambda case: in_topic(case.update(13), OPS, reply_to_message=dict(message_id=3))),
        ('settings topic, root reply carrying the creation', lambda case: in_topic(
            case.update(14), OPS, reply_to_message=dict(message_id=OPS, forum_topic_created=dict(name='x')))),
        ('settings topic, creation message', lambda case: service(15, OPS, 'forum_topic_created', name='ops')),
        ('leftover topics row', lambda case: in_topic(case.update(16), 13)),
        ('leftover topics row, creation message', lambda case: service(17, 13, 'forum_topic_created', name='gone')),
    )

    def run_with(self, auto, store_name):
        """Requests (without received_at), log and offset of UPDATES on a
        bridge with this [auto_topics] (None: no table) and its own ledger."""
        config = {k: v for k, v in self.config.items() if k != 'auto_topics'}
        if auto is not None:
            config['auto_topics'] = auto
        store = b.Store(Path(self.tmp.name) / store_name, 10)
        test_topic_inbound.record_topics(store)
        self.bridge = self.make_bridge(config, store)
        del self.calls[:]
        log = self.drain(*(make(self) for _, make in self.UPDATES))
        requests = [(url, {k: v for k, v in body.items() if k != 'received_at'}) for url, body in self.calls]
        return requests, log, store.offset(), store.auto_topic_rows()

    def test_general_settings_topics_and_leftover_rows_relay_identically_with_auto_on(self):
        requests, log, offset, rows = self.run_with(None, 'none.sqlite3')
        self.assertEqual([body['conversation'] for _, body in requests],
                         ['123', topic_id(OPS), topic_id(WEB), topic_id(OPS), topic_id(OPS), '123'])
        self.assertEqual(offset, 18)
        self.assertEqual(log, 'topic thread 13 not configured (msg 16); relayed to the main conversation\n')
        self.assertEqual(rows, [])
        for auto in (AUTO_OFF, AUTO_ON):
            with self.subTest(auto=auto):
                self.assertEqual(self.run_with(auto, f'{auto["enabled"]}.sqlite3'), (requests, log, offset, rows))

    def test_auto_off_leaves_rows_unserved_and_relays_their_threads_not_configured(self):
        self.store.add_auto_topic(NEW, 'yardmaster')
        self.store.set_auto_topic_name(NEW, 'Plan', False)
        rows = self.store.auto_topic_rows()
        for uid, auto in ((10, AUTO_OFF), (20, None)):
            with self.subTest(auto=auto):
                config = {k: v for k, v in self.config.items() if k != 'auto_topics'}
                if auto is not None:
                    config['auto_topics'] = auto
                self.bridge = self.make_bridge(config)
                del self.calls[:]
                log = self.drain(service(uid, OTHER, 'forum_topic_created', name='Other'),
                                 service(uid + 1, NEW, 'forum_topic_edited', name='Renamed'),
                                 in_topic(self.update(uid + 2), NEW, reply_to_message=dict(
                                     message_id=NEW, forum_topic_created=dict(name='x'))),
                                 in_topic(self.update(uid + 3), OTHER))
                self.assertEqual([url for url, _ in self.calls], [HOOK_URL, HOOK_URL])
                self.assertEqual([(body['conversation'], body['topic'], body['thread_id'])
                                  for body in self.deliveries()], [('123', 'general', None)] * 2)
                self.assertEqual([text for *_, text in self.relays()], [
                    f'[topic: thread {NEW} | not configured: reply lands in the main chat]\nhello',
                    f'[topic: thread {OTHER} | not configured: reply lands in the main chat]\nhello'])
                self.assertEqual(log, f'topic thread {NEW} not configured (msg {uid + 2}); relayed to the main '
                                      f'conversation\ntopic thread {OTHER} not configured (msg {uid + 3}); relayed '
                                      'to the main conversation\n')
                self.assertEqual(self.store.offset(), uid + 4)
                self.assertEqual(self.store.auto_topic_rows(), rows)


class AutoTopicVoiceTests(AutoTopicInboundCase):
    # TopicVoiceTests' helpers (imported by module, so discovery runs that
    # class once, in test_topic_inbound).
    voice = test_topic_inbound.TopicVoiceTests.voice
    pending = test_topic_inbound.TopicVoiceTests.pending
    restarted = test_topic_inbound.TopicVoiceTests.restarted

    def setUp(self):
        super().setUp()
        self.bridge.state_dir = Path(self.tmp.name)
        script = Path(self.tmp.name) / 'fake_transcriber.py'
        script.write_text('import json\nprint(json.dumps(dict(text="check the rack")))\n')
        self.bridge.transcribe = dict(python=sys.executable, script=str(script), model='base', timeout=20, env={})
        self.enterContext(patch.object(b.shutil, 'which', return_value='/usr/bin/ffmpeg'))

    def test_a_voice_note_in_an_auto_topic_gets_its_transcript_on_the_topics_conversation(self):
        with patch.object(self.bridge, '_enqueue_transcription'):
            self.drain(service(69, NEW, 'forum_topic_created', name='Rack'), self.voice(70, NEW))
        self.assertEqual(json.loads(self.pending(70).read_text())['thread_id'], NEW)
        self.assertEqual(self.restarted(), 1)
        (note_ref, note_id, _, note), (ref, pid, _, transcript) = self.relays()
        self.assertEqual((note_ref, note_id, ref, pid), (topic_id(NEW), '70', topic_id(NEW), '70-transcript'))
        self.assertTrue(note.startswith('[topic: Rack]\n[attachment saved: '))
        self.assertTrue(transcript.startswith('[topic: Rack]\n[voice 0:07 transcript (msg 70): check the rack]'))
        self.assertEqual([(body['kind'], body['topic'], body['thread_id']) for body in self.deliveries()],
                         [('message', 'Rack', NEW), ('transcript', 'Rack', NEW)])


class AutoTopicAskTests(AutoTopicInboundCase):
    def transport(self, url, body, *args, **kwargs):
        if url.endswith(('/answerCallbackQuery', '/editMessageText')):
            self.calls.append((url, body))
            if url.endswith('/answerCallbackQuery'):
                return 200, dict(ok=True, result=True)
            return 200, dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=123)))
        return super().transport(url, body, *args, **kwargs)

    def test_a_tap_on_an_ask_in_an_auto_topic_relays_on_its_conversation(self):
        ask_id = b.Bridge._ask_id_for_key('ask-1')
        options = [dict(text='Yes', callback_data=f'ask:{ask_id}:yes')]
        self.drain(service(40, NEW, 'forum_topic_created', name='Plan'))
        self.store.record_ask(ask_id, 'Pick one?', 'plain', options, conversation_id=topic_id(NEW))
        self.store.set_ask_message([ask_id], 77)
        press = in_topic(dict(update_id=41, callback_query=dict(
            id='query-41', data=f'ask:{ask_id}:yes', chat_instance='x', **{'from': dict(id=123, is_bot=False)},
            message=dict(message_id=77, chat=dict(id=123, type='private')))), NEW)
        self.drain(press)
        self.assertEqual(self.relays(), [(topic_id(NEW), f'ask-{ask_id}', None, '[topic: Plan]\n'
                                          '[ask answer | choice: Yes | message: 77]\nquestion: Pick one?')])
        [body] = self.deliveries()
        self.assertEqual((body['kind'], body['topic'], body['id']), ('answer', 'Plan', f'999:{topic_id(NEW)}:ask-{ask_id}'))
        self.assertTrue(self.store.get_ask(ask_id)['relayed'])
        self.assertEqual(self.store.offset(), 42)


class AutoTopicCommandTests(CommandCase):
    def setUp(self):
        super().setUp()
        self.bridge = self.make_bridge(auto_topics=AUTO_ON)

    def sent(self):
        return ('sendMessage', dict(chat_id=123, text='ok', link_preview_options={'is_disabled': True},
                                    message_thread_id=NEW))

    def test_a_command_in_an_auto_topic_sends_into_its_thread(self):
        self.store.add_auto_topic(NEW, 'yardmaster')
        self.drain(in_topic(self.message('/deploy', 40), NEW))
        self.assertEqual(self.telegram.calls, [self.sent()])

    def test_a_first_command_in_a_new_thread_records_its_topic_before_it_runs(self):
        log = self.drain(in_topic(self.message('/deploy', 40), NEW))
        self.assertEqual(self.telegram.calls, [self.sent()])
        row, = self.store.auto_topic_rows()
        self.assertEqual((row['thread_id'], row['name'], row['agent']), (NEW, None, 'yardmaster'))
        self.assertEqual([topic['thread_id'] for topic in self.bridge.served_auto_topics()], [NEW])
        self.assertEqual(log.count('not configured'), 0)
        self.drain(in_topic(self.message('/deploy', 41), NEW))
        self.assertEqual(self.telegram.methods(), ['sendMessage', 'sendMessage'])
        self.assertEqual(self.store.offset(), 42)

    def test_a_command_in_a_thread_with_auto_off_records_nothing(self):
        for uid, auto in ((40, None), (41, AUTO_OFF)):
            with self.subTest(auto=auto):
                self.telegram.calls.clear()
                self.bridge = self.make_bridge() if auto is None else self.make_bridge(auto_topics=auto)
                self.drain(in_topic(self.message('/deploy', uid), NEW))
                self.assertEqual(self.telegram.calls, [self.sent()])
                self.assertEqual(self.store.auto_topic_rows(), [])


class AutoTopicStartTests(AutoTopicInboundCase):
    def start(self, get_me, auto=AUTO_ON):
        """Run the daemon through its real probe and one empty poll; its stderr."""
        config = {k: v for k, v in self.config.items() if k != 'auto_topics'}
        if auto is not None:
            config['auto_topics'] = auto
        self.bridge = self.make_bridge(config)
        self.bridge.state_dir = Path(self.tmp.name)
        telegram, elsewhere = [], []

        def transport(url, body, *args, **kwargs):
            if '/bot' in url:
                telegram.append(url.rsplit('/', 1)[1])
            else:
                elsewhere.append(url)
            if url.endswith('/getMe'):
                return get_me
            if url.endswith('/getUpdates'):
                self.bridge.stop.set()
                return 200, dict(ok=True, result=[])
            return self.transport(url, body, *args, **kwargs)
        self.bridge.request = transport
        with patch('sys.stdout', new_callable=io.StringIO), patch('sys.stderr', new_callable=io.StringIO) as err:
            testports.run_bridge(self.bridge)
        self.assertEqual(telegram, ['getMe', 'getWebhookInfo', 'getUpdates'])  # no Telegram call of its own
        self.assertEqual(elsewhere, [])  # nothing registers with, or announces itself to, the yard
        return err.getvalue().splitlines()

    def test_auto_on_without_users_creating_topics_logs_once_at_start(self):
        for get_me in (GET_ME, MODE_OFF, (200, dict(ok=True, result=dict(id=999, is_bot=True)))):
            with self.subTest(get_me=get_me[1]['result']):
                self.assertEqual(self.start(get_me).count(b.Bridge.AUTO_TOPICS_USERS_OFF), 1)
        self.assertEqual(b.Bridge.AUTO_TOPICS_USERS_OFF,
                         'auto topics on, but users may not create topics (BotFather setting)')

    def test_no_line_with_users_creating_topics_or_auto_off(self):
        for get_me, auto in ((USERS_ALLOWED, AUTO_ON), (GET_ME, AUTO_OFF), (GET_ME, None), (MODE_OFF, None)):
            with self.subTest(get_me=get_me[1]['result'], auto=auto):
                self.assertNotIn(b.Bridge.AUTO_TOPICS_USERS_OFF, self.start(get_me, auto))


if __name__ == '__main__':
    unittest.main()
