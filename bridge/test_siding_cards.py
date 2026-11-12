"""Siding cards on plain calls: real native stores of any build, the production poll order and fake Telegram."""
import copy
from html import unescape
import importlib.util
import json
from pathlib import Path
import shutil
import subprocess
import sys
import tempfile
import time
from unittest.mock import patch

from test_bridge import BridgeTestCase, write_settings
from test_run_asks import NativeCase
import bridge as b
import testyardr

n = b.run_asks
s = b.siding_cards
FLOW = 'intake -> harbor | repair\nharbor -> launch | repair\nrepair -> harbor | launch\nlaunch -> complete\ncomplete terminal\n'
READS = ('GetDepot', 'Groups', 'Routes', 'Settings', 'ListBeads', 'SidingBeads', 'NextStages', 'GetBead')
# The bridge release before siding cards moved onto plain calls: its rows are the legacy ones.
PREVIOUS = '2b5f7443e451bd733ad9d7471c05dc7bde4f66c8'
# The plain-fold release a release card rolls back to: it reads every siding row of its table as a fold.
PLAIN_FOLD = 'e7a2c7cf911a1ddf29751cce53664ddf04c5547a'


class FakeSidingTests(NativeCase):
    """Plain siding calls answered by the fake socket: delivery resumption and read retries."""

    def setUp(self):
        super().setUp()
        self.sidings = {}
        self.paths = {}
        self.statuses = {}
        owner = self
        handler = self.native.server.RequestHandlerClass

        class Handler(handler):
            def do_POST(self):
                method = self.path.removeprefix('/call/')
                if method not in (*READS, 'FoldBeads', 'Advance'):
                    return super().do_POST()
                args = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.native.calls.append((method, args, self.headers.get('X-Yardr-Connection')))
                if owner.statuses.get(method):
                    self.send_error(owner.statuses[method].pop(0))
                    return
                if method == 'GetDepot':
                    result = dict(name='sample', path='/synthetic', kind='dir', base='')
                elif method == 'Groups':
                    result = [dict(name='keepers', runner='manual', config=dict(fold_after='24h'), limit=50)]
                elif method == 'Routes':
                    result = [dict(id='route-' + stage, stage=stage, depot='sample', group='keepers') for stage in owner.sidings]
                elif method == 'Settings':
                    result = dict(operator='pat')
                elif method == 'ListBeads':
                    result = [owner.bead(ident) for ident in owner.sidings[args[0]['Stage']]]
                elif method == 'SidingBeads':
                    result = [dict(bead=ident, entered=f'2026-01-01T00:00:0{i}Z', points=1) for i, ident in enumerate(args[0])]
                elif method == 'NextStages':
                    result = dict(bead=args[0], stage=owner.bead(args[0])['stage'], flow=dict(name='rotation.f'),
                                  source=dict(kind='repo', path=owner.paths[args[0]]) if args[0] in owner.paths
                                  else dict(kind='store'), stages=['launch', 'repair'])
                elif method == 'GetBead':
                    result = owner.bead(args[0])
                else:
                    self.reply({'error': {'code': 'error', 'message': 'unexpected fold'}})
                    return
                self.reply({'result': result})

        self.native.server.RequestHandlerClass = Handler

    def bead(self, ident):
        stage = next(stage for stage, ids in self.sidings.items() if ident in ids)
        return dict(id=ident, title='Synthetic part', stage=stage, status='open', depot='sample', type='task', priority=2,
                    created_at='2026-01-01T00:00:00Z', labels=['size:small'], revision='1')

    def test_same_key_resumes_only_unsent_cards(self):
        for kind in ('status', 'actionable', 'partial'):
            for failure in ('not-sent', 'crash', 'ambiguous', 'rejected', 'revoked', 'remapped'):
                with self.subTest(kind=kind, failure=failure):
                    with self.store.connect() as db:
                        for table in (s.TABLE, 'run_ask_keys', 'deliveries'):
                            db.execute('DELETE FROM ' + table)
                    self.calls.clear()
                    self.sidings = {} if kind == 'status' else {'harbor': ['sample-harbor-1', 'sample-harbor-2']}
                    if kind == 'partial':
                        self.sidings['repair'] = ['sample-repair-1', 'sample-repair-2']
                    attempts = []
                    publishes = []
                    send_card = self.bridge.siding_cards.send_card
                    def publish(row):
                        publishes.append(row['id'])
                        if failure == 'crash' and (kind != 'partial' or len(publishes) > 1):
                            raise RuntimeError('synthetic crash before send')
                        return send_card(row)
                    telegram = self.bridge.request
                    def fail(url, body, *args, **kwargs):
                        if url.endswith('/sendMessage'):
                            attempts.append(copy.deepcopy(body))
                            if kind != 'partial' or len(attempts) > 1:
                                if failure == 'ambiguous':
                                    raise b.RequestUnconfirmed('synthetic lost response', 56, 'after-send')
                                if failure == 'rejected':
                                    return 400, dict(ok=False, error_code=400, description='synthetic rejection')
                                raise b.RequestNotSent('synthetic offline', 7, 'connect')
                        return telegram(url, body, *args, **kwargs)
                    request = dict(depot='sample', key='resume', silent=True)
                    with patch.object(self.bridge, 'request', side_effect=fail), patch.object(b.time, 'sleep'), \
                            patch.object(self.bridge.siding_cards, 'send_card', side_effect=publish):
                        if failure == 'crash':
                            with self.assertRaisesRegex(RuntimeError, 'synthetic crash before send'):
                                self.bridge.api_siding_card(request)
                        else:
                            receipt = self.bridge.api_siding_card(request)
                            self.assertFalse(receipt['delivered'], receipt)
                            if failure in ('not-sent', 'revoked', 'remapped'):
                                self.assertEqual((receipt['reason'], receipt['failure_kind']),
                                                 ('transport_unavailable_not_sent', 'transient'))
                    first = self.bridge.siding_cards.rows()[0]
                    batch = [self.bridge.siding_cards.get(ident) for ident in first['batch']]
                    self.assertEqual([row['message_id'] is not None for row in batch],
                                     [True, False] if kind == 'partial' else [False])
                    sent = [(row, self.store.saved_delivery(self.bridge.siding_cards.send_request(row)[0]))
                            for row in batch if row['message_id'] is not None]
                    self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=telegram)
                    self.bridge.hook_secret = None
                    before = len(self.calls)
                    conflict = self.bridge.api_siding_card(dict(request, silent=False))
                    self.assertEqual(conflict['reason'], 'idempotency_key_conflict')
                    self.assertEqual(len(self.calls), before)
                    if failure in ('revoked', 'remapped'):
                        user = copy.deepcopy(self.native.user)
                        if failure == 'revoked':
                            self.native.user['role'] = 'member'
                        else:
                            self.native.user['identities']['tgbridge']['id'] = '456'
                        deliveries = [self.store.saved_delivery(self.bridge.siding_cards.send_request(row)[0])
                                      for row in batch]
                        try:
                            receipt = self.bridge.api_siding_card(request)
                            self.assertFalse(receipt['delivered'], receipt)
                            self.assertEqual(receipt['reason'], 'operator_not_authorized')
                            self.assertEqual(len(self.calls), before)
                            self.assertEqual([self.bridge.siding_cards.get(row['id']) for row in batch], batch)
                            self.assertEqual([self.store.saved_delivery(self.bridge.siding_cards.send_request(row)[0])
                                              for row in batch], deliveries)
                        finally:
                            self.native.user = user
                    receipt = self.bridge.api_siding_card(request)
                    can_retry = failure in ('not-sent', 'crash', 'revoked', 'remapped')
                    self.assertEqual(receipt['delivered'], can_retry, receipt)
                    sends = [body for url, body in self.calls[before:] if url.endswith('/sendMessage')]
                    self.assertEqual(len(sends), int(can_retry))
                    if can_retry:
                        if failure != 'crash':
                            self.assertEqual(sends[0], attempts[-1])
                        self.assertEqual(sends[0]['text'], batch[-1]['html'])
                        self.assertEqual(sends[0]['reply_markup']['inline_keyboard'], batch[-1]['publish_keyboard'])
                        self.assertEqual(bool(sends[0]['reply_markup']['inline_keyboard']), kind != 'status')
                        resumed = self.bridge.siding_cards.get(batch[-1]['id'])
                        saved = self.store.saved_delivery(self.bridge.siding_cards.send_request(resumed)[0])
                        self.assertTrue(saved[1]['delivered'])
                        self.assertEqual(int(saved[1]['message_id']), resumed['message_id'])
                    for row, saved in sent:
                        self.assertEqual(self.bridge.siding_cards.get(row['id']), row)
                        self.assertEqual(self.store.saved_delivery(self.bridge.siding_cards.send_request(row)[0]), saved)
                    before = len(self.calls)
                    repeat = self.bridge.api_siding_card(request)
                    if can_retry:
                        self.assertEqual(repeat['delivered'], kind == 'status', repeat)
                        if kind != 'status':
                            self.assertEqual(repeat['reason'], 'siding_still_awaiting_human')
                    self.assertEqual(len(self.calls), before)
                    self.assertFalse([method for method, _, _ in self.native.calls if method == 'FoldBeads'])

    def test_a_subset_stays_in_its_own_flow_source(self):
        # Two flows of one name from two sources are two sidings; a subset reply never reaches across them.
        ids = [f'sample-harbor-{i}' for i in range(1, 5)]
        self.sidings = {'harbor': ids}
        self.paths = {ids[2]: '.yardr/flows/rotation.f', ids[3]: '.yardr/flows/rotation.f'}
        receipt = self.bridge.api_siding_card(dict(depot='sample', key='sources'))
        self.assertTrue(receipt['delivered'], receipt)
        store, repo = sorted(self.bridge.siding_cards.rows(), key=lambda row: row['siding']['members'])
        self.assertEqual((store['siding']['members'], repo['siding']['members']), (ids[:2], ids[2:]))
        notices = {}
        command_message = self.bridge._command_message
        def notice(key, action, thread_id=None):
            notices[key] = action['text']
            return command_message(key, action, thread_id)
        self.bridge._command_message = notice
        for uid, (row, words) in enumerate(((store, ids[2:]), (repo, ids[:2]), (store, [ids[0], ids[2]])), 41):
            self.bridge.inbound(dict(update_id=uid, message=dict(
                message_id=900 + uid, text='fold ' + ' '.join(words), chat=dict(id=123, type='private'),
                **{'from': dict(id=123, is_bot=False)},
                reply_to_message=dict(message_id=row['message_id'], **{'from': dict(id=999, is_bot=True)}))))
            other = [ident for ident in words if ident not in row['siding']['members']]
            self.assertEqual(notices[f'siding:{uid}:notice'], f"Not open in this siding: {', '.join(other)}. Nothing folded.")
        self.assertEqual(len(self.bridge.siding_cards.rows()), 2)
        self.assertFalse([method for method, _, _ in self.native.calls if method == 'FoldBeads'])
        self.assertIn('rotation.f (.yardr/flows/rotation.f) / harbor / keepers → launch', repo['plain'])

    def test_reads_retry_on_replacing_and_the_fold_is_never_retried(self):
        # A yard replacing itself answers 503 before dispatch: every read the cards make is asked again.
        self.sidings = {'harbor': ['sample-harbor-1', 'sample-harbor-2']}
        client = self.bridge.siding_cards.client
        arguments = dict(GetDepot=['sample'], Groups=[], Routes=[], Settings=[], ListBeads=[{'Depot': 'sample', 'Stage': 'harbor'}],
                         SidingBeads=[['sample-harbor-1']], NextStages=['sample-harbor-1'], GetBead=['sample-harbor-1'])
        with patch.object(n.time, 'sleep'):
            for method in READS:
                with self.subTest(method=method):
                    self.statuses[method] = [503, 503]
                    start = len(self.native.calls)
                    client.call(method, arguments[method])
                    self.assertEqual([m for m, _, _ in self.native.calls[start:]], [method] * 3)
            self.native.read_statuses = [503, 503]
            n.verify_operator(client, self.config)
            self.statuses = {method: [503] for method in READS}
            receipt = self.bridge.api_siding_card(dict(depot='sample', key='replacing'))
            self.assertTrue(receipt['delivered'], receipt)
            row = self.bridge.siding_cards.rows()[0]
            self.assertEqual(row['siding']['members'], ['sample-harbor-1', 'sample-harbor-2'])
            # A write is never asked again: a 503 after the yard took it is an uncertain answer.
            self.statuses['FoldBeads'] = [503]
            start = len(self.native.calls)
            with self.assertRaises(n.RPCAmbiguous):
                client.call('FoldBeads', [['sample-harbor-1', 'sample-harbor-2'], {'To': 'launch', 'Title': ''}, 'pat'])
            self.assertEqual([m for m, _, _ in self.native.calls[start:]], ['FoldBeads'])
            # A release's advance is a write too: one call, never asked again.
            self.statuses['Advance'] = [503]
            start = len(self.native.calls)
            with self.assertRaises(n.RPCAmbiguous):
                client.call('Advance', ['sample-harbor-1', 'launch', dict(stage='harbor', revision='1'), 'pat'])
            self.assertEqual([m for m, _, _ in self.native.calls[start:]], ['Advance'])


class SidingTests(BridgeTestCase):
    """Real isolated stores. The plain calls exist on every build, before and after the alignment."""

    def setUp(self):
        super().setUp()
        binary = testyardr.binary()
        if binary is None:
            self.skipTest('yardr required for isolated siding integration')
        self.root = Path(self.tmp.name)
        server, self.home, self.command, self.env = testyardr.start(self.root, binary)
        self.addCleanup(testyardr.stop, server)
        self.client = n.YardSocketClient(self.home, 'tgbridge')
        self.cli('settings', 'set', 'operator', 'pat')
        self.cli('settings', 'set', 'default_kind', 'codex')
        source = self.root / 'source.yaml'
        source.write_text('name: tgbridge\nbase: https://example.invalid\ncalls:\n  health:\n    kind: read\n'
                          '    request: {method: GET, path: /health}\nhook:\n  bodies: unique\n  verify:\n'
                          '    scheme: secret\n    signature: {header: X-Secret}\n  normalize: [/bin/false]\n')
        self.cli('source', 'add', str(source))
        self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
        depot = self.root / 'depot'
        depot.mkdir()
        self.cli('depot', 'add', 'sample', str(depot), '--kind', 'dir')
        flow = self.root / 'rotation.flow'
        flow.write_text(FLOW)
        self.cli('flow', 'set', 'rotation.f', '-f', str(flow))
        self.cli('depot', 'flow', 'sample', 'rotation.f')
        self.cli('group', 'add', 'keepers', '--runner', 'manual', '--members', 'pat', '--set', 'fold_after=24h')
        self.cli('route', 'add', '--stage', 'harbor', '--group', 'keepers', '--depot', 'sample')
        self.config.update(yardr_home=str(self.home), yardr_operator='pat', yardr_source='tgbridge')
        self.counter = 76
        self.updates = []
        base = self.transport
        def telegram(url, body, *args, **kwargs):
            if url.endswith('/sendMessage'):
                self.counter += 1
                self.calls.append((url, copy.deepcopy(body)))
                return 200, dict(ok=True, result=dict(message_id=self.counter, chat=dict(id=self.config['chat_id'])))
            if url.endswith('/editMessageText'):
                self.calls.append((url, copy.deepcopy(body)))
                return 200, dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=self.config['chat_id'])))
            if url.endswith('/getUpdates'):
                updates, self.updates = self.updates, []
                return 200, dict(ok=True, result=updates)
            return base(url, body, *args, **kwargs)
        self.telegram = telegram
        self.restart()

    def cli(self, *args, check=True):
        result = subprocess.run(self.command + list(args), env=self.env, cwd=self.root, capture_output=True, text=True, timeout=20)
        if check:
            self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def restart(self):
        self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=self.telegram)
        self.bridge.hook_secret = None
        self.writes = []
        self.advances = []
        call = self.bridge.siding_cards.client.call
        def record(method, arguments):
            if method == 'FoldBeads':
                self.writes.append(copy.deepcopy(arguments))
            if method == 'Advance':
                self.advances.append(copy.deepcopy(arguments))
            return call(method, arguments)
        self.bridge.siding_cards.client.call = record
        self.notices = {}
        command_message = self.bridge._command_message
        def notice(key, action, thread_id=None):
            self.notices[key] = action['text']
            return command_message(key, action, thread_id)
        self.bridge._command_message = notice

    def part(self, name, size='small', stage='harbor'):
        self.cli('bead', 'create', 'Synthetic ' + name, '--id', 'sample-' + name, '--depot', 'sample', '--stage', stage, '--label', 'size:' + size)
        return 'sample-' + name

    def card(self, key='siding', **selection):
        receipt = self.bridge.api_siding_card(dict(depot='sample', key=key, **selection))
        self.assertTrue(receipt['delivered'], receipt)
        return self.bridge.siding_cards.get(receipt_row(self.bridge, receipt))

    def press_update(self, row, action='fold', uid=40, **overrides):
        query = dict(id='press-' + str(uid), data=f"siding:{row['id']}:{action}",
                     message=dict(message_id=row['message_id'], chat=dict(id=123, type='private'),
                                  **{'from': dict(id=999, is_bot=True)}), **{'from': dict(id=123, is_bot=False)})
        query.update(overrides)
        return dict(update_id=uid, callback_query=query)

    def reply_update(self, row, text, uid=41):
        return dict(update_id=uid, message=dict(message_id=900 + uid, text=text,
            chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)},
            reply_to_message=dict(message_id=row['message_id'], **{'from': dict(id=999, is_bot=True)})))

    def press(self, row, action='fold', uid=40, **overrides):
        self.updates = [self.press_update(row, action, uid, **overrides)]
        self.bridge.poll_once()
        return self.answer(uid)

    def reply(self, row, text, uid=41):
        self.updates = [self.reply_update(row, text, uid)]
        self.bridge.poll_once()
        return self.notice(uid)

    def answer(self, uid):
        answers = [body.get('text', '') for url, body in self.calls if url.endswith('/answerCallbackQuery')
                   and body.get('callback_query_id') == 'press-' + str(uid)]
        return answers[-1] if answers else None

    def notice(self, uid):
        return self.notices.get(f'siding:{uid}:notice')

    def explicit(self):
        return [row for row in self.bridge.siding_cards.rows() if row['siding'] and row['siding']['explicit']]

    def sent(self):
        return [dict(body, text=unescape(body['text'])) for url, body in self.calls if url.endswith('/sendMessage')]

    def edits(self):
        return [dict(body, text=unescape(body['text'])) for url, body in self.calls if url.endswith('/editMessageText')]

    def folds(self):
        return self.client.call('EventsOfKind', ['folded', 100]) or []

    def bead(self, ident):
        return self.client.call('GetBead', [ident])

    def row(self, row):
        return self.bridge.siding_cards.get(row['id'])

    def test_card_folds_exactly_the_shown_beads_and_unfold_returns_them(self):
        a, held, c = self.part('a'), self.part('b'), self.part('c')
        self.cli('bead', 'hold', held)
        row = self.card()
        value = row['siding']
        self.assertEqual([(w['bead'], w['stuck']) for w in value['waiting']], [(a, ''), (held, 'held'), (c, '')])
        self.assertEqual((value['members'], value['to'], value['points'], value['fold_max']), ([a, c], 'launch', 2, 6))
        card = self.sent()[-1]
        self.assertEqual(card['text'].split('\n'), [
            'test-city · siding · sample', 'rotation.f / harbor / keepers → launch', 'Waiting (3):',
            f'{a} · Synthetic a · 1pt · eligible', f'{held} · Synthetic b · 1pt · held', f'{c} · Synthetic c · 1pt · eligible',
            'Proposal: fold 2 beads, 2 points (max 6) into one carrier in launch:',
            f'1. {a} · Synthetic a · 1pt', f'2. {c} · Synthetic c · 1pt',
            'Fold makes one carrier in launch of exactly these beads and closes each “folded into” it; undo with yardr bead unfold.',
            s.REPLY])
        self.assertEqual(card['reply_markup']['inline_keyboard'], [
            [dict(text=f'Fold {a}, {c} → launch', callback_data=f"siding:{row['id']}:fold")],
            [dict(text='Leave waiting', callback_data=f"siding:{row['id']}:leave")]])
        closed = self.bridge.api_ask_close(dict(message_id=row['message_id'], decision='Caller text is no authority'))
        self.assertEqual(closed['reason'], 'siding_still_awaiting_human')
        self.assertEqual(self.writes, [])
        answer = self.press(row)
        self.assertEqual(self.writes, [[[a, c], {'To': 'launch', 'Title': ''}, 'pat']])
        folds = self.folds()
        self.assertEqual(len(folds), 1)
        carrier = folds[0]['bead']
        self.assertEqual((folds[0]['data']['members'], folds[0]['data']['to'], folds[0]['actor']), ([a, c], 'launch', 'pat'))
        self.assertEqual(answer, 'Folded into ' + carrier)
        for member in (a, c):
            self.assertEqual((self.bead(member)['status'], self.bead(member)['close_reason']), ('closed', 'folded into ' + carrier))
        self.assertEqual((self.bead(held)['status'], self.bead(held)['stage']), ('open', 'harbor'))
        self.assertEqual(self.bead(carrier)['stage'], 'launch')
        settled = self.row(row)
        self.assertEqual(self.bridge.siding_cards.state(settled), 'settled')
        footer = (f'Folded into {carrier} (launch): {a}, {c}.\n'
                  f'Undo one part: yardr bead unfold {carrier} <member> --reason <why>')
        self.assertEqual(self.edits()[-1]['text'], row['plain'] + '\n' + footer)
        self.assertEqual(self.edits()[-1]['reply_markup']['inline_keyboard'], [])
        receipt = self.bridge.api_ask_close(dict(ask_key='siding', decision='Closed'))
        self.assertTrue(receipt['delivered'], receipt)
        self.assertEqual(int(receipt['message_id']), row['message_id'])
        for member in (a, c):
            self.cli('bead', 'unfold', carrier, member, '--reason', 'synthetic undo', '--to', 'harbor')
            self.assertEqual((self.bead(member)['status'], self.bead(member)['stage']), ('open', 'harbor'))
        edits = len(self.edits())
        self.updates = []
        self.bridge.poll_once()
        self.assertEqual(len(self.edits()), edits)
        self.assertEqual(self.press(row, uid=41), 'Already folded into ' + carrier)
        self.assertEqual(len(self.writes), 1)
        self.assertEqual(len(self.folds()), 1)

    def test_repeated_taps_fold_once(self):
        # The same update twice, then a new tap on the settled card.
        one = [self.part('one-1'), self.part('one-2')]
        row = self.card(key='one')
        self.bridge.inbound(self.press_update(row, uid=40))
        self.bridge.inbound(self.press_update(row, uid=40))
        self.assertEqual((len(self.writes), len(self.folds())), (1, 1))
        self.assertEqual(self.press(row, uid=41), 'Already folded into ' + self.folds()[0]['bead'])
        self.assertEqual((len(self.writes), len(self.folds())), (1, 1))
        self.assertEqual(self.folds()[0]['data']['members'], one)
        cards = self.bridge.siding_cards
        real = cards.client.call
        for committed in (True, False):
            with self.subTest(ambiguous='committed' if committed else 'never sent'):
                folds = len(self.folds())
                members = [self.part(f'{committed}-1'.lower()), self.part(f'{committed}-2'.lower())]
                row = self.card(key=str(committed))
                self.assertEqual(row['siding']['members'], members)
                def lost(method, arguments):
                    if method == 'FoldBeads':
                        if committed:
                            real(method, arguments)
                        else:
                            self.writes.append(arguments)
                        raise n.RPCAmbiguous('native FoldBeads response uncertain')
                    return real(method, arguments)
                cards.client.call = lost
                answer = self.press(row, uid=50 if committed else 52)
                cards.client.call = real
                if committed:
                    carrier = self.folds()[-1]['bead']
                    self.assertEqual(answer, 'Folded into ' + carrier)
                    self.assertEqual(self.bridge.siding_cards.state(self.row(row)), 'settled')
                    self.assertEqual(self.press(row, uid=51), 'Already folded into ' + carrier)
                else:
                    self.assertEqual(answer, 'Nothing folded; the card stays open')
                    self.assertEqual(len(self.folds()), folds)
                    self.assertEqual(self.bridge.siding_cards.state(self.row(row)), 'open')
                    self.assertEqual(self.edits()[-1]['reply_markup']['inline_keyboard'], row['publish_keyboard'])
                    self.assertTrue(self.press(row, uid=53).startswith('Folded into '))
                self.assertEqual(len(self.folds()), folds + 1)
                self.assertEqual(self.folds()[-1]['data']['members'], members)
        self.assertEqual(len(self.writes), 4)

    def claim(self, ident):
        """A session claims ident: an isolated daemon runs an exec group's short command for it."""
        self.cli('group', 'add', 'workers', '--runner', 'exec', '--set', 'cmd=sleep 3')
        self.cli('route', 'add', '--stage', 'harbor', '--group', 'workers', '--depot', 'sample', '--label', 'busy')
        self.cli('bead', 'update', ident, '--add-label', 'busy')
        daemon = subprocess.Popen(self.command + ['daemon', '--interval', '1s'], env=self.env, cwd=self.root,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 20
            while not (self.bead(ident).get('session') and self.bead(ident).get('group') == 'workers'):
                self.assertLess(time.monotonic(), deadline, 'isolated daemon did not claim the bead')
                time.sleep(.1)
        finally:
            testyardr.stop(daemon)
        # Back on the siding's route, still held by the session: stuck as claimed.
        self.cli('bead', 'update', ident, '--remove-label', 'busy')

    def test_changed_beads_refuse_the_tap_before_the_write(self):
        for change in ('claimed', 'closed', 'blocked', 'moved', 'held', 'routed'):
            with self.subTest(change=change):
                case = SidingTests('test_changed_beads_refuse_the_tap_before_the_write')
                case.setUp()
                try:
                    one, two = case.part('one'), case.part('two')
                    row = case.card()
                    if change == 'claimed':
                        case.claim(two)
                    elif change == 'closed':
                        other = case.part('other')
                        case.cli('bead', 'fold', two, other, '--to', 'launch')
                    elif change == 'blocked':
                        case.part('blocker', stage='intake')
                        case.cli('dep', 'add', 'sample-blocker', two)
                    elif change == 'moved':
                        case.cli('bead', 'advance', two, '--to', 'launch')
                    elif change == 'routed':
                        # A more specific route now sends it to another group: it waits in no siding of this one.
                        case.cli('group', 'add', 'riggers', '--runner', 'manual', '--members', 'pat')
                        case.cli('route', 'add', '--stage', 'harbor', '--group', 'riggers', '--depot', 'sample', '--label', 'rush')
                        case.cli('bead', 'update', two, '--add-label', 'rush')
                    else:
                        case.cli('bead', 'hold', two)
                    folds = len(case.folds())
                    # The tap's own re-read refuses, before the poll's refresh would.
                    case.bridge.inbound(case.press_update(row))
                    case.assertEqual(case.answer(40), s.CLOSED['stale'])
                    case.assertEqual(case.writes, [])
                    case.assertEqual(len(case.folds()), folds)
                    case.assertEqual(case.bead(one)['status'], 'open')
                    text = case.edits()[-1]['text']
                    case.assertIn(s.CHANGED, text)
                    case.assertIn(f'{one} · harbor · open', text)
                    state = dict(claimed=f'{two} · harbor · open · claimed', held=f'{two} · harbor · open · held',
                                 blocked=f'{two} · harbor · open · blocked', moved=f'{two} · launch · open',
                                 routed=f'{two} · harbor · open · routed to riggers')
                    if change == 'closed':
                        carrier = case.folds()[-1]['bead']
                        state = f'{two} · harbor · closed · folded into {carrier}\nCarrier {carrier}: launch · open'
                    else:
                        state = state[change]
                    case.assertIn(state, text)
                    case.assertEqual(case.edits()[-1]['reply_markup']['inline_keyboard'], [])
                    case.assertEqual(case.press(row, uid=41), s.CLOSED['stale'])
                    case.assertEqual(case.writes, [])
                finally:
                    case.doCleanups()
                    case.tearDown()

    def test_refused_fold_leaves_the_card_open_and_the_next_read_retires_it(self):
        one, two = self.part('one'), self.part('two')
        row = self.card()
        self.part('blocker', stage='intake')
        cards = self.bridge.siding_cards
        real = cards.client.call
        def race(method, arguments):
            if method == 'FoldBeads':
                # A blocker lands between the bridge's re-read and the write; the store refuses the fold whole.
                self.cli('dep', 'add', 'sample-blocker', two)
            return real(method, arguments)
        cards.client.call = race
        self.bridge.inbound(self.press_update(row))
        cards.client.call = real
        self.assertEqual(self.answer(40), 'The yard refused the fold; nothing folded')
        self.assertEqual((len(self.writes), self.folds()), (1, []))
        self.assertEqual(cards.state(self.row(row)), 'open')
        self.bridge.poll_once()
        self.assertEqual(cards.state(self.row(row)), 'stale')
        self.assertIn(f'{two} · harbor · open · blocked', self.edits()[-1]['text'])
        self.assertEqual(self.bead(one)['status'], 'open')

    def test_crash_between_fold_and_ledger_settles_on_the_next_refresh(self):
        one, two = self.part('one'), self.part('two')
        row = self.card()
        cards = self.bridge.siding_cards
        real = cards.client.call
        def crash(saved):
            raise RuntimeError('synthetic crash before the ledger write')
        def fold(method, arguments):
            result = real(method, arguments)
            if method == 'FoldBeads':
                cards.save = crash
            return result
        cards.client.call = fold
        with self.assertRaisesRegex(RuntimeError, 'synthetic crash'):
            self.bridge.inbound(self.press_update(row))
        stored = self.row(row)
        self.assertEqual((stored['attempted'], stored['outcome']), (True, None))
        self.assertEqual(len(self.folds()), 1)
        carrier = self.folds()[0]['bead']
        self.restart()
        self.assertEqual(self.bridge.siding_cards.presentation(self.row(row)),
                         ('Fold sent; the card reads its beads again before anything else.', []))
        self.bridge.poll_once()
        settled = self.row(row)
        self.assertEqual((self.bridge.siding_cards.state(settled), settled['outcome']['carrier']), ('settled', carrier))
        self.assertIn(f'Folded into {carrier} (launch): {one}, {two}.', self.edits()[-1]['text'])
        self.assertEqual(self.press(row, uid=41), 'Already folded into ' + carrier)
        self.assertEqual((self.writes, len(self.folds())), ([], 1))

    def test_poll_order_replies_and_key_reuse(self):
        # A callback and a reply to the same card in one batch, in both orders.
        for base, first in ((70, 'callback'), (80, 'reply')):
            with self.subTest(first=first):
                members = [self.part(first + '-1'), self.part(first + '-2')]
                row = self.card(key=first)
                folds = len(self.folds())
                order = (base, base + 1) if first == 'callback' else (base + 1, base)
                press, reply = self.press_update(row, uid=order[0]), self.reply_update(row, 'fold ' + ' '.join(members), uid=order[1])
                self.updates = [press, reply] if first == 'callback' else [reply, press]
                self.bridge.poll_once()
                if first == 'callback':
                    self.assertEqual(len(self.folds()), folds + 1)
                    self.assertEqual(self.notice(base + 1), 'This card is closed; request a new card. Nothing folded.')
                    settled = row
                else:
                    self.assertEqual(len(self.folds()), folds)
                    self.assertEqual(self.notice(base), 'Review the new card; the reply folded nothing.')
                    self.assertEqual(self.answer(base + 1), s.CLOSED['withdrawn'])
                    settled, = self.explicit()
                    self.assertEqual(settled['siding']['members'], members)
                    self.assertEqual(self.press(settled, uid=base + 2), 'Folded into ' + self.folds()[-1]['bead'])
                    self.assertEqual(len(self.folds()), folds + 1)
                # A stale reply to a settled card answers and writes nothing.
                cards = len(self.bridge.siding_cards.rows())
                writes = len(self.writes)
                self.assertEqual(self.reply(settled, 'fold ' + ' '.join(members), uid=base + 3),
                                 'This card is closed; request a new card. Nothing folded.')
                self.assertEqual((len(self.bridge.siding_cards.rows()), len(self.writes)), (cards, writes))
        # An idempotency key reused with another request is a conflict, and nothing is sent.
        sent = len(self.sent())
        receipt = self.bridge.api_siding_card(dict(depot='sample', key='callback', stage='harbor'))
        self.assertEqual((receipt['delivered'], receipt['reason']), (False, 'idempotency_key_conflict'))
        self.assertEqual(len(self.sent()), sent)

    def test_unbound_home_on_a_reply_keeps_the_poll_loop(self):
        one, two = self.part('one'), self.part('two')
        row = self.card()
        self.bridge.hook_secret = 'HOOKSECRET'
        self.home.chmod(0o777)
        try:
            self.updates = [self.reply_update(row, f'fold {one} {two}', uid=80), self.press_update(row, uid=81)]
            with patch('sys.stderr') as log:
                self.bridge.poll_once()
            self.assertEqual(self.store.offset(), 82)
            logged = ''.join(c.args[0] for c in log.write.call_args_list)
            self.assertIn('siding reply not bound (update 80)', logged)
            self.assertIn('siding callback not bound (update 81)', logged)
            self.assertEqual(self.answer(81), 'yardr.home must be an owner-held directory without group/world writes')
            self.assertEqual(self.bridge.siding_cards.rows(), [row])
            # Not a siding update: the reply reaches the yard as any message does.
            relayed = [body for url, body in self.calls if url == self.config['hook_url']]
            self.assertEqual(len(relayed), 1)
        finally:
            self.home.chmod(0o700)
        self.assertEqual((self.writes, self.folds()), ([], []))
        self.assertTrue(self.press(row, uid=82).startswith('Folded into '))

    def test_status_cards_and_explicit_subsets(self):
        a, b_, c = self.part('a', 'large'), self.part('b', 'large'), self.part('c')
        held, asked = self.part('held'), self.part('asked')
        self.cli('bead', 'hold', held)
        self.cli('bead', 'ask', asked, 'Which wording?', '--to', 'pat', '--context', 'Synthetic question')
        row = self.card()
        self.assertEqual((row['siding']['points'], row['siding']['reasons'], row['actionable']), (9, ['ceiling'], False))
        card = self.sent()[-1]
        self.assertEqual(card['reply_markup']['inline_keyboard'], [])
        self.assertIn('Status only: over fold_max (9 > 6).', card['text'])
        self.assertIn(s.REPLY, card['text'])
        self.assertIn(f'{asked} · Synthetic asked · 1pt · asked', card['text'])
        self.assertEqual(self.press(row), s.CLOSED['status'])  # a status card has no button; a forged tap folds nothing
        self.assertEqual(self.reply(row, f'fold {a}', uid=43), s.REPLY)
        self.assertEqual(self.reply(row, f'fold {a} sample-none', uid=44), 'Not open in this siding: sample-none. Nothing folded.')
        self.assertEqual(self.reply(row, f'fold {a} {held}', uid=45),
                         f'{held} is held. Fold a held or asked bead with yardr bead fold in a terminal. Nothing folded.')
        self.assertEqual(self.reply(row, f'fold {asked} {a}', uid=46),
                         f'{asked} is asked. Fold a held or asked bead with yardr bead fold in a terminal. Nothing folded.')
        self.assertEqual((self.writes, self.folds(), len(self.bridge.siding_cards.rows())), ([], [], 1))
        self.assertEqual(self.reply(row, f'fold {b_} {a}', uid=47), 'Review the new card; the reply folded nothing.')
        explicit, = self.explicit()
        self.assertEqual(self.bridge.siding_cards.state(self.row(row)), 'withdrawn')
        self.assertEqual((explicit['siding']['members'], explicit['siding']['points'], explicit['actionable']), ([b_, a], 8, True))
        self.assertIn('Proposal: fold 2 beads, 8 points (explicit subset, no max) into one carrier in launch:', explicit['plain'])
        self.assertEqual(self.writes, [])
        self.assertTrue(self.press(explicit, uid=48).startswith('Folded into '))
        self.assertEqual(self.writes, [[[b_, a], {'To': 'launch', 'Title': ''}, 'pat']])
        # Fewer than two a fold would take, an empty siding and no siding at all are status only.
        few = self.card(key='few')
        self.assertEqual((few['siding']['members'], few['siding']['reasons']), ([c], ['few']))
        self.assertIn('Proposal: fold 1 bead, 1 point (max 6) into one carrier in launch:', few['plain'])
        self.assertIn('Status only: fewer than two beads a fold would take.', few['plain'])
        self.cli('route', 'add', '--stage', 'repair', '--group', 'keepers', '--depot', 'sample')
        empty = self.card(key='empty', stage='repair')
        self.assertEqual((empty['siding']['waiting'], empty['siding']['reasons']), ([], ['few']))
        none = self.card(key='none', stage='launch')
        self.assertIsNone(none['siding'])
        self.assertIn('No siding matches this selection', none['plain'])
        self.assertEqual(self.reply(none, f'fold {c} {held}', uid=49), 'No siding on this card; nothing folded.')
        self.cli('group', 'set', 'keepers', 'fold_max=12')
        self.assertEqual(self.card(key='max')['siding']['fold_max'], 12)

    def manual_claim(self, ident):
        """The siding's manual group claims ident: an isolated daemon's pass, then stopped."""
        daemon = subprocess.Popen(self.command + ['daemon', '--interval', '1s'], env=self.env, cwd=self.root,
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        try:
            deadline = time.monotonic() + 20
            while not (self.bead(ident).get('session') and self.bead(ident).get('group') == 'keepers'):
                self.assertLess(time.monotonic(), deadline, 'isolated daemon did not claim the bead for the siding')
                time.sleep(.1)
        finally:
            testyardr.stop(daemon)

    def release_rows(self):
        return [row for row in self.bridge.siding_cards.rows() if row.get('action') == 'release']

    def release_card(self, row, ident, uid):
        """A reply asks a release card for one bead of row's siding: sent, and nothing moves."""
        self.assertEqual(self.reply(row, f'release {ident}', uid=uid), 'Review the release card; the reply released nothing.')
        release = max(self.release_rows(), key=lambda r: r['message_id'])
        self.assertEqual((release['release']['bead'], self.bead(ident)['stage']), (ident, 'harbor'))
        return release

    def sent_card(self, start):
        return [m for m in self.sent() if m['text'].startswith(start)][-1]

    def test_release_card_moves_one_bead_on_one_tap(self):
        a, held, c = self.part('a'), self.part('b'), self.part('c')
        self.cli('bead', 'hold', held)
        row = self.card()
        self.assertEqual(self.reply(row, f'release {held}', uid=40), f'{held} is held. Nothing released.')
        self.assertEqual(self.reply(row, 'release', uid=41), s.RELEASE_REPLY)
        self.assertEqual(self.reply(row, f'release {a} {c}', uid=42), s.RELEASE_REPLY)
        self.assertEqual(self.reply(row, 'release sample-none', uid=43), 'Not open in this siding: sample-none. Nothing released.')
        self.assertEqual((self.advances, self.writes, self.release_rows()), ([], [], []))
        release = self.release_card(row, a, uid=44)
        card = self.sent_card(f"{self.config['yard']} · siding · sample\nrotation.f / harbor / keepers → launch\nRelease")
        self.assertEqual(card['text'].split('\n'), [
            f"{self.config['yard']} · siding · sample", 'rotation.f / harbor / keepers → launch',
            f'Release {a} · Synthetic a: from harbor into launch.',
            'One advance moves this bead alone; nothing else moves. A tap refuses if it changed since this card.'])
        self.assertEqual(card['reply_markup']['inline_keyboard'], [
            [dict(text=f'Release {a} → launch', callback_data=f"siding:{release['id']}:release")],
            [dict(text='Leave waiting', callback_data=f"siding:{release['id']}:leave")]])
        revision = self.bead(a)['revision']
        self.assertEqual(release['release'], dict(depot='sample', stage='harbor', group='keepers', flow='rotation.f', path='',
                                                  bead=a, title='Synthetic a', to='launch', bead_revision=revision))
        self.assertEqual(self.press(release, 'release', uid=45), 'Released into launch')
        self.assertEqual(self.advances, [[a, 'launch', dict(stage='harbor', revision=revision), 'pat']])
        self.assertEqual((self.bead(a)['stage'], self.bead(a)['status']), ('launch', 'open'))
        self.assertEqual((self.bead(c)['stage'], self.bead(held)['stage']), ('harbor', 'harbor'))
        self.assertEqual(self.writes, [])
        self.assertEqual(self.bridge.siding_cards.state(self.row(release)), 'settled')
        self.assertEqual(self.edits()[-1]['text'], release['plain'] + f'\nReleased {a} from harbor into launch.')
        self.assertEqual(self.edits()[-1]['reply_markup']['inline_keyboard'], [])
        self.assertEqual(self.press(release, 'release', uid=46), 'Already released into launch')
        self.assertEqual(len(self.advances), 1)
        # The siding card still lists a, which has moved: its fold refuses, and writes nothing.
        self.assertEqual(self.press(row, uid=47), s.CLOSED['stale'])
        self.assertEqual((self.writes, len(self.advances)), ([], 1))

    def test_release_moves_a_bead_its_siding_group_claims(self):
        # The siding's manual group claims its beads; the release moves the bead on, and the move ends that claim.
        a, c = self.part('a'), self.part('c')
        self.manual_claim(a)
        row = self.card()
        self.assertIn(f'{a} · Synthetic a · 1pt · eligible', self.sent()[-1]['text'])
        release = self.release_card(row, a, uid=40)
        self.assertEqual(self.press(release, 'release', uid=41), 'Released into launch')
        self.assertEqual((self.bead(a)['stage'], self.bead(a)['status'], bool(self.bead(a).get('session'))), ('launch', 'open', False))
        self.assertEqual([call[:2] for call in self.advances], [[a, 'launch']])
        self.assertEqual(self.bead(c)['stage'], 'harbor')

    def test_release_taps_write_once(self):
        a = self.part('a')
        row = self.card()
        release = self.release_card(row, a, uid=40)
        self.bridge.inbound(self.press_update(release, 'release', uid=41))
        self.bridge.inbound(self.press_update(release, 'release', uid=41))
        self.assertEqual((len(self.advances), self.bead(a)['stage']), (1, 'launch'))
        self.assertEqual(self.press(release, 'release', uid=42), 'Already released into launch')
        self.assertEqual(len(self.advances), 1)
        cards = self.bridge.siding_cards
        real = cards.client.call
        for committed in (True, False):
            with self.subTest(ambiguous='committed' if committed else 'never sent'):
                base = 50 if committed else 60
                ident = self.part(f'ambiguous-{committed}'.lower())
                row = self.card(key=f'ambiguous-{committed}'.lower())
                release = self.release_card(row, ident, uid=base)
                before = len(self.advances)
                def lost(method, arguments):
                    if method == 'Advance':
                        if committed:
                            real(method, arguments)
                        else:
                            self.advances.append(copy.deepcopy(arguments))
                        raise n.RPCAmbiguous('native Advance response uncertain')
                    return real(method, arguments)
                cards.client.call = lost
                answer = self.press(release, 'release', uid=base + 1)
                cards.client.call = real
                self.assertEqual(len(self.advances), before + 1)
                if committed:
                    self.assertEqual(answer, 'Released into launch')
                    self.assertEqual(self.bridge.siding_cards.state(self.row(release)), 'settled')
                    self.assertEqual(self.press(release, 'release', uid=base + 2), 'Already released into launch')
                    self.assertEqual(len(self.advances), before + 1)
                else:
                    self.assertEqual(answer, 'Nothing released; the card stays open')
                    self.assertEqual((self.bead(ident)['stage'], self.bridge.siding_cards.state(self.row(release))), ('harbor', 'open'))
                    self.assertEqual(self.edits()[-1]['reply_markup']['inline_keyboard'], release['publish_keyboard'])
                    self.assertEqual(self.press(release, 'release', uid=base + 2), 'Released into launch')
                    self.assertEqual(len(self.advances), before + 2)

    def test_release_refuses_a_changed_bead_before_the_write(self):
        for change in ('claimed', 'closed', 'blocked', 'moved', 'held', 'routed', 'revision', 'race'):
            with self.subTest(change=change):
                case = SidingTests('test_release_refuses_a_changed_bead_before_the_write')
                case.setUp()
                try:
                    one, two = case.part('one'), case.part('two')
                    row = case.card()
                    release = case.release_card(row, two, uid=40)
                    stage = 'harbor'
                    if change == 'claimed':
                        case.claim(two)
                    elif change == 'closed':
                        case.cli('bead', 'close', two, '--reason', 'synthetic close')
                    elif change == 'blocked':
                        case.part('blocker', stage='intake')
                        case.cli('dep', 'add', 'sample-blocker', two)
                    elif change == 'moved':
                        case.cli('bead', 'advance', two, '--to', 'launch')
                        stage = 'launch'
                    elif change == 'held':
                        case.cli('bead', 'hold', two)
                    elif change == 'routed':
                        # A more specific route now sends it to another group: it waits in no siding of this one.
                        case.cli('group', 'add', 'riggers', '--runner', 'manual', '--members', 'pat')
                        case.cli('route', 'add', '--stage', 'harbor', '--group', 'riggers', '--depot', 'sample', '--label', 'rush')
                        case.cli('bead', 'update', two, '--add-label', 'rush')
                    elif change == 'revision':
                        # Nothing about its stage or routing changes; only its revision does.
                        case.cli('bead', 'update', two, '--add-label', 'synthetic')
                    else:
                        # A label lands between the bridge's re-read and the advance: the store refuses the stale revision.
                        real = case.bridge.siding_cards.client.call
                        def race(method, arguments):
                            if method == 'Advance':
                                case.cli('bead', 'update', two, '--add-label', 'synthetic')
                            return real(method, arguments)
                        case.bridge.siding_cards.client.call = race
                    shown = case.bead(two)
                    answer = case.press(release, 'release', uid=41)
                    if change == 'race':
                        self.assertEqual(answer, 'Not released as shown; see the card')
                        self.assertEqual(case.advances, [[two, 'launch', dict(stage='harbor', revision=shown['revision']), 'pat']])
                        self.assertIn('Not released as shown; nothing more runs from this card.\nNow:\n' + f'{two} · harbor · open',
                                      case.edits()[-1]['text'])
                    else:
                        self.assertEqual(answer, s.RELEASE_CLOSED['stale'])
                        self.assertEqual(case.advances, [])
                        state = dict(claimed=f'{two} · harbor · open · claimed', closed=f'{two} · harbor · closed · synthetic close',
                                     blocked=f'{two} · harbor · open · blocked', moved=f'{two} · launch · open',
                                     held=f'{two} · harbor · open · held', routed=f'{two} · harbor · open · routed to riggers',
                                     revision=f'{two} · harbor · open')[change]
                        self.assertIn(s.RELEASE_CHANGED, case.edits()[-1]['text'])
                        self.assertIn('Now:\n' + state, case.edits()[-1]['text'])
                    self.assertEqual(case.edits()[-1]['reply_markup']['inline_keyboard'], [])
                    self.assertEqual(case.bead(two)['stage'], stage)
                    self.assertEqual(case.bead(one)['status'], 'open')
                    self.assertEqual(case.press(release, 'release', uid=42), s.RELEASE_CLOSED['stale'])
                    self.assertEqual(len(case.advances), 0 if change != 'race' else 1)
                    self.assertEqual(case.writes, [])
                finally:
                    case.doCleanups()
                    case.tearDown()

    def test_release_crash_between_advance_and_ledger_settles_on_the_next_refresh(self):
        one, two = self.part('one'), self.part('two')
        row = self.card()
        release = self.release_card(row, two, uid=40)
        cards = self.bridge.siding_cards
        real = cards.client.call
        def crash(saved):
            raise RuntimeError('synthetic crash before the ledger write')
        def advance(method, arguments):
            result = real(method, arguments)
            if method == 'Advance':
                cards.save = crash
            return result
        cards.client.call = advance
        with self.assertRaisesRegex(RuntimeError, 'synthetic crash'):
            self.bridge.inbound(self.press_update(release, 'release', uid=41))
        stored = self.row(release)
        self.assertEqual((stored['attempted'], stored['outcome']), (True, None))
        self.assertEqual((len(self.advances), self.bead(two)['stage']), (1, 'launch'))
        self.restart()
        self.assertEqual(self.bridge.siding_cards.presentation(self.row(release)),
                         ('Release sent; the card reads its bead again before anything else.', []))
        self.bridge.poll_once()
        settled = self.row(release)
        self.assertEqual((self.bridge.siding_cards.state(settled), settled['outcome']['to']), ('settled', 'launch'))
        self.assertIn(f'Released {two} from harbor into launch.', self.edits()[-1]['text'])
        self.assertEqual(self.press(release, 'release', uid=42), 'Already released into launch')
        # Nothing ran again after the restart: the advance was not sent twice, and the other bead is still open.
        self.assertEqual((self.advances, self.writes, self.bead(one)['status']), ([], [], 'open'))

    def test_release_poll_order_and_replies_that_never_release(self):
        a, c = self.part('a'), self.part('c')
        row = self.card()
        release = self.release_card(row, a, uid=40)
        # A tap and a reply to the same release card in one batch, the tap first and then the reply.
        self.updates = [self.press_update(release, 'release', uid=70), self.reply_update(release, f'release {a}', uid=71)]
        self.bridge.poll_once()
        self.assertEqual(self.answer(70), 'Released into launch')
        self.assertEqual(self.notice(71), s.RELEASE_CARD)
        self.assertEqual(self.bead(a)['stage'], 'launch')
        # The reply first: the card's own tap still releases its bead, once.
        row = self.card(key='second')
        second = self.release_card(row, c, uid=80)
        self.updates = [self.reply_update(second, f'release {c}', uid=81), self.press_update(second, 'release', uid=82)]
        self.bridge.poll_once()
        self.assertEqual(self.notice(81), s.RELEASE_CARD)
        self.assertEqual(self.answer(82), 'Released into launch')
        self.assertEqual(self.bead(c)['stage'], 'launch')
        # Neither a forged tap of the other kind nor a reply to a settled release card writes anything.
        self.assertEqual(self.press(second, 'fold', uid=83), 'Siding card not authorized')
        self.assertEqual(self.press(row, 'release', uid=84), 'Siding card not authorized')
        self.assertEqual(self.reply(second, f'fold {c}', uid=85), s.RELEASE_CARD)
        self.assertEqual((len(self.advances), self.writes), (2, []))

    def test_routes_decide_which_beads_wait_in_the_siding(self):
        # As the store's MatchRoute: a more specific route takes its beads to another group, siding or not.
        a, c = self.part('a'), self.part('c')
        rush, stow1, stow2 = self.part('rush'), self.part('stow1'), self.part('stow2')
        self.cli('group', 'add', 'riggers', '--runner', 'manual', '--members', 'pat')
        self.cli('group', 'add', 'stowers', '--runner', 'manual', '--members', 'pat', '--set', 'fold_max=3')
        self.cli('route', 'add', '--stage', 'harbor', '--group', 'riggers', '--depot', 'sample', '--label', 'rush')
        self.cli('route', 'add', '--stage', 'harbor', '--group', 'stowers', '--depot', 'sample', '--label', 'stow')
        self.cli('bead', 'update', rush, '--add-label', 'rush')
        for ident in (stow1, stow2):
            self.cli('bead', 'update', ident, '--add-label', 'stow')
        receipt = self.bridge.api_siding_card(dict(depot='sample', key='routes'))
        self.assertTrue(receipt['delivered'], receipt)
        rows = {row['siding']['group']: row for row in self.bridge.siding_cards.rows()}
        self.assertEqual({group: [w['bead'] for w in row['siding']['waiting']] for group, row in rows.items()},
                         dict(keepers=[a, c], stowers=[stow1, stow2]))
        self.assertEqual(rows['stowers']['siding']['fold_max'], 3)
        self.assertNotIn(rush, rows['keepers']['plain'])
        self.assertEqual(self.reply(rows['keepers'], f'fold {a} {rush}'), f'Not open in this siding: {rush}. Nothing folded.')
        self.assertEqual(self.reply(rows['keepers'], f'fold {a} {stow1}', uid=42), f'Not open in this siding: {stow1}. Nothing folded.')
        self.assertTrue(self.press(rows['stowers'], uid=43).startswith('Folded into '))
        self.assertEqual(self.writes, [[[stow1, stow2], {'To': 'launch', 'Title': ''}, 'pat']])
        self.assertEqual(self.bead(rush)['status'], 'open')

    def test_wrong_bindings_leave_waiting_and_expiry(self):
        self.part('1'), self.part('2')
        row = self.card()
        for uid, change in enumerate(({'from': dict(id=321, is_bot=False)}, {'from': dict(id=123, is_bot=True)},
                {'message': dict(message_id=row['message_id'] + 1, chat=dict(id=123, type='private'), **{'from': dict(id=999, is_bot=True)})},
                {'message': dict(message_id=row['message_id'], chat=dict(id=123, type='private'), forward_date=1, **{'from': dict(id=999, is_bot=True)})},
                {'message': dict(message_id=row['message_id'], chat=dict(id=123, type='private'), message_thread_id=12, is_topic_message=True, **{'from': dict(id=999, is_bot=True)})}), 50):
            self.press(row, uid=uid, **change)
        self.assertEqual(self.writes, [])
        self.config['yard'] = 'another-yard'
        self.restart()
        self.press(row, uid=60)
        self.config['yard'] = 'test-city'
        self.restart()
        self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '456')
        self.assertEqual(self.press(row, uid=61), 'operator_not_authorized')
        self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
        self.assertEqual(self.writes, [])
        self.assertEqual(self.press(row, action='leave', uid=62), 'Left waiting; nothing folded')
        self.assertEqual(self.edits()[-1]['reply_markup']['inline_keyboard'], [])
        self.restart()
        self.assertEqual(self.press(row, uid=63), s.CLOSED['withdrawn'])
        expiring = self.card(key='expires')
        expiring['expires'] = 0
        self.bridge.siding_cards.save(expiring)
        self.assertEqual(self.press(expiring, uid=64), s.CLOSED['expired'])
        self.assertIn('Card expired after 15 minutes', self.edits()[-1]['text'])
        # Another store with the same Telegram human never acts on a card from this one.
        other_home = self.root / 'other-home'
        other_home.mkdir(mode=0o700)
        self.config['yardr_home'] = str(other_home)
        self.restart()
        self.press(row, uid=65)
        self.assertEqual((self.writes, self.folds()), ([], []))

    def legacy_row(self):
        """A siding row as the previous release wrote it: guarded schema, sp1 digest, with its card delivered."""
        previous = load_previous(self.root)
        if previous is None:
            self.skipTest('the previous bridge release is not in this checkout')
        old = previous.siding_cards.SidingCards(self.bridge, self.bridge.siding_cards.client)
        members = [dict(bead='sample-old-' + str(i), revision='revision-' + str(i), points=1, stuck='',
                        entered='2026-01-01T00:00:00Z', title='Synthetic part') for i in (1, 2)]
        value = previous.siding_cards.proposal(dict(
            version=1, digest='sp1:' + 'a' * 64, depot='sample', stage='harbor', to='launch', guard='store', unguarded=[],
            ask=dict(depot='sample', stage='harbor', flow='rotation.f', group='keepers', action='fold',
                     members=[m['bead'] for m in members], to='launch'),
            flow=dict(name='rotation.f', source=dict(kind='store'), digest='c' * 64),
            group=dict(name='keepers', runner='manual', members=['pat'], fold=4, fold_after='1d', fold_max=6),
            routes=[dict(id='route-harbor', depot='sample', stage='harbor', group='keepers')], waiting=members,
            members=[dict(m, stage='harbor', route='route-harbor') for m in members], points=2, fold_max=6,
            ready=False, trigger='', due='', refusals=[]))
        row = old.new_row(dict(depot='sample'), value, 'legacy:0', '123', False)
        row['batch'] = [row['id']]
        old.save(row)
        receipt = old.send_card(row)
        self.assertTrue(receipt['delivered'], receipt)
        return previous, old, old.get(row['id'])

    def test_previous_release_rows_retire_and_survive_a_rollback(self):
        previous, old, legacy = self.legacy_row()
        self.assertTrue(legacy['publish_keyboard'])
        # This release retires the open row once: its own withdrawn field, and its message edited without buttons.
        self.restart()
        self.bridge.poll_once()
        retired = old.get(legacy['id'])
        self.assertEqual((retired['withdrawn'], retired['outcome'], retired['attempted']), (s.RETIRED, None, False))
        edit = self.edits()[-1]
        self.assertEqual((edit['message_id'], edit['reply_markup']['inline_keyboard']), (legacy['message_id'], []))
        self.assertEqual(edit['text'], legacy['plain'] + '\n' + s.RETIRED)
        edits = len(self.edits())
        self.assertEqual(self.press(legacy, action='approve', uid=40), s.RETIRED)
        self.assertEqual(self.reply(legacy, 'fold sample-old-1 sample-old-2', uid=41), s.RETIRED)
        self.bridge.poll_once()
        self.assertEqual((len(self.edits()), self.writes, self.folds()), (edits, [], []))
        receipt = self.bridge.api_ask_close(dict(message_id=legacy['message_id'], decision='Closed'))
        self.assertTrue(receipt['delivered'], receipt)
        # Rows this release writes leave the previous release's poll and refresh loop running after a rollback.
        one, two = self.part('one'), self.part('two')
        current = self.card(key='current')
        settled_members = [self.part('three'), self.part('four')]
        self.cli('bead', 'hold', one)
        self.cli('bead', 'hold', two)
        settled = self.card(key='settled')
        self.assertEqual(settled['siding']['members'], settled_members)
        self.press(settled, uid=42)
        self.assertEqual(self.bridge.siding_cards.state(self.row(settled)), 'settled')
        folds = len(self.folds())
        rollback = previous.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=self.telegram)
        rollback.hook_secret = 'HOOKSECRET'
        start = len(self.calls)
        self.updates = [self.press_update(current, uid=90), self.reply_update(current, f'fold {one} {two}', uid=91),
                        self.press_update(settled, uid=92), self.press_update(legacy, action='approve', uid=93)]
        rollback.poll_once()
        rollback.poll_once()
        rollback.siding_cards.refresh()
        rollback.run_asks.refresh()
        self.assertEqual(rollback.store.offset(), 94)
        self.assertEqual(len(self.folds()), folds)
        self.assertEqual(rollback.siding_cards.state(rollback.siding_cards.get(legacy['id'])), 'withdrawn')
        self.assertFalse([body for url, body in self.calls[start:] if url.endswith('/editMessageText')])
        self.assertEqual(rollback.api_ask_close(dict(ask_key='current', decision='Closed'))['reason'], 'ask_not_found')

    def test_release_card_survives_a_rollback_to_the_plain_fold_runtime(self):
        previous = load_previous(self.root, PLAIN_FOLD)
        if previous is None:
            self.skipTest('the plain-fold bridge release is not in this checkout')
        a, c = self.part('a'), self.part('c')
        row = self.card()
        release = self.release_card(row, a, uid=40)
        folds = len(self.folds())
        # The plain-fold poll reads the release row's table: it must neither crash nor act on the release card.
        rollback = previous.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=self.telegram)
        rollback.hook_secret = 'HOOKSECRET'
        self.updates = [self.press_update(release, 'leave', uid=90), self.press_update(row, uid=91)]
        rollback.poll_once()
        self.assertEqual(rollback.store.offset(), 92)
        self.assertEqual(self.answer(90), 'Siding card not authorized')
        self.assertEqual((self.bead(a)['status'], self.bead(c)['status']), ('closed', 'closed'))
        self.assertEqual((len(self.folds()), self.advances), (folds + 1, []))
        self.assertEqual(self.bridge.siding_cards.state(self.row(release)), 'open')

    def test_authenticated_endpoint_cli_and_installed_module(self):
        port = self.start_callback_server()
        empty_request = dict(depot='sample', key='empty-http')
        def unavailable(url, body, *args, **kwargs):
            if url.endswith('/sendMessage'):
                raise b.RequestNotSent('synthetic offline', 7, 'connect')
            return self.telegram(url, body, *args, **kwargs)
        with patch.object(self.bridge, 'request', side_effect=unavailable), patch.object(b.time, 'sleep'):
            status, result = self.api_call(port, '/v1/siding-card', empty_request)
        self.assertEqual(status, 200)
        self.assertEqual((result['delivered'], result['reason'], result['failure_kind']),
                         (False, 'transport_unavailable_not_sent', 'transient'))
        empty = self.bridge.siding_cards.rows()[0]
        self.assertEqual(empty['siding']['waiting'], [])
        self.assertIsNone(empty['message_id'])
        send_key, _ = self.bridge.siding_cards.send_request(empty)
        delivery = self.store.saved_delivery(send_key)
        self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '456')
        self.restart()
        port = self.start_callback_server()
        before = len(self.calls)
        status, result = self.api_call(port, '/v1/siding-card', empty_request)
        self.assertEqual(status, 200)
        self.assertFalse(result['delivered'], result)
        self.assertEqual(result['reason'], 'operator_not_authorized')
        self.assertEqual(self.calls[before:], [])
        self.assertEqual(self.bridge.siding_cards.get(empty['id']), empty)
        self.assertEqual(self.store.saved_delivery(send_key), delivery)
        self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
        for _ in range(2):
            status, result = self.api_call(port, '/v1/siding-card', empty_request)
            self.assertEqual(status, 200)
            self.assertTrue(result['delivered'], result)
        sends = [body for url, body in self.calls[before:] if url.endswith('/sendMessage')]
        self.assertEqual(len(sends), 1)
        sent = self.bridge.siding_cards.get(empty['id'])
        self.assertEqual(int(result['message_id']), sent['message_id'])
        self.assertEqual(self.store.saved_delivery(send_key)[1],
                         dict(result, conversation=dict(self.bridge.conversation, conversation_id=sent['conversation'])))
        self.part('1')
        self.part('2')
        self.bridge.topic_settings = (dict(name='ops', agent='yardmaster'),)
        self.store.record_topic('ops', 12, 'created')
        port = self.start_callback_server()
        request = dict(depot='sample', key='http', topic='ops')
        attempts = []
        def offline(url, body, *args, **kwargs):
            if url.endswith('/sendMessage'):
                attempts.append(copy.deepcopy(body))
                raise b.RequestNotSent('synthetic offline', 7, 'connect')
            return self.telegram(url, body, *args, **kwargs)
        with patch.object(self.bridge, 'request', side_effect=offline), patch.object(b.time, 'sleep'):
            status, result = self.api_call(port, '/v1/siding-card', request)
        self.assertEqual(status, 200)
        self.assertEqual((result['delivered'], result['reason'], result['failure_kind']),
                         (False, 'transport_unavailable_not_sent', 'transient'))
        self.assertEqual(len(attempts), 3)
        self.restart()
        self.bridge.topic_settings = (dict(name='ops', agent='yardmaster'),)
        port = self.start_callback_server()
        before = len(self.calls)
        status, result = self.api_call(port, '/v1/siding-card', request)
        self.assertEqual(status, 200)
        self.assertTrue(result['delivered'], result)
        sends = [body for url, body in self.calls[before:] if url.endswith('/sendMessage')]
        self.assertEqual(sends, [attempts[-1]])
        self.assertTrue(sends[0]['reply_markup']['inline_keyboard'])
        for key in ('actor', 'command', 'folded', 'members', 'to', 'digest', 'op', 'action'):
            status, _ = self.api_call(port, '/v1/siding-card', dict(depot='sample', key='bad', **{key: 'forged'}))
            self.assertEqual(status, 400)
        status, _ = self.api_call(port, '/v1/siding-card', dict(depot='sample', key='wrong-auth'), headers={'Authorization': 'Bearer wrong'})
        self.assertEqual(status, 403)
        declaration = (Path(b.__file__).resolve().parents[1] / 'yardr' / 'tgbridge.yaml').read_text()
        declaration = declaration.replace('base: http://127.0.0.1:8941', 'base: http://127.0.0.1:' + str(port), 1)
        declared = self.root / 'declared.yaml'
        declared.write_text(declaration)
        self.client.call('PutSource', [str(declared), declaration, 'pat'])
        credential = self.root / 'synthetic.env'
        credential.write_text('TGBRIDGE_SECRET=CAPABILITY\n')
        credential.chmod(0o600)
        self.cli('source', 'set', 'tgbridge', 'env_file=' + str(credential))
        self.cli('source', 'grant', 'tgbridge', 'siding_card', '--group', 'keepers')
        answer = json.loads(self.cli('call', 'tgbridge', 'siding_card', 'depot=sample', 'key=source-call', '--json'))
        self.assertTrue(answer['delivered'], answer)
        row = next(r for r in self.bridge.siding_cards.rows() if r['conversation'] == '123:topic:12')
        self.press(row)
        self.assertEqual(self.folds(), [])
        message = dict(message_id=row['message_id'], chat=dict(id=123, type='private'),
                       message_thread_id=12, is_topic_message=True, **{'from': dict(id=999, is_bot=True)})
        self.press(row, uid=42, message=message)
        self.assertEqual(len(self.folds()), 1)
        # A release copy loads adjacent modules under both supported launch modes.
        release = self.root / 'release'
        (release / 'bridge').mkdir(parents=True)
        (release / 'transcriber').mkdir()
        shutil.copytree(Path(b.__file__).resolve().parents[1] / 'vendor', release / 'vendor')
        for name in ('bridge.py', 'run_asks.py', 'siding_cards.py', 'markdown.py'):
            shutil.copyfile(Path(b.__file__).with_name(name), release / 'bridge' / name)
        shutil.copyfile(Path(b.__file__).resolve().parents[1] / 'transcriber' / 'voice_transcribe.py', release / 'transcriber' / 'voice_transcribe.py')
        program = str(release / 'bridge' / 'bridge.py')
        for argv in ([sys.executable, program, 'siding-card', '--help'],
                     [sys.executable, '-c', 'import runpy,sys; runpy.run_path(sys.argv[1])', program]):
            result = subprocess.run(argv, env=self.env, cwd=self.root, text=True, capture_output=True)
            self.assertEqual(result.returncode, 0, result.stderr)
        parser = b._build_parser()
        args = parser.parse_args(['siding-card', '--depot', 'sample', '--stage', 'harbor', '--idempotency-key', 'cli'])
        b._validate_attachment_flags(parser, args)
        self.assertEqual(args.depot, 'sample')
        settings = write_settings(self.root)
        settings.write_text(settings.read_text().replace('[yardr]', f'[yardr]\nhome = "{self.home}"\noperator = "pat"'))
        fake_bin = self.root / 'bin'
        fake_bin.mkdir()
        curl = fake_bin / 'curl'
        curl.write_text('#!' + sys.executable + '\nimport json,sys\n'
                        'sys.stdin.read()\n'
                        'print(json.dumps({"ok":True,"result":{"message_id":501,"chat":{"id":123}}}))\n'
                        'print("200 0.1 0.2 1", end="")\n')
        curl.chmod(0o700)
        result = subprocess.run([sys.executable, program, '--settings', str(settings), 'siding-card',
                                 '--depot', 'sample', '--stage', 'launch', '--idempotency-key', 'installed-cli'],
                                env=dict(self.env, PATH=str(fake_bin) + ':' + self.env['PATH']), cwd=self.root,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['delivered'])
        cli_store = b.Store(self.root / 'state' / 'telegram-bridge.sqlite3', 6)
        with cli_store.connect() as db:
            rows = [json.loads(r[0]) for r in db.execute('SELECT data FROM ' + s.TABLE)]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'siding')
        self.assertIsNone(rows[0]['siding'])

    def test_two_real_stores_same_telegram_human_are_independent(self):
        self.part('1')
        self.part('2')
        first = self.card()
        other = SidingTests(methodName='test_two_real_stores_same_telegram_human_are_independent')
        other.setUp()
        self.addCleanup(other.tearDown)
        self.addCleanup(other.doCleanups)
        other.part('1')
        other.part('2')
        second = other.card()
        self.assertEqual(self.config['user_id'], other.config['user_id'])
        other.press(first)
        self.assertEqual(other.folds(), [])
        other.press(second, uid=42)
        self.assertEqual(len(other.folds()), 1)
        self.assertEqual(self.folds(), [])
        self.press(first)
        self.assertEqual(len(self.folds()), 1)


def receipt_row(bridge, receipt):
    """The id of the row a delivered siding receipt belongs to."""
    return next(row['id'] for row in bridge.siding_cards.rows() if row['message_id'] == int(receipt['message_id']))


def load_previous(root, release=PREVIOUS):
    """The previous bridge release's modules from git, loaded from a copy under root; None outside a checkout."""
    repo = Path(b.__file__).resolve().parents[1]
    target = root / 'previous' / 'bridge'
    target.mkdir(parents=True)
    for name in ('bridge.py', 'run_asks.py', 'siding_cards.py', 'markdown.py'):
        shown = subprocess.run(['git', '-C', str(repo), 'show', f'{release}:bridge/{name}'], capture_output=True)
        if shown.returncode != 0:
            return None
        (target / name).write_bytes(shown.stdout)
    shutil.copytree(repo / 'vendor', root / 'previous' / 'vendor')
    (root / 'previous' / 'transcriber').mkdir()
    shutil.copyfile(repo / 'transcriber' / 'voice_transcribe.py', root / 'previous' / 'transcriber' / 'voice_transcribe.py')
    spec = importlib.util.spec_from_file_location('previous_bridge', target / 'bridge.py')
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module
