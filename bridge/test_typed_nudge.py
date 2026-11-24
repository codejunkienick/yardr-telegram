"""Typed /nudge with the operator's own words, over fake Telegram and the
fake or a real isolated core: the typed command, the reply to a nudge card,
the verified tap's one write, and the outcome relayed on the confirm card."""
from pathlib import Path
import io
import json
import re
import subprocess
import sys
import tempfile
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
import testports  # noqa: E402
import testyardr  # noqa: E402
import yard_commands as y  # noqa: E402
from test_bridge import BridgeTestCase  # noqa: E402
from test_commands import FakeTelegram, answer, handler_script  # noqa: E402
from test_run_asks import NativeCase  # noqa: E402

n = b.run_asks
tn = b.typed_nudge
nc = b.nudge_cards
JOURNAL = 'yardmaster-journal'
WORDS = 'go on \u2014 where you stopped? \u2713'

SOURCE = ('name: tgbridge\nbase: https://example.invalid\ncalls:\n  health:\n    kind: read\n    request: {method: GET, path: /health}\n'
          'hook:\n  bodies: unique\n  verify:\n    scheme: secret\n    signature: {header: X-Secret}\n  normalize: [/bin/false]\n')


def crew(**extra):
    member = dict(name='yardmaster', journal=JOURNAL, kind='claude', model='opus',
                  state='idle', status='', live=True)
    member.update(extra)
    return member


def session(sid='sess-1', bead='run-9', state='running'):
    return dict(id=sid, bead=bead, state=state, kind='claude', model='opus')


def nudged(seq, ident, outcome='typed', reason='', **data):
    data = dict(dict(id=ident, by='pat', crew='yardmaster', line=WORDS, outcome=outcome), **data)
    if reason or outcome != 'typed':
        data['reason'] = reason
    return dict(seq=seq, id=str(seq), bead=JOURNAL, kind='session_nudged', actor='yard',
               data=data, at='2026-11-20T12:00:01Z')


class TypedCase(NativeCase):
    def setUp(self):
        super().setUp()
        self.clock = [1000.0]
        self.native.crew = {'yardmaster': crew()}
        self.native.sessions = [session()]
        self.native.raw_events = []
        self.config['commands'] = (y.NUDGE_COMMAND,)
        self.restart()
        self.logs = []

    def restart(self):
        request = self.bridge.request
        self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=request,
                               request_multipart=self.bridge.request_multipart)
        self.bridge.hook_secret = 'HOOKSECRET'
        self.bridge.run_asks.now = lambda: self.clock[0]

    def poll(self, *updates):
        telegram = self.bridge.telegram

        def fetched(method, body, timeout=20):
            return (200, dict(ok=True, result=list(updates))) if method == 'getUpdates' else telegram(method, body, timeout)
        with patch.object(self.bridge, 'telegram', side_effect=fetched), \
                patch.object(self.bridge, '_log', side_effect=self.logs.append):
            self.bridge.poll_once()

    def sends(self):
        return [body for url, body in self.calls if url.endswith('/sendMessage')]

    def command(self, text, uid=30, **message):
        base = dict(message_id=uid, text=text, chat=dict(id=123, type='private'),
                    **{'from': dict(id=123, is_bot=False)})
        base.update(message)
        self.bridge.inbound(dict(update_id=uid, message=base))

    def tap(self, token, action='nudge', uid=40, **message):
        row = self.bridge.typed_nudge.get(token)
        base = dict(message_id=row['message'], chat=dict(id=123, type='private'),
                    **{'from': dict(id=999, is_bot=True)})
        base.update(message)
        return dict(update_id=uid, callback_query=dict(id=f'q{uid}', data=f'nudge:{token}:{action}',
                                                       message=base, **{'from': dict(id=123, is_bot=False)}))

    def row(self):
        rows = self.bridge.typed_nudge.rows()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def writes(self):
        return [args for method, args, _ in self.native.calls
                if method == 'NudgeSession' and args.get('target')]


class TypedCommandTests(TypedCase):
    def test_typed_words_open_confirm_then_tap_writes_once_and_relays_the_outcome(self):
        with patch.object(self.bridge, '_log', side_effect=self.logs.append):
            self.command(f'/nudge yardmaster {WORDS}')
        sends = self.sends()
        self.assertEqual(len(sends), 1)
        card = sends[0]
        self.assertIn('<b>Nudge crew member yardmaster?</b>', card['text'])
        self.assertIn('claude', card['text'])
        self.assertIn(WORDS, card['text'])
        self.assertEqual([button['text'] for row in card['reply_markup']['inline_keyboard'] for button in row],
                         ['Nudge', 'Cancel'])
        row = self.row()
        self.poll(self.tap(row['token'], uid=40))
        writes = self.writes()
        self.assertEqual(len(writes), 1)
        self.assertEqual(writes[0]['target'], 'yardmaster')
        self.assertEqual(writes[0]['text'], WORDS)
        self.assertEqual(writes[0]['actor'], 'pat')
        self.assertEqual(writes[0]['user'], '')
        self.assertEqual(writes[0]['clear'], False)
        # No outcome yet: nothing is said.
        self.clock[0] += 30
        self.poll()
        self.assertEqual(len(self.sends()), 1)
        ident = self.native.nudges['crew:yardmaster']['id']
        self.native.raw_events.append(nudged(33, ident))
        self.poll()
        reply = self.sends()[-1]
        self.assertEqual(reply['text'], f'\u2713 Nudge typed')
        self.assertEqual(reply['reply_parameters']['message_id'], row['message'])
        self.poll()
        self.restart()
        self.poll()
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.logs, [])

    def test_no_words_types_the_core_default_line(self):
        self.command('/nudge yardmaster')
        self.assertIn(tn.DEFAULT_LINE, self.sends()[0]['text'])
        self.poll(self.tap(self.row()['token'], uid=40))
        self.assertEqual(self.writes()[0]['text'], '')
        self.assertEqual(self.native.nudges['crew:yardmaster']['text'], tn.DEFAULT_LINE)

    def test_a_session_target_resolves_by_id_and_follows_its_bead(self):
        self.command('/nudge sess-1 go')
        card = self.sends()[0]
        self.assertIn('<b>Nudge session sess-1?</b>', card['text'])
        self.assertIn('run-9', card['text'])
        self.poll(self.tap(self.row()['token'], uid=40))
        self.assertEqual(self.writes()[0]['target'], 'sess-1')
        ident = self.native.nudges['session:sess-1']['id']
        event = nudged(33, ident)
        event['bead'] = 'run-9'
        del event['data']['crew']
        self.native.raw_events.append(event)
        self.poll()
        self.assertEqual(self.sends()[-1]['text'], '\u2713 Nudge typed')

    def test_a_bead_with_one_live_session_resolves(self):
        self.command('/nudge run-9 go')
        self.assertIn('<b>Nudge session run-9?</b>', self.sends()[0]['text'])
        self.poll(self.tap(self.row()['token'], uid=40))
        self.assertEqual(self.writes()[0]['target'], 'run-9')

    def test_a_session_without_a_recorded_model_opens_its_confirm(self):
        self.native.sessions = [dict(id='sess-9', bead='run-9', state='running')]
        self.command('/nudge sess-9 carry on')
        sends = self.sends()
        self.assertEqual(len(sends), 1)
        self.assertIn('<b>Nudge session sess-9?</b>', sends[0]['text'])
        self.poll(self.tap(self.row()['token'], uid=40))
        self.assertEqual(self.writes()[0]['target'], 'sess-9')

    def test_a_queued_outcome_is_relayed_in_words(self):
        self.command(f'/nudge yardmaster {WORDS}')
        self.poll(self.tap(self.row()['token'], uid=40))
        ident = self.native.nudges['crew:yardmaster']['id']
        self.native.raw_events.append(nudged(33, ident, working=True, queued=True))
        saved = self.row()
        before = len(self.sends())
        self.bridge.commands.clear()
        self.poll()
        self.assertEqual(self.row(), saved)
        self.assertEqual(len(self.sends()), before)
        self.bridge.commands['nudge'] = y.NUDGE_COMMAND
        self.poll()
        self.assertEqual(self.sends()[-1]['text'],
                         '\u2713 Nudge typed (the agent was working; the line is queued)')

    def test_taps_that_are_not_the_operators_write_nothing(self):
        self.command(f'/nudge yardmaster {WORDS}')
        row = self.row()
        sender = self.tap(row['token'], uid=50)
        sender['callback_query']['from'] = dict(id=124, is_bot=False)
        chat = self.tap(row['token'], uid=51, chat=dict(id=124, type='private'))
        topic = self.tap(row['token'], uid=52, is_topic_message=True, message_thread_id=7)
        forwarded = self.tap(row['token'], uid=53, forward_origin=dict(type='user'))
        self.poll(sender, chat, topic, forwarded)
        self.assertEqual(self.writes(), [])
        # A replay of the good tap and a double tap each write nothing more.
        self.poll(self.tap(row['token'], uid=54))
        self.assertEqual(len(self.writes()), 1)
        self.poll(self.tap(row['token'], uid=54))
        self.assertEqual(len(self.writes()), 1)
        self.poll(self.tap(row['token'], uid=55))
        self.assertEqual(len(self.writes()), 1)

        uid = 60
        for key, value in (('yard', 'other-yard'), ('yardr_source', 'other-source'),
                           ('yardr_operator', 'other-operator')):
            for action in ('nudge', 'cancel'):
                uid += 10
                with self.subTest(binding=key, action=action):
                    self.command('/nudge yardmaster retained words', uid=uid)
                    row = next(row for row in self.bridge.typed_nudge.rows() if row['state'] == 'open')
                    original = self.config[key]
                    try:
                        self.config[key] = value
                        self.restart()
                        self.native.calls.clear()
                        self.bridge.inbound(self.tap(row['token'], action, uid=uid + 1))
                        self.assertEqual(self.native.calls, [])
                        closed = self.bridge.typed_nudge.get(row['token'])
                        self.assertEqual(closed['state'], 'refused')
                        self.assertIn('another yard', closed['notice'])
                        answer = [body for url, body in self.calls if url.endswith('/answerCallbackQuery')][-1]
                        self.assertIn('another yard', answer['text'])
                        self.assertTrue(answer['show_alert'])
                        edit = [body for url, body in self.calls if url.endswith('/editMessageText')][-1]
                        self.assertIn(closed['notice'], edit['text'])
                        self.assertEqual(edit['reply_markup'], {'inline_keyboard': []})
                    finally:
                        self.config[key] = original
                        self.restart()
                    self.poll(self.tap(row['token'], uid=uid + 2))
                    self.assertEqual(self.writes(), [])
                    self.assertEqual(self.bridge.typed_nudge.get(row['token'])['state'], 'refused')

    def test_cancel_makes_no_write_and_takes_the_buttons_off(self):
        self.command(f'/nudge yardmaster {WORDS}')
        row = self.row()
        self.poll(self.tap(row['token'], 'cancel', uid=40))
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.bridge.typed_nudge.get(row['token'])['state'], 'cancelled')
        edit = [body for url, body in self.calls if url.endswith('/editMessageText')][-1]
        self.assertEqual(edit['reply_markup'], {'inline_keyboard': []})
        self.assertIn(tn.CANCELLED, edit['text'])

    def test_overlong_words_are_refused_before_any_call(self):
        self.command('/nudge yardmaster ' + 'x' * 1001)
        notice = self.sends()[0]
        self.assertIn('1001 characters', notice['text'])
        self.assertEqual(self.native.calls, [])

    def test_control_characters_and_an_empty_command_are_refused_before_any_call(self):
        self.command('/nudge yardmaster go\non', uid=30)
        self.assertIn('one line', self.sends()[0]['text'])
        self.command('/nudge', uid=31)
        self.assertIn('Usage', self.sends()[-1]['text'])
        self.assertEqual(self.native.calls, [])

    def test_an_unknown_target_names_itself_and_writes_nothing(self):
        self.command('/nudge nobody go')
        self.assertIn('No session, crew member or bead nobody', self.sends()[0]['text'])
        self.assertEqual(self.writes(), [])

    def test_a_stopped_target_opens_but_its_tap_is_refused(self):
        self.native.crew = {'yardmaster': crew(live=False, state='stopped')}
        self.command('/nudge yardmaster go')
        self.assertIn('stopped', self.sends()[0]['text'])
        self.poll(self.tap(self.row()['token'], uid=40))
        self.assertEqual(len(self.writes()), 1)
        edit = [body for url, body in self.calls if url.endswith('/editMessageText')][-1]
        self.assertIn('nothing nudged', edit['text'])
        self.assertEqual(self.bridge.typed_nudge.get(self.row()['token'])['state'], 'refused')

    def test_a_second_standing_request_is_refused(self):
        self.command('/nudge yardmaster go')
        self.poll(self.tap(self.row()['token'], uid=40))
        self.assertEqual(len(self.writes()), 1)
        self.command('/nudge yardmaster go again', uid=31)
        second = [row for row in self.bridge.typed_nudge.rows() if row['state'] == 'open'][0]
        self.poll(self.tap(second['token'], uid=41))
        self.assertEqual(len(self.native.nudges), 1)
        edit = [body for url, body in self.calls if url.endswith('/editMessageText')][-1]
        self.assertIn('stands already', edit['text'])

    def test_each_outcome_reads_as_its_own_reply(self):
        self.command('/nudge yardmaster go')
        self.poll(self.tap(self.row()['token'], uid=40))
        ident = self.native.nudges['crew:yardmaster']['id']
        self.native.raw_events.append(nudged(33, ident, outcome='refused', reason='the input holds text'))
        self.poll()
        self.assertEqual(self.sends()[-1]['text'], '\u2715 Nudge refused: the input holds text')

    def test_no_outcome_within_ten_minutes_is_said_as_unknown_and_not_retried(self):
        self.command('/nudge yardmaster go')
        self.poll(self.tap(self.row()['token'], uid=40))
        self.clock[0] += tn.FOLLOW_SECONDS + 1
        self.poll()
        self.poll()
        self.assertEqual(self.sends()[-1]['text'], tn.SILENT)
        self.assertEqual(len(self.writes()), 1)

    def test_a_core_without_the_method_answers_not_supported_and_writes_nothing(self):
        self.native.mode = 'no-nudge-method'
        self.command('/nudge yardmaster go')
        notice = self.sends()[0]
        self.assertIn('no session nudge', notice['text'])
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual([args for method, args, _ in self.native.calls if method == 'NudgeSession'],
                         [{}])
        self.assertEqual(self.bridge.typed_nudge.rows(), [])

    def test_no_such_method_is_classified_unsupported(self):
        self.native.mode = 'no-nudge-method'
        with self.assertRaises(n.RPCUnsupported):
            self.bridge.run_asks.client.call('NudgeSession', dict(target='x', user='', text='', clear=False,
                                                                  actor='pat'))

    def test_plain_chat_still_goes_to_the_hook_as_mail(self):
        self.bridge.inbound(dict(update_id=60, message=dict(message_id=60, text='hello',
                                 chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)})))
        self.assertEqual([call[1]['text'] for call in self.calls if call[0] == self.bridge.hook_url], ['hello'])
        self.assertEqual(self.bridge.typed_nudge.rows(), [])

    def test_a_reply_to_no_card_goes_to_the_hook_as_mail(self):
        self.bridge.inbound(dict(update_id=61, message=dict(message_id=61, text='hello',
                                 chat=dict(id=123, type='private'), reply_to_message=dict(message_id=999),
                                 **{'from': dict(id=123, is_bot=False)})))
        self.assertEqual([call[1]['text'] for call in self.calls if call[0] == self.bridge.hook_url], ['hello'])

    def test_refresh_runs_after_the_nudge_cards_and_before_the_updates(self):
        order = []
        for name, target in (('run_asks', self.bridge.run_asks), ('nudge_cards', self.bridge.nudge_cards),
                             ('typed_nudge', self.bridge.typed_nudge), ('siding_cards', self.bridge.siding_cards),
                             ('model_switch', self.bridge.model_switch)):
            patch.object(target, 'refresh', side_effect=lambda name=name: order.append(name)).start()
        self.addCleanup(patch.stopall)
        with patch.object(self.bridge, 'drain', side_effect=lambda updates: order.append('drain')):
            self.poll()
        self.assertEqual(order, ['run_asks', 'nudge_cards', 'typed_nudge', 'siding_cards', 'drain', 'model_switch'])

    def test_the_preview_renders_the_card_and_its_replies_as_synthetic(self):
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'scripts/preview_typed_nudge.py')],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Synthetic data', result.stdout)
        self.assertEqual(len(re.findall('<section>', result.stdout)), 5)

    def test_nudge_stays_out_of_the_telegram_menu(self):
        posted = {}

        def fake(method, body, timeout=20):
            if method == 'setMyCommands':
                posted['set'] = body['commands']
                return 200, dict(ok=True, result=True)
            if method == 'getMyCommands':
                return 200, dict(ok=True, result=posted['set'])
            return self.bridge.telegram(method, body, timeout)
        with patch.object(self.bridge, 'telegram', side_effect=fake):
            shown = self.bridge.set_commands()
        self.assertNotIn('nudge', [entry['command'] for entry in shown])
        self.assertIn('nudge', self.bridge.commands)


class NudgeSettingsTests(unittest.TestCase):
    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / 'yard'
        self.home.mkdir(mode=0o700)
        self.enterContext(patch.dict('os.environ', dict(
            HOME=str(self.root), PATH=str(self.root) + ':/usr/local/bin:/usr/bin:/bin',
            XDG_CONFIG_HOME=str(self.root / 'config'), XDG_STATE_HOME=str(self.root / 'state')), clear=True))

    def settings(self, extra='', native=True):
        from test_bridge import write_settings
        path = write_settings(self.root)
        text = path.read_text()
        if native:
            marker = '\n[yardr]\n'
            start = text.index(marker)
            text = text[:start] + marker + 'home = "%s"\noperator = "pat"\ncards = true\n' % self.home + text[start + len(marker):]
        path.write_text(text + extra)
        return path

    def custom(self, name, prefix):
        keyboard = [[dict(text='Again', callback_data=(prefix or name + ':') + 'again')]]
        handler = handler_script(self.root / 'handlers', answer(dict(type='send', text='My command', keyboard=keyboard)))
        fields = dict(name=name, description='Mine', handler=str(handler))
        if prefix is not None:
            fields['callback_prefix'] = prefix
        return '\n[[command]]\n' + ''.join(f'{key} = {json.dumps(value)}\n' for key, value in fields.items())

    def make_bridge(self, settings):
        root = Path(self.enterContext(tempfile.TemporaryDirectory(dir=self.root)))
        fake = FakeTelegram()
        fake.answers['setMyCommands'] = 200, dict(ok=True, result=True)
        fake.answers['getMyCommands'] = lambda body: (200, dict(ok=True, result=fake.of('setMyCommands')[-1]['commands']))
        config = dict(settings.config, hook_secret='TESTHOOK')
        bridge = b.Bridge(config, b.Store(root / 'state.sqlite3', 10), 'TESTTOKEN', 'TESTSECRET',
                          port=0, request=fake, state_dir=root)
        return bridge, fake

    def check_custom(self, settings, name, prefix):
        bridge, fake = self.make_bridge(settings)
        record = self.root / 'handlers' / 'record.jsonl'
        before = len(record.read_text().splitlines()) if record.exists() else 0
        with patch('sys.stderr', new_callable=io.StringIO):
            bridge.inbound(dict(update_id=40, message=dict(message_id=40, text=f'/{name} {WORDS}',
                chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)})))
            bridge.inbound(dict(update_id=41, callback_query=dict(id='q41', data=prefix + 'again',
                message=dict(message_id=501, chat=dict(id=123, type='private')),
                **{'from': dict(id=123, is_bot=False)})))
        records = [json.loads(json.loads(line)['stdin']) for line in record.read_text().splitlines()]
        self.assertEqual(len(records), before + 2)
        self.assertEqual((records[-2]['kind'], records[-2]['command'], records[-2]['args']), ('command', name, WORDS))
        self.assertEqual((records[-1]['kind'], records[-1]['command'], records[-1]['callback_data']),
                         ('callback', name, prefix + 'again'))
        self.assertEqual([body['text'] for body in fake.of('sendMessage')], ['My command', 'My command'])
        keyboard = dict(inline_keyboard=[[dict(text='Again', callback_data=prefix + 'again')]])
        self.assertEqual([body['reply_markup'] for body in fake.of('sendMessage')], [keyboard, keyboard])
        self.assertEqual(fake.of('answerCallbackQuery'), [dict(callback_query_id='q41')])
        self.assertEqual(fake.of('hook'), [])
        self.assertEqual(bridge.typed_nudge.rows(), [])
        self.assertNotEqual(bridge.commands.get('nudge', {}).get('native'), y.NATIVE_NUDGE)
        shown = bridge.set_commands()
        self.assertEqual([entry for entry in shown if entry['command'] == name], [dict(command=name, description='Mine')])

    def test_native_registers_a_typed_only_nudge(self):
        for extra in ('', self.custom('my_nudge', 'my_nudge:'), self.custom('mine', 'nudge-other:')):
            with self.subTest(extra=extra):
                settings = b.load_settings(self.settings(extra))
                (entry,) = [command for command in settings.config['commands'] if command['name'] == 'nudge']
                self.assertEqual(entry['menu'], False)
                self.assertEqual(entry['callback_prefix'], 'nudge:')
                self.assertEqual(entry['native'], 'nudge')
                self.assertNotIn('yardr_nudge_disabled', settings.config)

    def test_without_native_there_is_no_nudge_command(self):
        from test_bridge import write_settings
        settings = b.load_settings(write_settings(self.root))
        self.assertNotIn('nudge', [command['name'] for command in settings.config.get('commands', ())])

    def test_a_custom_nudge_keeps_its_handler_and_callbacks(self):
        for prefix in (None, 'nudge:', 'custom:'):
            with self.subTest(prefix=prefix):
                extra = self.custom('nudge', prefix)
                plain = b.load_settings(self.settings(extra, native=False))
                settings = b.load_settings(self.settings(extra))
                self.assertEqual(settings.config['commands'], plain.config['commands'])
                self.assertIn('[[command]] /nudge', settings.config['yardr_nudge_disabled'])
                self.assertNotIn('yardr_nudge_disabled', plain.config)
                for loaded in (plain, settings):
                    self.check_custom(loaded, 'nudge', prefix or 'nudge:')

    def test_a_custom_nudge_menu_keeps_its_description_and_mail_relay(self):
        extra = '\n[[menu]]\ncommand = "nudge"\ndescription = "Mine"\n'
        plain = b.load_settings(self.settings(extra, native=False))
        settings = b.load_settings(self.settings(extra))
        self.assertEqual(settings.config['menu'], plain.config['menu'])
        self.assertIn('[[menu]] /nudge', settings.config['yardr_nudge_disabled'])
        for loaded in (plain, settings):
            with self.subTest(native='yardr_home' in loaded.config):
                bridge, fake = self.make_bridge(loaded)
                self.assertNotIn('nudge', bridge.commands)
                shown = bridge.set_commands()
                self.assertEqual([entry for entry in shown if entry['command'] == 'nudge'],
                                 [dict(command='nudge', description='Mine')])
                bridge.inbound(dict(update_id=40, message=dict(message_id=40, text='/nudge ' + WORDS,
                    chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)})))
                self.assertEqual([body['text'] for body in fake.of('hook')], ['/nudge ' + WORDS])
                self.assertEqual(fake.of('sendMessage'), [])
                self.assertEqual(bridge.typed_nudge.rows(), [])

    def test_overlapping_custom_prefixes_keep_their_handlers_and_callbacks(self):
        for prefix in ('n', 'nudge', 'nudge:', 'nudge:x'):
            with self.subTest(prefix=prefix):
                extra = self.custom('mine', prefix)
                plain = b.load_settings(self.settings(extra, native=False))
                settings = b.load_settings(self.settings(extra))
                self.assertEqual(settings.config['commands'], plain.config['commands'])
                self.assertIn('[[command]] /mine callback_prefix', settings.config['yardr_nudge_disabled'])
                for loaded in (plain, settings):
                    self.check_custom(loaded, 'mine', prefix)

    def test_a_conflict_is_explained_once_at_startup_and_in_install_status(self):
        cases = (self.custom('nudge', 'custom:'),
                 '\n[[menu]]\ncommand = "nudge"\ndescription = "Mine"\n',
                 self.custom('mine', 'nudge:x'))
        for extra in cases:
            with self.subTest(extra=extra):
                settings = b.load_settings(self.settings(extra))
                reason = settings.config['yardr_nudge_disabled']
                self.assertIn('Built-in /nudge disabled', reason)
                self.assertIn('Rename', reason)
                self.assertIn('callback_prefix', reason)
                bridge, _ = self.make_bridge(settings)
                bridge.stop.set()
                bridge.typing_seconds = 0
                with patch.object(bridge, 'probe_with_retry', return_value=True), \
                     patch('sys.stdout', new_callable=io.StringIO) as out, \
                     patch('sys.stderr', new_callable=io.StringIO) as err:
                    testports.run_bridge(bridge)
                self.assertEqual(err.getvalue().splitlines(), [reason] + b.message_state_report(settings.config))
                self.assertIn('Telegram bridge ready', out.getvalue())
                self.assertEqual(out.getvalue().count(reason), 1)
                lines = b.install_problems(settings)
                self.assertEqual(lines.count((True, 'WARN  ' + reason)), 1)


class RealTypedNudgeTests(BridgeTestCase):
    """A real isolated core; the daemon's own writes (the outcome) come from an
    offline store fixture between restarts, as no socket caller may make them."""

    def start(self):
        binary = testyardr.binary()
        if binary is None:
            self.skipTest('yardr binary required for isolated native integration')
        self.root = Path(self.tmp.name)
        self.binary = binary
        self.server, self.home, self.command, self.env = testyardr.start(self.root, binary)
        self.addCleanup(lambda: testyardr.stop(self.server))
        self.client = n.YardSocketClient(self.home, 'tgbridge')
        self.cli('settings', 'set', 'operator', 'pat')
        self.cli('settings', 'set', 'default_kind', 'codex')
        (self.root / 'synthetic.yaml').write_text(SOURCE)
        self.cli('source', 'add', str(self.root / 'synthetic.yaml'))
        self.cli('source', 'set', 'tgbridge', 'calls_per_hour=120')
        self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
        self.cli('user', 'add', 'proposer')
        self.cli('crew', 'add', 'yardmaster', '--set', 'kind=claude')
        self.config.update(yardr_home=str(self.home), yardr_operator='pat', yardr_source='tgbridge',
                           commands=(y.NUDGE_COMMAND,))
        self.ids = iter(range(77, 300))
        self.logs = []
        self.restart_bridge()

    def cli(self, *args, actor='pat'):
        result = subprocess.run([self.binary, '--home', str(self.home), '--actor', actor, *args], env=self.env, cwd=self.root,
                                text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def telegram(self, url, body, *args, **kwargs):
        status, response = self.transport(url, body, *args, **kwargs)
        if url.endswith('/sendMessage'):
            response['result']['message_id'] = next(self.ids)
        elif url.endswith('/editMessageText'):
            response = dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=123)))
        return status, response

    def restart_bridge(self):
        self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=self.telegram)
        self.bridge.hook_secret = None

    def daemon(self, *args):
        """One write of the daemon's, offline: the server stops for it and starts again on the same home."""
        testyardr.stop(self.server)
        try:
            result = subprocess.run([str(self.fixture), str(self.home), *args], capture_output=True, text=True, timeout=60)
            self.assertEqual(result.returncode, 0, result.stderr)
        finally:
            self.server = testyardr.restart(self.root, self.binary, self.home, self.command, self.env)
        return result.stdout.split()

    def poll(self, *updates):
        telegram = self.bridge.telegram

        def fetched(method, body, timeout=20):
            return (200, dict(ok=True, result=list(updates))) if method == 'getUpdates' else telegram(method, body, timeout)
        with patch.object(self.bridge, 'telegram', side_effect=fetched), \
                patch.object(self.bridge, '_log', side_effect=self.logs.append):
            self.bridge.poll_once()

    def sends(self):
        return [body for url, body in self.calls if url.endswith('/sendMessage')]

    def tap(self, uid, token, action='nudge', **message):
        row = self.bridge.typed_nudge.get(token)
        message = dict(message_id=row['message'], chat=dict(id=123, type='private'),
                       **{'from': dict(id=999, is_bot=True)}, **message)
        return dict(update_id=uid, callback_query=dict(id=f'q{uid}', data=f'nudge:{token}:{action}',
                                                       message=message, **{'from': dict(id=123, is_bot=False)}))

    def test_real_native_typed_nudge_is_typed_once_and_its_outcome_told(self):
        self.start()
        if not testyardr.serves(self.home, 'NudgeSession'):
            self.skipTest('this core has no session nudge')
        self.fixture = testyardr.store_fixture(self.root, 'nudge')
        if self.fixture is None:
            self.skipTest('TGBRIDGE_TEST_YARDR_SOURCE names no candidate source for the daemon-write fixture')
        (journal,) = self.daemon('start', 'yardmaster')
        self.assertEqual(journal, JOURNAL)
        self.bridge.inbound(dict(update_id=30, message=dict(message_id=30, text='/nudge yardmaster ' + WORDS,
                                 chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)})))
        self.assertEqual(len(self.sends()), 1)
        card = self.sends()[0]
        self.assertIn('<b>Nudge crew member yardmaster?</b>', card['text'])
        token = self.bridge.typed_nudge.rows()[0]['token']
        self.poll(self.tap(40, token))
        ident = self.bridge.typed_nudge.get(token)['request']
        requested = [e for e in self.client.call('Events', [JOURNAL, 200])
                     if e['kind'] == 'session_nudge_requested' and e['data'].get('id') == ident]
        self.assertEqual(len(requested), 1)
        line = requested[0]['data']['line']
        self.assertEqual(line, WORDS)
        # A replay of the tap and a double tap write nothing more.
        self.poll(self.tap(40, token), self.tap(41, token))
        self.assertEqual(len([e for e in self.client.call('Events', [JOURNAL, 200])
                              if e['kind'] == 'session_nudge_requested']), 1)
        # The daemon finishes the request; its outcome is told once, as a reply on the card.
        self.daemon('finish', 'yardmaster', 'typed', '')
        self.poll()
        self.poll()
        self.assertEqual(len(self.sends()), 2)
        reply = self.sends()[-1]
        self.assertEqual(reply['text'], '\u2713 Nudge typed')
        self.assertEqual(reply['reply_parameters']['message_id'],
                         self.bridge.typed_nudge.get(token)['message'])
        self.poll()
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual([line for line in self.logs if 'typed nudge' in line], [])
        print('Native typed nudge:', self.binary, 'typed once, outcome told')

    def test_real_native_typed_nudge_with_no_words_types_the_core_default(self):
        self.start()
        if not testyardr.serves(self.home, 'NudgeSession'):
            self.skipTest('this core has no session nudge')
        self.fixture = testyardr.store_fixture(self.root, 'nudge')
        if self.fixture is None:
            self.skipTest('TGBRIDGE_TEST_YARDR_SOURCE names no candidate source for the daemon-write fixture')
        (journal,) = self.daemon('start', 'yardmaster')
        self.assertEqual(journal, JOURNAL)
        self.bridge.inbound(dict(update_id=30, message=dict(message_id=30, text='/nudge yardmaster',
                                 chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)})))
        card = self.sends()[0]
        self.assertIn(tn.DEFAULT_LINE, card['text'])
        token = self.bridge.typed_nudge.rows()[0]['token']
        self.poll(self.tap(40, token))
        ident = self.bridge.typed_nudge.get(token)['request']
        requested = [e for e in self.client.call('Events', [JOURNAL, 200])
                     if e['kind'] == 'session_nudge_requested' and e['data'].get('id') == ident]
        self.assertEqual(len(requested), 1)
        self.assertEqual(requested[0]['data']['line'], tn.DEFAULT_LINE)

    def test_real_native_reply_to_a_card_is_typed_once_with_its_exact_words(self):
        self.start()
        if not testyardr.serves(self.home, 'NudgeSession'):
            self.skipTest('this core has no session nudge')
        self.fixture = testyardr.store_fixture(self.root, 'nudge')
        if self.fixture is None:
            self.skipTest('TGBRIDGE_TEST_YARDR_SOURCE names no candidate source for the daemon-write fixture')
        (journal, _) = self.daemon('file', 'yardmaster', 'pat')
        self.assertEqual(journal, JOURNAL)
        self.poll()
        run = self.bridge.run_asks.rows()[0]
        self.bridge.inbound(dict(update_id=31, message=dict(message_id=31, text=WORDS,
                                 chat=dict(id=123, type='private'),
                                 reply_to_message=dict(message_id=run['message_id']),
                                 **{'from': dict(id=123, is_bot=False)})))
        sends = self.sends()
        self.assertEqual(len(sends), 2)
        self.assertIn(WORDS, sends[-1]['text'])
        token = [row for row in self.bridge.typed_nudge.rows() if row['state'] == 'open'][0]['token']
        self.poll(self.tap(40, token))
        requested = [e for e in self.client.call('Events', [JOURNAL, 200])
                     if e['kind'] == 'session_nudge_requested']
        self.assertEqual(len(requested), 1)
        self.assertEqual(requested[0]['data']['line'], WORDS)
        self.daemon('finish', 'yardmaster', 'typed', '')
        self.poll()
        self.poll()
        self.assertEqual(len(self.sends()), 3)
        self.assertEqual(self.sends()[-1]['text'], '\u2713 Nudge typed')

    def test_real_native_reply_to_a_conversation_card_nudges_that_conversation(self):
        self.start()
        if not testyardr.serves(self.home, 'NudgeSession'):
            self.skipTest('this core has no session nudge')
        self.fixture = testyardr.store_fixture(self.root, 'nudge')
        if self.fixture is None:
            self.skipTest('TGBRIDGE_TEST_YARDR_SOURCE names no candidate source for the daemon-write fixture')
        (journal, _) = self.daemon('file-conversation', 'yardmaster', 'pat')
        self.assertEqual(journal, JOURNAL)
        self.daemon('status-conversation', 'yardmaster')
        member = self.client.call('GetCrew', dict(name='yardmaster'))
        self.assertEqual(member.get('status'), 'idle')
        self.assertEqual(member['conversations']['pat'].get('status'), 'blocked')
        self.poll()
        run = self.bridge.run_asks.rows()[0]
        words = 'carry on with this conversation'
        self.bridge.inbound(dict(update_id=31, message=dict(message_id=31, text=words,
                                 chat=dict(id=123, type='private'),
                                 reply_to_message=dict(message_id=run['message_id']),
                                 **{'from': dict(id=123, is_bot=False)})))
        sends = self.sends()
        self.assertEqual(len(sends), 2)
        self.assertIn('<b>Nudge crew member yardmaster for pat?</b>', sends[-1]['text'])
        self.assertIn('blocked', sends[-1]['text'])
        self.assertNotIn('idle', sends[-1]['text'])
        token = [row for row in self.bridge.typed_nudge.rows() if row['state'] == 'open'][0]['token']
        self.poll(self.tap(40, token))
        member = self.client.call('GetCrew', dict(name='yardmaster'))
        self.assertTrue(member.get('conversations', {}).get('pat', {}).get('nudge'))
        self.assertFalse(member.get('nudge'))
        requested = [e for e in self.client.call('Events', [JOURNAL, 200])
                     if e['kind'] == 'session_nudge_requested' and e['data'].get('user') == 'pat']
        self.assertEqual(len(requested), 1)
        self.assertEqual(requested[0]['data']['line'], words)
        self.daemon('finish-conversation', 'yardmaster', 'typed', '')
        self.poll()
        self.poll()
        self.assertEqual(self.sends()[-1]['text'], '\u2713 Nudge typed')

    def test_real_native_tap_discipline_cancel_and_overlong(self):
        self.start()
        if not testyardr.serves(self.home, 'NudgeSession'):
            self.skipTest('this core has no session nudge')
        self.fixture = testyardr.store_fixture(self.root, 'nudge')
        if self.fixture is None:
            self.skipTest('TGBRIDGE_TEST_YARDR_SOURCE names no candidate source for the daemon-write fixture')
        (journal,) = self.daemon('start', 'yardmaster')
        self.assertEqual(journal, JOURNAL)
        self.bridge.inbound(dict(update_id=30, message=dict(message_id=30, text=f'/nudge yardmaster {WORDS}',
                                 chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)})))
        token = self.bridge.typed_nudge.rows()[0]['token']
        row = self.bridge.typed_nudge.get(token)
        base = dict(message_id=row['message'], chat=dict(id=123, type='private'),
                    **{'from': dict(id=999, is_bot=True)})
        wrong = lambda uid, **change: dict(update_id=uid, callback_query=dict(
            id=f'q{uid}', data=f'nudge:{token}:nudge',
            message=dict(base, **change), **{'from': dict(id=123, is_bot=False)}))
        foreign = dict(update_id=49, callback_query=dict(
            id='q49', data=f'nudge:{token}:nudge',
            message=dict(base), **{'from': dict(id=124, is_bot=False)}))
        self.poll(foreign,
                  wrong(50, **{'from': dict(id=124, is_bot=False)}),
                  wrong(51, chat=dict(id=124, type='private')),
                  wrong(52, is_topic_message=True, message_thread_id=7),
                  wrong(53, forward_origin=dict(type='user')))
        requested = lambda: [e for e in self.client.call('Events', [JOURNAL, 200])
                             if e['kind'] == 'session_nudge_requested']
        self.assertEqual(len(requested()), 0)
        self.poll(self.tap(54, token))
        self.poll(self.tap(54, token), self.tap(55, token))
        self.assertEqual(len(requested()), 1)
        self.bridge.inbound(dict(update_id=56, message=dict(message_id=56, text='/nudge yardmaster go again',
                                 chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)})))
        second = [row for row in self.bridge.typed_nudge.rows() if row['state'] == 'open'][0]
        self.poll(self.tap(57, second['token'], action='cancel'))
        self.assertEqual(len(requested()), 1)
        seen = []
        client, real_call = self.bridge.run_asks.client, self.bridge.run_asks.client.call
        client.call = lambda method, args=None: (seen.append(method), real_call(method, args))[1]
        try:
            self.bridge.inbound(dict(update_id=58, message=dict(message_id=58, text='/nudge yardmaster ' + 'x' * 1001,
                                     chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)})))
        finally:
            client.call = real_call
        self.assertIn('1001 characters', self.sends()[-1]['text'])
        self.assertEqual(seen, [])
        self.assertEqual(len(requested()), 1)

        retained = []
        for uid, action in enumerate(('nudge', 'cancel'), 60):
            self.bridge.inbound(dict(update_id=uid, message=dict(message_id=uid, text=f'/nudge yardmaster {WORDS}',
                                     chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)})))
            known = {token for token, _ in retained}
            row = next(row for row in self.bridge.typed_nudge.rows()
                       if row['state'] == 'open' and row['token'] not in known)
            retained.append((row['token'], action))

        other = RealTypedNudgeTests(methodName=self._testMethodName)
        other.setUp()
        self.addCleanup(other.tearDown)
        self.addCleanup(other.doCleanups)
        other.start()
        other.fixture = self.fixture
        self.assertEqual(other.daemon('start', 'yardmaster'), [JOURNAL])
        other.store = self.store
        other.restart_bridge()
        for index, (token, action) in enumerate(retained):
            uid = 70 + index * 2
            with self.subTest(repointed_action=action):
                row = other.bridge.typed_nudge.get(token)
                self.assertNotEqual(row['binding'], other.bridge.typed_nudge.binding_digest())
                client = other.bridge.run_asks.client
                with patch.object(client, 'call', wraps=client.call) as calls:
                    other.poll(other.tap(uid, token, action))
                    other.poll(other.tap(uid + 1, token))
                writes = [call.args for call in calls.call_args_list
                          if call.args[0] == 'NudgeSession' and call.args[1].get('target')]
                self.assertEqual(writes, [])
                self.assertEqual([event for event in other.client.call('Events', [JOURNAL, 200])
                                  if event['kind'] == 'session_nudge_requested'], [])
                self.assertEqual(other.bridge.typed_nudge.get(token)['state'], 'refused')
                answer = [body for url, body in other.calls if url.endswith('/answerCallbackQuery')][-1]
                self.assertIn('another yard', answer['text'])
                edits = [body for url, body in other.calls if url.endswith('/editMessageText')]
                self.assertIn('another yard', edits[-1]['text'])
                self.assertEqual(edits[-1]['reply_markup'], {'inline_keyboard': []})

        self.restart_bridge()
        for uid, (token, _) in enumerate(retained, 90):
            self.poll(self.tap(uid, token))
            self.assertEqual(self.bridge.typed_nudge.get(token)['state'], 'refused')
        self.assertEqual(len(requested()), 1)

    def test_real_native_session_without_a_model_opens_its_confirm(self):
        self.start()
        if not testyardr.serves(self.home, 'NudgeSession'):
            self.skipTest('this core has no session nudge')
        self.fixture = testyardr.store_fixture(self.root, 'nudge')
        if self.fixture is None:
            self.skipTest('TGBRIDGE_TEST_YARDR_SOURCE names no candidate source for the daemon-write fixture')
        (sid, _) = self.daemon('seed-session', 'synthetic')
        session = next(item for item in self.client.call('ListSessions', [{}]) if item['id'] == sid)
        self.assertNotIn('model', session)
        self.bridge.inbound(dict(update_id=30, message=dict(message_id=30, text=f'/nudge {sid} carry on',
                                 chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)})))
        sends = self.sends()
        self.assertEqual(len(sends), 1)
        self.assertIn(f'<b>Nudge session {sid}?</b>', sends[0]['text'])
        token = self.bridge.typed_nudge.rows()[0]['token']
        self.poll(self.tap(40, token))
        requested = [e for e in self.client.call('Events', [session['bead'], 200])
                     if e['kind'] == 'session_nudge_requested']
        self.assertEqual(len(requested), 1)

    def test_real_native_queued_outcome_is_relayed_in_words(self):
        self.start()
        if not testyardr.serves(self.home, 'NudgeSession'):
            self.skipTest('this core has no session nudge')
        self.fixture = testyardr.store_fixture(self.root, 'nudge')
        if self.fixture is None:
            self.skipTest('TGBRIDGE_TEST_YARDR_SOURCE names no candidate source for the daemon-write fixture')
        (journal,) = self.daemon('start', 'yardmaster')
        self.assertEqual(journal, JOURNAL)
        self.bridge.inbound(dict(update_id=30, message=dict(message_id=30, text=f'/nudge yardmaster {WORDS}',
                                 chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)})))
        token = self.bridge.typed_nudge.rows()[0]['token']
        self.poll(self.tap(40, token))
        self.daemon('finish-queued', 'yardmaster')
        self.poll()
        self.poll()
        self.assertEqual(self.sends()[-1]['text'],
                         '\u2713 Nudge typed (the agent was working; the line is queued)')

    def test_real_native_core_without_session_nudge_answers_not_supported(self):
        self.start()
        if testyardr.serves(self.home, 'NudgeSession'):
            self.skipTest('this core has a session nudge')
        self.bridge.inbound(dict(update_id=30, message=dict(message_id=30, text='/nudge yardmaster go',
                                 chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)})))
        self.assertEqual(len(self.sends()), 1)
        self.assertIn('no session nudge', self.sends()[0]['text'])
        self.assertEqual(self.bridge.typed_nudge.rows(), [])


class ReplyTests(TypedCase):
    def publish_card(self, argv=None):
        from test_nudge_cards import nudge as card_nudge
        ask = card_nudge() if argv is None else card_nudge(argv=argv)
        self.native.beads = {JOURNAL: dict(id=JOURNAL, title='Journal of yardmaster', asks=[ask])}
        self.poll()
        rows = self.bridge.run_asks.rows()
        self.assertEqual(len(rows), 1)
        return rows[0]

    def reply(self, text, uid=70, **changes):
        card = self.bridge.run_asks.rows()[0]
        message = dict(message_id=uid, text=text, chat=dict(id=123, type='private'),
                       reply_to_message={'message_id': card['message_id']},
                       **{'from': dict(id=123, is_bot=False)})
        message.update(changes)
        self.bridge.inbound(dict(update_id=uid, message=message))

    def test_a_reply_with_words_to_a_nudge_card_opens_confirm_with_that_target(self):
        self.publish_card()
        self.reply('go on now')
        sends = self.sends()
        self.assertEqual(len(sends), 2)
        confirm = sends[-1]
        self.assertIn('<b>Nudge crew member yardmaster?</b>', confirm['text'])
        self.assertIn('go on now', confirm['text'])
        row = [row for row in self.bridge.typed_nudge.rows() if row['state'] == 'open'][0]
        self.assertEqual(row['words'], 'go on now')
        self.poll(self.tap(row['token'], uid=80))
        self.assertEqual(self.writes()[0]['target'], 'yardmaster')
        self.assertEqual(self.writes()[0]['text'], 'go on now')
        saved = self.bridge.typed_nudge.rows()
        before = len(self.sends())
        self.bridge.commands.clear()
        self.bridge.hook_secret = 'TESTHOOK'
        self.native.calls.clear()
        self.reply('plain words', uid=71)
        self.assertEqual(self.bridge.typed_nudge.rows(), saved)
        self.assertEqual(len(self.sends()), before)
        self.assertEqual(self.native.calls, [])
        hooks = [body for url, body in self.calls if url == self.bridge.hook_url]
        self.assertEqual([body['text'] for body in hooks], ['plain words'])
        self.bridge = b.Bridge(dict(self.config, yardr_cards=False), self.store, 'TOKEN', 'CAPABILITY',
                               request=self.bridge.request)
        self.native.calls.clear()
        self.reply('go on', uid=90)
        self.assertIn('native_run_asks_not_configured', self.sends()[-1]['text'])
        self.assertEqual(self.native.calls, [])
        self.assertEqual(self.bridge.typed_nudge.rows(), saved)

    def test_a_reply_with_overlong_words_is_refused(self):
        self.publish_card()
        before = len(self.sends())
        del self.native.calls[:]
        self.reply('x' * 1001)
        self.assertIn('1001 characters', self.sends()[-1]['text'])
        self.assertEqual(len(self.sends()), before + 1)
        self.assertEqual(self.bridge.typed_nudge.rows(), [])
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.native.calls, [])

    def test_a_reply_padded_past_the_limit_is_refused_before_any_call(self):
        self.publish_card()
        before = len(self.sends())
        del self.native.calls[:]
        self.reply(' ' + 'x' * 1000)
        self.assertIn('1001 characters', self.sends()[-1]['text'])
        self.assertEqual(len(self.sends()), before + 1)
        self.assertEqual(self.bridge.typed_nudge.rows(), [])
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.native.calls, [])

    def test_a_reply_to_a_conversation_card_nudges_that_conversation(self):
        self.publish_card(argv=['session', 'nudge', 'yardmaster', '--for', 'pat'])
        self.native.crew = {'yardmaster': crew(state='running', status='idle',
                                               conversations={'pat': dict(state='running',
                                                                          status='blocked')})}
        self.reply('carry on with this conversation')
        sends = self.sends()
        self.assertEqual(len(sends), 2)
        confirm = sends[-1]
        self.assertIn('<b>Nudge crew member yardmaster for pat?</b>', confirm['text'])
        self.assertIn('blocked', confirm['text'])
        self.assertNotIn('idle', confirm['text'])
        self.assertIn('carry on with this conversation', confirm['text'])
        row = [row for row in self.bridge.typed_nudge.rows() if row['state'] == 'open'][0]
        self.assertEqual(row['words'], 'carry on with this conversation')
        self.poll(self.tap(row['token'], uid=80))
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.writes()[0]['target'], 'yardmaster')
        self.assertEqual(self.writes()[0]['user'], 'pat')
        self.assertEqual(self.writes()[0]['text'], 'carry on with this conversation')

    def test_a_reply_to_a_conversation_card_with_no_status_yet_shows_no_status(self):
        self.publish_card(argv=['session', 'nudge', 'yardmaster', '--for', 'pat'])
        self.native.crew = {'yardmaster': crew(state='running', status='idle',
                                               conversations={'pat': dict(state='running')})}
        self.reply('carry on with this conversation')
        sends = self.sends()
        self.assertEqual(len(sends), 2)
        confirm = sends[-1]
        self.assertIn('<b>Nudge crew member yardmaster for pat?</b>', confirm['text'])
        self.assertIn('running', confirm['text'])
        self.assertNotIn('status unknown', confirm['text'])
        self.assertNotIn('idle', confirm['text'])

    def test_a_reply_to_a_conversation_card_without_a_session_says_unknown(self):
        self.publish_card(argv=['session', 'nudge', 'yardmaster', '--for', 'pat'])
        self.native.crew = {'yardmaster': crew(state='running', status='idle')}
        self.reply('carry on with this conversation')
        sends = self.sends()
        self.assertEqual(len(sends), 2)
        confirm = sends[-1]
        self.assertIn('<b>Nudge crew member yardmaster for pat?</b>', confirm['text'])
        self.assertIn('status unknown', confirm['text'])
        self.assertNotIn('idle', confirm['text'])


if __name__ == '__main__':
    unittest.main()
