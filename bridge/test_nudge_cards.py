"""The yard's nudge cards: published from the operator's queue on each poll and their outcomes told on the card,
over fake Telegram, the SQLite ledger and the production poll order, against a fake or a real isolated core."""
from pathlib import Path
import re
import subprocess
import sys
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
import testyardr  # noqa: E402
from test_bridge import BridgeTestCase  # noqa: E402
from test_run_asks import NativeCase, proposal  # noqa: E402

n = b.run_asks
nc = b.nudge_cards
JOURNAL = 'yardmaster-journal'


def nudge(number=21, asker='yard', argv=None, **extra):
    argv = argv or ['session', 'nudge', 'yardmaster']
    return dict(dict(ask=number, turn=0, to='pat', asker=asker, since='2026-11-20T12:00:00Z',
                     question="crew member yardmaster's harness stopped on network: nudge it to go on?",
                     context=['The error line: synthetic network error', 'The harness: claude', 'Seen at 2026-11-20T17:00:00+05:00'],
                     options=[], state='open',
                     run=dict(argv=argv, command='yardr ' + ' '.join(argv), description="Type one line into an agent's pane by hand",
                              does=['types "yardr: the operator asks you to go on where you stopped." into crew member '
                                    'yardmaster (claude), past standing harness_error (network).'],
                              back='a typed line cannot be taken back', back_by='nobody')), **extra)


def event(seq, kind, data, actor='yard', bead=JOURNAL):
    return dict(seq=seq, id=str(seq), bead=bead, kind=kind, actor=actor, data=data, at='2026-11-20T12:00:01Z')


def nudged(seq, ask=21, outcome='refused', reason='the input holds text', **data):
    data = dict(dict(ask=ask, by='pat', crew='yardmaster', id='synthetic', line='go on', outcome=outcome), **data)
    if reason:
        data['reason'] = reason
    return event(seq, 'session_nudged', data)


class NudgeCase(NativeCase):
    def setUp(self):
        super().setUp()
        self.clock = [1000.0]
        self.native.beads = {
            JOURNAL: dict(id=JOURNAL, title='Journal of yardmaster', asks=[nudge()]),
            # Open asks to the operator that are not the yard's own nudge cards.
            'run-1': dict(id='run-1', title='Synthetic run', asks=[
                dict(proposal(), ask=3), nudge(4, asker='yardmaster'), nudge(5, argv=['crew', 'restart', 'yardmaster']),
                dict(nudge(6), run=None, options=[dict(label='Yes')])]),
        }
        self.native.beads['run-1']['asks'][3].pop('run')
        self.native.raw_events = []
        self.restart()
        self.logs = []

    def restart(self):
        request = self.bridge.request
        self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=request,
                               request_multipart=self.bridge.request_multipart)
        self.bridge.hook_secret = None
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

    def card(self):
        return self.bridge.run_asks.get(self.bridge.run_asks.ident(JOURNAL, 21))

    def tap(self, uid, action='approve', **message):
        row = self.card()
        message = dict(dict(message_id=row['message_id'], chat=dict(id=123, type='private'),
                            **{'from': dict(id=999, is_bot=True)}), **message)
        return dict(update_id=uid, callback_query=dict(id=f'q{uid}', data=f"runask:{row['id']}:{action}",
                                                         message=message, **{'from': dict(id=123, is_bot=False)}))

    def ask(self):
        return self.native.beads[JOURNAL]['asks'][0]


class NudgeCardTests(NudgeCase):
    def test_publishes_the_yard_nudge_card_once_in_the_paired_chat(self):
        self.poll()
        sends = self.sends()
        self.assertEqual(len(sends), 1)
        card = sends[0]
        self.assertEqual(card['chat_id'], 123)
        self.assertNotIn('message_thread_id', card)
        self.assertFalse(card['disable_notification'])
        self.assertIn("<b>crew member yardmaster's harness stopped on network: nudge it to go on?</b>", card['text'])
        self.assertIn('yardr session nudge yardmaster', card['text'])
        self.assertIn('Proposed by yard', card['text'])
        self.assertEqual([button['text'] for row in card['reply_markup']['inline_keyboard'] for button in row],
                         ['✓ Approve in test-city', '✕ Decline', '📄 Report'])
        with self.store.connect() as db:
            keys = [row[0] for row in db.execute('SELECT key FROM run_ask_keys')]
        self.assertEqual(keys, ['nudge:' + self.card()['id']])
        # Again, and after a restart: the same card is not sent twice, and nothing is written to the yard.
        self.poll()
        self.restart()
        self.poll()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.logs, [])

    def test_a_card_already_sent_by_decide_is_not_sent_again(self):
        self.native.beads.pop('run-1')
        self.bridge.run_asks.decide_open(JOURNAL, 21, '123')
        self.assertEqual(len(self.sends()), 1)
        self.poll()
        self.poll()
        self.assertEqual(len(self.sends()), 1)

    def test_approve_relays_the_outcome_once_as_a_reply_on_the_card(self):
        self.poll()
        self.native.raw_events = [event(30, 'noted', dict(note=21))]
        self.poll(self.tap(40))
        approvals = self.writes()
        self.assertEqual([m for m, _ in approvals], ['ApproveAsk'])
        self.assertEqual(approvals[0][1], dict(bead=JOURNAL, ask=21, actor='pat', weighed=False, values={}))
        # No outcome yet: nothing is said.
        self.clock[0] += 30
        self.poll()
        self.assertEqual(len(self.sends()), 1)
        self.native.raw_events += [event(31, 'session_nudge_requested', dict(approved=dict(ask=21, bead=JOURNAL)), actor='pat'),
                                   nudged(32, ask=20, outcome='typed', reason=''), nudged(33)]
        self.poll()
        reply = self.sends()[-1]
        self.assertEqual(reply['text'], '✕ Nudge refused: the input holds text')
        self.assertEqual(reply['reply_parameters']['message_id'], self.card()['message_id'])
        self.poll()
        self.restart()
        self.poll()
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual(len(self.writes()), 1)

    def test_each_outcome_reads_as_its_own_reply(self):
        for outcome, reason, said in (('typed', '', '✓ Nudge typed'),
                                      ('expired', 'session ended', '✕ Nudge expired: session ended'),
                                      ('unknown', 'look at the pane', '? Nudge unknown: look at the pane')):
            with self.subTest(outcome=outcome):
                self.assertEqual(nc.outcome_text(nudged(5, outcome=outcome, reason=reason, held='harness_error')), said)
        for bad in (nudged(5, outcome='maybe'), dict(nudged(5), actor='pat'), nudged(5, extra='field'),
                    nudged(5, reason=7)):
            with self.subTest(bad=bad), self.assertRaisesRegex(n.NativeError, 'invalid native session_nudged'):
                nc.outcome_text(bad)

    def test_an_unreadable_outcome_is_said_as_unreadable_once(self):
        self.poll()
        self.poll(self.tap(40))
        self.native.raw_events = [nudged(33, outcome='maybe')]
        self.poll()
        self.poll()
        self.assertEqual(self.sends()[-1]['text'],
                         "? Nudge outcome unreadable (invalid native session_nudged event); look at the agent's pane.")
        self.assertEqual(len(self.sends()), 2)

    def test_no_outcome_within_ten_minutes_is_said_as_unknown_and_not_retried(self):
        self.poll()
        self.poll(self.tap(40))
        # The ten minutes run from the first refresh that finds the card approved.
        self.poll()
        self.clock[0] += nc.FOLLOW_SECONDS
        self.poll()
        self.assertEqual(len(self.sends()), 1)
        self.clock[0] += 1
        self.poll()
        self.assertEqual(self.sends()[-1]['text'], nc.SILENT)
        # An outcome logged later is not told: the card said unknown once, and nothing is asked again.
        self.native.raw_events = [nudged(33, outcome='typed', reason='')]
        self.poll()
        self.assertEqual(len(self.sends()), 2)
        self.assertEqual(len(self.writes()), 1)

    def test_a_lost_approval_reply_gives_no_second_approval(self):
        self.poll()
        self.native.mode = 'lost-commit'
        self.poll(self.tap(40))
        self.native.mode = None
        self.assertEqual(self.ask()['state'], 'answered')
        self.native.raw_events = [nudged(33, outcome='typed', reason='')]
        self.poll(self.tap(40), self.tap(41))
        self.assertEqual([m for m, _ in self.writes()], ['ApproveAsk'])
        self.assertEqual(self.sends()[-1]['text'], '✓ Nudge typed')

    def test_a_card_known_unsent_is_sent_again_with_the_same_key_after_restart(self):
        with patch.object(self.bridge, '_send', side_effect=b.RequestNotSent('synthetic unavailable before send')):
            self.poll()
        saved = self.store.saved_delivery(self.bridge.run_asks.send_key(self.card()))[1]
        self.assertTrue(b.resendable(saved))
        self.assertEqual(self.logs, ['nudge cards: yardmaster-journal#21 not sent yet: transport_unavailable_not_sent'])
        self.restart()
        self.poll()
        self.poll()
        self.assertEqual(len(self.sends()), 1)
        self.assertIsNotNone(self.card()['message_id'])
        with self.store.connect() as db:
            self.assertEqual([r[0] for r in db.execute('SELECT key FROM run_ask_keys')], ['nudge:' + self.card()['id']])

    def test_a_confirmed_card_is_bound_from_the_ledger_after_a_crash_without_a_second_send(self):
        save = self.bridge.run_asks.save

        def interrupted(row):
            if row['message_id'] is not None:
                raise RuntimeError('synthetic crash after the receipt, before the row binding')
            return save(row)
        with patch.object(self.bridge.run_asks, 'save', side_effect=interrupted), \
                self.assertRaisesRegex(RuntimeError, 'synthetic crash'):
            self.poll()
        receipt = self.store.saved_delivery(self.bridge.run_asks.send_key(self.card()))[1]
        self.assertTrue(receipt['delivered'])
        self.assertIsNone(self.card()['message_id'])
        self.restart()
        self.poll()
        self.poll()
        self.assertEqual(self.card()['message_id'], int(receipt['message_id']))
        self.assertEqual(len(self.sends()), 1)
        self.poll(self.tap(40))
        self.assertEqual([m for m, _ in self.writes()], ['ApproveAsk'])

    def test_an_ambiguous_card_send_is_not_sent_again(self):
        with patch.object(self.bridge, '_send', side_effect=b.TransportError('synthetic lost reply')):
            self.poll()
        self.poll()
        self.restart()
        self.poll()
        self.assertEqual(len(self.logs), 1)
        self.assertRegex(self.logs[0], r'card not confirmed sent: ambiguous_delivery_requires_manual_reconciliation')
        self.assertIsNone(self.card()['message_id'])

    def test_a_known_unsent_outcome_is_sent_again_unchanged_without_another_approval(self):
        self.poll()
        self.poll(self.tap(40))
        self.native.raw_events = [nudged(33, outcome='typed', reason='')]
        with patch.object(self.bridge, '_send', side_effect=b.RequestNotSent('synthetic unavailable before send')):
            self.poll()
        ident = self.card()['id']
        self.assertTrue(b.resendable(self.store.saved_delivery(f'nudge:{ident}:outcome:0')[1]))
        self.assertEqual(self.bridge.nudge_cards.get(ident)['state'], 'telling')
        # The outcome told is the one chosen, even once the window has passed and the journal reads otherwise.
        self.native.raw_events = []
        self.restart()
        self.clock[0] += nc.FOLLOW_SECONDS + 1
        self.poll()
        self.poll()
        self.assertEqual([s['text'] for s in self.sends()[1:]], ['✓ Nudge typed'])
        self.assertEqual(self.bridge.nudge_cards.get(ident)['state'], 'done')
        self.assertEqual(len(self.writes()), 1)

    def test_an_unread_queue_does_not_stop_following_an_approved_card(self):
        self.poll()
        self.poll(self.tap(40))
        self.poll()
        self.clock[0] += nc.FOLLOW_SECONDS + 1
        self.native.raw_queue = dict(open=[dict(bead=JOURNAL)], decided=[])
        self.poll()
        self.assertEqual(self.sends()[-1]['text'], nc.SILENT)
        self.assertEqual(self.logs, ['nudge cards: operator queue unread: invalid native UserAsks response'])

    def test_an_unread_queue_does_not_close_an_unsent_card(self):
        with patch.object(self.bridge, '_send', side_effect=b.RequestNotSent('synthetic unavailable before send')):
            self.poll()
        self.native.raw_queue = dict(open=[dict(bead=JOURNAL)], decided=[])
        self.poll()
        self.assertEqual(self.bridge.nudge_cards.get(self.card()['id'])['state'], 'open')
        self.native.raw_queue = None
        self.poll()
        self.assertEqual(len(self.sends()), 1)

    def test_a_revoked_operator_stops_following(self):
        self.poll()
        self.poll(self.tap(40))
        self.poll()
        self.clock[0] += nc.FOLLOW_SECONDS + 1
        self.config['user_id'] = 999
        calls = len(self.native.calls)
        self.poll()
        self.assertEqual([m for m, _, _ in self.native.calls[calls:] if m not in ('GetUser',)], [])
        self.assertEqual(len(self.sends()), 1)
        self.assertIn('nudge cards: operator queue unread: operator_not_authorized', self.logs)

    def test_the_outcome_is_found_behind_a_busy_journal(self):
        self.poll()
        self.poll(self.tap(40))
        self.native.raw_events = [event(30, 'session_nudge_requested', dict(approved=dict(ask=21, bead=JOURNAL)), actor='pat'),
                                  nudged(31, outcome='typed', reason='')]
        self.native.raw_events += [event(i, 'updated', {}) for i in range(32, 2100)]
        self.poll()
        self.assertEqual(self.sends()[-1]['text'], '✓ Nudge typed')
        limits = [a[1] for m, a, _ in self.native.calls if m == 'Events']
        self.assertEqual(limits[-1], 4096)

    def test_an_unread_journal_is_never_said_as_unknown(self):
        self.poll()
        self.poll(self.tap(40))
        self.poll()
        self.clock[0] += nc.FOLLOW_SECONDS + 1
        client = self.bridge.run_asks.client
        call = client.call

        def journal_unavailable(method, args):
            if method == 'Events':
                raise n.RPCAmbiguous('native Events response uncertain')
            return call(method, args)
        with patch.object(client, 'call', side_effect=journal_unavailable):
            self.poll()
            self.poll()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.logs, ['nudge cards: yardmaster-journal#21 outcome unread: native Events response uncertain'])
        self.native.raw_events = [nudged(33, outcome='typed', reason='')]
        self.poll()
        self.assertEqual(self.sends()[-1]['text'], '✓ Nudge typed')

    def test_taps_that_are_not_the_operators_write_nothing(self):
        self.poll()
        sender = self.tap(50)
        sender['callback_query']['from'] = dict(id=124, is_bot=False)
        chat = self.tap(51, chat=dict(id=124, type='private'))
        topic = self.tap(52, is_topic_message=True, message_thread_id=7)
        forwarded = self.tap(53, forward_origin=dict(type='user'))
        self.poll(sender, chat, topic, forwarded)
        self.assertEqual(self.writes(), [])
        self.poll(self.tap(54))
        self.poll(self.tap(54))
        self.assertEqual(len(self.writes()), 1)

    def test_a_taken_back_ask_closes_its_card_and_is_not_followed(self):
        self.poll()
        self.ask().update(state='taken back', by='yard', words='the harness error of yardmaster is over')
        self.poll()
        edit = [body for url, body in self.calls if url.endswith('/editMessageText')][-1]
        self.assertIn('✕ Taken back by yard: the harness error of yardmaster is over', edit['text'])
        self.assertEqual(edit['reply_markup'], {'inline_keyboard': []})
        self.native.raw_events = [nudged(33, outcome='typed', reason='')]
        self.clock[0] += 2 * nc.FOLLOW_SECONDS
        self.poll()
        self.assertEqual(len(self.sends()), 1)
        self.assertEqual(self.writes(), [])

    def test_a_declined_card_is_not_followed(self):
        self.poll()
        self.poll(self.tap(40, 'decline'))
        self.assertEqual([m for m, _ in self.writes()], ['DeclineAsk'])
        self.clock[0] += 2 * nc.FOLLOW_SECONDS
        self.poll()
        self.assertEqual(len(self.sends()), 1)

    def test_a_queue_without_nudge_asks_sends_nothing_and_says_nothing(self):
        self.native.beads.pop(JOURNAL)
        self.poll()
        self.poll()
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.logs, [])
        self.assertEqual([m for m, _, _ in self.native.calls if m not in ('GetUser', 'UserAsks')], [])

    def test_an_unreadable_queue_is_logged_once_while_it_stands(self):
        self.native.raw_queue = dict(open=[dict(bead=JOURNAL)], decided=[])
        self.poll()
        self.poll()
        self.assertEqual(self.logs, ['nudge cards: operator queue unread: invalid native UserAsks response'])
        self.native.raw_queue = None
        self.poll()
        self.assertEqual(len(self.sends()), 1)
        self.native.raw_queue = dict(open=[dict(bead=JOURNAL)], decided=[])
        self.poll()
        self.assertEqual(len(self.logs), 2)

    def test_a_card_telegram_cannot_show_whole_is_refused_once(self):
        self.ask()['run']['does'] = ['x' * 4000]
        self.poll()
        self.poll()
        self.assertEqual(self.sends(), [])
        self.assertEqual(len(self.logs), 1)
        self.assertRegex(self.logs[0], r'^nudge cards: yardmaster-journal#21 refused: run_ask_payload_too_large_for_card')
        bead_reads = [a for m, a, _ in self.native.calls if m == 'Bead']
        self.assertEqual(len(bead_reads), 1)

    def test_refresh_runs_after_the_run_cards_and_before_the_updates(self):
        order = []
        for name, target in (('run_asks', self.bridge.run_asks), ('nudge_cards', self.bridge.nudge_cards),
                             ('typed_nudge', self.bridge.typed_nudge),
                             ('siding_cards', self.bridge.siding_cards), ('model_switch', self.bridge.model_switch)):
            patch.object(target, 'refresh', side_effect=lambda name=name: order.append(name)).start()
        self.addCleanup(patch.stopall)
        with patch.object(self.bridge, 'drain', side_effect=lambda updates: order.append('drain')):
            self.poll()
        self.assertEqual(order, ['run_asks', 'nudge_cards', 'typed_nudge', 'siding_cards', 'drain', 'model_switch'])

    def test_the_preview_renders_the_card_and_its_replies_as_synthetic(self):
        result = subprocess.run([sys.executable, '-B', str(Path(__file__).resolve().parents[1] / 'scripts/preview_nudge_cards.py')],
                                capture_output=True, text=True, timeout=120)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertIn('Synthetic data', result.stdout)
        self.assertEqual(len(re.findall('<section>', result.stdout)), 5)
        for said in ('✓ Approve in test-city', 'nudge requested: yardmaster', '✓ Nudge typed', '✕ Nudge refused: the input holds text'):
            self.assertIn(said, result.stdout)

    def test_without_native_keys_nothing_is_read(self):
        bridge = b.Bridge(dict(self.config, yardr_home=None, yardr_operator=None, yardr_cards=False),
                          self.store, 'TOKEN', 'CAPABILITY', request=self.transport)
        calls = len(self.native.calls)
        bridge.nudge_cards.refresh()
        self.assertEqual(len(self.native.calls), calls)


SOURCE = ('name: tgbridge\nbase: https://example.invalid\ncalls:\n  health:\n    kind: read\n    request: {method: GET, path: /health}\n'
          'hook:\n  bodies: unique\n  verify:\n    scheme: secret\n    signature: {header: X-Secret}\n  normalize: [/bin/false]\n')


class RealNudgeTests(BridgeTestCase):
    """A real isolated core; the daemon's own writes (the card, the nudge's outcome, the take-back) come from an offline
    store fixture built from TGBRIDGE_TEST_YARDR_SOURCE between restarts, as no socket caller may make them."""

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
        self.cli('bead', 'create', 'Synthetic run', '--id', 'run-1')
        self.cli('bead', 'ask', 'run-1', 'Lower the test limit?', '--to', 'pat', '--context', 'Synthetic ordinary run card',
                 '--run', 'source set tgbridge calls_per_hour=60', actor='proposer')
        self.config.update(yardr_home=str(self.home), yardr_operator='pat', yardr_source='tgbridge',
                           yardr_cards=True)
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

    def tap(self, uid, row, action='approve', **message):
        message = dict(dict(message_id=row['message_id'], chat=dict(id=123, type='private'),
                            **{'from': dict(id=999, is_bot=True)}), **message)
        return dict(update_id=uid, callback_query=dict(id=f'q{uid}', data=f"runask:{row['id']}:{action}",
                                                         message=message, **{'from': dict(id=123, is_bot=False)}))

    def requested(self, seq):
        return [e for e in self.client.call('Events', [JOURNAL, 200])
                if e['kind'] == 'session_nudge_requested' and e['data'].get('approved', {}).get('ask') == seq]

    def test_real_native_nudge_card_is_sent_once_approved_once_and_its_outcome_told(self):
        self.start()
        if not testyardr.serves(self.home, 'NudgeSession'):
            self.skipTest('this core has no session nudge: its yard files no nudge card')
        self.fixture = testyardr.store_fixture(self.root, 'nudge')
        if self.fixture is None:
            self.skipTest('TGBRIDGE_TEST_YARDR_SOURCE names no candidate source for the daemon-write fixture')
        journal, seq = self.daemon('file', 'yardmaster', 'pat')
        self.assertEqual(journal, JOURNAL)
        seq = int(seq)
        self.poll()
        self.assertEqual(len(self.sends()), 1)
        card = self.sends()[0]
        self.assertIn("<b>crew member yardmaster's harness stopped on network: nudge it to go on?</b>", card['text'])
        self.assertIn('Proposed by yard', card['text'])
        self.assertIn('<pre>yardr session nudge yardmaster</pre>', card['text'])
        # Published once across a restart of the bridge and of the core.
        testyardr.stop(self.server)
        self.server = testyardr.restart(self.root, self.binary, self.home, self.command, self.env)
        self.restart_bridge()
        self.poll()
        self.poll()
        self.assertEqual(len(self.sends()), 1)
        row = self.bridge.run_asks.get(self.bridge.run_asks.ident(JOURNAL, seq))
        self.assertEqual(row['message_id'], 77)

        # Taps that are not the operator's on this card write nothing.
        head = self.client.call('Head', [])
        sender = self.tap(40, row)
        sender['callback_query']['from'] = dict(id=124, is_bot=False)
        self.poll(sender, self.tap(41, row, chat=dict(id=124, type='private')),
                  self.tap(42, row, is_topic_message=True, message_thread_id=7),
                  self.tap(43, row, forward_origin=dict(type='user')))
        self.assertEqual(self.client.call('Head', []), head)
        self.assertEqual(self.requested(seq), [])

        # The approval's reply is lost after the core committed it: no second approval, by replay or by a new tap.
        call = self.bridge.run_asks.client.call

        def lost(method, args):
            value = call(method, args)
            if method == 'ApproveAsk':
                raise n.RPCAmbiguous('native ApproveAsk response uncertain')
            return value
        with patch.object(self.bridge.run_asks.client, 'call', side_effect=lost):
            self.poll(self.tap(44, row))
        self.assertEqual(len(self.requested(seq)), 1)
        ask = next(a for a in self.client.call('Bead', dict(bead=JOURNAL, viewer='pat'))['asks'] if a['ask'] == seq)
        self.assertEqual((ask['state'], ask['ran']['result']), ('answered', 'nudge requested: yardmaster'))
        head = self.client.call('Head', [])
        self.poll(self.tap(44, row), self.tap(45, row))
        self.assertEqual(self.client.call('Head', []), head)
        self.assertEqual(len(self.requested(seq)), 1)
        edits = [body for url, body in self.calls if url.endswith('/editMessageText')]
        self.assertIn('✓ approved, ran: yardr session nudge yardmaster', edits[-1]['text'])
        self.assertEqual(len(self.sends()), 1)

        # The daemon finishes the request; its outcome is told once, as a reply on the card.
        self.daemon('finish', 'yardmaster', 'refused', 'the input holds text')
        self.poll()
        self.poll()
        self.assertEqual(len(self.sends()), 2)
        reply = self.sends()[-1]
        self.assertEqual(reply['text'], '✕ Nudge refused: the input holds text')
        self.assertEqual(reply['reply_parameters']['message_id'], row['message_id'])

        # A card the yard takes back closes, and nothing is followed for it.
        journal, again = self.daemon('file', 'yardmaster', 'pat')
        self.poll()
        self.assertEqual(len(self.sends()), 3)
        second = self.bridge.run_asks.get(self.bridge.run_asks.ident(JOURNAL, int(again)))
        self.daemon('takeback', 'yardmaster', again)
        self.poll()
        edit = [body for url, body in self.calls if url.endswith('/editMessageText')][-1]
        self.assertEqual(edit['message_id'], second['message_id'])
        self.assertIn('✕ Taken back by yard: the harness error of yardmaster is over', edit['text'])
        self.assertEqual(edit['reply_markup'], {'inline_keyboard': []})
        self.poll()
        self.assertEqual(len(self.sends()), 3)
        self.assertEqual([line for line in self.logs if 'nudge' in line], [])
        print('Native nudge card:', self.binary, 'sent once, approved once, outcome told, taken back')

    def test_real_native_core_without_yard_nudge_asks_sends_no_card_and_says_nothing(self):
        # On a core without the row (unmodified upstream) the yard files no such ask; an ordinary run ask stays /decide's.
        self.start()
        self.poll()
        self.poll()
        self.assertEqual(self.sends(), [])
        self.assertEqual(self.logs, [])
        self.assertEqual(self.bridge.run_asks.rows(), [])
        print('Native nudge cards absent:', self.binary, 'no card, no log')


if __name__ == '__main__':
    unittest.main()
