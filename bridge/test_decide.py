"""/decide: the operator's open canonical decisions listed and opened as the
same native cards, over fake Telegram and a fake or real isolated core."""
import copy
from pathlib import Path
import shutil
import subprocess
import sys
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
from test_bridge import BridgeTestCase, write_settings  # noqa: E402
from test_run_asks import NativeCase, NativeSocket, decision, group_add_proposal, pack_proposal, repack, source_proposal, proposal  # noqa: E402

n = b.run_asks
OPS = 7


def ask(number, question, to='pat', **extra):
    return dict(dict(ask=number, turn=0, to=to, asker='proposer', since='2026-01-01T00:00:00Z', question=question,
                     context=['Synthetic context'], options=[], state='open'), **extra)


def beads():
    run = dict(proposal(), ask=3)
    files = dict(proposal(), ask=5)
    files['run'] = dict(files['run'], argv=['source', 'add', 'x.yaml'], command='yardr source add x.yaml')
    return {
        'sample-1': dict(id='sample-1', title='Synthetic change', asks=[
            decision(), ask(8, 'Words only?'), ask(9, 'Not for pat?', to='sam'),
            ask(10, 'Already answered?', state='answered', options=decision()['options'], option=1, words='done'),
            ask(11, 'Put aside?', state='put aside', return_at='2026-01-02T00:00:00Z'),
            ask(12, 'Taken back?', state='taken back', by='proposer', words='moot')]),
        'run-1': dict(id='run-1', title='Synthetic run', asks=[run]),
        'secret-1': dict(id='secret-1', title='Synthetic secret', asks=[ask(2, 'Which token?', secret=dict(names=['TOKEN']))]),
        'weighted-1': dict(id='weighted-1', title='Synthetic weight', asks=[
            ask(4, 'Map the identity?', weight=[{'class': 'trust', 'line': 'maps a person'}])]),
        'file-1': dict(id='file-1', title='Synthetic file', asks=[files]),
    }


class DecideCase(NativeCase):
    def setUp(self):
        super().setUp()
        self.config['topics'] = (dict(name='ops', agent='yardmaster'),)
        self.store.record_topic('ops', OPS, 'created')
        self.restart()
        self.native.beads = beads()

    def restart(self):
        request = self.bridge.request
        self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=request)
        self.bridge.hook_secret = None

    def command(self, value, uid, thread=None, **changes):
        message = dict(message_id=500 + uid, text=value, chat=dict(id=123, type='private'),
                       **{'from': dict(id=123, is_bot=False)})
        if thread is not None:
            message.update(is_topic_message=True, message_thread_id=thread)
        message.update(changes)
        self.bridge.inbound(dict(update_id=uid, message=message))

    def tap(self, data, message_id, uid, thread=None, sender=123, **changes):
        message = dict(message_id=message_id, chat=dict(id=123, type='private'), **{'from': dict(id=999, is_bot=True)})
        if thread is not None:
            message.update(is_topic_message=True, message_thread_id=thread)
        message.update(changes)
        query = dict(id=f'q{uid}', data=data, message=message, **{'from': dict(id=sender, is_bot=False)})
        self.bridge.inbound(dict(update_id=uid, callback_query=query))

    def sends(self):
        return [body for url, body in self.calls if url.endswith('/sendMessage')]

    def answers(self):
        return [body.get('text', '') for url, body in self.calls if url.endswith('/answerCallbackQuery')]

    def listing(self, uid=1, thread=None):
        self.command('/decide', uid, thread)
        body = self.sends()[-1]
        return body, self.sent_count + 76

    def buttons(self, body):
        return [button for row in body['reply_markup']['inline_keyboard'] for button in row]

    def reads(self, method='UserAsks'):
        return [args for name, args, _ in self.native.calls if name == method]


class DecideTests(DecideCase):
    def test_lists_open_decisions_of_the_operator_with_ids_kinds_and_buttons(self):
        body, _ = self.listing()
        text = body['text']
        self.assertNotIn('parse_mode', body)
        self.assertTrue(text.startswith('6 open decisions in test-city for pat\nAn ID is bead#ask'))
        for line in ('1. Which token?\nsecret · secret-1#2 · Synthetic secret · asked by proposer\nNot complete in Telegram',
                     '2. Change the test?\nrun · run-1#3 · Synthetic run',
                     '3. Map the identity?\nweighted decision · weighted-1#4',
                     '4. Change the test?\nterminal-only run · file-1#5',
                     '5. Where should the links go?\ndecision · sample-1#7 · Synthetic change',
                     '6. Words only?\ndecision · sample-1#8'):
            self.assertIn(line, text)
        for absent in ('Not for pat?', 'Already answered?', 'Put aside?', 'Taken back?'):
            self.assertNotIn(absent, text)
        labels = [button['text'] for button in self.buttons(body)]
        self.assertEqual(labels, ['2. run-1#3', '5. sample-1#7', '6. sample-1#8', '↻ Refresh'])
        self.assertTrue(all(len(button['callback_data'].encode()) <= 64 for button in self.buttons(body)))
        self.assertEqual(self.reads(), [dict(name='pat', viewer='pat')])
        self.assertEqual([m for m, _, _ in self.native.calls][:2], ['GetUser', 'UserAsks'])
        self.assertEqual(self.writes(), [])

    def test_tap_and_id_open_exactly_the_notification_card_once(self):
        body, list_id = self.listing()
        data = next(b['callback_data'] for b in self.buttons(body) if b['text'] == '5. sample-1#7')
        self.tap(data, list_id, 2)
        card = self.sends()[-1]
        self.assertEqual(self.answers()[-1], 'Card sent')
        self.assertEqual(self.writes(), [])
        # The same card the notification path sends, controls and all.
        rows = self.bridge.run_asks.rows()
        self.assertEqual(len(rows), 1)
        with self.store.connect() as db:
            for table in ('run_asks', 'run_ask_keys', 'deliveries'):
                db.execute('DELETE FROM ' + table)
        self.assertTrue(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='notify'))['delivered'])
        notified = self.sends()[-1]
        self.assertEqual({k: card[k] for k in ('text', 'reply_markup', 'disable_notification')},
                         {k: notified[k] for k in ('text', 'reply_markup', 'disable_notification')})
        sent = self.sent_count
        card_id = self.bridge.run_asks.rows()[0]['message_id']
        # Asking again, by tap or by ID, before and after a restart, sends no second live card.
        self.tap(data, list_id, 3)
        self.command('/decide sample-1#7', 4)
        self.restart()
        self.command('/decide   sample-1#7  ', 5)
        notices = self.sends()[-3:]
        self.assertEqual(self.sent_count, sent + 3)  # three notices, never a card
        self.assertEqual(self.answers()[-1], 'The card for sample-1#7 is already open.')
        for notice in notices:
            self.assertEqual(notice['text'], 'The card for sample-1#7 is already open.')
            self.assertEqual(notice['reply_parameters'], {'message_id': card_id})
        transport = self.bridge.request

        def refused(url, body, *args, **kwargs):
            if url.endswith('/sendMessage') and 'reply_parameters' in body:
                self.calls.append((url, body))
                return 400, dict(ok=False, error_code=400, description=description)
            return transport(url, body, *args, **kwargs)
        self.bridge.request = refused
        for uid, description in ((8, 'Bad Request: message to be replied not found'),
                                 (9, 'Bad Request: message is too old')):
            with self.subTest(quote_refused=description):
                before = len(self.sends())
                def select():
                    if uid == 8:
                        self.command('/decide sample-1#7', uid)
                    else:
                        self.tap(data, list_id, uid)
                select()
                attempts = self.sends()[before:]
                self.assertEqual(len(attempts), 2)
                self.assertEqual(attempts[0], dict(attempts[1], reply_parameters={'message_id': card_id}))
                self.assertEqual(attempts[1]['text'], 'The card for sample-1#7 is already open.')
                self.assertNotIn('reply_parameters', attempts[1])
                self.assertNotIn('reply_markup', attempts[1])
                self.assertTrue(self.store.saved_delivery(f'decide:{uid}:notice:0')[1]['delivered'])
                self.restart()
                select()
                self.assertEqual(len(self.sends()), before + 2)
        before = len(self.sends())
        with patch.object(self.store, 'finish', side_effect=RuntimeError('interrupted before receipt')):
            with self.assertRaisesRegex(RuntimeError, 'interrupted before receipt'):
                self.command('/decide sample-1#7', 10)
        self.assertEqual(len(self.sends()), before + 2)
        self.restart()
        self.command('/decide sample-1#7', 10)
        self.assertEqual(len(self.sends()), before + 2)
        self.assertEqual(len(self.bridge.run_asks.rows()), 1)
        self.bridge.request = transport
        # A receipt committed before its generation row (a crash, or a notification mid-save):
        # listing recovers the generation outside the decide_ids write and sends no card.
        row = self.bridge.run_asks.rows()[0]
        row['generations'], row['message_id'] = [], None
        with self.store.connect() as db:
            db.execute('UPDATE run_asks SET data=?', (n.canonical(row),))
        connect = b.sqlite3.connect
        with patch.object(b.sqlite3, 'connect', lambda path, timeout: connect(path, timeout=0.2)):
            self.listing(7)
        self.assertTrue(self.sends()[-1]['text'].startswith('6 open decisions'))
        self.assertEqual(self.sent_count, sent + 7)
        self.assertEqual([g['message_id'] for g in self.bridge.run_asks.rows()[0]['generations']], [card_id])
        # The card opened by /decide answers through the existing native path.
        self.press('pick2', uid=6)
        self.assertEqual(self.writes()[-1][0], 'AnswerAskExpected')
        self.assertEqual(self.native.beads['sample-1']['asks'][0]['state'], 'answered')

    def test_run_card_opens_by_id_and_unsupported_kinds_refuse_without_buttons(self):
        self.command('/decide run-1#3', 1)
        card = self.sends()[-1]
        self.assertIn('yardr depot test web', card['text'])
        self.assertEqual([b['text'] for b in self.buttons(card)], ['✓ Approve in test-city', '✕ Decline'])
        for uid, (ident, kind) in enumerate((('secret-1#2', 'secret'), ('weighted-1#4', 'weighted decision'),
                                            ('file-1#5', 'terminal-only run')), 2):
            self.command('/decide ' + ident, uid)
            notice = self.sends()[-1]
            self.assertEqual(notice['text'], f'{ident} is a {kind} ask a Telegram card cannot show whole; '
                                             'use Footplate or the yard cards.')
            self.assertNotIn('reply_markup', notice)
        self.assertEqual(len(self.bridge.run_asks.rows()), 1)
        self.assertEqual(self.writes(), [])
        # An ordinary decision whose card would pass the Telegram cap is listed without a button and never sent.
        self.native.beads = {'big-1': dict(id='big-1', title='Synthetic big', asks=[
            ask(6, 'Too long?', context=['x' * 3600])])}
        body, list_id = self.listing(uid=10)
        self.assertIn('1. Too long?\ndecision · big-1#6 · Synthetic big · asked by proposer\nNot complete in Telegram',
                      body['text'])
        self.assertEqual([b['text'] for b in self.buttons(body)], ['↻ Refresh'])
        sent = self.sent_count
        self.command('/decide big-1#6', 11)
        self.assertEqual(self.sends()[-1]['text'], 'big-1#6 is a decision ask a Telegram card cannot show whole; '
                                                   'use Footplate or the yard cards.')
        self.assertEqual(self.sent_count, sent + 1)
        self.assertEqual(len(self.bridge.run_asks.rows()), 1)
        self.assertEqual(self.writes(), [])
        # Typed bundles open whole through the same listing and selection path.
        for index, (kind, factory, question, command) in enumerate((
                ('group', group_add_proposal, 'Create the synthetic builders?', 'yardr group add builders'),
                ('source', source_proposal, 'Review the synthetic source?', 'yardr source add /synthetic/source-replace.yaml'))):
            with self.subTest(bundle=kind):
                bead = kind + '-1'
                self.native.beads = {bead: dict(id=bead, title='Synthetic ' + kind, asks=[dict(factory(), ask=13)])}
                body, _ = self.listing(uid=20 + 2*index)
                self.assertIn(f'1. {question}\nrun · {bead}#13 · Synthetic {kind}', body['text'])
                self.assertEqual([b['text'] for b in self.buttons(body)], [f'1. {bead}#13', '↻ Refresh'])
                sent = len(self.sends())
                self.command('/decide ' + bead + '#13', 21 + 2*index)
                row = self.bridge.run_asks.get(self.bridge.run_asks.ident(bead, 13))
                parts, sends = row['bundle']['parts'], self.sends()[sent:]
                self.assertEqual([body['text'] for body in sends[:-1]], [part['html'] for part in parts])
                self.assertIn('Part 1/', sends[0]['text'])
                self.assertIn('UNTRUSTED ' + kind.upper() + ' REVIEW', sends[0]['text'])
                self.assertEqual(sends[-1]['reply_parameters']['message_id'], parts[-1]['message_id'])
                self.assertIn(command, sends[-1]['text'])
                self.assertEqual([b['text'] for b in self.buttons(sends[-1])], ['✓ Approve in test-city', '✕ Decline'])
                self.assertEqual(self.writes(), [])

        unsupported = []
        for kind in ('git', 'dir'):
            pending = dict(proposal(), ask=13, question='Apply the depot pack?')
            pending['run'] = dict(argv=['pack', 'apply', 'starter'], command='yardr pack apply starter',
                description='Import a pack into the yard: set the yard to what the pack says',
                does=[f'pack starter (embedded in yardr test), of kind {kind}, into depot web: synthetic plan'],
                back='yardr pack apply starter', back_by='the operator',
                pack=dict(name='starter', kind=kind, label='embedded in yardr test', depot='web',
                          plan={}, mappings=[], summary=[], files=[]))
            unsupported.append((kind, pending))
        pending = dict(pack_proposal(), ask=13)
        manifest = next(f for f in pending['run']['pack']['directory']['files'] if f['path'] == 'pack.yaml')
        manifest['text'] += 'future: true\n'
        repack(pending)
        unsupported.append(('manifest', pending))
        pending = dict(source_proposal(), ask=13)
        pending['run']['carried'][0]['future'] = True
        unsupported.append(('source', pending))
        for index, (kind, pending) in enumerate(unsupported):
            with self.subTest(unsupported_bundle=kind):
                self.reset_native(pending)
                self.native.beads = {
                    'sample-1': dict(id='sample-1', title='Synthetic decision', asks=[decision()]),
                    'file-1': dict(id='file-1', title='Synthetic unsupported bundle', asks=[pending]),
                }
                body, list_id = self.listing(uid=30 + 3*index)
                self.assertTrue(body['text'].startswith('2 open decisions in test-city for pat\n'), body['text'])
                self.assertIn('Where should the links go?\ndecision · sample-1#7', body['text'])
                self.assertIn(pending['question'], body['text'])
                self.assertIn('file-1#13 · Synthetic unsupported bundle · asked by proposer\n'
                              'Not complete in Telegram; use Footplate or the yard cards.', body['text'])
                self.assertEqual([b['text'] for b in self.buttons(body)], ['1. sample-1#7', '↻ Refresh'])
                self.assertEqual(self.bridge.run_asks.rows(), [])
                sent = self.sent_count
                receipt = self.bridge.api_run_ask(dict(bead='file-1', ask=13, key='unsupported-' + kind))
                self.assertFalse(receipt['delivered'], receipt)
                self.assertTrue(receipt['reason'].endswith(n.FALLBACK), receipt)
                if kind != 'source':
                    self.assertIn('use yardr pack apply <dir> in a terminal', receipt['reason'])
                self.assertEqual(self.sent_count, sent)
                self.command('/decide file-1#13', 31 + 3*index)
                self.assertIn('ask a Telegram card cannot show whole', self.sends()[-1]['text'])
                self.assertNotIn('reply_markup', self.sends()[-1])
                self.tap(self.buttons(body)[0]['callback_data'], list_id, 32 + 3*index)
                self.assertEqual(self.answers()[-1], 'Card sent')
                self.assertIn('Where should the links go?', self.sends()[-1]['text'])
                self.assertTrue(self.buttons(self.sends()[-1]))
                self.assertEqual(self.writes(), [])

    def test_stale_selection_reports_the_canonical_outcome_and_never_reopens(self):
        body, list_id = self.listing()
        data = {b['text']: b['callback_data'] for b in self.buttons(body)}
        asks = self.native.beads['sample-1']['asks']
        asks[0].update(state='answered', option=2, words='keep them', turn=1)
        asks[1].update(state='put aside', return_at='2026-01-02T00:00:00Z', turn=1)
        self.native.beads['run-1']['asks'][0].update(state='taken back', by='proposer', words='not needed')
        self.tap(data['5. sample-1#7'], list_id, 2)
        self.assertEqual(self.sends()[-1]['text'], 'Ask sample-1#7 is no longer open: ✓ Answered: 2. Keep\nkeep them')
        self.assertEqual(self.answers()[-1], 'Ask sample-1#7 is no longer open: ✓ Answered: 2. Keep\nkeep them')
        self.tap(data['6. sample-1#8'], list_id, 3)
        self.assertEqual(self.sends()[-1]['text'], 'Ask sample-1#8 is no longer open: ⏳ Put aside until 2026-01-02T00:00:00Z')
        self.command('/decide run-1#3', 4)
        self.assertEqual(self.sends()[-1]['text'], 'Ask run-1#3 is no longer open: ✕ Taken back by proposer: not needed')
        self.native.beads['run-1']['asks'][0].update(state='answered', ran=dict(
            argv=['depot', 'test', 'web'], result='operation failed: synthetic', failed=True))
        self.command('/decide run-1#3', 41)
        self.assertEqual(self.sends()[-1]['text'], 'Ask run-1#3 is no longer open: ✕ approved, failed: '
                                                   'yardr depot test web\noperation failed: synthetic')
        self.native.beads['run-1']['asks'][0]['ran'] = dict(argv=['depot', 'test', 'web'], result=n.PENDING,
                                                            pending_reason='waits for the depot lock')
        self.command('/decide run-1#3', 42)
        self.assertEqual(self.sends()[-1]['text'], 'Ask run-1#3 is no longer open: ⏳ approved, to be carried out: '
                                                   f'yardr depot test web\n{n.PENDING}\nwaits for the depot lock')
        self.command('/decide sample-1#12', 5)
        self.assertEqual(self.sends()[-1]['text'], 'Ask sample-1#12 is no longer open: ✕ Taken back by proposer: moot')
        asks[1]['state'] = 'declined'
        self.command('/decide sample-1#8', 6)
        self.assertEqual(self.sends()[-1]['text'], 'Ask sample-1#8 is no longer open: ✕ Declined: ')
        self.assertEqual(self.bridge.run_asks.rows(), [])
        self.assertEqual(self.writes(), [])
        # A refresh of the stale list drops what is no longer open.
        self.tap(data['↻ Refresh'], list_id, 7)
        edit = self.edits()[-1]
        self.assertEqual(edit['message_id'], list_id)
        self.assertTrue(edit['text'].startswith('3 open decisions in test-city for pat'))
        self.assertNotIn('sample-1#7', edit['text'])
        # A list from another binding, or tampered, is out of date rather than guessed.
        self.tap('decide:o:' + 'a' * 24, list_id, 8)
        self.assertEqual(self.answers()[-1], 'This list is out of date; send /decide again')
        # The same store re-pointed at another yard where sample-1#7 is open: the old button opens nothing.
        other_home = Path(self.tmp.name) / 'switched-yard'
        other_home.mkdir(mode=0o700)
        other = NativeSocket(other_home)
        self.addCleanup(other.close)
        other.beads = {'sample-1': dict(id='sample-1', title='Switched yard', asks=[decision()])}
        self.config.update(yard='switched-city', yardr_home=str(other_home.resolve()))
        self.restart()
        self.tap(data['5. sample-1#7'], list_id, 9)
        self.assertEqual(self.answers()[-1], 'This list is out of date; send /decide again')
        self.assertEqual(self.bridge.run_asks.rows(), [])
        self.assertFalse([c for c in other.calls if c[0] not in ('GetUser', 'UserAsks', 'Bead')])

    def test_ids_invalid_ambiguous_unknown_and_linked_yard(self):
        self.native.beads['sample-1']['asks'][1]['state'] = 'open'
        cases = [
            ('/decide sample-1', 'sample-1 has 2 open decisions (sample-1#7, sample-1#8); send /decide <ID>.'),
            ('/decide run-1#99', 'No ask run-1#99 in test-city.'),
            ('/decide nothing-1#1', 'No ask nothing-1#1 in test-city.'),
            ('/decide nothing-1', 'No open decision on nothing-1 for you; send /decide for the list.'),
            ('/decide sample-1#9', 'Ask sample-1#9 is not addressed to pat.'),
            ('/decide #7', '#7 is no decision ID; send /decide for the list. An ID is bead#ask.'),
            ('/decide sample-1#0', 'sample-1#0 is no decision ID; send /decide for the list. An ID is bead#ask.'),
            ('/decide sample-1#18446744073709551616', 'sample-1#18446744073709551616 is no decision ID; '
             'send /decide for the list. An ID is bead#ask.'),
            ('/decide far@peer#3', 'far@peer#3 is not an open ask in your queue; answer a linked yard ask in Footplate.'),
        ]
        for uid, (value, expected) in enumerate(cases, 1):
            self.command(value, uid)
            self.assertEqual(self.sends()[-1]['text'], expected, value)
        self.command('/decide run-1', 20)
        self.assertIn('yardr depot test web', self.sends()[-1]['text'])
        self.assertEqual(self.writes(), [])

    def test_linked_yard_rows_are_listed_and_never_opened(self):
        self.native.raw_queue = dict(open=[dict(bead='far-1@peer', bead_title='Far', topic='', ask=3, question='Far?',
                                                 asker='jo@peer', since='2026-01-01T00:00:00Z', turn=0)], decided=[])
        body, _ = self.listing()
        self.assertIn('linked yard · far-1@peer#3 · Far · asked by jo@peer\nNot complete in Telegram', body['text'])
        self.assertEqual([b['text'] for b in self.buttons(body)], ['↻ Refresh'])
        self.command('/decide far-1@peer#3', 2)
        self.assertIn('far-1@peer#3 is a linked yard ask', self.sends()[-1]['text'])
        self.assertEqual(self.reads('Bead'), [])

    def test_pages_are_bounded_and_navigation_rereads_fresh_state(self):
        for number in range(20, 28):
            self.native.beads[f'many-{number}'] = dict(id=f'many-{number}', title='Many', asks=[ask(number, f'Q{number}?')])
        body, list_id = self.listing()
        self.assertTrue(body['text'].startswith('14 open decisions in test-city for pat · page 1/3'))
        self.assertEqual(body['text'].count('\n\n'), n.DECIDE_PAGE)
        nav = body['reply_markup']['inline_keyboard'][-1]
        self.assertEqual([(b['text'], b['callback_data']) for b in nav],
                         [('↻ Refresh', 'decide:p:1'), ('Next ›', 'decide:p:2')])
        self.tap('decide:p:3', list_id, 2)
        page = self.edits()[-1]
        self.assertTrue(page['text'].startswith('14 open decisions in test-city for pat · page 3/3'))
        self.assertIn('13. Q26?', page['text'])
        self.assertEqual([b['text'] for b in page['reply_markup']['inline_keyboard'][-1]], ['‹ Prev', '↻ Refresh'])
        self.assertEqual(self.answers()[-1], '')
        # The list shrank meanwhile: a stale page shows the last that exists.
        for number in range(20, 28):
            del self.native.beads[f'many-{number}']
        self.tap('decide:p:3', list_id, 3)
        self.assertTrue(self.edits()[-1]['text'].startswith('6 open decisions in test-city for pat\n'))
        self.assertEqual(len(self.reads()), 3)
        # Replaying an update edits nothing twice.
        edits = len(self.edits())
        self.tap('decide:p:3', list_id, 3)
        self.assertEqual(len(self.edits()), edits)

    def test_empty_unavailable_and_unverified_operator(self):
        self.native.beads = {}
        body, list_id = self.listing()
        self.assertEqual(body['text'], 'No open decisions in test-city for pat.')
        self.assertEqual([b['text'] for b in self.buttons(body)], ['↻ Refresh'])
        self.native.mode = 'unreadable'
        self.command('/decide', 2)
        self.assertEqual(self.sends()[-1]['text'], 'The yard test-city is unavailable; nothing was listed or opened.')
        self.native.mode = None
        self.native.raw_queue = dict(open=[dict(bead='x-1', bead_title='X', ask=1, question='Q', asker='a', turn=0,
                                                 extra=True)], decided=[])
        self.command('/decide', 3)
        self.assertEqual(self.sends()[-1]['text'], 'invalid native UserAsks response')
        self.native.raw_queue = None
        self.native.user = dict(name='pat', role='member', identities={'tgbridge': {'id': '123'}})
        calls = len(self.reads())
        self.command('/decide', 4)
        self.assertEqual(self.sends()[-1]['text'], 'operator_not_authorized')
        self.tap('decide:p:1', list_id, 5)
        self.assertEqual(self.answers()[-1], 'operator_not_authorized')
        self.native.user = dict(name='pat', role='operator', identities={'tgbridge': {'id': '124'}})
        self.command('/decide sample-1#7', 6)
        self.assertEqual(self.sends()[-1]['text'], 'operator_not_authorized')
        self.assertEqual(len(self.reads()), calls)
        self.native.close()
        self.native.user = dict(name='pat', role='operator', identities={'tgbridge': {'id': '123'}})
        self.command('/decide', 7)
        self.assertEqual(self.sends()[-1]['text'], 'The yard test-city is unavailable; nothing was listed or opened.')
        self.native.close = lambda: None  # already closed above

    def test_wrong_user_chat_bot_topic_and_forward_reveal_nothing(self):
        body, list_id = self.listing()
        data = next(b['callback_data'] for b in self.buttons(body) if b['text'] == '5. sample-1#7')
        sent, reads = self.sent_count, len(self.reads())
        self.command('/decide', 2, **{'from': dict(id=124, is_bot=False)})
        self.command('/decide', 3, chat=dict(id=124, type='private'))
        self.command('/decide', 4, chat=dict(id=123, type='group'))
        self.assertEqual(self.sent_count, sent)
        refusals = [dict(sender=124), dict(chat=dict(id=124, type='private')),
                    {'from': dict(id=998, is_bot=True)}, {'from': dict(id=999, is_bot=False)},
                    dict(forward_origin=dict(type='user')), dict(thread=55), dict(message_thread_id=0)]
        for uid, changes in enumerate(refusals, 10):
            self.tap(data, list_id, uid, **changes)
        self.assertEqual(self.sent_count, sent)
        self.assertEqual(len(self.reads()), reads)
        # The bridge's own guard drops another user or chat before /decide sees it.
        self.assertEqual(self.answers()[-len(refusals):], ['', ''] + ['Decision list not authorized'] * (len(refusals) - 2))
        self.assertEqual(self.bridge.run_asks.rows(), [])
        # A command from a thread the bridge does not serve says so, and reads nothing.
        self.command('/decide', 30, thread=55)
        self.assertEqual(self.sends()[-1]['text'],
                         'This topic is not one the bridge serves; send /decide in the chat or a served topic.')
        self.assertEqual(self.sends()[-1]['message_thread_id'], 55)
        self.assertEqual(len(self.reads()), reads)

    def test_served_topic_lists_and_opens_there_and_cards_keep_their_conversation(self):
        body, list_id = self.listing(thread=OPS)
        self.assertEqual(body['message_thread_id'], OPS)
        data = next(b['callback_data'] for b in self.buttons(body) if b['text'] == '5. sample-1#7')
        self.tap(data, list_id, 2, thread=OPS)
        card = self.sends()[-1]
        card_id = self.bridge.run_asks.rows()[0]['message_id']
        self.assertEqual(card['message_thread_id'], OPS)
        self.assertEqual(self.bridge.run_asks.rows()[0]['conversation'], f'123:topic:{OPS}')
        # From the chat itself the open card is pointed at, not sent again in another conversation.
        self.command('/decide sample-1#7', 3)
        notice = self.sends()[-1]
        self.assertEqual(notice['text'], 'The card for sample-1#7 is already open.')
        self.assertEqual(notice['reply_parameters'], {'message_id': card_id})
        self.assertEqual(notice['message_thread_id'], OPS)
        self.command('/decide sample-1#8', 4)
        root_card = self.bridge.run_asks.get(self.bridge.run_asks.ident('sample-1', 8))
        self.assertNotIn('message_thread_id', self.sends()[-1])
        _, root_list_id = self.listing(uid=5)
        root_data = next(b['callback_data'] for b in self.buttons(body) if b['text'] == '6. sample-1#8')
        for uid, selected, target, target_thread, origin, origin_thread in (
                (6, data, card_id, OPS, root_list_id, None),
                (7, root_data, root_card['message_id'], None, list_id, OPS)):
            self.tap(selected, origin, uid, thread=origin_thread)
            notice = self.sends()[-1]
            self.assertEqual(notice['reply_parameters'], {'message_id': target})
            self.assertEqual(notice.get('message_thread_id'), target_thread)
            self.assertNotIn('reply_markup', notice)
        self.command('/decide sample-1#8', 8, thread=OPS)
        self.assertEqual(self.sends()[-1]['reply_parameters'], {'message_id': root_card['message_id']})
        self.assertNotIn('message_thread_id', self.sends()[-1])
        transport = self.bridge.request

        def lost(url, body, *args, **kwargs):
            if url.endswith('/sendMessage') and 'reply_parameters' in body:
                self.calls.append((url, body))
                raise b.TransportError('connection reset after send')
            return transport(url, body, *args, **kwargs)
        self.bridge.request = lost
        before = len(self.sends())
        self.command('/decide sample-1#7', 11)
        self.restart()
        self.command('/decide sample-1#7', 11)
        self.assertEqual(len(self.sends()), before + 1)
        self.assertEqual(self.sends()[-1]['reply_parameters'], {'message_id': card_id})
        self.assertEqual(self.sends()[-1]['message_thread_id'], OPS)
        self.assertEqual(len(self.bridge.run_asks.rows()), 2)
        cards = [s for s in self.sends() if 'askcard:' in str(s.get('reply_markup'))]
        self.assertEqual(len(cards), 2)
        self.bridge.request = transport
        # A run row saved for ops whose send was interrupted, then ops no longer served: never relocated or sent.
        def interrupted(row):
            raise RuntimeError('interrupted before delivery')
        self.bridge.run_asks.send_card = interrupted
        with self.assertRaises(RuntimeError):
            self.bridge.api_run_ask(dict(bead='run-1', ask=3, topic='ops', key='notification'))
        self.config['topics'] = ()
        self.restart()
        before = len(self.sends())
        for uid, value in ((12, '/decide run-1#3'), (13, '/decide sample-1#7')):
            self.command(value, uid)
            label = value.split()[1]
            self.assertEqual(self.sends()[-1]['text'], f'The card for {label} belongs to a conversation this bridge '
                                                       'no longer serves; use Footplate or the yard cards.')
        self.assertEqual(len(self.sends()), before + 2)
        self.assertFalse([s for s in self.sends()[before:] if 'reply_markup' in s])
        run = self.bridge.run_asks.get(self.bridge.run_asks.ident('run-1', 3))
        self.assertEqual((run['conversation'], run['message_id']), (f'123:topic:{OPS}', None))
        self.assertEqual(self.writes(), [])

    def test_ambiguous_card_delivery_is_never_sent_again_or_acted_on(self):
        base = self.bridge.request

        def lost(url, body, *args, **kwargs):
            if url.endswith('/sendMessage') and 'askcard:' in str(body.get('reply_markup')):
                self.calls.append((url, body))
                raise b.TransportError('connection reset after send')
            return base(url, body, *args, **kwargs)
        self.bridge.request = lost
        self.command('/decide sample-1#7', 1)
        attempts = [body for url, body in self.calls if url.endswith('/sendMessage') and 'askcard:' in str(body.get('reply_markup'))]
        self.assertEqual(len(attempts), 1)
        self.assertIn('is not confirmed sent (ambiguous_delivery_requires_manual_reconciliation); nothing was answered.',
                      self.sends()[-1]['text'])
        self.restart()
        self.bridge.request = lost
        self.command('/decide sample-1#7', 2)
        attempts = [body for url, body in self.calls if url.endswith('/sendMessage') and 'askcard:' in str(body.get('reply_markup'))]
        self.assertEqual(len(attempts), 1)
        self.assertEqual(self.writes(), [])

    def test_unicode_and_boundary_lengths_stay_whole_on_the_card(self):
        family = '👩‍👩‍👧'
        question = 'é' * 40 + family * 10 + 'end'
        long_bead = 'b' + 'x' * 199
        self.native.beads = {long_bead: dict(id=long_bead, title='T' * 300, asks=[ask(18446744073709551615, question)])}
        body, list_id = self.listing()
        line = body['text'].split('\n')[3]
        self.assertTrue(line.startswith('1. ' + 'é' * 40))
        self.assertTrue(line.endswith('…'))
        cut = line[3:-1]
        self.assertLessEqual(n.units(cut + '…'), 100)
        self.assertTrue(cut.endswith(family) or cut.endswith('é'))  # never a half emoji sequence
        self.assertIn(f'{long_bead}#18446744073709551615', body['text'])
        button = self.buttons(body)[0]
        self.assertLessEqual(len(button['callback_data'].encode()), 64)
        self.tap(button['callback_data'], list_id, 2)
        self.assertIn(question, self.sends()[-1]['text'])

    def test_two_yards_sharing_the_operator_never_cross(self):
        other_home = Path(self.tmp.name) / 'other-yard'
        other_home.mkdir(mode=0o700)
        other = NativeSocket(other_home)
        self.addCleanup(other.close)
        other.beads = {'far-1': dict(id='far-1', title='Other yard', asks=[ask(7, 'Other yard question?')])}
        config = dict(self.config, yard='other-city', bot_id=998, yardr_home=str(other_home.resolve()))
        store = b.Store(Path(self.tmp.name) / 'other.sqlite3', 10)
        other_bridge = b.Bridge(config, store, 'TOKEN2', 'CAPABILITY', request=self.bridge.request)
        other_bridge.hook_secret = None
        body, list_id = self.listing()
        self.assertNotIn('Other yard question?', body['text'])
        mine = next(b['callback_data'] for b in self.buttons(body) if b['text'] == '5. sample-1#7')
        other_bridge.inbound(dict(update_id=1, message=dict(message_id=601, text='/decide', chat=dict(id=123, type='private'),
                                                             **{'from': dict(id=123, is_bot=False)})))
        theirs = self.sends()[-1]
        self.assertTrue(theirs['text'].startswith('1 open decision in other-city for pat'))
        self.assertNotIn('sample-1', theirs['text'])
        # This yard's button pressed on the other yard's bot: neither its message nor its ID is the other's.
        query = dict(id='x', data=mine, message=dict(message_id=list_id, chat=dict(id=123, type='private'),
                                                     **{'from': dict(id=999, is_bot=True)}), **{'from': dict(id=123, is_bot=False)})
        other_bridge.inbound(dict(update_id=2, callback_query=query))
        query['message']['from'] = dict(id=998, is_bot=True)
        other_bridge.inbound(dict(update_id=3, callback_query=query))
        self.assertEqual([body.get('text') for url, body in self.calls if url.endswith('/answerCallbackQuery')][-2:],
                         ['Decision list not authorized', 'This list is out of date; send /decide again'])
        other_bridge.inbound(dict(update_id=4, message=dict(message_id=602, text='/decide sample-1#7', chat=dict(id=123, type='private'),
                                                             **{'from': dict(id=123, is_bot=False)})))
        self.assertEqual(self.sends()[-1]['text'], 'No ask sample-1#7 in other-city.')
        self.assertEqual(n.RunAsks(other_bridge).rows(), [])
        self.assertFalse([c for c in other.calls if c[0] not in ('GetUser', 'UserAsks', 'Bead')])


class DecideNamespaceTests(BridgeTestCase):
    def native_settings(self, extra='', native=True):
        root = Path(self.tmp.name)
        home = root / 'yard'
        home.mkdir(mode=0o700, exist_ok=True)
        path = write_settings(root)
        text = path.read_text()
        if native:
            text = text.replace('[yardr]\n', f'[yardr]\nhome = "{home}"\noperator = "pat"\n')
        path.write_text(text + extra)
        return path

    def test_decide_and_its_prefix_are_reserved_only_with_native_keys(self):
        handler = '\n[[command]]\nname = "{}"\ndescription = "x"\nurl = "http://127.0.0.1:9/x"\n'
        with self.assertRaisesRegex(b.SettingsError, 'clashes with /decide'):
            b.load_settings(self.native_settings(handler.format('decide')))
        with self.assertRaisesRegex(b.SettingsError, 'overlaps the bridge-owned decide: prefix'):
            b.load_settings(self.native_settings(handler.format('mine') + 'callback_prefix = "decide:x"\n'))
        with self.assertRaisesRegex(b.SettingsError, 'overlaps the bridge-owned decide: prefix'):
            b.load_settings(self.native_settings(handler.format('mine') + 'callback_prefix = "dec"\n'))
        with self.assertRaisesRegex(b.SettingsError, r'\[\[menu\]\] /decide clashes'):
            b.load_settings(self.native_settings('\n[[menu]]\ncommand = "decide"\ndescription = "x"\n'))
        # Without native keys a bridge's own /decide keeps its handler, prefix and menu entry.
        loaded = b.load_settings(self.native_settings(handler.format('decide'), native=False))
        self.assertEqual(loaded.config['commands'][0]['callback_prefix'], 'decide:')
        bridge = b.Bridge(loaded.config, self.store, 'T', 'C', request=self.transport)
        self.assertFalse(bridge.native_decide())
        self.assertEqual(bridge._match_command('/decide x')[0]['name'], 'decide')
        self.assertIsNot(bridge._callback_command('decide:1'), None)
        self.assertEqual(bridge._publish_keyboard([[dict(text='x', callback_data='decide:1')]])[1], None)

    def test_native_menu_publish_and_routing_own_decide(self):
        loaded = b.load_settings(self.native_settings())
        bridge = b.Bridge(loaded.config, self.store, 'T', 'C', request=self.transport)
        self.assertTrue(bridge.native_decide())
        self.assertIs(bridge._match_command('/decide@test_bot sample-1#7')[0], bridge.DECIDE_COMMAND)
        self.assertEqual(bridge._match_command('/decide@test_bot sample-1#7')[1], 'sample-1#7')
        self.assertIsNone(bridge._match_command('/decide@other_bot'))
        self.assertIsNone(bridge._match_command('/decided'))
        self.assertEqual(bridge._publish_keyboard([[dict(text='x', callback_data='decide:o:1')]]), (None, 'foreign_callback_data'))
        sent = []

        def telegram(method, body, timeout=None):
            sent.append((method, body))
            if method == 'getMyCommands':
                return 200, dict(ok=True, result=sent[0][1]['commands'])
            return 200, dict(ok=True, result=True)
        bridge.telegram = telegram
        commands = bridge.set_commands()
        self.assertEqual(commands[-1], dict(command='decide', description='Open decisions waiting on you'))
        self.assertEqual([c['command'] for c in commands], ['quota', 'status', 'waiting', 'decide'])
        too_many = ''.join(f'\n[[command]]\nname = "c{i}"\ndescription = "x"\nurl = "http://127.0.0.1:9/x"\n' for i in range(97))
        with self.assertRaisesRegex(b.SettingsError, 'with the native /decide'):
            b.load_settings(self.native_settings(too_many))


class RealDecideTests(BridgeTestCase):
    def test_real_core_lists_opens_answers_and_reports_outcomes(self):
        binary = shutil.which('yardr')
        if binary is None:
            self.skipTest('yardr binary required for isolated native integration')
        root = Path(self.tmp.name)
        home = root / 'yard'
        home.mkdir(mode=0o700)
        env = dict(HOME=str(root), XDG_CONFIG_HOME=str(root / 'config'), XDG_STATE_HOME=str(root / 'state'),
                   PATH=str(root) + ':/usr/local/bin:/usr/bin:/bin', USER='pat')
        command = [binary, '--home', str(home), '--actor', 'pat']
        server = subprocess.Popen(command + ['server', 'run'], env=env, cwd=root, stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        self.addCleanup(lambda: server.poll() is None and (server.kill(), server.wait()))
        deadline = time.monotonic() + 15
        while not (home / 'yard.sock').exists():
            if server.poll() is not None or time.monotonic() > deadline:
                self.fail('isolated native server did not start')
            time.sleep(.02)

        def cli(*args, actor='pat'):
            result = subprocess.run([binary, '--home', str(home), '--actor', actor] + list(args), env=env, cwd=root,
                                    text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        cli('settings', 'set', 'operator', 'pat')
        cli('settings', 'set', 'default_kind', 'codex')
        source = root / 'synthetic.yaml'
        source.write_text('name: tgbridge\nbase: https://example.invalid\ncalls:\n  health:\n    kind: read\n    request: {method: GET, path: /health}\nhook:\n  bodies: unique\n  verify:\n    scheme: secret\n    signature: {header: X-Secret}\n  normalize: [/bin/false]\n')
        cli('source', 'add', str(source))
        cli('source', 'set', 'tgbridge', 'calls_per_hour=120')
        cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
        cli('user', 'add', 'proposer')
        cli('user', 'add', 'sam')
        cli('bead', 'create', 'Synthetic decisions', '--id', 'sample-1')
        cli('bead', 'create', 'Synthetic run', '--id', 'run-1')

        def asked(bead, *args, actor='proposer'):
            return int(cli('bead', 'ask', bead, *args, actor=actor).split()[1])
        option = asked('sample-1', 'Where do the links go?', '--to', 'pat', '--context', 'Synthetic ordinary decision',
                       '--option', 'Move: Move the links.', '--option', 'Keep: Keep the links here.')
        words = asked('sample-1', 'Which name?', '--to', 'pat', '--context', 'Synthetic words decision')
        asked('sample-1', 'Not for pat?', '--to', 'sam', '--context', 'Another recipient')
        later = asked('sample-1', 'Later?', '--to', 'pat', '--context', 'Put aside below')
        back = asked('sample-1', 'Taken back?', '--to', 'pat', '--context', 'Taken back below')
        answered = asked('sample-1', 'Answered?', '--to', 'pat', '--context', 'Answered below')
        run = asked('run-1', 'Lower the test limit?', '--to', 'pat', '--context', 'Synthetic isolated integration',
                    '--run', 'source set tgbridge calls_per_hour=60')
        result = subprocess.run([binary, 'bead', 'ask', '--take-back', 'sample-1', str(back), 'moot now'], cwd=root,
                                env=dict(env, YARDR_HOME=str(home), YARDR_ACTOR='proposer'), text=True, capture_output=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        cli('bead', 'answer', 'sample-1', str(answered), '--words', 'done already')
        cli('bead', 'answer', 'sample-1', str(later), '--later')  # after the asker's notes, which bring it back

        self.config.update(yardr_home=str(home), yardr_operator='pat', yardr_source='tgbridge',
                           topics=(dict(name='ops', agent='yardmaster'),))
        self.store.record_topic('ops', OPS, 'created')
        ids = iter(range(77, 300))
        quote_refused = False

        def telegram(url, body, *args, **kwargs):
            status, response = self.transport(url, body, *args, **kwargs)
            if url.endswith('/sendMessage'):
                if quote_refused and 'reply_parameters' in body:
                    return 400, dict(ok=False, error_code=400, description='Bad Request: message to be replied not found')
                response['result']['message_id'] = next(ids)
            elif url.endswith('/editMessageText'):
                response = dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=123)))
            return status, response
        bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=telegram)
        bridge.hook_secret = None
        sends = lambda: [body for url, body in self.calls if url.endswith('/sendMessage')]

        def say(value, uid, thread=None):
            message = dict(message_id=900 + uid, text=value, chat=dict(id=123, type='private'),
                           **{'from': dict(id=123, is_bot=False)})
            if thread is not None:
                message.update(is_topic_message=True, message_thread_id=thread)
            bridge.inbound(dict(update_id=uid, message=message))
            return sends()[-1]

        def tap(data, message_id, uid, thread=None):
            message = dict(message_id=message_id, chat=dict(id=123, type='private'), **{'from': dict(id=999, is_bot=True)})
            if thread is not None:
                message.update(is_topic_message=True, message_thread_id=thread)
            bridge.inbound(dict(update_id=uid, callback_query=dict(id=f'q{uid}', data=data,
                **{'from': dict(id=123, is_bot=False)}, message=message)))

        listing = say('/decide', 1)
        text = listing['text']
        self.assertTrue(text.startswith('3 open decisions in test-city for pat'), text)
        for ident in (f'sample-1#{option}', f'sample-1#{words}', f'run-1#{run}'):
            self.assertIn(ident, text)
        for absent in ('Not for pat?', 'Later?', 'Taken back?', 'Answered?'):
            self.assertNotIn(absent, text)
        buttons = {b['text'].split('. ', 1)[-1]: b['callback_data'] for row in listing['reply_markup']['inline_keyboard'] for b in row}
        card = say(f'/decide sample-1#{option}', 20, thread=OPS)
        self.assertIn('Where do the links go?', card['text'])
        row = next(r for r in bridge.run_asks.rows() if r['ask'] == option)
        self.assertEqual((row['conversation'], card['message_thread_id']), (f'123:topic:{OPS}', OPS))
        before = len(sends())
        tap(buttons[f'sample-1#{option}'], 77, 2)
        self.assertEqual(len(sends()), before + 1)
        self.assertEqual(sends()[-1]['text'], f'The card for sample-1#{option} is already open.')
        self.assertEqual(sends()[-1]['reply_parameters'], {'message_id': row['message_id']})
        self.assertEqual(sends()[-1]['message_thread_id'], OPS)
        notice = say(f'/decide sample-1#{option}', 21)
        self.assertEqual(notice['reply_parameters'], {'message_id': row['message_id']})
        self.assertEqual(notice['message_thread_id'], OPS)
        quote_refused = True
        before = len(sends())
        notice = say(f'/decide sample-1#{option}', 22)
        self.assertEqual(len(sends()), before + 2)
        self.assertEqual(sends()[-2], dict(notice, reply_parameters={'message_id': row['message_id']}))
        self.assertEqual(notice['text'], f'The card for sample-1#{option} is already open.')
        self.assertNotIn('reply_parameters', notice)
        self.assertEqual(notice['message_thread_id'], OPS)
        self.assertEqual(len([s for s in sends() if 'askcard:' in str(s.get('reply_markup'))]), 1)
        self.assertEqual(len(bridge.run_asks.rows()), 1)
        self.assertEqual(len(row['generations']), 1)
        quote_refused = False
        tap(f"askcard:{row['id']}:pick1:{row['turn']}", row['message_id'], 3, thread=OPS)
        client = n.YardSocketClient(home, 'tgbridge')
        stored = next(a for a in client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'] if a['ask'] == option)
        self.assertEqual((stored['state'], stored['option']), ('answered', 1))
        self.assertEqual(say(f'/decide sample-1#{option}', 4)['text'], f'Ask sample-1#{option} is no longer open: ✓ Answered: 1. Move')
        self.assertEqual(say(f'/decide sample-1#{later}', 5)['text'][:len(f'Ask sample-1#{later} is no longer open: ⏳ Put aside until ')],
                         f'Ask sample-1#{later} is no longer open: ⏳ Put aside until ')
        self.assertEqual(say(f'/decide sample-1#{back}', 6)['text'], f'Ask sample-1#{back} is no longer open: ✕ Taken back by proposer: moot now')
        self.assertEqual(say(f'/decide sample-1#{answered}', 7)['text'], f'Ask sample-1#{answered} is no longer open: ✓ Answered\ndone already')
        self.assertEqual(say('/decide sample-1#999', 8)['text'], 'No ask sample-1#999 in test-city.')
        self.assertEqual(say('/decide nothing-1#1', 9)['text'], 'No ask nothing-1#1 in test-city.')
        card = say(f'/decide run-1#{run}', 10)
        self.assertIn('yardr source set tgbridge calls_per_hour=60', card['text'])
        row = next(r for r in bridge.run_asks.rows() if r['ask'] == run)
        tap(f"runask:{row['id']}:approve", row['message_id'], 11)
        self.assertEqual(client.call('GetSource', ['tgbridge'])['calls_per_hour'], 60)
        tap(buttons[f'sample-1#{option}'], 77, 12)
        self.assertEqual(sends()[-1]['text'], f'Ask sample-1#{option} is no longer open: ✓ Answered: 1. Move')
        tap('decide:p:1', 77, 13)
        refreshed = [body for url, body in self.calls if url.endswith('/editMessageText') and body['message_id'] == 77][-1]
        self.assertTrue(refreshed['text'].startswith('1 open decision in test-city for pat'), refreshed['text'])
        self.assertIn(f'sample-1#{words}', refreshed['text'])
        # The asker's next note brings a put-aside ask back into the open queue.
        cli('bead', 'note', 'sample-1', 'A nudge', actor='proposer')
        tap('decide:p:1', 77, 14)
        refreshed = [body for url, body in self.calls if url.endswith('/editMessageText') and body['message_id'] == 77][-1]
        self.assertTrue(refreshed['text'].startswith('2 open decisions in test-city for pat'), refreshed['text'])
        self.assertIn(f'sample-1#{later}', refreshed['text'])


if __name__ == '__main__':
    unittest.main()
