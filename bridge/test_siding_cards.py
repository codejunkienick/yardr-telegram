"""Siding cards through real native stores, the production poll order and fake Telegram."""
import copy
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
from unittest.mock import patch

from test_bridge import BridgeTestCase, write_settings
from test_run_asks import NativeCase
import bridge as b
import testyardr

n = b.run_asks
SIDING_METHODS = ('SidingProposal', 'SidingOutcome', 'FoldSidingExpected', 'ReleaseSidingExpected')
FLOW = 'intake -> harbor | repair\nharbor -> launch | repair\nrepair -> harbor | launch\nlaunch -> complete\ncomplete terminal\n'


class SidingRetryTests(NativeCase):
    def setUp(self):
        super().setUp()
        self.values = []
        owner = self
        handler = self.native.server.RequestHandlerClass

        class Handler(handler):
            def do_POST(self):
                method = self.path.removeprefix('/call/')
                if method not in (*SIDING_METHODS, 'GetDepot', 'Groups', 'Routes'):
                    return super().do_POST()
                args = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                owner.native.calls.append((method, args, self.headers.get('X-Yardr-Connection')))
                if method in SIDING_METHODS[1:] and not args:
                    self.reply({'error': {'code': 'error', 'message': 'missing required arguments'}})
                    return
                if method == 'GetDepot':
                    result = dict(name='sample')
                elif method == 'Groups':
                    result = [dict(name='keepers', runner='manual', config=dict(fold=4))]
                elif method == 'Routes':
                    result = [route for value in owner.values for route in value['routes']]
                elif method == 'SidingProposal':
                    result = next(value for value in owner.values if value['stage'] == args['ask']['stage'])
                elif method == 'SidingOutcome':
                    result = None
                else:
                    self.reply({'error': {'code': 'error', 'message': 'unexpected siding write'}})
                    return
                self.reply({'result': result})

        self.native.server.RequestHandlerClass = Handler

    def value(self, stage):
        members = [dict(bead='sample-' + stage + '-' + str(i), revision='revision-' + str(i),
                        points=1, stuck='', entered='2026-01-01T00:00:00Z', title='Synthetic part') for i in (1, 2)]
        return dict(version=1, digest='sp1:' + ('a' if stage == 'harbor' else 'b') * 64,
                    ask=dict(depot='sample', stage=stage, flow='rotation.f', group='keepers',
                             action='fold', members=[row['bead'] for row in members], to='launch'),
                    depot='sample', stage=stage, to='launch', guard='store', unguarded=[],
                    flow=dict(name='rotation.f', source=dict(kind='store'), digest='c' * 64),
                    group=dict(name='keepers', runner='manual', members=['pat'], fold=4, fold_after='1d', fold_max=6),
                    routes=[dict(id='route-' + stage, depot='sample', stage=stage, group='keepers')],
                    waiting=members, members=[dict(row, stage=stage, route='route-' + stage) for row in members],
                    points=2, fold_max=6, ready=False, trigger='', due='', refusals=[])

    def test_same_key_resumes_only_unsent_cards(self):
        for kind in ('status', 'actionable', 'partial'):
            for failure in ('not-sent', 'crash', 'ambiguous', 'rejected', 'revoked', 'remapped'):
                with self.subTest(kind=kind, failure=failure):
                    with self.store.connect() as db:
                        for table in ('siding_cards', 'run_ask_keys', 'deliveries'):
                            db.execute('DELETE FROM ' + table)
                    self.calls.clear()
                    self.values = [] if kind == 'status' else [self.value('harbor')]
                    if kind == 'partial':
                        self.values.append(self.value('repair'))
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
                    self.assertFalse(any(method.endswith('Expected') and args for method, args, _ in self.native.calls))


class SidingTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        binary = testyardr.binary()
        if binary is None:
            self.skipTest('yardr required for isolated siding integration')
        self.root = Path(self.tmp.name)
        server, self.home, self.command, self.env = testyardr.start(self.root, binary)
        self.addCleanup(testyardr.stop, server)
        # The guarded siding calls exist only on a pre-alignment build.
        if testyardr.aligned(self.home):
            self.skipTest('needs a pre-alignment yardr build (guarded siding calls); this build is aligned or upstream')
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

    def cli(self, *args):
        result = subprocess.run(self.command + list(args), env=self.env, cwd=self.root, capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout.strip()

    def restart(self):
        self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=self.telegram)
        self.bridge.hook_secret = None

    def part(self, name, size='small'):
        self.cli('bead', 'create', 'Synthetic ' + name, '--id', 'sample-' + name, '--depot', 'sample', '--stage', 'harbor', '--label', 'size:' + size)
        return 'sample-' + name

    def card(self, key='siding', **selection):
        receipt = self.bridge.api_siding_card(dict(depot='sample', key=key, **selection))
        self.assertTrue(receipt['delivered'], receipt)
        return self.bridge.siding_cards.rows()[-1]

    def press(self, row, action='approve', uid=40, **overrides):
        query = dict(id='press-' + str(uid), data=f"siding:{row['id']}:{action}",
                     message=dict(message_id=row['message_id'], chat=dict(id=123, type='private'),
                                  **{'from': dict(id=999, is_bot=True)}), **{'from': dict(id=123, is_bot=False)})
        query.update(overrides)
        self.updates = [dict(update_id=uid, callback_query=query)]
        self.bridge.poll_once()

    def reply(self, row, text, uid=41):
        self.updates = [dict(update_id=uid, message=dict(message_id=900 + uid, text=text,
            chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)},
            reply_to_message=dict(message_id=row['message_id'], **{'from': dict(id=999, is_bot=True)})))]
        self.bridge.poll_once()

    def test_real_fold_release_and_replay(self):
        # Every closure result is a receipt for this card, across restart and edit failure.
        states = ('open', 'repository', 'empty', 'stale', 'expired', 'withdrawn', 'settled', 'uncertain', 'pruned')
        for state in states:
            for code in (200, 400, 500):
                for selector in ('ask_key', 'message_id'):
                    with self.subTest(closure=state, telegram=code, selector=selector):
                        case = SidingTests('test_real_fold_release_and_replay')
                        case.setUp()
                        try:
                            if state != 'empty':
                                case.part('matrix-a')
                                case.part('matrix-b')
                            if state == 'repository':
                                path = case.root / 'depot' / '.yardr' / 'flows' / 'default.flow'
                                path.parent.mkdir(parents=True)
                                path.write_text(FLOW)
                                case.cli('depot', 'flow', 'sample', 'default')
                            row = case.card(key='matrix')
                            attempts = []
                            def telegram(url, body, *args, **kwargs):
                                if url.endswith('/editMessageText'):
                                    attempts.append(copy.deepcopy(body))
                                    if code != 200:
                                        return code, dict(ok=False, error_code=code, description='synthetic edit failure')
                                return case.telegram(url, body, *args, **kwargs)
                            case.bridge.request = telegram
                            if state == 'settled':
                                value = row['canonical']
                                case.client.call('FoldSidingExpected', dict(ask=value['ask'], digest=value['digest'],
                                                                          op=row['op'], actor='pat'))
                            elif state == 'stale':
                                case.cli('group', 'set', 'keepers', 'fold_after=12h')
                            elif state == 'expired':
                                row['expires'] = 0
                                case.bridge.siding_cards.save(row)
                            elif state == 'withdrawn':
                                case.press(row, action='leave')
                            elif state in ('uncertain', 'pruned'):
                                native = case.bridge.siding_cards.client.call
                                committed = []
                                def lost(method, args):
                                    if method == 'FoldSidingExpected' and args:
                                        if state == 'pruned':
                                            committed.append(native(method, args))
                                        raise n.RPCAmbiguous('synthetic lost write response')
                                    if method == 'SidingOutcome' and args and committed:
                                        raise n.RPCAmbiguous('synthetic lost outcome response')
                                    return native(method, args)
                                with patch.object(case.bridge.siding_cards.client, 'call', side_effect=lost):
                                    case.press(row)
                                if committed:
                                    case.cli('bead', 'note', committed[0]['carrier']['id'], 'Synthetic retention noise')
                                    case.client.call('Prune', [dict(events=1, hook_payloads=1, inbox=1), 'pat'])
                            before = case.client.call('ListBeads', [{}])
                            effects = case.client.call('EventsOfKind', ['folded', 100])
                            request = dict(decision='Caller text has no authority',
                                           **{selector: 'matrix' if selector == 'ask_key' else row['message_id']})
                            closable = state not in ('open', 'uncertain', 'pruned')
                            status = state in ('repository', 'empty')
                            for restart in (False, True):
                                if restart:
                                    case.restart()
                                    case.bridge.request = telegram
                                receipt = case.bridge.api_ask_close(request)
                                expected = closable and (code == 200 or status)
                                case.assertEqual(receipt['delivered'], expected, receipt)
                                if expected:
                                    case.assertEqual(int(receipt['message_id']), row['message_id'])
                                elif state in ('open', 'uncertain', 'pruned'):
                                    reason = 'siding_still_awaiting_human' if state == 'open' else 'siding_outcome_uncertain'
                                    case.assertEqual(receipt['reason'], reason)
                                else:
                                    reason = ('telegram_rejected_not_sent_400' if code == 400
                                              else 'ambiguous_delivery_requires_manual_reconciliation')
                                    case.assertEqual(receipt['reason'], reason)
                            case.restart()
                            receipt = case.bridge.api_ask_close(request)
                            case.assertEqual(receipt['delivered'], closable, receipt)
                            if closable:
                                case.assertEqual(int(receipt['message_id']), row['message_id'])
                                if not status:
                                    edit = next(body for url, body in reversed(case.calls) if url.endswith('/editMessageText'))
                                    case.assertEqual(edit['reply_markup']['inline_keyboard'], [])
                                    if attempts:
                                        case.assertTrue(all(body == edit for body in attempts))
                                with patch.object(case.bridge.siding_cards.client, 'call', side_effect=AssertionError('finished row read RPC')):
                                    with patch.object(case.bridge, 'request', side_effect=AssertionError('finished row edited')):
                                        case.bridge.siding_cards.refresh()
                            case.assertEqual(case.client.call('ListBeads', [{}]), before)
                            case.assertEqual(case.client.call('EventsOfKind', ['folded', 100]), effects)
                        finally:
                            case.doCleanups()
                            case.tearDown()
        one, two, three = [self.part(str(i)) for i in range(1, 4)]
        row = self.card()
        self.assertEqual(row['canonical']['group'], dict(name='keepers', runner='manual', members=['pat'], fold=4, fold_after='1d', fold_max=6))
        self.assertEqual(row['canonical']['ask']['members'], [one, two, three])
        self.assertEqual(self.bridge.run_asks.rows(), [])
        self.reply(row, 'fold ' + two + ' ' + one)
        selected = next(r for r in self.bridge.siding_cards.rows() if r['id'] != row['id'])
        self.assertFalse(selected['attempted'])
        self.assertEqual(selected['canonical']['ask']['members'], [two, one])
        self.assertIn('2. ' + one, selected['plain'])
        self.restart()
        self.press(selected, uid=42)
        selected = self.bridge.siding_cards.get(selected['id'])
        self.assertEqual(self.bridge.siding_cards.state(selected), 'settled')
        result = self.bridge.siding_cards.outcome(selected)[0]
        carrier = result['carrier']
        self.assertEqual(carrier['stage'], 'launch')
        self.assertEqual(result['members'], [two, one])
        self.assertEqual(result['left'], [three])
        self.assertEqual(self.client.call('GetBead', [one])['status'], 'closed')
        self.assertIn(carrier['id'], self.client.call('GetBead', [two])['close_reason'])
        self.assertEqual(len(self.client.call('EventsOfKind', ['folded', 100])), 1)
        self.press(selected, uid=43)
        self.assertEqual(len(self.client.call('EventsOfKind', ['folded', 100])), 1)
        single = self.card(key='remaining')
        self.reply(single, 'release ' + three, uid=44)
        release = next(r for r in self.bridge.siding_cards.rows() if r['canonical'] and r['canonical']['ask']['action'] == 'release')
        self.press(release, uid=45)
        self.assertEqual(self.client.call('GetBead', [three])['stage'], 'launch')
        self.assertEqual(self.bridge.siding_cards.state(self.bridge.siding_cards.get(release['id'])), 'settled')
        self.restart()
        self.press(release, uid=46)
        advanced = self.client.call('Events', [three, 100])
        self.assertEqual(sum(e['kind'] == 'advanced' for e in advanced), 1)
        receipt = self.bridge.api_ask_close(dict(ask_key='remaining', decision='caller text is not authority'))
        self.assertTrue(receipt['delivered'])
        self.assertIsNone(self.card(key='empty')['canonical'])
        self.cli('bead', 'create', 'Ordinary decision beside siding cards', '--id', 'ordinary-1')
        self.cli('bead', 'ask', 'ordinary-1', 'Which wording?', '--to', 'pat', '--context', 'Synthetic compatibility check')
        ask = self.client.call('Bead', dict(bead='ordinary-1', viewer='pat'))['asks'][0]
        receipt = self.bridge.api_ask_card(dict(bead='ordinary-1', ask=ask['ask'], key='ordinary'))
        self.assertTrue(receipt['delivered'], receipt)
        ordinary = self.bridge.run_asks.rows()[0]
        self.assertEqual(len(self.bridge.run_asks.rows()), 1)
        self.reply(ordinary, 'Keep this wording', uid=47)
        self.assertEqual(self.client.call('Bead', dict(bead='ordinary-1', viewer='pat'))['asks'][0]['words'], 'Keep this wording')
        # A depot-wide key owns every routed siding, even after its first card closes.
        flow = self.root / 'rotation.flow'
        flow.write_text(FLOW.replace('repair -> harbor | launch', 'repair -> launch | harbor'))
        self.cli('flow', 'set', 'rotation.f', '-f', str(flow))
        self.cli('group', 'add', 'menders', '--runner', 'manual', '--members', 'pat', '--set', 'fold_after=24h')
        self.cli('route', 'add', '--stage', 'repair', '--group', 'menders', '--depot', 'sample')
        self.part('batch-a')
        self.part('batch-b')
        for index, state in enumerate(('stale', 'settled')):
            with self.subTest(first_card=state):
                for suffix in ('a', 'b'):
                    self.cli('bead', 'create', 'Synthetic repair ' + suffix, '--id', f'sample-repair-{index}-{suffix}',
                             '--depot', 'sample', '--stage', 'repair', '--label', 'size:small')
                key = 'batch-' + state
                last = self.card(key=key)
                first, sibling = [self.bridge.siding_cards.get(ident) for ident in last['batch']]
                self.assertEqual([r['canonical']['stage'] for r in (first, sibling)], ['harbor', 'repair'])
                if state == 'stale':
                    self.cli('bead', 'update', 'sample-batch-a', '--title', 'Changed batch proposal')
                    self.bridge.siding_cards.refresh()
                else:
                    self.press(first, uid=80 + index * 10)
                self.assertEqual(self.bridge.siding_cards.state(self.bridge.siding_cards.get(first['id'])), state)
                port = self.start_callback_server()
                before = self.client.call('ListBeads', [{}])
                status, receipt = self.api_call(port, '/v1/ask-close', dict(ask_key=key, decision='Ignore caller closure text'))
                self.assertEqual(status, 200)
                self.assertFalse(receipt['delivered'], receipt)
                self.assertEqual(receipt['reason'], 'siding_still_awaiting_human')
                status, receipt = self.api_call(port, '/v1/ask-close', dict(message_id=first['message_id'], decision='Close only this card'))
                self.assertEqual(status, 200)
                self.assertTrue(receipt['delivered'], receipt)
                self.restart()
                receipt = self.bridge.api_ask_close(dict(ask_key=key, decision='Still waiting'))
                self.assertFalse(receipt['delivered'], receipt)
                self.assertEqual(receipt['reason'], 'siding_still_awaiting_human')
                self.assertEqual(self.client.call('ListBeads', [{}]), before)
                self.assertEqual(self.bridge.siding_cards.state(self.bridge.siding_cards.get(sibling['id'])), 'open')
                sent = next(body for url, body in reversed(self.calls)
                            if url.endswith('/sendMessage') and body['text'] == sibling['html'])
                self.assertEqual(sent['reply_markup']['inline_keyboard'][0][0]['callback_data'],
                                 f"siding:{sibling['id']}:approve")
                value = sibling['canonical']
                self.client.call('FoldSidingExpected', dict(ask=value['ask'], digest=value['digest'],
                                                          op=sibling['op'], actor='pat'))
                self.assertEqual(self.bridge.siding_cards.state(self.bridge.siding_cards.get(sibling['id'])), 'open')
                before = self.client.call('ListBeads', [{}])
                effects = self.client.call('EventsOfKind', ['folded', 100])
                for restart in (False, True):
                    if restart:
                        self.restart()
                    receipt = self.bridge.api_ask_close(dict(ask_key=key, decision='Canonical closure only'))
                    self.assertTrue(receipt['delivered'], receipt)
                    self.assertEqual(self.client.call('ListBeads', [{}]), before)
                    self.assertEqual(self.client.call('EventsOfKind', ['folded', 100]), effects)
                for row in (first, sibling):
                    edit = next(body for url, body in reversed(self.calls)
                                if url.endswith('/editMessageText') and body['message_id'] == row['message_id'])
                    self.assertEqual(edit['reply_markup']['inline_keyboard'], [])
                    self.assertNotIn('Canonical closure only', edit['text'])
        self.part('refresh-a')
        self.part('refresh-b')
        for suffix in ('a', 'b'):
            self.cli('bead', 'create', 'Synthetic repair refresh ' + suffix, '--id', 'sample-repair-refresh-' + suffix,
                     '--depot', 'sample', '--stage', 'repair', '--label', 'size:small')
        last = self.card(key='batch-refresh-errors')
        self.cli('group', 'set', 'keepers', 'fold_after=12h')
        self.cli('group', 'set', 'menders', 'fold_after=12h')
        before = self.client.call('ListBeads', [{}])
        receipt = self.bridge.api_ask_close(dict(ask_key='batch-refresh-errors', decision='Refresh every card'))
        self.assertTrue(receipt['delivered'], receipt)
        for ident in last['batch']:
            row = self.bridge.siding_cards.get(ident)
            self.assertEqual(self.bridge.siding_cards.state(row), 'stale')
            edit = next(body for url, body in reversed(self.calls)
                        if url.endswith('/editMessageText') and body['message_id'] == row['message_id'])
            self.assertEqual(edit['reply_markup']['inline_keyboard'], [])
        self.assertTrue(self.bridge.api_ask_close(dict(ask_key='batch-refresh-errors', decision='Already stale'))['delivered'])
        self.assertEqual(self.client.call('ListBeads', [{}]), before)
        # Native settlement and confirmed Telegram retirement are separate receipts.
        for index, (state, code) in enumerate((('settled', 400), ('stale', 400), ('settled', 500), ('stale', 500))):
            with self.subTest(retirement=state, telegram_error=code):
                self.part('delivery-' + str(index) + '-a')
                self.part('delivery-' + str(index) + '-b')
                key = 'delivery-' + str(index)
                row = self.card(key=key, stage='harbor')
                if state == 'settled':
                    value = row['canonical']
                    self.client.call('FoldSidingExpected', dict(ask=value['ask'], digest=value['digest'],
                                                              op=row['op'], actor='pat'))
                else:
                    self.cli('group', 'set', 'keepers', 'fold_after=' + str(11 - index) + 'h')
                    if code == 400:
                        display = self.bridge.siding_cards.display
                        def crash_before_edit(saved, *args, **kwargs):
                            if saved['id'] == row['id']:
                                raise RuntimeError('synthetic crash before retirement presentation is saved')
                            return display(saved, *args, **kwargs)
                        with patch.object(self.bridge.siding_cards, 'display', side_effect=crash_before_edit):
                            with self.assertRaisesRegex(RuntimeError, 'synthetic crash'):
                                self.press(row, uid=120)
                        saved = self.bridge.siding_cards.get(row['id'])
                        self.assertEqual(self.bridge.siding_cards.state(saved), 'stale')
                        self.assertNotIn('display', saved)
                        self.restart()
                before = self.client.call('ListBeads', [{}])
                effects = self.client.call('EventsOfKind', ['folded', 100])
                attempted = []
                def reject_edit(url, body, *args, **kwargs):
                    if url.endswith('/editMessageText') and body['message_id'] == row['message_id']:
                        attempted.append(copy.deepcopy(body))
                        saved = self.bridge.siding_cards.get(row['id'])
                        self.assertEqual(self.bridge.siding_cards.state(saved), state)
                        self.assertEqual(saved['display']['keyboard'], [])
                        return code, dict(ok=False, error_code=code, description='synthetic unconfirmed retirement')
                    return self.telegram(url, body, *args, **kwargs)
                with patch.object(self.bridge, 'request', side_effect=reject_edit):
                    port = self.start_callback_server()
                    status, receipt = self.api_call(port, '/v1/ask-close', dict(ask_key=key, decision='Not authority'))
                    self.assertEqual(status, 200)
                    self.assertFalse(receipt['delivered'], receipt)
                self.assertTrue(attempted)
                saved = self.bridge.siding_cards.get(row['id'])
                revision, display = saved['revision'], saved['display']
                self.restart()
                attempted.clear()
                with patch.object(self.bridge, 'request', side_effect=reject_edit):
                    self.bridge.siding_cards.refresh()
                    self.assertTrue(attempted, 'restart must retry the unconfirmed canonical edit')
                    receipt = self.bridge.api_ask_close(dict(ask_key=key, decision='Still unconfirmed'))
                    self.assertFalse(receipt['delivered'], receipt)
                    expected = 'telegram_rejected_not_sent_400' if code == 400 else 'ambiguous_delivery_requires_manual_reconciliation'
                    self.assertEqual(receipt['reason'], expected)
                self.assertEqual(self.bridge.siding_cards.get(row['id'])['revision'], revision)
                self.assertEqual(self.bridge.siding_cards.get(row['id'])['display'], display)
                for restart in (False, True):
                    if restart:
                        self.restart()
                    receipt = self.bridge.api_ask_close(dict(ask_key=key, decision='Canonical retirement only'))
                    self.assertTrue(receipt['delivered'], receipt)
                edit = next(body for url, body in reversed(self.calls)
                            if url.endswith('/editMessageText') and body['message_id'] == row['message_id'])
                self.assertEqual(edit, attempted[-1])
                self.assertEqual(edit['reply_markup']['inline_keyboard'], [])
                self.assertEqual(self.client.call('ListBeads', [{}]), before)
                self.assertEqual(self.client.call('EventsOfKind', ['folded', 100]), effects)

    def test_status_reasons_ceiling_empty_and_complete_limits(self):
        missing = self.bridge.api_siding_card(dict(depot='missing', key='missing-depot'))
        self.assertFalse(missing['delivered'])
        one, two = self.part('1'), self.part('2')
        for route in self.client.call('Routes', []):
            self.cli('route', 'rm', route['id'])
        self.cli('group', 'add', 'sample-keepers', '--runner', 'manual', '--members', 'pat', '--set', 'fold_after=24h')
        self.cli('route', 'add', '--stage', 'harbor', '--group', '{depot}-keepers')
        self.assertEqual(self.client.call('Routes', [])[0]['group'], '{depot}-keepers')
        canonical = self.client.call('SidingProposal', dict(ask=dict(depot='sample', stage='harbor', action='fold', members=[])))
        self.assertEqual(canonical['guard'], 'store')
        self.assertEqual(canonical['refusals'], [])
        for index, selection in enumerate(({}, dict(stage='harbor', flow='rotation.f', group='sample-keepers'))):
            with self.subTest(template_selection=selection):
                row = self.card(key='template-' + str(index), **selection)
                self.assertEqual(row['canonical'], canonical)
                self.assertEqual(row['canonical']['ask']['group'], 'sample-keepers')
                self.assertEqual(row['canonical']['ask']['members'], [one, two])
                self.assertIn('"group":"sample-keepers"', row['plain'])
                sent = next(body for url, body in reversed(self.calls) if url.endswith('/sendMessage'))
                self.assertEqual(sent['text'], row['html'])
                self.assertEqual(sent['reply_markup']['inline_keyboard'][0][0]['callback_data'],
                                 f"siding:{row['id']}:approve")
        self.cli('bead', 'hold', one)
        self.cli('bead', 'ask', two, 'Waiting question', '--to', 'pat', '--context', 'Synthetic blocked review')
        row = self.card(key='stuck')
        self.assertEqual([r['stuck'] for r in row['canonical']['waiting']], ['held', 'asked'])
        self.assertIn('few', row['canonical']['refusals'])
        self.assertEqual(row['canonical']['ask']['members'], [])
        self.assertFalse(row['actionable'])
        self.assertEqual(self.bridge.siding_cards.keyboard(row), [])
        send_key, identity = self.bridge.siding_cards.send_request(row)
        fingerprint, published = self.store.saved_delivery(send_key)
        before = self.client.call('ListBeads', [{}])
        effects = self.client.call('EventsOfKind', ['folded', 100])
        start = len(self.calls)
        for restart in (False, True):
            if restart:
                self.restart()
            for selector in ('ask_key', 'message_id', 'repeat'):
                with self.subTest(all_stuck=True, restart=restart, selector=selector):
                    if selector == 'repeat':
                        receipt = self.bridge.api_siding_card(dict(depot='sample', key='stuck'))
                    else:
                        receipt = self.bridge.api_ask_close(dict(decision='Status receipt only',
                            **{selector: 'stuck' if selector == 'ask_key' else row['message_id']}))
                    self.assertTrue(receipt['delivered'], receipt)
                    self.assertEqual(int(receipt['message_id']), row['message_id'])
        for bad in (None, dict(delivered=False), dict(delivered=True),
                    dict(delivered=True, message_id=0), dict(delivered=True, message_id=row['message_id'] + 1)):
            with self.subTest(all_stuck_unconfirmed=bad):
                with self.store.connect() as db:
                    db.execute('DELETE FROM deliveries WHERE key=?', (send_key,))
                    if bad is not None:
                        db.execute('INSERT INTO deliveries VALUES (?,?,?)', (send_key, fingerprint, json.dumps(bad)))
                for selector in ('ask_key', 'message_id', 'repeat'):
                    if selector == 'repeat':
                        receipt = self.bridge.api_siding_card(dict(depot='sample', key='stuck'))
                    else:
                        receipt = self.bridge.api_ask_close(dict(decision='Require saved receipt',
                            **{selector: 'stuck' if selector == 'ask_key' else row['message_id']}))
                    self.assertFalse(receipt['delivered'], receipt)
        with self.store.connect() as db:
            db.execute('UPDATE deliveries SET receipt=?, fingerprint=? WHERE key=?',
                       (json.dumps(published), 'wrong', send_key))
        self.assertFalse(self.bridge.api_ask_close(dict(ask_key='stuck', decision='Wrong fingerprint'))['delivered'])
        with self.store.connect() as db:
            db.execute('UPDATE deliveries SET fingerprint=? WHERE key=?', (fingerprint, send_key))
        self.assertTrue(self.bridge.api_ask_close(dict(ask_key='stuck', decision='Confirmed status'))['delivered'])
        self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '456')
        self.config.update(user_id=456, chat_id=456)
        self.restart()
        receipt = self.bridge.api_ask_close(dict(ask_key='stuck', decision='New pairing'))
        self.assertEqual(receipt['reason'], 'siding_wrong_yard_or_pairing')
        receipt = self.bridge.api_ask_close(dict(message_id=row['message_id'], decision='New chat'))
        self.assertEqual(receipt['reason'], 'ask_not_found')
        self.assertFalse(self.bridge.api_siding_card(dict(depot='sample', key='stuck'))['delivered'])
        self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
        self.config.update(user_id=123, chat_id=123)
        self.restart()
        self.assertEqual(self.calls[start:], [])
        self.assertEqual(self.client.call('ListBeads', [{}]), before)
        self.assertEqual(self.client.call('EventsOfKind', ['folded', 100]), effects)
        self.cli('bead', 'create', 'Unreleased blocker', '--id', 'sample-blocker', '--depot', 'sample', '--stage', 'intake')
        blocked = self.part('blocked')
        self.cli('dep', 'add', 'sample-blocker', blocked)
        self.part('3', 'large')
        self.part('4', 'large')
        row = self.card(key='ceiling')
        self.assertIn('ceiling', row['canonical']['refusals'])
        self.assertIn('blocked', [r['stuck'] for r in row['canonical']['waiting']])
        self.assertIn('Ceiling exceeded', row['plain'])
        self.assertEqual(self.bridge.siding_cards.keyboard(row), [])
        self.cli('bead', 'update', one, '--title', '🙂' * 2000)
        before = self.counter
        refused = self.bridge.api_siding_card(dict(depot='sample', key='oversize'))
        self.assertFalse(refused['delivered'])
        self.assertIn('too_large', refused['reason'])
        self.assertEqual(self.counter, before)

    def test_wrong_bindings_withdrawal_and_expiry(self):
        # A current native owner wins over a different chat's retained message id.
        for family in ('run', 'ordinary'):
            for state in ('open', 'settled', 'stale', 'status'):
                with self.subTest(message_collision=family, state=state):
                    case = SidingTests('test_wrong_bindings_withdrawal_and_expiry')
                    case.setUp()
                    try:
                        if family == 'run':
                            case.cli('bead', 'create', 'Synthetic previous pairing request', '--id', 'sample-old',
                                     '--depot', 'sample', '--stage', 'intake')
                            case.cli('user', 'add', 'proposer')
                            asked = subprocess.run(case.command[:-1] + ['proposer', 'bead', 'ask', 'sample-old',
                                'Change the synthetic default?', '--to', 'pat', '--context', 'Synthetic collision',
                                '--run', 'settings set default_kind claude'],
                                env=case.env, cwd=case.root, capture_output=True, text=True, timeout=20)
                            case.assertEqual(asked.returncode, 0, asked.stderr)
                            number = case.client.call('Bead', dict(bead='sample-old', viewer='pat'))['asks'][-1]['ask']
                            receipt = case.bridge.api_run_ask(dict(bead='sample-old', ask=number, key='old-run'))
                            case.assertTrue(receipt['delivered'], receipt)
                            old = case.bridge.run_asks.rows()[0]
                        else:
                            key = 'old-ordinary'
                            request = case.publish(key=key, text='Synthetic previous pairing choice')
                            request['keyboard'] = [[dict(text='Keep', callback_data=f'ask:{b.Bridge._ask_id_for_key(key)}:keep')]]
                            receipt = case.bridge.publish(request)
                            case.assertTrue(receipt['delivered'], receipt)
                            old = case.store.get_ask(b.Bridge._ask_id_for_key(key))
                        case.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '456')
                        case.config.update(user_id=456, chat_id=456)
                        case.counter = 76
                        case.restart()
                        n.verify_operator(case.bridge.siding_cards.client, case.config)
                        if state != 'status':
                            case.part('current-a')
                            case.part('current-b')
                        row = case.card(key='current-siding')
                        case.assertEqual(row['message_id'], old['message_id'])
                        if state == 'settled':
                            value = row['canonical']
                            case.client.call('FoldSidingExpected', dict(ask=value['ask'], digest=value['digest'],
                                                                      op=row['op'], actor='pat'))
                        elif state == 'stale':
                            case.cli('group', 'set', 'keepers', 'fold_after=12h')
                        case.restart()
                        before = case.client.call('ListBeads', [{}])
                        effects = case.client.call('EventsOfKind', ['folded', 100])
                        start = len(case.calls)
                        for selector in ('message_id', 'ask_key'):
                            receipt = case.bridge.api_ask_close(dict(decision='Caller text is no authority',
                                **{selector: row['message_id'] if selector == 'message_id' else 'current-siding'}))
                            case.assertEqual(receipt['delivered'], state != 'open', receipt)
                            if state == 'open':
                                case.assertEqual(receipt['reason'], 'siding_still_awaiting_human')
                            else:
                                case.assertEqual(int(receipt['message_id']), row['message_id'])
                        edits = [body for url, body in case.calls[start:] if url.endswith('/editMessageText')]
                        case.assertEqual(len(edits), int(state in ('settled', 'stale')))
                        for edit in edits:
                            case.assertEqual((edit['chat_id'], edit['message_id']), (456, row['message_id']))
                            case.assertEqual(edit['reply_markup']['inline_keyboard'], [])
                        if family == 'ordinary':
                            case.assertEqual(case.store.get_ask(old['ask_id']), old)
                        else:
                            receipt = case.bridge.api_ask_close(dict(ask_key='old-run', decision='Old pairing'))
                            case.assertEqual(receipt['reason'], 'run_ask_wrong_yard_or_pairing')
                            if state == 'open':
                                receipt = case.bridge.api_run_ask(dict(bead='sample-old', ask=number, key='current-run'))
                                case.assertTrue(receipt['delivered'], receipt)
                                current = next(r for r in case.bridge.run_asks.rows() if r['binding']['chat_id'] == '456')
                                receipt = case.bridge.api_ask_close(dict(message_id=current['message_id'], decision='No authority'))
                                case.assertEqual(receipt['reason'], 'run_ask_still_open_in_yard')
                                case.counter = current['message_id'] - 1
                                collision = case.card(key='ambiguous')
                                case.assertEqual(collision['message_id'], current['message_id'])
                                calls = len(case.calls)
                                receipt = case.bridge.api_ask_close(dict(message_id=collision['message_id'], decision='Ambiguous'))
                                case.assertFalse(receipt['delivered'], receipt)
                                case.assertEqual(receipt['reason'], 'ask_message_binding_ambiguous')
                                case.assertEqual(len(case.calls), calls)
                        case.assertEqual(case.client.call('ListBeads', [{}]), before)
                        case.assertEqual(case.client.call('EventsOfKind', ['folded', 100]), effects)
                        case.assertEqual(len(effects or []), int(state == 'settled'))
                    finally:
                        case.doCleanups()
                        case.tearDown()
        # A valid migration cannot retire an old chat's message into the new chat.
        for state in ('settled', 'stale', 'failed-retirement'):
            with self.subTest(pairing_change=state):
                case = SidingTests('test_wrong_bindings_withdrawal_and_expiry')
                case.setUp()
                try:
                    case.part('pair-a')
                    case.part('pair-b')
                    old = case.card(key='pairing')
                    value = old['canonical']
                    if state == 'stale':
                        case.cli('group', 'set', 'keepers', 'fold_after=12h')
                    else:
                        case.client.call('FoldSidingExpected', dict(ask=value['ask'], digest=value['digest'],
                                                                  op=old['op'], actor='pat'))
                    def reject(url, body, *args, **kwargs):
                        if url.endswith('/editMessageText'):
                            return 400, dict(ok=False, error_code=400, description='synthetic rejected retirement')
                        return case.telegram(url, body, *args, **kwargs)
                    if state == 'failed-retirement':
                        case.bridge.request = reject
                    receipt = case.bridge.api_ask_close(dict(ask_key='pairing', decision='No authority'))
                    case.assertEqual(receipt['delivered'], state != 'failed-retirement', receipt)
                    before = case.client.call('ListBeads', [{}])
                    case.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '456')
                    case.config.update(user_id=456, chat_id=456)
                    case.restart()
                    n.verify_operator(case.bridge.siding_cards.client, case.config)
                    start = len(case.calls)
                    receipt = case.bridge.api_ask_close(dict(ask_key='pairing', decision='New pairing'))
                    case.assertFalse(receipt['delivered'], receipt)
                    case.assertEqual(receipt['reason'], 'siding_wrong_yard_or_pairing')
                    receipt = case.bridge.api_ask_close(dict(message_id=old['message_id'], decision='New chat'))
                    case.assertFalse(receipt['delivered'], receipt)
                    case.assertEqual(receipt['reason'], 'ask_not_found')
                    case.assertFalse(case.bridge.siding_cards.reply(301, dict(
                        message_id=302, text='release sample-pair-a', chat=dict(id=456, type='private'),
                        reply_to_message=dict(message_id=old['message_id']), **{'from': dict(id=456, is_bot=False)})))
                    with case.assertRaisesRegex(n.NativeError, 'siding_wrong_yard_or_pairing'):
                        case.bridge.siding_cards.display(case.bridge.siding_cards.get(old['id']))
                    case.assertEqual(case.calls[start:], [])
                    case.assertEqual(case.client.call('ListBeads', [{}]), before)
                finally:
                    case.doCleanups()
                    case.tearDown()
        one, two = self.part('1'), self.part('2')
        row = self.card()
        for uid, change in enumerate(({'from': dict(id=321, is_bot=False)}, {'from': dict(id=123, is_bot=True)},
                {'message': dict(message_id=row['message_id'] + 1, chat=dict(id=123, type='private'), **{'from': dict(id=999, is_bot=True)})},
                {'message': dict(message_id=row['message_id'], chat=dict(id=123, type='private'), forward_date=1, **{'from': dict(id=999, is_bot=True)})},
                {'message': dict(message_id=row['message_id'], chat=dict(id=123, type='private'), message_thread_id=12, is_topic_message=True, **{'from': dict(id=999, is_bot=True)})}), 50):
            self.press(row, uid=uid, **change)
        self.home.chmod(0o777)
        try:
            self.press(row, uid=59)
        finally:
            self.home.chmod(0o700)
        self.config['yard'] = 'another-yard'
        self.restart()
        self.press(row, uid=60)
        self.config['yard'] = 'test-city'
        self.restart()
        self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '456')
        self.press(row, uid=61)
        self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
        self.assertFalse(self.client.call('EventsOfKind', ['folded', 100]))
        self.press(row, action='leave', uid=62)
        self.restart()
        self.press(row, uid=63)
        self.assertEqual(self.bridge.siding_cards.state(self.bridge.siding_cards.get(row['id'])), 'withdrawn')
        self.assertFalse(self.client.call('EventsOfKind', ['folded', 100]))
        expiring = self.card(key='expires')
        expiring['expires'] = 0
        self.bridge.siding_cards.save(expiring)
        self.press(expiring, uid=64)
        self.assertEqual(self.bridge.siding_cards.state(self.bridge.siding_cards.get(expiring['id'])), 'expired')
        self.assertFalse(self.client.call('EventsOfKind', ['folded', 100]))
        # Independent stores can use the same Telegram human, but never a card from this store.
        other_home = self.root / 'other-home'
        other_home.mkdir(mode=0o700)
        self.config['yardr_home'] = str(other_home)
        self.restart()
        self.press(row, uid=65)
        self.assertFalse(self.client.call('EventsOfKind', ['folded', 100]))

    def test_stale_revision_group_target_route_and_automatic_fold(self):
        # Another card's guarded effect retires only never-attempted proposals.
        for entry in ('refresh', 'ask_key', 'message_id', 'repeat', 'approve', 'withdrawn', 'uncertain'):
            for code in (200, 400, 500):
                with self.subTest(shared_proposal=entry, telegram=code):
                    case = SidingTests('test_stale_revision_group_target_route_and_automatic_fold')
                    case.setUp()
                    try:
                        parts = [case.part('shared-a'), case.part('shared-b')]
                        first, second = case.card(key='first'), case.card(key='second')
                        case.assertEqual(first['canonical'], second['canonical'])
                        case.assertNotEqual(first['op'], second['op'])
                        if entry == 'uncertain':
                            native = case.bridge.siding_cards.client.call
                            def lost(method, args):
                                if method == 'FoldSidingExpected' and args:
                                    raise n.RPCAmbiguous('synthetic in-flight write')
                                return native(method, args)
                            with patch.object(case.bridge.siding_cards.client, 'call', side_effect=lost):
                                case.press(second, uid=30)
                        elif entry == 'withdrawn':
                            case.press(second, action='leave', uid=30)
                        # Approve A without the poll also refreshing B after the effect.
                        query = dict(id='first', data=f"siding:{first['id']}:approve",
                                     message=dict(message_id=first['message_id'], chat=dict(id=123, type='private'),
                                                  **{'from': dict(id=999, is_bot=True)}),
                                     **{'from': dict(id=123, is_bot=False)})
                        case.bridge.siding_cards.callback(40, query)
                        result = case.bridge.siding_cards.get(first['id'])['outcome']['result']
                        carrier = result['carrier']['id']
                        case.assertEqual(result['members'], parts)
                        case.assertEqual(result['carrier']['stage'], 'launch')
                        case.assertTrue(all(case.client.call('GetBead', [p])['status'] == 'closed' for p in parts))
                        before = case.client.call('ListBeads', [{}])
                        events = case.client.call('EventsOfKind', ['folded', 100])
                        case.assertEqual(len(events), 1)
                        case.restart()
                        if entry == 'refresh' and code == 200:
                            native = case.bridge.siding_cards.client.call
                            start = len(case.calls)
                            def outage(method, args):
                                if method == 'SidingProposal':
                                    raise n.RPCUnavailable('synthetic proposal outage after outcome conflict')
                                return native(method, args)
                            with patch.object(case.bridge.siding_cards.client, 'call', side_effect=outage):
                                case.bridge.siding_cards.refresh()
                            unchanged = case.bridge.siding_cards.get(second['id'])
                            case.assertEqual(case.bridge.siding_cards.state(unchanged), 'open')
                            case.assertIsNone(unchanged['refused'])
                            case.assertEqual(case.bridge.siding_cards.keyboard(unchanged), second['publish_keyboard'])
                            case.assertEqual(case.calls[start:], [])
                        edits = []
                        def telegram(url, body, *args, **kwargs):
                            if url.endswith('/editMessageText') and body['message_id'] == second['message_id']:
                                edits.append(copy.deepcopy(body))
                                if code != 200:
                                    return code, dict(ok=False, error_code=code, description='synthetic retirement failure')
                            return case.telegram(url, body, *args, **kwargs)
                        case.bridge.request = telegram
                        native = case.bridge.siding_cards.client.call
                        with patch.object(case.bridge.siding_cards.client, 'call', wraps=native) as calls:
                            if entry == 'refresh':
                                case.bridge.siding_cards.refresh()
                            elif entry in ('ask_key', 'message_id'):
                                receipt = case.bridge.api_ask_close(dict(decision='Canonical retirement only',
                                    **{entry: 'second' if entry == 'ask_key' else second['message_id']}))
                                case.assertEqual(receipt['delivered'], code == 200, receipt)
                            elif entry == 'repeat':
                                receipt = case.bridge.api_siding_card(dict(depot='sample', key='second'))
                                case.assertEqual(receipt['delivered'], code == 200, receipt)
                            elif entry == 'approve':
                                query['id'], query['data'] = 'second', f"siding:{second['id']}:approve"
                                query['message']['message_id'] = second['message_id']
                                case.bridge.siding_cards.callback(41, query)
                            else:
                                case.bridge.siding_cards.refresh()
                            case.assertFalse(any(c.args[0].endswith('Expected') and c.args[1]
                                                 for c in calls.call_args_list))
                        current = case.bridge.siding_cards.get(second['id'])
                        case.assertIsNone(current['outcome'])
                        case.assertEqual(current['attempted'], entry == 'uncertain')
                        case.assertEqual(case.bridge.siding_cards.state(current),
                                         entry if entry in ('uncertain', 'withdrawn') else 'stale')
                        if entry in ('uncertain', 'withdrawn'):
                            case.assertIsNone(current['refused'])
                            footer = 'Outcome uncertain' if entry == 'uncertain' else 'Card withdrawn'
                            case.assertIn(footer, current['display']['footer'])
                            case.assertEqual(edits, [])
                        else:
                            case.assertIn('Carrier now: ' + carrier + ' · launch · open', current['refused'])
                            case.assertIn('folded into ' + carrier, current['refused'])
                            case.assertIn('Current waiting: ', current['refused'])
                            case.assertTrue(edits)
                            case.assertEqual(edits[-1]['reply_markup']['inline_keyboard'], [])
                        # Both selectors require this card's confirmed retirement after restart.
                        for selector in ('ask_key', 'message_id'):
                            receipt = case.bridge.api_ask_close(dict(decision='Own receipt required',
                                **{selector: 'second' if selector == 'ask_key' else second['message_id']}))
                            case.assertEqual(receipt['delivered'], (code == 200 or entry == 'withdrawn')
                                             and entry != 'uncertain', receipt)
                            if receipt['delivered']:
                                case.assertEqual(int(receipt['message_id']), second['message_id'])
                            elif entry != 'uncertain':
                                reason = ('telegram_rejected_not_sent_400' if code == 400
                                          else 'ambiguous_delivery_requires_manual_reconciliation')
                                case.assertEqual(receipt['reason'], reason)
                        case.restart()
                        for selector in ('ask_key', 'message_id', 'repeat'):
                            if selector == 'repeat':
                                receipt = case.bridge.api_siding_card(dict(depot='sample', key='second'))
                            else:
                                receipt = case.bridge.api_ask_close(dict(decision='Retry retirement',
                                    **{selector: 'second' if selector == 'ask_key' else second['message_id']}))
                            case.assertEqual(receipt['delivered'], entry != 'uncertain', receipt)
                            if receipt['delivered']:
                                case.assertEqual(int(receipt['message_id']), second['message_id'])
                        if entry not in ('uncertain', 'withdrawn'):
                            edit = next(body for url, body in reversed(case.calls) if url.endswith('/editMessageText'))
                            case.assertTrue(all(body == edit for body in edits))
                        case.assertEqual(case.client.call('ListBeads', [{}]), before)
                        case.assertEqual(case.client.call('EventsOfKind', ['folded', 100]), events)
                    finally:
                        case.doCleanups()
                        case.tearDown()
        one, two = self.part('1'), self.part('2')
        mutations = [lambda: self.cli('bead', 'update', one, '--title', 'Changed title'),
                     lambda: self.cli('group', 'set', 'keepers', 'fold_max=5'),
                     lambda: (self.root / 'rotation.flow').write_text(FLOW.replace('harbor -> launch | repair', 'harbor -> repair | launch')),
                     lambda: self.cli('route', 'add', '--stage', 'harbor', '--group', 'keepers', '--depot', 'sample', '--label', 'size:small')]
        for index, mutate in enumerate(mutations):
            row = self.card(key='drift-' + str(index))
            mutate()
            if index == 2:
                self.cli('flow', 'set', 'rotation.f', '-f', str(self.root / 'rotation.flow'))
            self.press(row, uid=80 + index)
            self.assertEqual(self.bridge.siding_cards.state(self.bridge.siding_cards.get(row['id'])), 'stale')
            self.assertFalse(self.client.call('EventsOfKind', ['folded', 100]))
        row = self.card(key='auto')
        beads, routes = self.client.call('ListBeads', [{}]), self.client.call('Routes', [])
        result = self.client.call('FoldSidings', ['2099-01-01T00:00:00Z', beads, routes])
        self.assertEqual(len(result['folded']), 1)
        self.press(row, uid=85)
        self.assertEqual(len(self.client.call('EventsOfKind', ['folded', 100])), 1)
        current = self.bridge.siding_cards.get(row['id'])
        self.assertEqual(self.bridge.siding_cards.state(current), 'stale')
        self.assertIn('folded into', current['display']['footer'])
        self.assertIn('Carrier now: ' + result['folded'][0]['carrier'] + ' · repair · open', current['display']['footer'])
        left, right = self.part('left'), self.part('right')
        external = self.card(key='external-move')
        self.cli('bead', 'advance', left, '--to', 'launch')
        self.press(external, uid=86)
        current = self.bridge.siding_cards.get(external['id'])
        self.assertEqual(self.bridge.siding_cards.state(current), 'stale')
        self.assertIn(left + ' · launch · open', current['display']['footer'])
        self.assertIn('Current waiting: ' + right, current['display']['footer'])
        self.assertEqual(len(self.client.call('EventsOfKind', ['folded', 100])), 1)

    def test_expected_release_revision_and_native_gate(self):
        case = SidingTests('test_expected_release_revision_and_native_gate')
        case.setUp()
        try:
            bead = case.part('shared-release')
            first, second = case.card(key='first'), case.card(key='second')
            for uid, row in enumerate((first, second), 20):
                case.reply(row, 'release ' + bead, uid=uid)
            first, second = [row for row in case.bridge.siding_cards.rows()
                             if row['canonical']['ask']['action'] == 'release']
            case.assertEqual(first['canonical'], second['canonical'])
            case.assertNotEqual(first['op'], second['op'])
            case.press(first)
            case.restart()
            receipt = case.bridge.api_ask_close(dict(message_id=second['message_id'], decision='Canonical departure'))
            case.assertTrue(receipt['delivered'], receipt)
            case.assertEqual(int(receipt['message_id']), second['message_id'])
            current = case.bridge.siding_cards.get(second['id'])
            case.assertFalse(current['attempted'])
            case.assertIsNone(current['outcome'])
            case.assertEqual(case.bridge.siding_cards.state(current), 'stale')
            case.assertIn(bead + ' · launch · open', current['refused'])
            case.assertEqual(current['display']['keyboard'], [])
            case.assertEqual(case.client.call('GetBead', [bead])['stage'], 'launch')
            case.assertEqual(len(case.client.call('EventsOfKind', ['advanced', 100])), 1)
        finally:
            case.doCleanups()
            case.tearDown()
        one, two = self.part('1'), self.part('2')
        row = self.card()
        self.reply(row, 'release ' + one)
        release = next(r for r in self.bridge.siding_cards.rows() if r['canonical']['ask']['action'] == 'release')
        original = self.bridge.siding_cards.client.call
        raced = []
        def change_before_write(method, args):
            if method == 'ReleaseSidingExpected' and args:
                raced.append(args)
                self.cli('bead', 'update', one, '--title', 'Revision changed during click')
            return original(method, args)
        with patch.object(self.bridge.siding_cards.client, 'call', side_effect=change_before_write):
            self.press(release, uid=90)
        self.assertEqual(len(raced), 1)
        self.assertEqual(self.client.call('GetBead', [one])['stage'], 'harbor')
        self.assertFalse(self.client.call('EventsOfKind', ['advanced', 100]))
        self.assertEqual(self.bridge.siding_cards.state(self.bridge.siding_cards.get(release['id'])), 'uncertain')
        # A wagon is eligible to read but native Advance refuses to move it on its own.
        self.cli('bead', 'update', one, '--type', 'wagon')
        row = self.card(key='wagon')
        self.reply(row, 'release ' + one, uid=91)
        release = next(r for r in self.bridge.siding_cards.rows() if r['id'] != release['id'] and r['canonical']['ask']['action'] == 'release')
        self.press(release, uid=92)
        self.assertEqual(self.client.call('GetBead', [one])['stage'], 'harbor')
        self.assertFalse(self.client.call('EventsOfKind', ['advanced', 100]))

    def test_post_commit_errors_restart_tampered_identity_and_pruned_uncertainty(self):
        # Intent survives outage, Leave, expiry and proposal drift until the saved op settles.
        case = SidingTests('test_post_commit_errors_restart_tampered_identity_and_pruned_uncertainty')
        case.setUp()
        try:
            case.part('intent-a')
            case.part('intent-b')
            pending = case.card(key='intent')
            open_card = case.card(key='open-outage')
            native = case.bridge.siding_cards.client.call
            def lost(method, args):
                if method == 'FoldSidingExpected' and args:
                    raise n.RPCAmbiguous('synthetic in-flight write')
                return native(method, args)
            with patch.object(case.bridge.siding_cards.client, 'call', side_effect=lost):
                case.press(pending)
            saved = case.bridge.siding_cards.get(pending['id'])
            footer = saved['display']['footer']
            start = len(case.calls)
            with patch.object(case.bridge.siding_cards.client, 'call', side_effect=n.RPCUnavailable('synthetic socket outage')):
                case.bridge.poll_once()
                receipt = case.bridge.api_ask_close(dict(ask_key='intent', decision='No authority'))
                case.assertFalse(receipt['delivered'])
            case.assertEqual(case.calls[start:], [])
            saved = case.bridge.siding_cards.get(pending['id'])
            case.assertTrue(saved['attempted'])
            case.assertIsNone(saved.get('refused'))
            case.assertEqual(saved['display']['footer'], footer)
            case.assertEqual(case.bridge.siding_cards.keyboard(case.bridge.siding_cards.get(open_card['id'])),
                             open_card['publish_keyboard'])
            case.assertFalse(case.client.call('EventsOfKind', ['folded', 100]))
            case.press(pending, action='leave', uid=70)
            saved = case.bridge.siding_cards.get(pending['id'])
            saved['expires'] = 0
            case.bridge.siding_cards.save(saved)
            receipt = case.bridge.api_ask_close(dict(ask_key='intent', decision='No authority'))
            case.assertEqual(receipt['reason'], 'siding_outcome_uncertain')
            value = pending['canonical']
            native('FoldSidingExpected', dict(ask=value['ask'], digest=value['digest'], op=pending['op'], actor='pat'))
            case.cli('group', 'set', 'keepers', 'fold_after=12h')
            def delayed(method, args):
                if method == 'SidingOutcome' and args:
                    return None
                return native(method, args)
            with patch.object(case.bridge.siding_cards.client, 'call', side_effect=delayed):
                case.bridge.siding_cards.refresh()
                receipt = case.bridge.api_ask_close(dict(ask_key='intent', decision='No authority'))
                case.assertEqual(receipt['reason'], 'siding_outcome_uncertain')
                saved = case.bridge.siding_cards.get(pending['id'])
                case.assertTrue(saved['attempted'])
                case.assertIsNone(saved['refused'])
                case.assertEqual(saved['display']['footer'], footer)
            case.restart()
            receipt = case.bridge.api_ask_close(dict(ask_key='intent', decision='Canonical outcome only'))
            case.assertTrue(receipt['delivered'], receipt)
            saved = case.bridge.siding_cards.get(pending['id'])
            case.assertTrue(saved['attempted'])
            case.assertIsNotNone(saved['outcome'])
            case.assertIsNotNone(saved['withdrawn'])
            settled = copy.deepcopy(saved)
            with patch.object(case.bridge.siding_cards.client, 'call', side_effect=n.RPCUnavailable('synthetic second outage')):
                case.bridge.poll_once()
            case.assertEqual(case.bridge.siding_cards.get(pending['id']), settled)
            case.assertEqual(len(case.client.call('EventsOfKind', ['folded', 100])), 1)
        finally:
            case.doCleanups()
            case.tearDown()
        one, two = self.part('1'), self.part('2')
        row = self.card()
        original = self.bridge.siding_cards.client.call
        writes = []
        def lose_response(method, args):
            result = original(method, args)
            if method == 'FoldSidingExpected' and args:
                writes.append(args)
                raise n.RPCAmbiguous('synthetic response lost after real commit')
            return result
        with patch.object(self.bridge.siding_cards.client, 'call', side_effect=lose_response):
            self.press(row)
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.bridge.siding_cards.state(self.bridge.siding_cards.get(row['id'])), 'settled')
        self.restart()
        self.press(row, uid=42)
        self.assertEqual(len(self.client.call('EventsOfKind', ['folded', 100])), 1)
        saved = self.bridge.siding_cards.get(row['id'])
        for mutation in ('action', 'members', 'depot', 'op'):
            tampered = copy.deepcopy(saved)
            if mutation == 'op':
                tampered['op'] += ':another'
            elif mutation == 'members':
                tampered['canonical']['ask']['members'].reverse()
            elif mutation == 'depot':
                tampered['canonical']['ask']['depot'] = 'elsewhere'
            else:
                tampered['canonical']['ask']['action'] = 'release'
                tampered['canonical']['ask']['members'] = [one]
            with self.assertRaises(n.RPCRefused):
                self.bridge.siding_cards.outcome(tampered)
        # Prune the real isolated effect event; absence is never permission to write again.
        self.cli('bead', 'note', self.bridge.siding_cards.outcome(saved)[1]['id'], 'Synthetic retention noise')
        before = self.client.call('ListBeads', [{}])
        pruned = self.client.call('Prune', [dict(events=1, hook_payloads=1, inbox=1), 'pat'])
        self.assertGreater(pruned['events'], 0)
        self.assertIsNone(self.bridge.siding_cards.outcome(saved))
        self.assertIsNotNone(saved['outcome'])
        with patch.object(self.bridge.siding_cards.client, 'call', wraps=self.bridge.siding_cards.client.call) as calls:
            self.press(saved, action='leave', uid=43)
            self.press(saved, uid=44)
            self.bridge.siding_cards.refresh()
            self.assertFalse(any(c.args[0].endswith('Expected') for c in calls.call_args_list))
        self.assertEqual(self.client.call('ListBeads', [{}]), before)
        with self.assertRaises(n.RPCRefused):
            self.client.call('FoldSidingExpected', writes[0])
        self.assertEqual(self.client.call('ListBeads', [{}]), before)

    def test_repository_sources_and_source_class_race(self):
        one, two = self.part('1'), self.part('2')
        row = self.card()
        repo_flow = self.root / 'depot' / '.yardr' / 'flows' / 'default.flow'
        repo_flow.parent.mkdir(parents=True)
        repo_flow.write_text(FLOW)
        original = self.bridge.siding_cards.client.call
        def source_race(method, args):
            if method == 'FoldSidingExpected' and args:
                self.cli('depot', 'flow', 'sample', 'default')
            return original(method, args)
        with patch.object(self.bridge.siding_cards.client, 'call', side_effect=source_race):
            self.press(row)
        self.assertFalse(self.client.call('EventsOfKind', ['folded', 100]))
        repository = self.card(key='repository')
        self.assertEqual(repository['canonical']['guard'], 'repository')
        self.assertIn('Repository inputs cannot be guarded', repository['plain'])
        self.assertEqual(self.bridge.siding_cards.keyboard(repository), [])
        self.press(repository, uid=42)
        self.assertFalse(self.client.call('EventsOfKind', ['folded', 100]))
        # A store flow may still depend on a repository-backed until release.
        self.cli('depot', 'flow', 'sample', 'rotation.f')
        blockers = self.root / 'blockers'
        (blockers / '.yardr' / 'flows').mkdir(parents=True)
        (blockers / '.yardr' / 'flows' / 'default.flow').write_text('new -> ready\nready -> later\nlater -> done\ndone terminal\n')
        self.cli('depot', 'add', 'blockers', str(blockers), '--kind', 'dir')
        self.cli('bead', 'create', 'Synthetic blocker beyond ready', '--id', 'blockers-1', '--depot', 'blockers', '--stage', 'later')
        self.cli('dep', 'add', 'blockers-1', one, '--until', 'ready')
        until = self.card(key='until')
        self.assertEqual(until['canonical']['guard'], 'repository')
        self.assertEqual(until['canonical']['flow']['source']['kind'], 'store')
        self.assertEqual(until['canonical']['unguarded'][0]['input'], 'until')
        self.assertEqual(self.bridge.siding_cards.keyboard(until), [])

    def test_missing_core_unknown_schema_and_idempotency_collisions(self):
        config = dict(self.config)
        self.config.pop('yardr_home')
        self.config.pop('yardr_operator')
        self.restart()
        self.bridge.hook_secret = 'synthetic-hook'
        with patch.object(self.bridge.run_asks, 'binding', side_effect=AssertionError('ordinary native binding')):
            with patch.object(self.bridge.siding_cards, 'binding', side_effect=AssertionError('ordinary siding binding')):
                receipt = self.bridge.api_ask_close(dict(message_id=77, decision='Unknown ordinary ask'))
                self.assertEqual(receipt['reason'], 'ask_not_found')
                key = 'without-native'
                request = self.publish(key=key, text='Synthetic ordinary choice')
                request['keyboard'] = [[dict(text='Keep', callback_data=f'ask:{b.Bridge._ask_id_for_key(key)}:keep')]]
                receipt = self.bridge.publish(request)
                self.assertTrue(receipt['delivered'], receipt)
                self.reply(dict(message_id=int(receipt['message_id'])), 'Synthetic ordinary reply', uid=10)
                relayed = [body for url, body in self.calls if url == self.bridge.hook_url]
                self.assertEqual((relayed[-1]['text'], relayed[-1]['reply_to']),
                                 ('Synthetic ordinary reply', receipt['message_id']))
                receipt = self.bridge.api_ask_close(dict(message_id=receipt['message_id'], decision='Keep'))
                self.assertTrue(receipt['delivered'], receipt)
        self.config.update(config)
        self.restart()
        with patch.object(self.bridge.run_asks, 'binding', side_effect=AssertionError('unmatched native binding')):
            with patch.object(self.bridge.siding_cards, 'binding', side_effect=AssertionError('unmatched siding binding')):
                receipt = self.bridge.api_ask_close(dict(message_id=9999, decision='Unknown ask'))
                self.assertEqual(receipt['reason'], 'ask_not_found')
        with patch.object(self.bridge.siding_cards, 'client', None):
            receipt = self.bridge.api_ask_close(dict(message_id=9999, decision='Unknown ordinary ask'))
            self.assertFalse(receipt['delivered'])
            self.assertEqual(receipt['reason'], 'ask_not_found')
            self.assertFalse(self.bridge.siding_cards.reply(77, dict(reply_to_message=dict(message_id=77))))
        self.part('1')
        self.part('2')
        original = self.bridge.siding_cards.client.call
        native = self.card(key='native-schema')
        self.assertEqual(native['canonical'].get('held', []), [])
        self.assertEqual(self.bridge.siding_cards.state(native), 'open')
        self.assertTrue(self.bridge.siding_cards.keyboard(native))
        def first_slice(method, args):
            result = original(method, args)
            if method == 'SidingProposal':
                result.pop('held', None)
            return result
        with patch.object(self.bridge.siding_cards.client, 'call', side_effect=first_slice):
            earlier = self.card(key='first-slice-schema')
            self.assertNotIn('held', earlier['canonical'])
            self.assertTrue(self.bridge.siding_cards.keyboard(earlier))
        held = dict(input='flow', depot='sample', ref='refs/heads/main', commit='0' * 40,
                    path='.yardr/flows/default.flow')
        invalid_schemas = [dict(held=value) for value in (None, False, {}, ['unknown'], [held])]
        invalid_schemas += [dict(future=[]), dict(guard='git', held=[]), dict(guard='git', held=[held]),
                            dict(guard='future', held=[])]
        for index, fields in enumerate(invalid_schemas):
            with self.subTest(unsupported_schema=fields):
                def unsupported(method, args):
                    result = original(method, args)
                    if method == 'SidingProposal':
                        result.update(fields)
                    return result
                sent = self.counter
                with patch.object(self.bridge.siding_cards.client, 'call', side_effect=unsupported):
                    result = self.bridge.api_siding_card(dict(depot='sample', key='unsupported-schema-' + str(index)))
                self.assertFalse(result['delivered'], result)
                self.assertIn('invalid native siding', result['reason'])
                self.assertEqual(self.counter, sent)
        self.assertFalse(self.client.call('EventsOfKind', ['folded', 100]))
        for missing in SIDING_METHODS:
            def old(method, args):
                if method == missing:
                    raise n.RPCUnsupported('siding_card_requires_guarded_core')
                return original(method, args)
            with patch.object(self.bridge.siding_cards.client, 'call', side_effect=old):
                result = self.bridge.api_siding_card(dict(depot='sample', key='missing-' + missing))
                self.assertFalse(result['delivered'])
                self.assertIn('guarded_core', result['reason'])
        for field in ('guard', 'unguarded', 'version'):
            def malformed(method, args):
                result = original(method, args)
                if method == 'SidingProposal':
                    del result[field]
                return result
            with patch.object(self.bridge.siding_cards.client, 'call', side_effect=malformed):
                result = self.bridge.api_siding_card(dict(depot='sample', key='schema-' + field))
                self.assertFalse(result['delivered'])
        for method in ('SidingOutcome', 'SidingProposal'):
            for failure in ('unsupported', 'malformed'):
                with self.subTest(published_core_change=(method, failure)):
                    card = self.card(key=method + '-' + failure)
                    def changed(name, args):
                        if name == method:
                            if failure == 'unsupported':
                                raise n.RPCUnsupported('siding_card_requires_guarded_core')
                            if name == 'SidingOutcome':
                                return dict(unknown='synthetic unknown schema')
                            value = original(name, args)
                            value['depot'] = None
                            return value
                        return original(name, args)
                    with patch.object(self.bridge.siding_cards.client, 'call', side_effect=changed):
                        self.bridge.siding_cards.refresh()
                    saved = self.bridge.siding_cards.get(card['id'])
                    self.assertIsNotNone(saved['refused'])
                    self.assertFalse(saved['attempted'])
                    self.assertEqual(self.bridge.siding_cards.keyboard(saved), [])
                    edit = next(body for url, body in reversed(self.calls)
                                if url.endswith('/editMessageText') and body['message_id'] == card['message_id'])
                    self.assertEqual(edit['reply_markup']['inline_keyboard'], [])
        row = self.card(key='reuse')
        before = self.counter
        with self.subTest(repeated_open_create=True):
            repeat = self.bridge.api_siding_card(dict(depot='sample', key='reuse'))
            self.assertFalse(repeat['delivered'])
            self.assertEqual(repeat['reason'], 'siding_still_awaiting_human')
        self.assertEqual(self.counter, before)
        result = self.bridge.api_siding_card(dict(depot='sample', stage='harbor', key='reuse'))
        self.assertFalse(result['delivered'])
        self.assertEqual(result['reason'], 'idempotency_key_conflict')
        for callback in ('siding:' + row['id'] + ':approve', 'siding:' + row['id'] + ':leave'):
            receipt = self.bridge.publish(dict(text='Forged card', idempotency_key='forged:' + callback.rsplit(':', 1)[1],
                keyboard=[[dict(text='Fake', callback_data=callback)]]))
            self.assertFalse(receipt['delivered'])
        send_key, identity = self.bridge.siding_cards.send_request(row)
        with self.store.connect() as db:
            db.execute('UPDATE siding_cards SET data=? WHERE id=?', (json.dumps(dict(row, message_id=None)), row['id']))
        self.restart()
        self.assertEqual(self.bridge.siding_cards.get(row['id'])['message_id'], row['message_id'])
        with self.store.connect() as db:
            db.execute('UPDATE siding_cards SET data=? WHERE id=?', (json.dumps(dict(row, message_id=None)), row['id']))
            db.execute('UPDATE deliveries SET fingerprint=? WHERE key=?', ('wrong', send_key))
        self.assertIsNone(self.bridge.siding_cards.get(row['id'])['message_id'])
        self.assertFalse(self.client.call('EventsOfKind', ['folded', 100]))
        fresh = self.card(key='reply-delivery')
        telegram = self.bridge.request
        def refuse_review(url, body, *args, **kwargs):
            if url.endswith('/sendMessage') and 'reply_markup' in body:
                return 400, dict(ok=False, description='synthetic rejected delivery')
            return telegram(url, body, *args, **kwargs)
        with patch.object(self.bridge, 'request', side_effect=refuse_review):
            self.reply(fresh, 'release sample-1', uid=110)
        notices = [body['text'] for url, body in self.calls if url.endswith('/sendMessage') and 'reply_markup' not in body]
        self.assertTrue(any('siding_review_delivery_unconfirmed' in text for text in notices))
        self.assertFalse(self.client.call('EventsOfKind', ['folded', 100]))
        status = self.card(key='receipt-gap', stage='repair')
        send_key, identity = self.bridge.siding_cards.send_request(status)
        fingerprint, original_receipt = self.store.saved_delivery(send_key)
        for bad in (None, dict(delivered=False), dict(delivered=True),
                    dict(delivered=True, message_id=0), dict(delivered=True, message_id=status['message_id'] + 1)):
            with self.subTest(unconfirmed_receipt=bad):
                with self.store.connect() as db:
                    db.execute('DELETE FROM deliveries WHERE key=?', (send_key,))
                    if bad is not None:
                        db.execute('INSERT INTO deliveries VALUES (?,?,?)', (send_key, fingerprint, json.dumps(bad)))
                receipt = self.bridge.api_ask_close(dict(ask_key='receipt-gap', decision='Require own receipt'))
                self.assertFalse(receipt['delivered'], receipt)
        with self.store.connect() as db:
            db.execute('UPDATE deliveries SET receipt=? WHERE key=?', (json.dumps(original_receipt), send_key))
        self.assertTrue(self.bridge.api_ask_close(dict(ask_key='receipt-gap', decision='Confirmed status'))['delivered'])
        for state in ('stale', 'settled'):
            with self.subTest(send_recovery_after=state):
                key = 'send-identity-' + state
                card = self.card(key=key)
                send_key, identity = self.bridge.siding_cards.send_request(card)
                published = self.store.saved_delivery(send_key)
                if state == 'stale':
                    self.cli('bead', 'update', 'sample-1', '--title', 'Changed send recovery proposal')
                else:
                    value = card['canonical']
                    self.client.call('FoldSidingExpected', dict(ask=value['ask'], digest=value['digest'],
                                                              op=card['op'], actor='pat'))
                self.assertTrue(self.bridge.api_ask_close(dict(ask_key=key, decision='Canonical fact'))['delivered'])
                saved = self.bridge.siding_cards.get(card['id'])
                self.assertEqual(self.bridge.siding_cards.send_request(saved), (send_key, identity))
                self.assertNotIn('state', saved)
                saved['message_id'] = None
                self.bridge.siding_cards.save(saved)
                self.restart()
                recovered = self.bridge.siding_cards.get(card['id'])
                self.assertEqual(recovered['message_id'], card['message_id'])
                self.assertEqual(self.store.saved_delivery(send_key), published)
                start = len(self.calls)
                receipt = self.bridge.api_siding_card(dict(depot='sample', key=key))
                self.assertTrue(receipt['delivered'], receipt)
                self.assertEqual(int(receipt['message_id']), card['message_id'])
                self.assertEqual(self.calls[start:], [])

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
        self.assertIsNone(empty['canonical'])
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
        self.assertEqual(row['conversation'], '123:topic:12')
        self.press(row)
        self.assertFalse(self.client.call('EventsOfKind', ['folded', 100]))
        message = dict(message_id=row['message_id'], chat=dict(id=123, type='private'),
                       message_thread_id=12, is_topic_message=True, **{'from': dict(id=999, is_bot=True)})
        self.press(row, uid=42, message=message)
        self.assertEqual(len(self.client.call('EventsOfKind', ['folded', 100])), 1)
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
                                 '--depot', 'sample', '--idempotency-key', 'installed-cli'],
                                env=dict(self.env, PATH=str(fake_bin) + ':' + self.env['PATH']), cwd=self.root,
                                text=True, capture_output=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertTrue(json.loads(result.stdout)['delivered'])
        cli_store = b.Store(self.root / 'state' / 'telegram-bridge.sqlite3', 6)
        with cli_store.connect() as db:
            rows = [json.loads(r[0]) for r in db.execute('SELECT data FROM siding_cards')]
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]['kind'], 'siding')
        self.assertIsNone(rows[0]['canonical'])

    def test_two_real_stores_same_telegram_human_are_independent(self):
        self.part('1')
        self.part('2')
        first = self.card()
        other = SidingTests(methodName='test_real_fold_release_and_replay')
        other.setUp()
        self.addCleanup(other.tearDown)
        self.addCleanup(other.doCleanups)
        other.part('1')
        other.part('2')
        second = other.card()
        self.assertEqual(self.config['user_id'], other.config['user_id'])
        other.press(first)
        self.assertFalse(other.client.call('EventsOfKind', ['folded', 100]))
        other.press(second, uid=42)
        self.assertEqual(len(other.client.call('EventsOfKind', ['folded', 100])), 1)
        self.assertFalse(self.client.call('EventsOfKind', ['folded', 100]))
        self.press(first)
        self.assertEqual(len(self.client.call('EventsOfKind', ['folded', 100])), 1)
