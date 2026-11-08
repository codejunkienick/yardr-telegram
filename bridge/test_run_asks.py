"""Native paths through the bridge, a framed Unix RPC and fake Telegram."""
import copy
from concurrent.futures import ThreadPoolExecutor
import hashlib
import http.server
import json
import os
from pathlib import Path
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b
from test_bridge import BridgeTestCase, write_settings

import run_asks as n
n = getattr(b, 'run_asks', n)


def proposal():
    return dict(ask=7, turn=0, to='pat', asker='proposer', since='2026-01-01T00:00:00Z',
                question='Change the test?', context=['First <context>', 'Second & context'], options=[], state='open',
                run=dict(argv=['depot', 'test', 'web'], command='yardr depot test web', description='Set its test',
                         does=['current: make test', 'proposed: sh scripts/test.sh'],
                         back='propose the old command', back_by='an operator'))


def decision():
    return dict(ask=7, turn=0, to='pat', asker='proposer', since='2026-01-01T00:00:00Z',
                question='Where should the links go?', context=['First <context>', 'Second & context'],
                options=[dict(label='Move', consequence='Move every link.'),
                         dict(label='Keep', consequence='Keep the links here.'),
                         dict(label='You decide', consequence='leave it to proposer')],
                recommend=1, why='The new location is ready.',
                about=[dict(id='related-1', title='Related <work>', stage='backlog')], state='open')


def skill_proposal(extra=''):
    # Captured from Bead on the real isolated core; only provenance/seq/time stabilized.
    ask = json.loads(Path(__file__).with_name('fixtures').joinpath('skill_add.json').read_text())
    if extra:
        carried = ask['run']['carried'][0]
        file, change = carried['files'][0], carried['old'][0]
        file['text'] += extra
        file['sha256'] = hashlib.sha256(file['text'].encode()).hexdigest()
        change['new_sha256'] = file['sha256']
        before = carried['text'].removesuffix('\n').split('\n')
        after = file['text'].removesuffix('\n').split('\n')
        change['diff'] = ('--- was\n+++ is\n' + f'@@ -1,{len(before)} +1,{len(after)} @@\n'
                          + ''.join('-' + line + '\n' for line in before)
                          + ''.join('+' + line + '\n' for line in after))
        does = ask['run']['does'][:7]
        does[2] = f"writes skills/snapshot/SKILL.md, mode 0644; sha256 {change['old_sha256']} -> {file['sha256']}"
        for label, value in [('proposed SKILL.md', file['text']), ('standing SKILL.md', carried['text']), ('replacement diff', change['diff'])]:
            does.append(label + ' (quoted UTF-8 lines, including line endings):')
            rows = [line + '\n' for line in value.split('\n')[:-1]]
            if not value or not value.endswith('\n'):
                rows.append(value.rsplit('\n', 1)[-1])
            does.extend(json.dumps(line, ensure_ascii=False) for line in rows)
        ask['run']['does'] = does
    return ask


class NativeSocket:
    def __init__(self, root):
        self.ask = proposal()
        self.user = dict(name='pat', role='operator', identities={'tgbridge': {'id': '123', 'key': 'Pat'}})
        self.calls = []
        self.mode = None
        self.read_statuses = []
        self.bead = dict(id='sample-1', title='Synthetic change')
        owner = self

        class Handler(http.server.BaseHTTPRequestHandler):
            def log_message(self, *_):
                pass

            def do_POST(self):
                args = json.loads(self.rfile.read(int(self.headers['Content-Length'])))
                method = self.path.removeprefix('/call/')
                owner.calls.append((method, args, self.headers.get('X-Yardr-Connection')))
                if method in ('GetUser', 'Bead') and owner.read_statuses:
                    self.send_error(owner.read_statuses.pop(0))
                    return
                if method == 'GetUser':
                    result = owner.user
                elif method == 'Bead':
                    if owner.mode == 'unreadable':
                        self.send_error(503)
                        return
                    result = dict(owner.bead, asks=[owner.ask])
                elif method in ('ApproveAsk', 'DeclineAsk', 'AnswerAsk', 'DeferAsk',
                                'AnswerAskExpected', 'DeferAskExpected', 'DeclineAskExpected'):
                    if method.endswith('Expected'):
                        if owner.mode == 'old-method':
                            self.reply({'error': {'code': 'error', 'message': 'no such method ' + method}})
                            return
                        if args.get('turn') != owner.ask.get('turn'):
                            self.reply({'error': {'code': 'conflict', 'message': 'stale turn'}})
                            return
                        method = method.removesuffix('Expected')
                    replacing = method == 'AnswerAsk' and owner.ask['state'] == 'answered' and owner.ask.get('options')
                    if (owner.ask['state'] != 'open' and not replacing) or owner.mode == 'refused':
                        self.reply({'error': {'code': 'conflict', 'message': 'refused'}})
                        return
                    if owner.mode != 'lost-open':
                        owner.ask['turn'] = owner.ask.get('turn', 0) + 1
                        owner.ask['state'] = {'ApproveAsk': 'answered', 'AnswerAsk': 'answered',
                                              'DeclineAsk': 'declined', 'DeferAsk': 'put aside'}[method]
                        owner.ask['words'] = args.get('words', 'canonical decision')
                        if method == 'AnswerAsk':
                            owner.ask['option'] = args['option']
                        if method == 'DeferAsk':
                            owner.ask['return_at'] = '2026-01-02T00:00:00Z'
                        if method == 'ApproveAsk':
                            owner.ask['ran'] = dict(argv=owner.ask['run']['argv'],
                                                   result=n.PENDING if owner.mode == 'carrying' else 'stored operation applied')
                    if owner.mode == 'lost-unreadable':
                        owner.mode = 'unreadable'
                        return
                    if owner.mode == 'wire-error-commit':
                        self.reply({'error': {'code': 'error', 'message': 'transport failed after commit'}})
                        return
                    if owner.mode in ('lost-commit', 'lost-open', 'invalid-commit', 'bad-request-commit'):
                        if owner.mode in ('invalid-commit', 'bad-request-commit'):
                            self.send_response(400 if owner.mode == 'bad-request-commit' else 502)
                            self.end_headers()
                            self.wfile.write(b'not a rollback')
                        return
                    result = dict(bead='sample-1', ask=7, result='stored operation applied') if method == 'ApproveAsk' else None
                else:
                    self.reply({'error': {'code': 'not-found', 'message': 'unknown method'}})
                    return
                self.reply({'result': result})

            def reply(self, value):
                raw = json.dumps(value).encode()
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.server = Server(str(root / 'yard.sock'), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class NativeTests(BridgeTestCase):
    def setUp(self):
        super().setUp()
        self.home = Path(self.tmp.name) / 'yard'
        self.home.mkdir(mode=0o700)
        self.native = NativeSocket(self.home)
        self.addCleanup(self.native.close)
        self.config.update(yardr_home=str(self.home.resolve()), yardr_operator='pat', yardr_source='tgbridge')
        self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=self.transport)
        self.sent_count = 0
        self.config['hook_secret'] = None
        self.bridge.hook_secret = None
        base = self.bridge.request

        def telegram(url, body, *args, **kwargs):
            if url.endswith('/sendMessage'):
                self.sent_count += 1
                self.calls.append((url, body))
                return 200, dict(ok=True, result=dict(message_id=76 + self.sent_count, chat=dict(id=123)))
            if url.endswith('/editMessageText'):
                self.calls.append((url, body))
                return 200, dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=123)))
            if url.endswith('/answerCallbackQuery'):
                self.calls.append((url, body))
                return 200, dict(ok=True, result=True)
            return base(url, body, *args, **kwargs)
        self.bridge.request = telegram

    def reset_native(self, ask):
        with self.store.connect() as db:
            for table in ('run_asks', 'run_ask_keys', 'run_ask_updates', 'deliveries'):
                db.execute('DELETE FROM ' + table)
            db.execute('UPDATE cursor SET offset=0')
        self.native.ask = ask
        self.native.calls.clear()
        self.native.mode = None
        self.native.user = dict(name='pat', role='operator', identities={'tgbridge': {'id': '123'}})
        self.calls.clear()
        self.sent_count = 0
        self.bridge.run_asks = n.RunAsks(self.bridge)

    def create(self, **kwargs):
        return self.bridge.api_run_ask(dict(bead='sample-1', ask=7, key='native-1', **kwargs))

    def row(self):
        return self.bridge.run_asks.rows()[0]

    def press(self, action='approve', uid=40, **changes):
        row = self.row()
        message = dict(message_id=row['message_id'], chat=dict(id=123, type='private'),
                       **{'from': dict(id=999, is_bot=True)})
        message.update(changes)
        prefix = 'askcard' if row.get('kind') == 'decision' else 'runask'
        suffix = ':' + str(row['turn']) if prefix == 'askcard' else ''
        query = dict(id=f'query-{uid}', data=f"{prefix}:{row['id']}:{action}{suffix}", message=message,
                     **{'from': dict(id=123, is_bot=False)})
        self.bridge.inbound(dict(update_id=uid, callback_query=query))
        return query

    def reply(self, value='7', uid=50, **changes):
        message = dict(message_id=uid, text=value, chat=dict(id=123, type='private'),
                       reply_to_message={'message_id': self.row()['message_id']},
                       **{'from': dict(id=123, is_bot=False)})
        message.update(changes)
        self.bridge.inbound(dict(update_id=uid, message=message))

    def writes(self):
        return [(method, args) for method, args, _ in self.native.calls if method in
                ('ApproveAsk', 'DeclineAsk', 'AnswerAsk', 'DeferAsk', 'AnswerAskExpected', 'DeferAskExpected', 'DeclineAskExpected')]

    def edits(self):
        return [body for url, body in self.calls if url.endswith('/editMessageText')]

    def create_decision(self, ask=None, **kwargs):
        self.reset_native(decision() if ask is None else ask)
        return self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='decision-1', **kwargs))

    def test_ordinary_options_words_later_reopen_decline_and_native_close(self):
        self.assertTrue(self.create_decision()['delivered'])
        card = self.calls[-1][1]
        for field in ('Where should the links go?', 'First &lt;context&gt;', 'Second &amp; context',
                      '1. Move', 'Move every link.', '2. Keep', 'Keep the links here.',
                      '3. You decide', 'leave it to proposer', 'Recommended: 1. Move',
                      'Why: The new location is ready.', 'About: related-1 · Related &lt;work&gt; · backlog'):
            self.assertIn(field, card['text'])
        self.assertEqual(len(card['reply_markup']['inline_keyboard']), 4)
        self.assertFalse(self.bridge.api_ask_close(dict(ask_key='decision-1', decision='forged'))['delivered'])
        self.press('pick3')
        self.assertEqual(self.writes(), [('AnswerAskExpected', dict(bead='sample-1', ask=7, turn=0, actor='pat', option=3, words='', weighed=False))])
        self.assertIn('Answered: 3. You decide', self.edits()[-1]['text'])
        self.bridge.run_asks = n.RunAsks(self.bridge)
        self.press('pick1', uid=41)
        self.reply('1 changed', uid=42)
        self.assertEqual(len(self.writes()), 1)
        self.assertTrue(self.bridge.api_ask_close(dict(ask_key='decision-1', decision='forged'))['delivered'])
        self.assertNotIn('forged', self.edits()[-1]['text'])
        self.assertTrue(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='decision-1'))['delivered'])
        self.assertEqual(self.sent_count, 2)  # card and bound-reply notice, never a second card

        self.assertTrue(self.create_decision()['delivered'])
        self.reply('2 keep them until Monday', uid=43)
        self.assertEqual(self.writes()[-1][1]['words'], 'keep them until Monday')
        self.assertEqual(self.writes()[-1][1]['option'], 2)
        self.assertFalse(any(url.endswith('/deleteMessage') for url, _ in self.calls))
        ask = decision()
        ask.update(options=None)
        ask.pop('recommend')
        ask.pop('why')
        self.assertTrue(self.create_decision(ask)['delivered'])
        self.assertIn('with your answer.', self.calls[-1][1]['text'])
        self.reply('Use the new location 🙂', uid=44)
        self.assertEqual(self.writes()[-1], ('AnswerAskExpected', dict(bead='sample-1', ask=7, turn=0, actor='pat', option=0,
                                                             words='Use the new location 🙂', weighed=False)))
        self.assertTrue(self.create_decision()['delivered'])
        self.press('later', uid=45)
        self.assertEqual(self.row()['state'], 'put aside')
        self.assertIn('until 2026-01-02', self.edits()[-1]['text'])
        self.assertEqual(self.edits()[-1]['reply_markup']['inline_keyboard'], [])
        self.reply('1', uid=46)
        self.assertEqual(self.writes(), [('DeferAskExpected', dict(bead='sample-1', ask=7, turn=0, actor='pat'))])
        self.native.ask.update(state='open')
        self.bridge.run_asks.refresh()
        self.assertEqual(self.row()['state'], 'open')
        self.assertEqual(self.edits()[-1]['reply_markup']['inline_keyboard'], [])
        self.assertNotEqual(self.row()['generations'][0]['message_id'], self.row()['message_id'])
        self.press('decline', uid=47)
        self.assertEqual(self.writes()[-1], ('DeclineAskExpected', dict(bead='sample-1', ask=7, turn=1,
                                                                     actor='pat', words='Declined in Telegram')))
        self.assertEqual(self.row()['state'], 'declined')
        self.native.ask.update(state='taken back', by='proposer', words='canonical withdrawal')
        self.bridge.run_asks.refresh()
        self.assertIn('Taken back by proposer: canonical withdrawal', self.edits()[-1]['text'])

        self.bridge.sleep = lambda _: None
        for restart in (False, True):
            for failure in ('not-sent', 'unconfirmed', 'server-error'):
                with self.subTest(reopen_restart=restart, edit_failure=failure):
                    self.assertTrue(self.create_decision()['delivered'])
                    self.press('later')
                    transport = self.bridge.request
                    attempts = []

                    def unavailable(url, body, *args, **kwargs):
                        if url.endswith('/editMessageText'):
                            attempts.append(body)
                            if failure == 'not-sent':
                                raise b.RequestNotSent('synthetic connection failure')
                            if failure == 'unconfirmed':
                                raise b.RequestUnconfirmed('synthetic uncertain edit')
                            return 503, dict(ok=False, error_code=503)
                        return transport(url, body, *args, **kwargs)

                    try:
                        self.bridge.request = unavailable
                        self.native.ask['state'] = 'open'
                        self.bridge.run_asks.refresh()
                        self.assertTrue(attempts)
                        self.assertEqual(self.row()['state'], 'open')
                        revision = self.row()['generations'][0]['revision']
                        if restart:
                            self.bridge.run_asks = n.RunAsks(self.bridge)
                    finally:
                        self.bridge.request = transport
                    count = len(self.edits())
                    self.bridge.run_asks.refresh()
                    self.assertEqual(len(self.edits()), count + 1)
                    self.assertEqual(self.edits()[-1], attempts[-1])
                    self.assertNotIn('Put aside', self.edits()[-1]['text'])
                    self.assertEqual(self.edits()[-1]['reply_markup']['inline_keyboard'], [])
                    self.assertEqual(self.row()['generations'][0]['revision'], revision)
                    count = len(self.edits())
                    self.bridge.run_asks.refresh()
                    self.assertEqual(len(self.edits()), count)
                    self.assertEqual([m for m, _ in self.writes()], ['DeferAskExpected'])

        for uncertain_method in ('sendMessage', 'editMessageText'):
            for state, label in (('answered', 'Answered: 2. Keep'), ('declined', 'Declined'),
                                 ('taken back', 'Taken back by proposer'), ('put aside', 'Put aside')):
                with self.subTest(superseding_reopen=state, uncertain_method=uncertain_method):
                    self.assertTrue(self.create_decision()['delivered'])
                    self.press('later')
                    transport = self.bridge.request
                    def uncertain_retirement(url, body, *args, **kwargs):
                        if url.endswith('/' + uncertain_method):
                            raise b.RequestUnconfirmed('synthetic uncertain edit')
                        return transport(url, body, *args, **kwargs)
                    with patch.object(self.bridge, 'request', side_effect=uncertain_retirement):
                        self.native.ask['state'] = 'open'
                        self.bridge.run_asks.refresh()
                    self.native.ask.update(state=state, option=2, words='Newer canonical outcome', by='proposer')
                    self.bridge.run_asks = n.RunAsks(self.bridge)
                    self.bridge.run_asks.refresh()
                    self.assertEqual(self.row()['state'], state)
                    settled = next(body for body in reversed(self.edits()) if body['message_id'] == self.row()['generations'][-1]['message_id'])
                    self.assertIn(label, settled['text'])
                    self.assertEqual(self.edits()[-1]['reply_markup']['inline_keyboard'], [])
                    count = len(self.edits())
                    self.bridge.run_asks.refresh()
                    self.assertEqual(len(self.edits()), count)
                    self.assertEqual([m for m, _ in self.writes()], ['DeferAskExpected'])

    def test_ordinary_refuses_incomplete_unknown_operation_and_weighted_payloads(self):
        for change in (dict(run=proposal()['run']), dict(secret={}), dict(setup={}), dict(identity={}),
                       dict(depot_push={}), dict(unknown='sensitive input'), dict(to='another'),
                       dict(options=[dict(label='incomplete')]), dict(recommend=True), dict(recommend=9),
                       dict(context='wrong shape'), dict(about=[dict(id='missing')]),
                       dict(turn=None), dict(turn=True), dict(turn=-1), dict(turn='0'), dict(turn=2**63),
                       dict(weight=[dict(**{'class': 'outside'}, line='canonical weight')]),
                       dict(question='oversized 🙂' * 600), dict(question='bad\ud800'),
                       dict(why='oversized' * 600), dict(options=[dict(label='long' * 900, consequence='all')]),
                       dict(options=[dict(label='short', consequence='long' * 900)]),
                       dict(options=[dict(label='x', consequence='x')] * 99),
                       dict(context=['long' * 900]), dict(about=[dict(id='other', title='long' * 900, stage='new')])):
            with self.subTest(change=list(change)):
                self.assertFalse(self.create_decision(dict(decision(), **change))['delivered'])
                self.assertEqual(self.calls, [])
                self.assertEqual(self.writes(), [])
        old = decision()
        old.pop('turn')
        receipt = self.create_decision(old)
        self.assertFalse(receipt['delivered'])
        self.assertIn('requires_guarded_core', receipt['reason'])
        self.assertEqual(self.calls, [])
        for action, method in (('pick1', 'AnswerAskExpected'), ('later', 'DeferAskExpected'),
                               ('decline', 'DeclineAskExpected'), ('reply', 'AnswerAskExpected')):
            with self.subTest(old_method=action):
                self.assertTrue(self.create_decision()['delivered'])
                self.native.mode = 'old-method'
                if action == 'reply':
                    self.reply('2 older core')
                else:
                    self.press(action)
                self.assertEqual([m for m, _ in self.writes()], [method])
                self.assertEqual(self.native.ask['state'], 'open')
                self.assertIn('requires_guarded_core', self.edits()[-1]['text'])
                self.bridge.run_asks = n.RunAsks(self.bridge)
                self.bridge.run_asks.refresh()
                self.assertEqual(len(self.writes()), 1)
        self.assertTrue(self.create_decision()['delivered'])
        remaining = n.CAP - n.units(self.row()['plain'])
        full = decision()
        full['question'] += '🙂' * (remaining // 2) + 'x' * (remaining % 2)
        self.assertTrue(self.create_decision(full)['delivered'])
        self.assertEqual(n.units(self.row()['plain']), n.CAP)
        self.assertFalse(self.create_decision(dict(full, question=full['question'] + 'x'))['delivered'])
        self.assertTrue(self.create_decision()['delivered'])
        self.assertFalse(self.bridge.api_run_ask(dict(bead='sample-1', ask=7, key='wrong-kind'))['delivered'])
        self.native.ask = proposal()
        self.assertFalse(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='wrong-kind'))['delivered'])

    def test_ordinary_ambiguous_writes_settle_only_from_canonical_state(self):
        for action in ('pick1', 'later', 'decline', 'reply'):
            for mode in ('lost-commit', 'invalid-commit', 'bad-request-commit', 'wire-error-commit', 'lost-open', 'lost-unreadable'):
                with self.subTest(action=action, mode=mode):
                    self.assertTrue(self.create_decision()['delivered'])
                    self.native.mode = mode
                    if action == 'reply':
                        self.reply('2 canonical note')
                    else:
                        self.press(action)
                    self.assertEqual(len(self.writes()), 1)

                    expected = 'put aside' if action == 'later' else 'declined' if action == 'decline' else 'answered'
                    if mode in ('lost-open', 'lost-unreadable'):
                        self.assertIn(n.UNCERTAIN, self.edits()[-1]['text'])
                        self.assertEqual(self.row()['state'], 'open')
                    else:
                        self.assertEqual(self.row()['state'], expected)
                    self.native.mode = None
                    self.bridge.run_asks = n.RunAsks(self.bridge)
                    self.bridge.run_asks.refresh()
                    self.assertEqual(self.row()['state'], 'open' if mode == 'lost-open' else expected)
                    self.assertEqual(len(self.writes()), 1)

    def test_ordinary_bindings_staleness_and_replay_reject_without_writes(self):
        for change in (dict(chat=dict(id=456, type='private')), dict(chat=dict(id=123, type='group')),
                       {'from': dict(id=456, is_bot=False)}, {'from': dict(id=123, is_bot=True)},
                       dict(reply_to_message=dict(message_id=999)), dict(message_thread_id=99),
                       dict(forward_origin=dict(type='user')), dict(reply_to_message=dict(message_id=77,
                            **{'from': dict(id=888, is_bot=True)}))):
            with self.subTest(reply=change):
                self.create_decision()
                with patch.object(self.bridge, '_relay', return_value=self.bridge.conversation):
                    self.reply('1 forged', **change)
                self.assertEqual(self.writes(), [])
        for change in (dict(chat=dict(id=456, type='private')), dict(chat=dict(id=123, type='group')),
                       {'from': dict(id=123, is_bot=False)}, dict(message_id=999),
                       dict(forward_origin=dict(type='user')), dict(message_thread_id=99)):
            with self.subTest(callback=change):
                self.create_decision()
                self.press('pick1', **change)
                self.assertEqual(self.writes(), [])
        for field, value in (('question', 'Changed'), ('options', decision()['options'][:1]),
                             ('recommend', 2), ('why', 'Changed'), ('to', 'another'),
                             ('about', []), ('state', 'taken back')):
            with self.subTest(stale=field):
                self.create_decision()
                self.native.ask[field] = value
                self.native.ask.update(by='proposer', words='withdrawn')
                self.press('pick1')
                self.reply('1 forged')
                self.assertEqual(self.writes(), [])
        for role, mapping in (('member', {'id': '123'}), ('operator', {'id': '456'}), ('operator', '123')):
            self.create_decision()
            self.native.user.update(role=role, identities={'tgbridge': mapping})
            self.press('pick1')
            self.reply('1')
            self.assertEqual(self.writes(), [])
        self.create_decision()
        row = self.row()
        query = self.press('pick99')
        for namespace, action in (('runask', 'approve'), ('askcard', 'cancel'), ('askcard', 'approve')):
            query['data'] = f"{namespace}:{row['id']}:{action}"
            self.bridge.inbound(dict(update_id=41, callback_query=query))
        self.assertEqual(self.writes(), [])
        self.press('pick1', uid=42)
        self.native.ask.update(state='open')
        self.bridge.run_asks.refresh()
        self.press('pick2', uid=42)
        self.assertEqual(len(self.writes()), 1)

        for action in ('pick1', 'later', 'decline', 'reply'):
            with self.subTest(missed_reopen=action):
                self.create_decision()
                # A put-aside can become due before the bridge sees either transition.
                self.native.ask.update(turn=1, state='open')
                if action == 'reply':
                    self.reply('1 stale')
                else:
                    stale_query = self.press(action)
                self.assertEqual(self.writes(), [])
                self.assertEqual(self.row()['turn'], 1)
                self.assertEqual(self.edits()[-1]['reply_markup']['inline_keyboard'], [])
                self.bridge.run_asks = n.RunAsks(self.bridge)
                count = self.sent_count
                self.assertTrue(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='decision-1'))['delivered'])
                self.assertEqual(self.sent_count, count)
                if action != 'reply':
                    self.bridge.inbound(dict(update_id=401, callback_query=stale_query))
                    self.assertEqual(self.writes(), [])
                self.press('pick2', uid=402)
                self.assertEqual(self.writes()[0][1]['turn'], 1)
                self.assertEqual(self.native.ask['option'], 2)

        self.bridge.sleep = lambda _: None

        port = self.start_callback_server()

        def poll(*updates):
            telegram = self.bridge.telegram
            def fetched(method, body, timeout=15):
                if method == 'getUpdates':
                    return 200, dict(ok=True, result=list(updates))
                return telegram(method, body, timeout)
            with patch.object(self.bridge, 'telegram', side_effect=fetched):
                self.bridge.poll_once()

        def queued(row, uid=410, value='1 old words'):
            return dict(update_id=uid, message=dict(message_id=uid, text=value,
                chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)},
                reply_to_message=dict(message_id=row['message_id'])))

        def cards():
            return [body for url, body in self.calls if url.endswith('/sendMessage')
                    and body.get('reply_markup', {}).get('inline_keyboard')]

        for initial in (True, False):
            with self.subTest(public_send_key_collision=initial):
                self.reset_native(decision())
                manager = self.bridge.run_asks
                ident = n.digest(dict(binding=manager.binding(), bead='sample-1', ask=7))[:24]
                original = None
                if not initial:
                    self.assertTrue(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='decision-1'))['delivered'])
                    original = self.row()
                key = f'runask:{ident}:send' + ('' if initial else ':1')
                sent = self.bridge.api_send(dict(text='An unrelated diagram; reply 1 or 2.', key=key, format='plain'))
                self.assertIs(sent['delivered'], True)
                saved_send = self.store.saved_delivery(key)
                unrelated = dict(message_id=int(sent['message_id']))
                if initial:
                    receipt = self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='decision-1'))
                    self.assertFalse(receipt['delivered'])
                    self.assertEqual(receipt['reason'], 'idempotency_key_conflict')
                else:
                    manager.client.call('DeferAskExpected', dict(bead='sample-1', ask=7, turn=0, actor='pat'))
                    self.native.ask['state'] = 'open'
                    poll()
                canonical = copy.deepcopy(self.native.ask)
                self.native.calls.clear()
                count = len(cards())
                self.bridge.run_asks = n.RunAsks(self.bridge)
                response = queued(unrelated, uid=460, value='2 the second diagram')
                with patch.object(self.bridge, '_relay', return_value=self.bridge.conversation) as relay:
                    poll(response)
                    self.assertEqual(self.writes(), [])
                    relay.assert_called_once()
                self.assertEqual(self.writes(), [])
                self.assertEqual(self.native.ask, canonical)
                current = self.row()
                self.assertIsNone(current['message_id'])
                self.assertEqual([(g['turn'], g['message_id']) for g in current['generations']],
                                 [] if initial else [(0, original['message_id'])])
                if original:
                    retired = next(body for body in reversed(self.edits()) if body['message_id'] == original['message_id'])
                    self.assertEqual(retired['reply_markup']['inline_keyboard'], [])
                    self.assertNotIn('newer card', retired['text'])
                    self.assertIn('unavailable in Telegram', retired['text'])
                for caller_key in ('decision-1', 'another-decision-key'):
                    receipt = self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key=caller_key))
                    self.assertFalse(receipt['delivered'])
                    self.assertEqual(receipt['reason'], 'idempotency_key_conflict')
                receipt = self.bridge.api_ask_close(dict(message_id=unrelated['message_id'], decision='ignored'))
                self.assertEqual(receipt['reason'], 'ask_not_found')
                deliver = self.bridge._reserve_and_deliver
                conflicts = []
                def delivery(*args, **kwargs):
                    receipt = deliver(*args, **kwargs)
                    if args[0] == key:
                        conflicts.append(receipt['metadata']['reason'])
                    return receipt
                with patch.object(self.bridge, '_reserve_and_deliver', side_effect=delivery):
                    self.assertFalse(self.bridge.api_ask_close(dict(ask_key='decision-1', decision='ignored'))['delivered'])
                self.assertEqual(conflicts, ['idempotency_key_conflict'])
                query = dict(id='collision-461', data=f"askcard:{ident}:pick2:{current['turn']}",
                    **{'from': dict(id=123, is_bot=False)}, message=dict(message_id=unrelated['message_id'],
                    chat=dict(id=123, type='private'), **{'from': dict(id=999, is_bot=True)}))
                with patch.object(self.bridge, '_relay') as relay:
                    poll(dict(update_id=461, callback_query=query))
                    if original:
                        poll(queued(original, uid=462))
                        query = dict(query, id='collision-463', data=f'askcard:{ident}:pick1:0',
                            message=dict(query['message'], message_id=original['message_id']))
                        poll(dict(update_id=463, callback_query=query))
                    relay.assert_not_called()
                self.assertEqual(self.writes(), [])
                self.assertEqual(self.native.ask, canonical)
                self.assertIsNone(self.row()['message_id'])
                self.assertEqual(self.row()['generations'], current['generations'])
                self.assertEqual(len(cards()), count)
                self.assertEqual(self.store.saved_delivery(key), saved_send)

        self.bridge.state_dir = Path(self.tmp.name) / 'inbound-state'
        self.bridge.request_download = self.download_transport
        self.bridge.hook_secret = 'SYNTHETIC-HOOK'
        for state in ('open', 'retired', 'answered'):
            for payload in (dict(photo=[dict(file_id='photo', width=1, height=1)]),
                            dict(photo=[dict(file_id='photo', width=1, height=1)], caption='1 caption'),
                            dict(document=dict(file_id='document'), caption='1 caption'),
                            dict(video=dict(file_id='video'), caption='1 caption'),
                            dict(voice=dict(file_id='voice')), dict(audio=dict(file_id='audio')),
                            dict(sticker=dict(file_id='sticker')), dict(video_note=dict(file_id='video-note')),
                            dict(contact=dict(first_name='Example')), dict(location=dict(latitude=0, longitude=0)),
                            {}, dict(text='1 mixed message', photo=[dict(file_id='photo', width=1, height=1)])):
                with self.subTest(nontext_reply=payload, generation_state=state):
                    self.create_decision()
                    self.download_calls.clear()
                    original = self.row()
                    if state == 'retired':
                        self.native.ask.update(turn=1)
                    elif state == 'answered':
                        self.native.ask.update(turn=1, state='answered', option=2, words='External answer', by='pat')
                    canonical = copy.deepcopy(self.native.ask)
                    response = queued(original, uid=440)
                    response['message'].pop('text')
                    response['message'].update(payload)
                    poll(response)
                    poll(response)
                    self.assertEqual(self.relayed_texts(), [])
                    self.assertEqual(self.download_calls, [])
                    self.assertFalse(any(url.endswith('/getFile') for url, _ in self.calls))
                    self.assertEqual(self.writes(), [])
                    self.assertEqual(self.native.ask, canonical)
                    notices = [body for url, body in self.calls if url.endswith('/sendMessage')
                               and 'text only' in body.get('text', '')]
                    self.assertEqual(len(notices), 1)
                    self.assertFalse(any(url.endswith('/deleteMessage') for url, _ in self.calls))

        self.create_decision()
        self.download_calls.clear()
        response = queued(dict(message_id=987654), uid=441)
        response['message'].pop('text')
        response['message'].update(photo=[dict(file_id='photo', width=1, height=1)], caption='Ordinary attachment')
        poll(response)
        self.assertEqual(len(self.relayed_texts()), 1)
        self.assertIn('Ordinary attachment', self.relayed_texts()[0])
        self.assertEqual(len(self.download_calls), 1)
        self.assertEqual(self.writes(), [])

        class Crash(BaseException):
            pass

        for initial in (True, False):
            for state in ('open', 'reopened', 'reopened-close', 'reopened-create', 'put aside',
                          'answered', 'declined', 'taken back', 'proposal-changed', 'unreadable'):
                with self.subTest(confirmed_send_crash=state, initial=initial):
                    self.reset_native(decision())
                    manager = self.bridge.run_asks
                    turn = 0 if initial else 1
                    if not initial:
                        self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='decision-1'))
                        manager.client.call('DeferAskExpected', dict(bead='sample-1', ask=7, turn=0, actor='pat'))
                        self.native.ask['state'] = 'open'
                    save = manager.save
                    def crash(row):
                        if any(g['turn'] == turn for g in row.get('generations', [])):
                            raise Crash()
                        save(row)
                    with patch.object(manager, 'save', side_effect=crash), self.assertRaises(Crash):
                        if initial:
                            self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='decision-1'))
                        else:
                            poll()
                    # Read the durable row directly, without triggering receipt recovery.
                    with self.store.connect() as db:
                        pending = json.loads(db.execute('SELECT data FROM run_asks').fetchone()[0])
                    self.assertIsNone(pending['message_id'])
                    self.assertFalse(any(g['turn'] == turn for g in pending['generations']))
                    key = f"runask:{pending['id']}:send" + ('' if initial else ':1')
                    receipt = self.store.saved_receipt(key)
                    self.assertTrue(receipt['delivered'])
                    original = dict(message_id=int(receipt['message_id']))
                    count = len(cards())
                    if state.startswith('reopened') or state == 'put aside':
                        manager.client.call('DeferAskExpected', dict(bead='sample-1', ask=7, turn=turn, actor='pat'))
                        if state.startswith('reopened'):
                            self.native.ask['state'] = 'open'
                    elif state == 'answered':
                        manager.client.call('AnswerAskExpected', dict(bead='sample-1', ask=7, turn=turn,
                            actor='pat', option=2, words='External answer', weighed=False))
                    elif state == 'declined':
                        manager.client.call('DeclineAskExpected', dict(bead='sample-1', ask=7, turn=turn,
                            actor='pat', words='External decline'))
                    elif state == 'taken back':
                        self.native.ask.update(turn=turn + 1, state=state, by='proposer', words='Withdrawn')
                    elif state == 'proposal-changed':
                        self.native.ask.update(turn=turn + 1, question='Changed question')
                    elif state == 'unreadable':
                        self.native.mode = 'unreadable'
                    canonical = copy.deepcopy(self.native.ask)
                    self.native.calls.clear()
                    self.bridge.run_asks = n.RunAsks(self.bridge)
                    if state == 'reopened-close':
                        closed = self.bridge.api_ask_close(dict(message_id=original['message_id'], decision='ignored'))
                        self.assertFalse(closed['delivered'])
                        self.assertEqual(closed['reason'], 'run_ask_still_open_in_yard')
                    elif state == 'reopened-create':
                        self.assertTrue(self.bridge.api_ask_card(dict(bead='sample-1', ask=7,
                            key='decision-1'))['delivered'])
                    if state == 'open':
                        poll()
                    else:
                        response = queued(original, uid=450)
                        poll(response)
                        poll(response)
                    self.assertEqual(self.relayed_texts(), [])
                    self.assertEqual(self.writes(), [])
                    self.assertEqual(self.native.ask, canonical)
                    with self.store.connect() as db:
                        current = json.loads(db.execute('SELECT data FROM run_asks').fetchone()[0])
                    recovered = [g for g in current['generations'] if g['turn'] == turn]
                    self.assertEqual(len(recovered), 1)
                    self.assertEqual(recovered[0]['message_id'], original['message_id'])
                    self.assertEqual(len(cards()), count + int(state.startswith('reopened')))
                    if state not in ('open', 'unreadable'):
                        retired = [body for body in self.edits() if body['message_id'] == original['message_id']]
                        self.assertTrue(retired)
                        self.assertEqual(retired[-1]['reply_markup']['inline_keyboard'], [])
                    if state.startswith('reopened'):
                        self.assertIn('Re-presented', retired[-1]['text'])
                        self.assertIn('newer card', self.calls[-1][1]['text'])
                        self.bridge.run_asks = n.RunAsks(self.bridge)
                        poll(queued(original, uid=451))
                        self.assertEqual(self.relayed_texts(), [])
                        self.assertEqual(self.writes(), [])
                        self.assertEqual(self.native.ask, canonical)
                    if state == 'open' or state.startswith('reopened'):
                        answer = queued(current, uid=452, value='2 Current words')
                        poll(answer)
                        poll(answer, queued(current, uid=453))
                        self.assertEqual(self.writes(), [('AnswerAskExpected', dict(bead='sample-1', ask=7,
                            turn=turn + int(state.startswith('reopened')), actor='pat', option=2,
                            words='Current words', weighed=False))])
                        self.assertEqual(len(cards()), count + int(state.startswith('reopened')))

        for options in (True, False):
            for timing in ('same-batch', 'next-batch', 'http-close', 'http-create'):
                with self.subTest(queued_reply=timing, options=options):
                    ask = decision()
                    if not options:
                        ask.update(options=[])
                        ask.pop('recommend')
                        ask.pop('why')
                    self.create_decision(ask, silent=True)
                    original = self.row()
                    response = queued(original, value='1 old words' if options else 'Old free words')
                    self.bridge.run_asks.client.call('DeferAskExpected',
                        dict(bead='sample-1', ask=7, turn=0, actor='pat'))
                    self.native.ask['state'] = 'open'  # Due reopening retains the defer turn.
                    self.native.calls.clear()
                    canonical = copy.deepcopy(self.native.ask)
                    if timing == 'next-batch':
                        poll()
                    elif timing.startswith('http'):
                        path, body = ('/v1/ask-close', dict(message_id=original['message_id'], decision='ignored')) if timing == 'http-close' else (
                            '/v1/ask-card', dict(bead='sample-1', ask=7, key='decision-1', silent=True))
                        status, _ = self.api_call(port, path, body)
                        self.assertEqual(status, 200)
                    with patch.object(self.bridge, '_relay') as relay:
                        poll(response)
                        relay.assert_not_called()
                    self.assertEqual(self.writes(), [])
                    self.assertEqual(self.native.ask, canonical)
                    self.assertEqual(len(cards()), 2)
                    self.assertTrue(all(card['disable_notification'] for card in cards()))
                    current = self.row()
                    self.assertNotEqual(current['message_id'], original['message_id'])
                    self.assertEqual([(g['turn'], g['message_id']) for g in current['generations']],
                                     [(0, original['message_id']), (1, current['message_id'])])
                    self.assertIn('newer card', self.calls[-1][1]['text'])
                    retired = [body for body in self.edits() if body['message_id'] == original['message_id']]
                    self.assertIn('Re-presented', retired[-1]['text'])
                    self.assertEqual(retired[-1]['reply_markup']['inline_keyboard'], [])
                    self.bridge.run_asks = n.RunAsks(self.bridge)
                    poll(queued(original, uid=411, value=response['message']['text']))
                    self.assertEqual(self.writes(), [])
                    # Callback data and the message's generation must agree, then be current.
                    for uid, mid, turn in ((412, original['message_id'], 0),
                                           (413, original['message_id'], 1), (414, current['message_id'], 0)):
                        query = dict(id=str(uid), data=f"askcard:{current['id']}:later:{turn}",
                            **{'from': dict(id=123, is_bot=False)}, message=dict(message_id=mid,
                            chat=dict(id=123, type='private'), **{'from': dict(id=999, is_bot=True)}))
                        poll(dict(update_id=uid, callback_query=query))
                    self.assertEqual(self.writes(), [])
                    self.assertEqual(self.native.ask, canonical)
                    answer = queued(current, uid=415, value='2 current words' if options else 'Current free words')
                    poll(answer)
                    poll(answer, queued(current, uid=416))
                    self.assertEqual(self.writes(), [('AnswerAskExpected', dict(bead='sample-1', ask=7,
                        turn=1, actor='pat', option=2 if options else 0,
                        words='current words' if options else 'Current free words', weighed=False))])
                    self.assertEqual(len(cards()), 2)
                    self.assertTrue(self.bridge.api_ask_close(dict(message_id=original['message_id'], decision='ignored'))['delivered'])

        for restart in (False, True):
            for failure in ('not-sent', 'unconfirmed', 'server-error'):
                with self.subTest(retirement_failure=failure, restart=restart):
                    self.create_decision()
                    original = self.row()
                    self.native.ask.update(turn=1, state='open')
                    transport = self.bridge.request
                    attempts = []
                    def unavailable(url, body, *args, **kwargs):
                        if url.endswith('/editMessageText') and body['message_id'] == original['message_id']:
                            attempts.append(body)
                            if failure == 'not-sent':
                                raise b.RequestNotSent('synthetic unsent retirement')
                            if failure == 'unconfirmed':
                                raise b.RequestUnconfirmed('synthetic uncertain retirement')
                            return 503, dict(ok=False, error_code=503)
                        return transport(url, body, *args, **kwargs)
                    try:
                        self.bridge.request = unavailable
                        poll(queued(original))
                        self.assertTrue(attempts)
                        self.assertEqual(len(cards()), 2)
                        self.assertEqual(self.writes(), [])
                        if restart:
                            self.bridge.run_asks = n.RunAsks(self.bridge)
                        poll(queued(original, uid=411))
                        self.assertEqual(self.writes(), [])
                        self.press('pick1', uid=412, message_id=original['message_id'])
                        self.assertEqual(self.writes(), [])
                        poll(queued(self.row(), uid=413, value='2 after retirement failure'))
                        self.assertEqual(self.writes()[0][1]['turn'], 1)
                        self.assertEqual(self.native.ask['option'], 2)
                    finally:
                        self.bridge.request = transport
                    poll()
                    self.assertEqual(len(cards()), 2)
                    self.assertEqual(self.edits()[-1]['reply_markup']['inline_keyboard'], [])

        for initial in (False, True):
            for failure in ('not-sent', 'unconfirmed', 'server-error'):
                with self.subTest(generation_send=failure, initial=initial):
                    self.create_decision()
                    original = self.row()
                    if initial:
                        self.reset_native(decision())
                    else:
                        self.native.ask.update(turn=1, state='open')
                    transport = self.bridge.request
                    attempts = []
                    def unavailable(url, body, *args, **kwargs):
                        if url.endswith('/sendMessage') and body.get('reply_markup'):
                            attempts.append(body)
                            if failure == 'not-sent':
                                raise b.RequestNotSent('synthetic unsent generation')
                            if failure == 'unconfirmed':
                                raise b.RequestUnconfirmed('synthetic uncertain generation')
                            return 503, dict(ok=False, error_code=503)
                        return transport(url, body, *args, **kwargs)
                    try:
                        self.bridge.request = unavailable
                        if initial:
                            self.assertFalse(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='decision-1'))['delivered'])
                        else:
                            poll()
                        self.assertTrue(attempts)
                        self.assertFalse(any(g['turn'] == (0 if initial else 1) for g in self.row()['generations']))
                        if not initial:
                            retired = next(body for body in reversed(self.edits()) if body['message_id'] == original['message_id'])
                            self.assertEqual(retired['reply_markup']['inline_keyboard'], [])
                            self.assertNotIn('newer card', retired['text'])
                            self.assertIn('unavailable in Telegram', retired['text'])
                        self.bridge.run_asks = n.RunAsks(self.bridge)
                        count = len(attempts)
                    finally:
                        self.bridge.request = transport
                    poll()
                    poll()
                    if failure == 'not-sent':
                        self.assertEqual(len(cards()), 1 if initial else 2)
                        self.assertEqual(self.row()['generations'][-1]['turn'], 0 if initial else 1)
                        self.reply('2 confirmed', uid=420)
                        self.assertEqual(len(self.writes()), 1)
                    else:
                        self.assertEqual(len(cards()), 0 if initial else 1)
                        self.assertEqual(len(attempts), count)
                        unknown = queued(dict(message_id=987654), uid=421)
                        with patch.object(self.bridge, '_relay', return_value=self.bridge.conversation) as relay:
                            poll(unknown)
                            relay.assert_called_once()
                        self.assertEqual(self.writes(), [])
                        if not initial:
                            poll(queued(original, uid=422))
                        self.assertEqual(self.writes(), [])

        self.create_decision()
        original = self.row()
        for missing in ('generations', 'initial_turn'):
            with self.subTest(unconfirmed_row=missing):
                row = copy.deepcopy(original)
                row.pop(missing)
                self.bridge.run_asks.save(row)
                self.bridge.run_asks = n.RunAsks(self.bridge)
                with patch.object(self.bridge, '_relay') as relay:
                    poll(queued(row, uid=430))
                    relay.assert_not_called()
                self.assertEqual(self.writes(), [])
                receipt = self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='decision-1'))
                self.assertFalse(receipt['delivered'])
                self.assertIn('unconfirmed', receipt['reason'])
        self.bridge.run_asks.save(original)
        duplicate = copy.deepcopy(original)
        duplicate['id'] = 'f' * 24
        self.bridge.run_asks.save(duplicate)
        with patch.object(self.bridge, '_relay') as relay:
            poll(queued(original, uid=431))
            relay.assert_not_called()
        self.assertEqual(self.writes(), [])
        self.assertIn('ambiguous', self.calls[-1][1]['text'])
        self.assertIn('ambiguous', self.bridge.api_ask_close(dict(message_id=original['message_id'], decision='ignored'))['reason'])

    def test_ordinary_same_operator_two_homes_and_read_replacement(self):
        self.assertTrue(self.create_decision()['delivered'])
        original = self.row()
        with tempfile.TemporaryDirectory(prefix='other-') as other:
            home = Path(other) / 'native'
            home.mkdir(mode=0o700)
            server = NativeSocket(home)
            try:
                server.ask = decision()
                self.config.update(yardr_home=str(home), yard='arbitrary-instance')
                self.bridge.run_asks = n.RunAsks(self.bridge)
                self.assertFalse(self.bridge.api_ask_close(dict(ask_key='decision-1', decision='forged'))['delivered'])
                self.assertTrue(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='second-yard'))['delivered'])
                query = dict(id='other-yard', data=f"askcard:{original['id']}:pick1",
                             **{'from': dict(id=123, is_bot=False)}, message=dict(message_id=original['message_id'],
                             chat=dict(id=123, type='private'), **{'from': dict(id=999, is_bot=True)}))
                self.bridge.inbound(dict(update_id=60, callback_query=query))
                self.assertEqual(self.writes(), [])
                self.assertFalse(any(m in ('AnswerAsk', 'DeferAsk', 'DeclineAsk', 'AnswerAskExpected', 'DeferAskExpected', 'DeclineAskExpected') for m, _, _ in server.calls))
            finally:
                server.close()
        self.config.update(yardr_home=str(self.home), yard='test-city')
        self.bridge.run_asks = n.RunAsks(self.bridge)
        self.native.read_statuses = [503, 503]
        self.press('pick1', uid=61)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.bridge.run_asks.get(original['id'])['state'], 'answered')
        self.create_decision()
        self.native.read_statuses = [503, 503, 503]
        self.press('pick1', uid=62)
        self.assertEqual(self.writes(), [])
        self.create_decision()
        previous = self.home.with_name('previous-yard')
        self.home.rename(previous)
        self.home.mkdir(mode=0o700)
        replaced = NativeSocket(self.home)
        try:
            replaced.ask = decision()
            self.press('pick1', uid=63)
            self.assertEqual(self.row()['state'], 'stale')
            self.assertFalse(any(m in ('AnswerAsk', 'AnswerAskExpected') for m, _, _ in replaced.calls))
        finally:
            replaced.close()

    def test_routine_render_approve_once_restart_and_canonical_close(self):
        self.native.ask.pop('turn')  # run cards still accept the older core shape
        self.assertTrue(self.create()['delivered'])
        sent = self.calls[-1][1]['text']
        for field in ['Change the test?', 'Proposed by proposer', 'First &lt;context&gt;', 'Second &amp; context',
                      'current: make test', 'proposed: sh scripts/test.sh', 'yardr depot test web',
                      'Set its test', 'an operator can take it back', 'test-city', 'as pat']:
            self.assertIn(field, sent)
        close = self.bridge.api_ask_close({'ask_key': 'native-1', 'decision': 'approved by anyone'})
        self.assertEqual(close['reason'], 'run_ask_still_open_in_yard')
        self.press()
        self.assertEqual(self.writes(), [('ApproveAsk', dict(bead='sample-1', ask=7, actor='pat', weighed=False, values={}))])
        self.assertEqual(self.row()['state'], 'approved')
        self.assertIn('stored operation applied', self.edits()[-1]['text'])
        self.bridge.run_asks = n.RunAsks(self.bridge)
        self.press(uid=41)
        self.press(uid=41)
        self.assertEqual(len(self.writes()), 1)
        self.assertTrue(self.create()['delivered'])
        self.assertEqual(self.sent_count, 1)
        self.assertTrue(self.bridge.api_ask_close({'message_id': 77, 'decision': 'forged'})['delivered'])
        self.assertNotIn('forged', self.edits()[-1]['text'])
        self.assertTrue(all(source == 'tgbridge' for _, _, source in self.native.calls))

        for key, native_first in (('ordinary-first', False), ('native-1', True)):
            with self.subTest(native_first=native_first):
                ask_id = self.bridge._ask_id_for_key(key)
                ordinary = dict(self.publish(key=key, text='Pick one?'), keyboard=[[dict(
                    text='Choice A', callback_data=f'ask:{ask_id}:a')]])
                count = self.sent_count
                if native_first:
                    refused = self.bridge.publish(ordinary)
                    self.assertIsNone(self.store.get_ask(ask_id))
                else:
                    first = self.bridge.publish(ordinary)
                    self.assertTrue(first['delivered'])
                    count += 1
                    refused = self.bridge.api_run_ask(dict(bead='sample-1', ask=7, key=key))
                    self.assertEqual(self.bridge.publish(ordinary), first)
                    self.assertTrue(self.bridge.api_ask_close(dict(ask_key=key, decision='Ordinary choice'))['delivered'])
                    self.assertEqual(self.store.get_ask(ask_id)['answer'], 'Ordinary choice')
                    self.assertEqual(self.edits()[-1]['message_id'], int(first['message_id']))
                self.assertFalse(refused['delivered'])
                self.assertEqual(refused.get('reason', refused.get('metadata', {}).get('reason')), 'idempotency_key_conflict')
                self.assertEqual(self.sent_count, count)
                with self.store.connect() as db:
                    self.assertEqual(bool(db.execute('SELECT 1 FROM run_ask_keys WHERE key=?', (key,)).fetchone()),
                                     native_first)
        self.assertEqual(len(self.writes()), 1)

        # Ledgers from before collision prevention keep their ordinary ask-key selectors.
        key = 'legacy-shared'
        ask_id = self.bridge._ask_id_for_key(key)
        ordinary = dict(self.publish(key=key, text='Legacy choice?'), keyboard=[[dict(
            text='Choice A', callback_data=f'ask:{ask_id}:a')]])
        first = self.bridge.publish(ordinary)
        self.assertTrue(first['delivered'])
        with self.store.connect() as db:
            request = db.execute('SELECT request FROM run_ask_keys WHERE key=?', ('native-1',)).fetchone()[0]
            db.execute('INSERT INTO run_ask_keys VALUES (?,?,?)', (key, self.row()['id'], request))
        self.bridge.run_asks = n.RunAsks(self.bridge)
        self.assertEqual(self.bridge.publish(ordinary), first)
        self.assertTrue(self.bridge.api_ask_close(dict(ask_key=key, decision='Legacy choice'))['delivered'])
        self.assertEqual(self.store.get_ask(ask_id)['answer'], 'Legacy choice')
        self.assertEqual(self.edits()[-1]['message_id'], int(first['message_id']))

    def test_decline_and_take_back_use_canonical_words_without_execution(self):
        for factory in (proposal, skill_proposal):
            with self.subTest(payload=factory.__name__):
                self.reset_native(factory())
                for state in ('decline', 'taken back'):
                    with self.subTest(state=state):
                        if state == 'decline':
                            self.create()
                            self.press('decline')
                            self.assertEqual(self.writes()[0][0], 'DeclineAsk')
                            self.assertEqual(self.row()['state'], 'declined')
                        else:
                            self.native.ask.update(state='taken back', words='new canonical reason', by='proposer')
                            self.bridge.run_asks.refresh()
                            self.assertIn('Taken back by proposer: new canonical reason', self.edits()[-1]['text'])
                self.assertFalse(any(method == 'ApproveAsk' for method, _ in self.writes()))

    def test_weight_requires_bound_reply_cancel_expiry_mismatch_and_one_use(self):
        for factory in (proposal, skill_proposal):
            with self.subTest(payload=factory.__name__):
                self.reset_native(factory())
                if factory is proposal:
                    self.native.ask['weight'] = [{'class': 'code', 'line': 'brings a program the yard will run: web'}]
                self.sent_count = -76  # first synthetic card has message ID 1, distinct from boolean True
                first = self.create()
                self.press()
                self.assertEqual(self.writes(), [])
                self.assertIn("type the ask's number, 7", b.html_visible_text(self.edits()[-1]['text'])[0])
                pending = self.row()['pending']
                for payload in (dict(text=None, photo=[dict(file_id='photo', width=1, height=1)], caption='7'),
                                dict(text='7', document=dict(file_id='document')), dict(text=None, sticker=dict(file_id='sticker'))):
                    with self.subTest(nontext_confirmation=payload), \
                         patch.object(self.bridge, '_relay', return_value=self.bridge.conversation), \
                         patch.object(self.bridge, '_fetch_inbound_attachment',
                                      return_value=(Path(self.tmp.name) / 'reply', 1, None)):
                        self.reply(uid=80, **payload)
                    self.assertEqual(self.row()['pending'], pending)
                    self.assertEqual(self.writes(), [])
                count = len(self.calls)
                for restart in (False, True):
                    with self.subTest(restart=restart):
                        if restart:
                            self.bridge.run_asks = n.RunAsks(self.bridge)
                        self.assertEqual(self.create(), first)
                        self.assertEqual(self.row()['pending'], pending)
                        self.assertEqual(len(self.calls), count)
                        self.assertEqual(self.writes(), [])
                for changes in ({'reply_to_message': {'message_id': 900}}, {'reply_to_message': {}},
                                {'reply_to_message': {'message_id': True}},
                                {'forward_origin': {'type': 'hidden_user'}}, {'from': {'id': 124, 'is_bot': False}},
                                {'from': {'id': 123, 'is_bot': True}}, {'is_topic_message': True, 'message_thread_id': 12}):
                    # Non-confirmations retain their ordinary routing; only the bound reply is consumed.
                    with patch.object(self.bridge, '_relay'):
                        self.reply(**changes)
                    self.assertEqual(self.writes(), [])
                self.reply(value='wrong', uid=51)
                self.assertIsNone(self.row()['pending'])
                self.assertEqual(self.writes(), [])
                self.press(uid=42)
                self.press('cancel', uid=43)
                with patch.object(self.bridge, '_relay'):
                    self.reply(uid=52)
                self.assertEqual(self.writes(), [])
                self.press(uid=44)
                row = self.row()
                row['pending']['expires'] = 0
                self.bridge.run_asks.save(row)
                self.reply(uid=53)
                self.assertEqual(self.writes(), [])
                self.press(uid=45)
                self.bridge.run_asks = n.RunAsks(self.bridge)
                self.native.mode = 'lost-open'
                self.reply(uid=54)
                self.assertEqual(self.writes()[-1][1]['weighed'], True)
                self.assertEqual(self.row()['state'], 'open')
                self.assertEqual([button['callback_data'] for button in self.edits()[-1]['reply_markup']['inline_keyboard'][0]],
                                 [f"runask:{self.row()['id']}:approve", f"runask:{self.row()['id']}:decline"])
                with patch.object(self.bridge, '_relay'):
                    self.reply(uid=54)
                self.assertEqual(len(self.writes()), 1)
                self.native.mode = None
                self.press(uid=46)
                self.assertEqual(len(self.writes()), 1)  # retry still needs a fresh bound confirmation
                self.reply(uid=55)
                self.assertEqual(self.row()['state'], 'approved')
                self.assertEqual(len(self.writes()), 2)
                self.assertTrue(self.writes()[-1][1]['weighed'])

    def test_callback_and_proposal_binding_refusals(self):
        for factory in (proposal, skill_proposal):
            with self.subTest(payload=factory.__name__):
                self.reset_native(factory())
                self.create()
                for changes in ({'message_id': 200}, {'chat': {'id': 456, 'type': 'private'}},
                                {'from': {'id': 998, 'is_bot': True}}, {'from': {'id': 999, 'is_bot': False}},
                                {'is_topic_message': True, 'message_thread_id': 12}, {'forward_date': 123}):
                    self.press(**changes)
                self.assertEqual(self.writes(), [])
                query = self.press(message_id=200, uid=60)
                query['message']['message_id'] = self.row()['message_id']
                for sender in ({'id': 456, 'is_bot': False}, {'id': 123, 'is_bot': True}):
                    query['from'] = sender
                    self.bridge.inbound(dict(update_id=61, callback_query=query))
                self.assertEqual(self.writes(), [])
                for key, value in [('to', 'someone'), ('context', ['changed']), ('question', 'changed question'),
                                   ('run', dict(self.native.ask['run'], does=['changed']))]:
                    saved = copy.deepcopy(self.native.ask)
                    self.native.ask[key] = value
                    self.press(uid=41)
                    self.assertEqual(self.writes(), [])
                    self.native.ask = saved
                    row = self.row()
                    row['state'] = 'open'
                    self.bridge.run_asks.save(row)
                self.native.user['identities']['tgbridge'] = {'id': '456'}
                self.press(uid=42)
                self.assertEqual(self.writes(), [])
                self.native.user['identities']['tgbridge'] = '123'
                self.press(uid=43)
                self.assertEqual(self.writes(), [])
                self.native.user.update(role='member')
                self.press(uid=44)
                self.assertEqual(self.writes(), [])

    def test_same_operator_two_homes_and_conversations_cannot_cross(self):
        for factory in (proposal, skill_proposal):
            with self.subTest(payload=factory.__name__):
                self.reset_native(factory())
                self.create()
                other_home = Path(self.tmp.name) / ('other-' + factory.__name__)
                other_home.mkdir(mode=0o700)
                other = NativeSocket(other_home)
                other.ask = factory()
                self.addCleanup(other.close)
                original = self.bridge.run_asks
                self.config.update(yardr_home=str(other_home), yard='other-yard')
                self.bridge.run_asks = n.RunAsks(self.bridge)
                self.press()
                self.assertEqual(self.writes(), [])
                self.assertFalse(any(method == 'ApproveAsk' for method, _, _ in other.calls))
                result = self.bridge.api_ask_close(dict(ask_key='native-1', decision='approved'))
                self.assertEqual(result['reason'], 'run_ask_wrong_yard_or_pairing')
                self.assertTrue(self.bridge.api_run_ask(dict(bead='sample-1', ask=7, key='other-home'))['delivered'])
                other_row = self.bridge.run_asks.rows()[-1]
                self.assertNotEqual(other_row['id'], self.row()['id'])
                self.assertNotEqual(other_row['proposal'], self.row()['proposal'])
                if factory is skill_proposal:
                    self.assertNotEqual(other_row['bundle']['parts'][0]['key'], self.row()['bundle']['parts'][0]['key'])
                self.config.update(yardr_home=str(self.home), yard='test-city')
                self.bridge.run_asks = original
                self.bridge.topic_settings = (dict(name='ops', agent='yardmaster'),)
                self.store.record_topic('ops', 12, 'created')
                self.assertEqual(self.create(topic='ops')['reason'], 'idempotency_key_conflict')
                duplicate = dict(self.row(), id='f' * 24)
                self.bridge.run_asks.save(duplicate)
                close = self.bridge.close_ask('forged', message_id=self.row()['message_id'])
                self.assertEqual(close['metadata']['reason'], 'run_ask_message_binding_ambiguous')

    def test_unsupported_and_malformed_payloads_never_send(self):
        base = proposal()
        cases = [{field: {}} for field in ('secret', 'setup', 'identity', 'depot_push')]
        cases += [{'run': dict(base['run'], **extra)} for extra in
                  ({'ask': ['name']}, {'carried': [{}]}, {'pack': {}}, {'argv': ['crew', 'add', 'helper']},
                   {'does': 'not an array'}, {'future_payload': 'unrecognized'},
                   {'argv': ['source', 'add', 'declaration.yaml'], 'carried': []},
                   {'argv': ['pack', 'apply', 'example'], 'pack': None})]
        cases += [{'context': 'not an array'}, {'weight': 'not an array'}, {'future_payload': {}},
                  {'options': [{'label': 'unknown choice'}]},
                  {'weight': [{'class': 'future', 'line': 'unknown'}]}]
        for change in cases:
            self.native.ask = dict(base, **change)
            result = self.create()
            self.assertFalse(result['delivered'], change)
            self.assertEqual(self.sent_count, 0)
        self.native.ask = base
        self.native.user['role'] = 'member'
        self.assertFalse(self.create()['delivered'])
        self.assertEqual(self.sent_count, 0)

        # Mutate the actual core fixture at every authoritative boundary.
        mutations = [
            (('weight',), []), (('weight', 0, 'class'), 'trust'),
            (('weight', 0, 'line'), 'WEIGHTY · invented'),
            (('run', 'argv'), ['skill', 'add', 'relative']),
            (('run', 'argv'), ['skill', 'add', '/synthetic/../proposal']),
            (('run', 'command'), 'yardr skill add /elsewhere'),
            (('run', 'description'), 'invented metadata'),
            (('run', 'pack'), {}), (('run', 'ask'), ['value']),
            (('run', 'carried'), []), (('run', 'carried', 0), None),
            (('run', 'carried', 0, 'name'), '../other'),
            (('run', 'carried', 0, 'removed'), []),
            (('run', 'carried', 0, 'files'), []),
            (('run', 'carried', 0, 'files', 0, 'program'), 'other.md'),
            (('run', 'carried', 0, 'files', 0, 'text'), True),
            (('run', 'carried', 0, 'files', 0, 'sha256'), '0' * 64),
            (('run', 'carried', 0, 'text'), ''),
            (('run', 'carried', 0, 'old'), []),
            (('run', 'carried', 0, 'old', 0, 'old_sha256'), '0' * 64),
            (('run', 'carried', 0, 'old', 0, 'new_sha256'), '0' * 64),
            (('run', 'carried', 0, 'old', 0, 'diff'), ''),
            (('run', 'carried', 0, 'inventory'), []),
            (('run', 'carried', 0, 'inventory', 0, 'size'), True),
            (('run', 'carried', 0, 'inventory', 0, 'mode'), 'symlink'),
            (('run', 'carried', 0, 'inventory', 0, 'sha256'), '0' * 64),
            (('run', 'does', 0), 'skill snapshot: "wrong description"'),
            (('run', 'does', 3), 'standing metadata name: snapshot; description: "wrong"'),
            (('run', 'does', 4), 'current holders and affected readers: '),
            (('run', 'does', 8), '"partial bytes"'),
            (('run', 'back'), 'automatic undo'),
        ]
        for path, value in mutations:
            with self.subTest(path=path):
                self.reset_native(skill_proposal())
                target = self.native.ask
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.assertFalse(self.create()['delivered'])
                self.assertEqual(self.sent_count, 0)
        for field in ('files', 'old', 'inventory'):
            self.reset_native(skill_proposal())
            self.native.ask['run']['carried'][0][field] *= 2
            self.assertFalse(self.create()['delivered'])
            self.assertEqual(self.sent_count, 0)
        for missing in (('inventory',), ('old_sha256',), ('inventory', 'old_sha256')):
            with self.subTest(refresh_missing=missing):
                self.reset_native(skill_proposal())
                carried = self.native.ask['run']['carried'][0]
                for field in missing:
                    del (carried if field == 'inventory' else carried['old'][0])[field]
                self.assertFalse(self.create()['delivered'])
                self.assertEqual(self.sent_count, 0)
                self.assertEqual(self.writes(), [])

    def test_skill_bundle_complete_escaped_bounded_and_resume_every_delivery(self):
        self.bridge.sleep = lambda _: None
        ask = skill_proposal('Synthetic long line <&>🙂\t\r' * 180 + '\n')
        self.reset_native(ask)
        self.bridge.topic_settings = (dict(name='ops', agent='yardmaster'),)
        self.store.record_topic('ops', 12, 'created')
        self.assertTrue(self.create(topic='ops')['delivered'])
        row = self.row()
        parts = row['bundle']['parts']
        sent = [body for url, body in self.calls if url.endswith('/sendMessage')]
        self.assertGreater(len(parts), 3)
        self.assertEqual(len(sent), len(parts) + 1)
        bodies = []
        for index, (part, body) in enumerate(zip(parts, sent)):
            visible, error = b.html_visible_text(body['text'])
            self.assertFalse(error)
            self.assertEqual(visible, part['plain'])
            self.assertLessEqual(n.units(visible), n.CAP)
            self.assertEqual(hashlib.sha256(visible.encode()).hexdigest(), part['sha256'])
            self.assertEqual(body['message_thread_id'], 12)
            self.assertNotIn('reply_markup', body)
            self.assertEqual(body.get('reply_parameters'), None if index == 0 else {'message_id': parts[index-1]['message_id']})
            self.assertIn(row['proposal'], part['plain'])
            bodies.append(visible.split('\n', 3)[3])
        review = ''.join(bodies)
        self.assertEqual(hashlib.sha256(review.encode()).hexdigest(), row['bundle']['sha256'])
        self.assertTrue(review.endswith('\n'.join(ask['run']['does'])))
        self.assertIn('"WEIGHTY · not authority\\r\\n"', review)
        self.assertEqual(sent[-1]['reply_parameters']['message_id'], parts[-1]['message_id'])
        self.assertIn(row['proposal'], sent[-1]['text'])
        self.assertEqual(sent[-1]['reply_markup']['inline_keyboard'][0][0]['callback_data'], f"runask:{row['id']}:approve")
        expected = [body['text'] for body in sent]
        original = self.bridge.request
        for failure in ('transient', 'ambiguous', 'crash-after-receipt'):
            for index, target in enumerate(expected):
                with self.subTest(failure=failure, part=index):
                    self.reset_native(copy.deepcopy(ask))
                    blocked = [True]
                    def telegram(url, body, *args, **kwargs):
                        if url.endswith('/sendMessage') and body['text'] == target and blocked[0]:
                            self.calls.append((url, body))
                            error = b.RequestNotSent if failure == 'transient' else b.RequestUnconfirmed
                            raise error('synthetic interruption')
                        return original(url, body, *args, **kwargs)
                    self.bridge.request = telegram
                    if failure == 'crash-after-receipt':
                        # The ledger confirms a send, but the native row save is interrupted.
                        self.bridge.request = original
                        save = self.bridge.run_asks.save
                        def crash(row):
                            delivered = sum(p['message_id'] is not None for p in row['bundle']['parts'])
                            if delivered == index + 1 or (index == len(parts) and row['message_id'] is not None):
                                raise RuntimeError('synthetic crash after ledger receipt')
                            save(row)
                        with patch.object(self.bridge.run_asks, 'save', side_effect=crash):
                            with self.assertRaises(RuntimeError):
                                self.create(topic='ops')
                    else:
                        result = self.create(topic='ops')
                        self.assertFalse(result['delivered'])
                        self.assertIsNone(self.row()['message_id'])
                        self.assertEqual(sum(p['message_id'] is not None for p in self.row()['bundle']['parts']), index)
                    confirmed = [p['message_id'] for p in self.row()['bundle']['parts'][:index]]
                    before = len([1 for url, _ in self.calls if url.endswith('/sendMessage')])
                    blocked[0] = False
                    self.bridge.run_asks = n.RunAsks(self.bridge)
                    retry = self.create(topic='ops')
                    if failure == 'ambiguous':
                        self.assertFalse(retry['delivered'])
                        self.assertEqual(len([1 for url, _ in self.calls if url.endswith('/sendMessage')]), before)
                        # Existing ledger reconciliation can confirm an observed message; same key resumes.
                        key = self.row()['bundle']['parts'][index]['key'] if index < len(parts) else f"runask:{self.row()['id']}:send"
                        self.store.finish(key, self.bridge.receipt(True, message_id='700'))
                        self.assertTrue(self.create(topic='ops')['delivered'])
                    else:
                        self.assertTrue(retry['delivered'])
                    self.assertEqual([p['message_id'] for p in self.row()['bundle']['parts'][:index]], confirmed)
                    sends = [body for url, body in self.calls if url.endswith('/sendMessage')]
                    complete = len(sends)
                    self.assertTrue(self.bridge.api_run_ask(dict(bead='sample-1', ask=7, key='new-repost-key', topic='ops'))['delivered'])
                    self.assertEqual(len([1 for url, _ in self.calls if url.endswith('/sendMessage')]), complete)
                    self.assertEqual(self.writes(), [])
        self.bridge.request = original

    def test_skill_bundle_drift_rechecks_during_transport_retry_and_repost(self):
        original = self.bridge.request
        self.bridge.sleep = lambda _: None
        for drift in ('proposal', 'operator', 'state'):
            with self.subTest(drift=drift):
                self.reset_native(skill_proposal())
                attempts = []
                def telegram(url, body, *args, **kwargs):
                    if url.endswith('/sendMessage'):
                        attempts.append(body)
                        if drift == 'proposal':
                            self.native.ask['context'].append('new proposal')
                        elif drift == 'operator':
                            self.native.user['role'] = 'member'
                        else:
                            self.native.ask.update(state='taken back', words='withdrawn', by='proposer')
                        raise b.RequestNotSent('not sent before drift')
                    return original(url, body, *args, **kwargs)
                self.bridge.request = telegram
                self.assertFalse(self.create()['delivered'])
                self.assertEqual(len(attempts), 1)
                self.bridge.run_asks = n.RunAsks(self.bridge)
                self.assertFalse(self.bridge.api_run_ask(dict(bead='sample-1', ask=7, key='new-retry'))['delivered'])
                self.assertEqual(len(attempts), 1)
                self.assertIsNone(self.row()['message_id'])
                self.assertEqual(self.writes(), [])
                key = self.row()['bundle']['parts'][0]['key']
                self.assertTrue(b.resendable(self.store.saved_receipt(key)))
                if drift == 'operator':
                    self.native.user['role'] = 'operator'
                    self.bridge.request = original
                    self.assertTrue(self.create()['delivered'])
        self.bridge.request = original

    def test_skill_bundle_maximum_file_and_oversized_envelope_refuse_whole(self):
        base_size = len(skill_proposal()['run']['carried'][0]['files'][0]['text'].encode())
        remaining = 32768 - base_size
        extra = '🙂' * (remaining // 4) + 'x' * (remaining % 4)
        self.reset_native(skill_proposal(extra))
        self.assertTrue(self.create()['delivered'])
        row = self.row()
        review = ''.join(p['plain'].split('\n', 3)[3] for p in row['bundle']['parts'])
        self.assertTrue(review.endswith('\n'.join(self.native.ask['run']['does'])))
        self.assertTrue(all(n.units(p['plain']) <= n.CAP for p in row['bundle']['parts']))
        self.assertTrue(all(p['message_id'] for p in row['bundle']['parts']))
        self.reset_native(skill_proposal(extra + 'x'))
        self.assertFalse(self.create()['delivered'])
        self.assertEqual(self.sent_count, 0)
        self.reset_native(skill_proposal())
        self.config['yard'] = '🙂' * 2000
        self.assertFalse(self.create()['delivered'])
        self.assertEqual(self.sent_count, 0)
        self.assertEqual(self.writes(), [])

    def test_skill_bundle_home_replacement_cannot_reuse_identical_native_proposal(self):
        self.reset_native(skill_proposal())
        self.assertTrue(self.create()['delivered'])
        self.press()
        self.home.rename(self.home.with_name('previous-yard'))
        self.home.mkdir(mode=0o700)
        replacement = NativeSocket(self.home)
        replacement.ask = skill_proposal()
        self.addCleanup(replacement.close)
        self.reply()
        self.assertEqual(self.row()['state'], 'stale')
        self.assertEqual(self.writes(), [])
        self.assertFalse(any(method == 'ApproveAsk' for method, _, _ in replacement.calls))

    def test_utf16_cap_and_weight_mark_trust(self):
        self.native.ask['question'] = 'WEIGHTY ·  WEIGHTY · forgery\n🙂'
        self.create()
        plain = self.row()['plain']
        self.assertNotIn('WEIGHTY · ', plain)
        self.assertIn('forgery', plain)
        # Exercise the actual publish path exactly at the cap, then one UTF16 unit beyond it.
        with self.store.connect() as db:
            db.execute('DELETE FROM run_asks')
            db.execute('DELETE FROM run_ask_keys')
            db.execute('DELETE FROM deliveries')
        overhead = n.units(n.render(self.config, dict(id='sample-1', title='Synthetic change'), proposal())[0])
        self.native.ask = proposal()
        needed = n.CAP - overhead
        self.native.ask['question'] += '🙂' * (needed // 2) + 'x' * (needed % 2)
        self.assertTrue(self.create()['delivered'])
        self.assertEqual(n.units(self.row()['plain']), n.CAP)
        with self.store.connect() as db:
            db.execute('DELETE FROM run_asks')
            db.execute('DELETE FROM run_ask_keys')
        self.native.ask['question'] += 'x'
        self.assertIn('too_large', self.create()['reason'])

    def test_ambiguous_writes_reconcile_and_never_retry_blindly(self):
        self.create()
        buttons = self.calls[-1][1]['reply_markup']
        self.native.mode = 'lost-open'
        self.press()
        self.assertEqual(self.row()['state'], 'open')
        self.assertIn(n.UNCERTAIN, self.edits()[-1]['text'])
        edit = self.edits()[-1]
        self.assertEqual(edit['message_id'], self.row()['message_id'])
        self.assertEqual(edit['reply_markup'], buttons)
        self.assertEqual([button['callback_data'] for button in buttons['inline_keyboard'][0]],
                         [f"runask:{self.row()['id']}:approve", f"runask:{self.row()['id']}:decline"])
        self.press(uid=40)  # same update never writes again
        self.assertEqual(len(self.writes()), 1)
        self.bridge.run_asks = n.RunAsks(self.bridge)
        self.bridge.run_asks.refresh()
        self.assertTrue(self.create()['delivered'])
        self.assertEqual(self.sent_count, 1)
        self.assertEqual(self.edits()[-1]['reply_markup'], buttons)
        self.native.mode = 'lost-unreadable'
        self.bridge.inbound(dict(update_id=41, callback_query=dict(
            id='recovered-card', data=buttons['inline_keyboard'][0][0]['callback_data'],
            message=dict(message_id=edit['message_id'], chat=dict(id=123, type='private'),
                         **{'from': dict(id=999, is_bot=True)}),
            **{'from': dict(id=123, is_bot=False)})))
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual(self.row()['state'], 'open')
        self.assertIn(n.UNCERTAIN, self.edits()[-1]['text'])
        self.native.mode = None
        self.bridge.run_asks = n.RunAsks(self.bridge)
        self.press(uid=42)
        self.assertEqual(self.row()['state'], 'approved')
        self.assertEqual(len(self.writes()), 2)
        self.assertEqual(self.edits()[-1]['reply_markup'], {'inline_keyboard': []})

        for mode in ('lost-commit', 'invalid-commit', 'wire-error-commit', 'lost-open', 'lost-unreadable', 'refused'):
            with self.subTest(skill_write=mode):
                self.reset_native(skill_proposal())
                self.create()
                self.native.mode = mode
                self.press()
                self.assertEqual(self.writes(), [])
                self.reply()
                self.assertEqual(len(self.writes()), 1)
                self.assertTrue(self.writes()[0][1]['weighed'])
                if mode in ('lost-commit', 'invalid-commit', 'wire-error-commit'):
                    self.assertEqual(self.row()['state'], 'approved')
                else:
                    self.assertEqual(self.row()['state'], 'open')
                self.bridge.run_asks = n.RunAsks(self.bridge)
                self.bridge.run_asks.refresh()
                self.assertEqual(len(self.writes()), 1)
                if mode == 'lost-unreadable':
                    self.assertIn(n.UNCERTAIN, self.edits()[-1]['text'])
                    self.native.mode = None
                    self.bridge.run_asks.refresh()
                    self.assertEqual(self.row()['state'], 'approved')


    def test_pending_settlement_long_results_and_outcome_edits(self):
        for factory in (proposal, skill_proposal):
            with self.subTest(payload=factory.__name__):
                self.reset_native(factory())
                self.create()
                initial_sends = self.sent_count
                self.native.mode = 'carrying'
                self.press()
                if factory is skill_proposal:
                    self.reply()
                self.assertEqual(self.row()['state'], 'carrying')
                self.assertIn('approved, to be carried out', self.edits()[-1]['text'])
                self.native.ask['ran']['pending_reason'] = 'waiting for report'
                self.bridge.run_asks.refresh()
                self.assertIn('waiting for report', self.edits()[-1]['text'])
                result = 'canonical <failure>\r\n' + '🙂' * 4200
                self.native.ask['ran'].update(result=result, failed=True, pending_reason='')
                self.bridge.run_asks = n.RunAsks(self.bridge)
                self.bridge.run_asks.refresh()
                self.assertEqual(self.row()['state'], 'failed')
                sends = [body['text'] for url, body in self.calls if url.endswith('/sendMessage')][initial_sends:]
                footer = ''.join(sends)
                self.assertTrue(footer.endswith(result))
                self.assertTrue(all(n.units(part) <= 4096 for part in sends))
                count = self.sent_count
                self.bridge.run_asks.refresh()
                self.assertEqual(self.sent_count, count)
                self.native.ask['ran'].update(result='edited canonical outcome', failed=False)
                self.bridge.run_asks.refresh()
                self.assertIn('edited canonical outcome', self.edits()[-1]['text'])
                self.native.ask['ran']['result'] = ''
                self.bridge.run_asks.refresh()
                self.assertIn('approval recorded', self.edits()[-1]['text'])
                self.assertNotIn('approved, ran', self.edits()[-1]['text'])
                self.assertEqual(len(self.writes()), 1)
        self.create_decision()
        result = 'Canonical complete words <&> 🙂\n' + '🙂' * 4200
        self.native.ask.update(state='answered', option=2, words=result)
        self.bridge.run_asks.refresh()
        parts = [body['text'] for url, body in self.calls if url.endswith('/sendMessage')][1:]
        self.assertEqual(''.join(parts), '✓ Answered: 2. Keep\n' + result)
        self.assertTrue(all(n.units(part) <= 4096 for part in parts))
        count = self.sent_count
        self.bridge.run_asks = n.RunAsks(self.bridge)
        self.bridge.run_asks.refresh()
        self.assertEqual(self.sent_count, count)
        revision = self.row()['generations'][-1]['revision']
        self.native.ask.update(option=3, words='Newer canonical choice', picks_before=1)
        self.bridge.run_asks.refresh()
        self.assertGreater(self.row()['generations'][-1]['revision'], revision)
        self.assertIn('3. You decide\nNewer canonical choice', self.edits()[-1]['text'])
        self.assertEqual(self.edits()[-1]['reply_markup']['inline_keyboard'], [])
        self.native.ask.update(option=2, words=result, picks_before=2)
        self.bridge.run_asks.refresh()
        self.assertGreater(self.sent_count, count)

    def test_concurrent_terminal_decisions_execute_once(self):
        self.create()
        first = self.press  # independent RunAsks objects share the process/file lock
        other = n.RunAsks(self.bridge)
        threads = [threading.Thread(target=first, kwargs={'uid': uid}) for uid in (40, 41)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()
        other.refresh()
        self.assertEqual(len(self.writes()), 1)

        self.create_decision()
        with ThreadPoolExecutor(max_workers=2) as pool:
            list(pool.map(lambda uid: self.press('pick1', uid=uid), (70, 71)))
        self.assertEqual([method for method, _ in self.writes()], ['AnswerAskExpected'])
        self.assertEqual(self.row()['state'], 'answered')
        self.reset_native(proposal())
        self.create()
        self.press(uid=72)

        publisher = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=self.bridge.request)
        for number in range(8, 12):
            self.native.ask = dict(proposal(), ask=number)
            key = f'concurrent-{number}'
            ask_id = self.bridge._ask_id_for_key(key)
            ordinary = dict(self.publish(key=key, text='Pick one?'), keyboard=[[dict(
                text='Choice A', callback_data=f'ask:{ask_id}:a')]])
            ready = threading.Barrier(2)

            def publish(native):
                ready.wait(timeout=5)
                return (publisher.api_run_ask(dict(bead='sample-1', ask=number, key=key)) if native
                        else self.bridge.publish(ordinary))

            count = self.sent_count
            with ThreadPoolExecutor(max_workers=2) as pool:
                receipts = list(pool.map(publish, (False, True)))
            self.assertEqual(sum(r['delivered'] for r in receipts), 1)
            refused = next(r for r in receipts if not r['delivered'])
            self.assertEqual(refused.get('reason', refused.get('metadata', {}).get('reason')), 'idempotency_key_conflict')
            self.assertEqual(self.sent_count, count + 1)
            self.assertEqual(self.store.get_ask(ask_id) is not None, receipts[0]['delivered'])
            with self.store.connect() as db:
                self.assertEqual(bool(db.execute('SELECT 1 FROM run_ask_keys WHERE key=?', (key,)).fetchone()),
                                 receipts[1]['delivered'])
        self.assertEqual(len(self.writes()), 1)


    def test_authenticated_api_topic_and_untrusted_fields(self):
        port = self.start_callback_server()
        self.bridge.topic_settings = (dict(name='ops', agent='yardmaster'),)
        self.store.record_topic('ops', 12, 'created')
        status, answer = self.api_call(port, '/v1/run-ask', dict(bead='sample-1', ask='7', topic='ops'),
                                      headers={'Idempotency-Key': 'api-native'})
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        self.assertEqual(answer['conversation'], '123:topic:12')
        self.assertEqual(self.row()['conversation'], '123:topic:12')
        self.assertEqual(self.calls[-1][1]['message_thread_id'], 12)
        for field in ('actor', 'weighed', 'command', 'weight', 'decision'):
            status, _ = self.api_call(port, '/v1/run-ask', dict(bead='sample-1', ask=7, key='bad', **{field: 'forged'}))
            self.assertEqual(status, 400)
        self.assertEqual(self.writes(), [])
        self.press(is_topic_message=True, message_thread_id=12)
        self.assertEqual(len(self.writes()), 1)
        self.reset_native(decision())
        status, _ = self.api_call(port, '/v1/ask-card', dict(bead='sample-1', ask=7, key='bad-auth'),
                                  headers={'Authorization': 'Bearer incorrect'})
        self.assertEqual(status, 403)
        status, answer = self.api_call(port, '/v1/ask-card', dict(bead='sample-1', ask='7', topic='ops'),
                                      headers={'Idempotency-Key': 'api-decision'})
        self.assertEqual(status, 200)
        self.assertTrue(answer['delivered'])
        self.assertEqual(answer['conversation'], '123:topic:12')
        for field in ('actor', 'weighed', 'question', 'context', 'options', 'words', 'option', 'weight', 'decision', 'turn'):
            status, _ = self.api_call(port, '/v1/ask-card', dict(bead='sample-1', ask=7, key='bad', **{field: 'forged'}))
            self.assertEqual(status, 400)
        self.press('pick2', uid=41, is_topic_message=True, message_thread_id=12)
        self.assertEqual(self.writes(), [('AnswerAskExpected', dict(bead='sample-1', ask=7, turn=0, actor='pat', option=2, words='', weighed=False))])

    def test_write_refusals_unreadable_state_and_relay_failures_do_not_forge_success(self):
        for committed_mode in ('wire-error-commit', 'invalid-commit'):
            self.native.ask = proposal()
            self.native.mode = None
            self.native.calls.clear()
            self.calls.clear()
            self.sent_count = 0
            with self.store.connect() as db:
                for table in ('run_asks', 'run_ask_keys', 'run_ask_updates', 'deliveries'):
                    db.execute('DELETE FROM ' + table)
            self.create()
            buttons = self.calls[-1][1]['reply_markup']
            self.native.mode = 'refused'
            self.press()
            self.assertEqual(self.row()['state'], 'open')
            self.assertNotIn('approved, ran', self.edits()[-1]['text'])
            self.assertEqual(self.edits()[-1]['reply_markup'], buttons)
            self.bridge.run_asks = n.RunAsks(self.bridge)
            self.bridge.run_asks.refresh()
            self.assertTrue(self.create()['delivered'])
            self.assertEqual(self.sent_count, 1)
            self.assertEqual(self.edits()[-1]['reply_markup'], buttons)
            self.native.mode = committed_mode
            self.bridge.hook_secret = 'FAKEHOOK'
            with patch.object(self.bridge, '_relay', side_effect=b.TransportError('unavailable')):
                self.press(uid=41)
            self.assertEqual(self.row()['state'], 'approved')
            self.assertIn('stored operation applied', self.edits()[-1]['text'])
            self.assertEqual(len(self.writes()), 2)
            with patch.object(self.bridge, '_relay') as relay:
                self.bridge.run_asks.refresh()
                relay.assert_called_once()
            self.native.mode = 'unreadable'
            count = len(self.writes())
            self.press(uid=42)
            self.assertEqual(len(self.writes()), count)

    def test_ambiguous_card_delivery_is_not_resent_and_publish_cannot_spoof_native_buttons(self):
        original = self.bridge.request
        def uncertain(url, body, *args, **kwargs):
            if url.endswith('/sendMessage'):
                self.calls.append((url, body))
                raise b.RequestUnconfirmed('lost')
            return original(url, body, *args, **kwargs)
        self.bridge.request = uncertain
        self.assertFalse(self.create()['delivered'])
        count = len(self.calls)
        self.bridge.run_asks = n.RunAsks(self.bridge)
        self.assertFalse(self.create()['delivered'])
        self.assertEqual(len(self.calls), count)
        self.assertIsNone(self.row()['message_id'])
        receipt = self.bridge.publish(dict(text='fake approval', idempotency_key='spoof', keyboard=[[dict(
            text='Approve', callback_data=f"runask:{self.row()['id']}:approve")]]))
        self.assertFalse(receipt['delivered'])
        self.assertEqual(len(self.calls), count)
        self.reset_native(decision())
        self.assertFalse(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='uncertain-decision'))['delivered'])
        count = len(self.calls)
        self.bridge.run_asks = n.RunAsks(self.bridge)
        for key in ('uncertain-decision', 'different-decision-key'):
            self.assertFalse(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key=key))['delivered'])
        self.assertEqual(len(self.calls), count)
        self.assertFalse(self.bridge.publish(dict(text='fake decision', idempotency_key='spoof-decision', keyboard=[[dict(
            text='Choose 1', callback_data=f"askcard:{self.row()['id']}:pick1")]]))['delivered'])
        self.assertEqual(len(self.calls), count)
        self.assertIsNone(self.row()['message_id'])
        self.assertEqual(self.row()['generations'], [])
        self.bridge.request = original
        update = dict(update_id=470, message=dict(message_id=470, text='2 uncertain card',
            chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)},
            reply_to_message=dict(message_id=77)))
        telegram = self.bridge.telegram
        def fetched(method, body, timeout=15):
            if method == 'getUpdates':
                return 200, dict(ok=True, result=[update])
            return telegram(method, body, timeout)
        canonical = copy.deepcopy(self.native.ask)
        with patch.object(self.bridge, 'telegram', side_effect=fetched), patch.object(self.bridge, '_relay') as relay:
            self.bridge.poll_once()
            relay.assert_called_once()
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.native.ask, canonical)
        self.assertEqual(self.row()['generations'], [])
        self.assertEqual(len(self.calls), count)

    def test_fresh_role_proposal_and_state_checks_on_confirmation(self):
        for factory in (proposal, skill_proposal):
            with self.subTest(payload=factory.__name__):
                self.reset_native(factory())
                if factory is proposal:
                    self.native.ask['weight'] = [{'class': 'code', 'line': 'brings a program the yard will run'}]
                self.create()
                for change in ('role', 'proposal', 'state'):
                    self.native.ask = factory()
                    if factory is proposal:
                        self.native.ask['weight'] = [{'class': 'code', 'line': 'brings a program the yard will run'}]
                    self.native.user['role'] = 'operator'
                    self.press(uid=60 + len(self.calls))
                    if change == 'role':
                        self.native.user['role'] = 'member'
                    elif change == 'proposal':
                        self.native.ask['run']['does'].append('changed since tap')
                    else:
                        self.native.ask.update(state='taken back', by='proposer', words='replaced')
                    self.reply(uid=200 + len(self.calls))
                    self.assertEqual(self.writes(), [])
                    row = self.row()
                    row.update(state='open', pending=None)
                    self.bridge.run_asks.save(row)

    def test_installed_native_cli_uses_the_same_ledger_and_module(self):
        from test_bridge import write_fake_curl
        from test_topics import topic_tables
        root = Path(self.tmp.name)
        settings = write_settings(root)
        settings.write_text(settings.read_text().replace('[yardr]',
                            f'[yardr]\nhome = "{self.home}"\noperator = "pat"')
                            + topic_tables(('ops', None), ('uncreated', None)))
        bin_dir = root / 'bin'
        write_fake_curl(bin_dir, f'#!{sys.executable}\nimport json,sys\nfrom pathlib import Path\n'
                       'with Path(__file__).with_name("requests.jsonl").open("a") as stream:\n'
                       '    stream.write(json.dumps(sys.stdin.read()) + "\\n")\n'
                       'print(json.dumps({"ok":True,"result":{"message_id":90,"chat":{"id":123}}}))\n'
                       'print("200 0.01 0.02 0", end="")\n')
        env = dict(HOME=str(root), PATH=str(bin_dir) + ':/usr/local/bin:/usr/bin:/bin',
                   TELEGRAM_BRIDGE_SETTINGS=str(settings))
        loaded = b.load_settings(settings)
        checks = b.install_problems(loaded, platform='linux', version=lambda _: (3, 11))
        self.assertIn((True, 'native yard socket and current operator identity'), checks)
        self.native.user['role'] = 'member'
        checks = b.install_problems(loaded, platform='linux', version=lambda _: (3, 11))
        self.assertIn((False, 'operator_not_authorized'), checks)
        self.native.user['role'] = 'operator'
        (root / 'state').mkdir(mode=0o700)
        installed = b.Store(root / 'state' / 'telegram-bridge.sqlite3', 0)
        installed.record_topic('ops', 12, 'created')
        command = [sys.executable, '-B', str(Path(b.__file__).resolve()), 'run-ask', '--bead', 'sample-1',
                   '--ask', '7', '--idempotency-key', 'installed-native']
        for routing in (['--topic', 'ops'], ['--conversation-id', '123:topic:12'], ['--topic', 'ops']):
            result = subprocess.run(command + routing, cwd=root, env=env, text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            receipt = json.loads(result.stdout)
            self.assertTrue(receipt['delivered'])
            self.assertEqual(receipt['conversation'], '123:topic:12')
        requests = (bin_dir / 'requests.jsonl').read_text().splitlines()
        self.assertEqual(len(requests), 1)
        body = next(json.loads(json.loads(line[len('data = '):])) for line in json.loads(requests[0]).splitlines()
                    if line.startswith('data = '))
        self.assertEqual((body['chat_id'], body['message_thread_id']), (123, 12))
        self.assertIn('Change the test?', body['text'])
        self.assertTrue(body['reply_markup']['inline_keyboard'][0][0]['callback_data'].startswith('runask:'))
        with installed.connect() as db:
            rows = [json.loads(row[0]) for row in db.execute('SELECT data FROM run_asks')]
        self.assertEqual(len(rows), 1)
        self.assertEqual((rows[0]['conversation'], rows[0]['message_id']), ('123:topic:12', 90))
        for routing, message in ((['--topic', 'nope'], 'topic nope is not configured, created or served'),
                                 (['--topic', 'uncreated'], 'topic uncreated is not configured, created or served'),
                                 (['--topic', 'ops', '--conversation-id', '123'],
                                  '--topic and --conversation-id are exclusive')):
            with self.subTest(routing=routing):
                result = subprocess.run(command + routing, cwd=root, env=env, text=True,
                                        capture_output=True, timeout=20)
                self.assertNotEqual(result.returncode, 0)
                self.assertIn(message, result.stderr)
        self.assertEqual((bin_dir / 'requests.jsonl').read_text().splitlines(), requests)
        self.assertEqual(self.writes(), [])
        self.native.ask = dict(decision(), ask=8)
        command[3] = 'ask-card'
        command[command.index('--ask') + 1] = '8'
        command[command.index('--idempotency-key') + 1] = 'installed-decision'
        for routing in (['--topic', 'ops'], ['--conversation-id', '123:topic:12']):
            result = subprocess.run(command + routing, cwd=root, env=env, text=True, capture_output=True, timeout=20)
            self.assertEqual(result.returncode, 0, result.stderr)
            self.assertTrue(json.loads(result.stdout)['delivered'])
        requests = (bin_dir / 'requests.jsonl').read_text().splitlines()
        self.assertEqual(len(requests), 2)
        body = next(json.loads(json.loads(line[len('data = '):])) for line in json.loads(requests[-1]).splitlines()
                    if line.startswith('data = '))
        self.assertIn('Where should the links go?', body['text'])
        self.assertEqual((body['chat_id'], body['message_thread_id']), (123, 12))
        self.assertTrue(body['reply_markup']['inline_keyboard'][0][0]['callback_data'].startswith('askcard:'))
        self.assertEqual(self.writes(), [])


class NativeSettingsTests(unittest.TestCase):
    def test_native_settings_check_then_atomic_add_preserves_all_other_bytes(self):
        script = Path(__file__).resolve().parents[1] / 'scripts' / 'native_settings.py'
        with tempfile.TemporaryDirectory(prefix='native-') as tmp:
            root = Path(tmp)
            home = root / 'yard'
            home.mkdir(mode=0o700)
            path = root / 'settings.toml'
            command = [sys.executable, '-B', str(script), '--settings', str(path), '--home', str(home), '--operator', 'pat']
            env = dict(HOME=tmp, PATH=tmp + ':/usr/local/bin:/usr/bin:/bin')

            def run(*args, timeout=10):
                result = subprocess.run(command + list(args), env=env, cwd=tmp, capture_output=True, text=True, timeout=timeout)
                self.assertNotIn('synthetic-secret', result.stdout + result.stderr)
                return result

            for newline in ('\n', '\r\n'):
                original = ('# Keep comments\n[bot]\ntoken = "synthetic-secret"\n[yardr] # native keys\n'
                            'source = "tgbridge"\n[limits]\ncalls = 600\nupdates = 300\n').replace('\n', newline).encode()
                path.write_bytes(original)
                path.chmod(0o600)
                self.assertEqual(run().returncode, 0)
                self.assertEqual(path.read_bytes(), original)
                self.assertFalse(path.with_name(path.name + '.before-native').exists())
                self.assertEqual(run('--apply').returncode, 0)
                changed = path.read_bytes()
                insert = (newline + 'home = ' + json.dumps(str(home)) + newline + 'operator = "pat"').encode()
                self.assertEqual(changed.replace(insert, b'', 1), original)
                backup = path.with_name(path.name + '.before-native')
                self.assertEqual(backup.read_bytes(), original)
                self.assertEqual(backup.stat().st_mode & 0o777, 0o600)
                self.assertEqual(path.stat().st_mode & 0o777, 0o600)
                self.assertEqual(run('--apply').returncode, 0)
                self.assertEqual(path.read_bytes(), changed)
                backup.unlink()
            for bad in ('[yardr]\nhome = "/conflicting"\n', '[yardr]\noperator = "another"\n',
                        '[yardr]\nhome = "one"\nhome = "two"\n', 'yardr = {source="tgbridge"}\n',
                        '[yardr]\noperator = "synthetic-secret', '["yardr"]\nsource="tgbridge"\n'):
                path.write_text(bad)
                before = path.read_bytes()
                self.assertNotEqual(run('--apply').returncode, 0)
                self.assertEqual(path.read_bytes(), before)
                self.assertFalse(path.with_name(path.name + '.before-native').exists())
            path.write_text('[yardr]\nsource="tgbridge"\n')
            path.chmod(0o644)
            self.assertNotEqual(run('--apply').returncode, 0)
            path.chmod(0o600)
            target = root / 'target'
            path.rename(target)
            path.symlink_to(target)
            self.assertNotEqual(run('--apply').returncode, 0)
            self.assertEqual(target.read_text(), '[yardr]\nsource="tgbridge"\n')
            path.unlink()
            path.write_text('[yardr]\nhome="~/yard"\nsource="tgbridge"\n')
            path.chmod(0o600)
            self.assertEqual(run('--apply').returncode, 0)
            self.assertEqual(path.read_text(), '[yardr]\noperator = "pat"\nhome="~/yard"\nsource="tgbridge"\n')
            path.unlink()
            path.with_name(path.name + '.before-native').unlink()
            for extra in ((), ('--apply',)):
                with self.subTest(settings_fifo=extra):
                    os.mkfifo(path, 0o600)
                    try:
                        self.assertEqual(run(*extra, timeout=2).returncode, 1)
                        self.assertFalse(path.with_name(path.name + '.before-native').exists())
                    finally:
                        path.unlink()
            path.write_text('[yardr]\nsource="tgbridge"\n')
            path.chmod(0o600)
            lock = path.with_name(path.name + '.native.lock')
            lock.unlink()
            os.mkfifo(lock, 0o600)
            self.assertEqual(run('--apply', timeout=2).returncode, 1)
            self.assertEqual(path.read_text(), '[yardr]\nsource="tgbridge"\n')
            self.assertFalse(path.with_name(path.name + '.before-native').exists())

    def test_optional_pair_private_home_and_all_installed_import_modes(self):
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            yard = root / 'yard'
            yard.mkdir(mode=0o700)
            settings = write_settings(root)
            original = settings.read_text()
            for extra in ('home = "' + str(yard) + '"', 'operator = "pat"',
                          'home = "' + str(yard) + '"\noperator = "invalid operator"'):
                settings.write_text(original.replace('[yardr]', '[yardr]\n' + extra))
                with self.assertRaises(b.SettingsError):
                    b.load_settings(settings)
            settings.write_text(original.replace('[yardr]', f'[yardr]\nhome = "{yard}"\noperator = "pat"'))
            loaded = b.load_settings(settings)
            self.assertEqual(loaded.config['yardr_socket'], str(yard / 'yard.sock'))
            yard.chmod(0o777)
            with self.assertRaises(b.SettingsError):
                b.load_settings(settings)
            yard.chmod(0o700)
            bridge = Path(b.__file__).resolve()
            env = dict(HOME=tmp, PATH=tmp + ':/usr/local/bin:/usr/bin:/bin')
            # install.sh uses runpy from an unrelated cwd; services execute the absolute script.
            for argv in ([sys.executable, '-c', 'import runpy,sys; runpy.run_path(sys.argv[1])', str(bridge)],
                         [sys.executable, str(bridge), '--help'],
                         [sys.executable, str(bridge), '--settings', str(settings), 'render-unit']):
                result = subprocess.run(argv, cwd=tmp, env=env, capture_output=True, text=True)
                self.assertEqual(result.returncode, 0, result.stderr)
            unit = b.render_unit(loaded)
            writes = next(line for line in unit.splitlines() if line.startswith('ReadWritePaths='))
            self.assertNotIn(str(yard), writes)


class RealNativeTests(BridgeTestCase):
    def test_real_native_store_records_answer_and_applies_stored_operation_once(self):
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
        self.addCleanup(lambda: server.poll() is None and server.kill())
        try:
            client = n.YardSocketClient(home, 'tgbridge')
            deadline = time.monotonic() + 15
            while not (home / 'yard.sock').exists():
                if server.poll() is not None or time.monotonic() > deadline:
                    self.fail('isolated native server did not start')
                time.sleep(.02)

            def cli(*args):
                result = subprocess.run(command + list(args), env=env, cwd=root, text=True, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout

            cli('settings', 'set', 'operator', 'pat')
            cli('settings', 'set', 'default_kind', 'codex')
            source = root / 'synthetic.yaml'
            source.write_text('name: tgbridge\nbase: https://example.invalid\ncalls:\n  health:\n    kind: read\n    request: {method: GET, path: /health}\nhook:\n  bodies: unique\n  verify:\n    scheme: secret\n    signature: {header: X-Secret}\n  normalize: [/bin/false]\n')
            cli('source', 'add', str(source))
            cli('pack', 'check', str(Path(__file__).resolve().parents[1] / 'yardr'))
            cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
            cli('bead', 'create', 'Synthetic operation', '--id', 'sample-1')
            # Lowering a limit is a routine real native operation with no network side effect.
            cli('source', 'set', 'tgbridge', 'calls_per_hour=120')
            cli('user', 'add', 'proposer')
            result = subprocess.run([binary, '--home', str(home), '--actor', 'proposer', 'bead', 'ask', 'sample-1',
                                     'Lower the test limit?', '--to', 'pat', '--context', 'Synthetic isolated integration',
                                     '--run', 'source set tgbridge calls_per_hour=60'], env=env, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            ask = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][0]
            self.assertEqual(ask.get('weight', []), [])
            self.config.update(yardr_home=str(home), yardr_operator='pat', yardr_source='tgbridge')
            ids = iter(range(77, 300))
            def telegram(url, body, *args, **kwargs):
                status, response = self.transport(url, body, *args, **kwargs)
                if url.endswith('/sendMessage'):
                    response['result']['message_id'] = next(ids)
                elif url.endswith('/editMessageText'):
                    response = dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=123)))
                return status, response
            self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=telegram)
            self.bridge.hook_secret = None
            self.assertTrue(self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key='real-native'))['delivered'])
            row = self.bridge.run_asks.rows()[0]
            query = dict(id='real-callback', data=f"runask:{row['id']}:approve", **{'from': dict(id=123, is_bot=False)},
                         message=dict(message_id=77, chat=dict(id=123, type='private'), **{'from': dict(id=999, is_bot=True)}))
            self.bridge.inbound(dict(update_id=40, callback_query=query))
            stored = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][0]
            self.assertEqual(stored['state'], 'answered')
            self.assertEqual(stored['ran']['argv'], ['source', 'set', 'tgbridge', 'calls_per_hour=60'])
            self.assertIn('60', stored['ran']['result'])
            self.assertEqual(client.call('GetSource', ['tgbridge'])['calls_per_hour'], 60)
            events = client.call('EventsOfKind', ['source_set', 100])
            applied = [event for event in events if event['data'].get('approved', {}).get('ask') == ask['ask']]
            self.assertEqual(len(applied), 1)
            self.bridge.inbound(dict(update_id=41, callback_query=query))
            after = client.call('EventsOfKind', ['source_set', 100])
            self.assertEqual(after, events)
            # The same real store supplies the structured trust weight for a second, fresh proposal.
            result = subprocess.run([binary, '--home', str(home), '--actor', 'proposer', 'bead', 'ask', 'sample-1',
                                     'Add the synthetic member?', '--to', 'pat', '--context', 'Isolated trust gate',
                                     '--run', 'user add synthetic-member'], env=env, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            strong = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]
            self.assertTrue(strong['weight'])
            self.assertEqual(strong['weight'][0]['class'], 'trust')
            self.assertTrue(self.bridge.api_run_ask(dict(bead='sample-1', ask=strong['ask'], key='real-weight'))['delivered'])
            row = next(row for row in self.bridge.run_asks.rows() if row['ask'] == strong['ask'])
            query['data'] = f"runask:{row['id']}:approve"
            query['message']['message_id'] = row['message_id']
            self.bridge.inbound(dict(update_id=42, callback_query=query))
            self.assertEqual(client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]['state'], 'open')
            self.bridge.inbound(dict(update_id=43, message=dict(message_id=901, text=str(strong['ask']),
                chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)},
                reply_to_message=dict(message_id=row['message_id'], **{'from': dict(id=999, is_bot=True)}))))
            settled = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]
            self.assertEqual(settled['state'], 'answered')
            self.assertEqual(settled['ran']['argv'], ['user', 'add', 'synthetic-member'])
            self.assertEqual(client.call('GetUser', {'name': 'synthetic-member'})['name'], 'synthetic-member')
            additions = client.call('EventsOfKind', ['user_added', 100])
            self.assertEqual(len([event for event in additions if event['data'].get('approved', {}).get('ask') == strong['ask']]), 1)

            def publish_decision(bead_id, ask):
                receipt = self.bridge.api_ask_card(dict(bead=bead_id, ask=ask['ask'], key=f"ordinary-{ask['ask']}"))
                self.assertTrue(receipt['delivered'], receipt)
                return next(r for r in self.bridge.run_asks.rows() if r['bead'] == bead_id and r['ask'] == ask['ask'])

            def assert_fixture(ask, name):
                actual = dict(ask, ask=7, since='2026-01-01T00:00:00Z')
                fixture = Path(__file__).with_name('fixtures') / ('ordinary_' + name + '.json')
                self.assertEqual(actual, json.loads(fixture.read_text()))

            def bound_reply(row, value, update):
                return dict(update_id=update, message=dict(message_id=1000 + update, text=value,
                    chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)},
                    reply_to_message=dict(message_id=row['message_id'], **{'from': dict(id=999, is_bot=True)})))

            cli('bead', 'ask', 'sample-1', 'Where do the links go?', '--to', 'pat', '--context', 'Synthetic ordinary decision',
                '--option', 'Move: Move the links.', '--option', 'Keep: Keep the links here.', '--recommend', '1', '--why', 'The target is ready.')
            ordinary = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]
            assert_fixture(ordinary, 'option')
            self.assertEqual(ordinary['options'], [dict(label='Move', consequence='Move the links.'),
                dict(label='Keep', consequence='Keep the links here.'), dict(label='You decide', consequence='leave it to pat')])
            row = publish_decision('sample-1', ordinary)
            response = bound_reply(row, '2 keep this note', 300)
            self.bridge.inbound(response)
            self.bridge.inbound(response)
            stored = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]
            self.assertEqual((stored['state'], stored['option'], stored['words']), ('answered', 2, 'keep this note'))
            before_reply = client.call('Bead', dict(bead='sample-1', viewer='pat'))
            self.bridge.inbound(bound_reply(row, '1 stale', 301))
            self.assertEqual(client.call('Bead', dict(bead='sample-1', viewer='pat')), before_reply)

            for index, (action, method) in enumerate((('pick1', 'AnswerAskExpected'), ('reply', 'AnswerAskExpected'),
                                                       ('later', 'DeferAskExpected'), ('decline', 'DeclineAskExpected'))):
                for external in ('AnswerAsk', 'DeferAsk', 'DeclineAsk', 'TakeBackAsk'):
                    with self.subTest(race=action, external=external):
                        bead_id = f'race-{index}-{external.lower()}'
                        cli('bead', 'create', 'Synthetic concurrent decision', '--id', bead_id)
                        cli('bead', 'ask', bead_id, 'Which option?', '--to', 'pat', '--context', 'Isolated race',
                            '--option', 'First: Choose first.', '--option', 'Second: Choose second.')
                        original = client.call('Bead', dict(bead=bead_id, viewer='pat'))['asks'][0]
                        receipt = self.bridge.api_ask_card(dict(bead=bead_id, ask=original['ask'], key=bead_id))
                        self.assertTrue(receipt['delivered'], receipt)
                        row = next(r for r in self.bridge.run_asks.rows() if r['bead'] == bead_id)
                        actual_call = self.bridge.run_asks.client.call
                        writes, external_page, external_notes = [], [], []

                        def racing_call(name, args):
                            if name in ('AnswerAsk', 'DeferAsk', 'DeclineAsk',
                                        'AnswerAskExpected', 'DeferAskExpected', 'DeclineAskExpected'):
                                writes.append((name, copy.deepcopy(args)))
                                if len(writes) == 1:
                                    other = dict(bead=bead_id, ask=original['ask'], actor='pat')
                                    if external == 'AnswerAsk':
                                        other.update(option=2, words='external answer', weighed=False)
                                    elif external == 'TakeBackAsk':
                                        other['reason'] = 'external withdrawal'
                                    elif external == 'DeclineAsk':
                                        other['words'] = 'external decision'
                                    client.call(external, other)
                                    external_page.append(client.call('Bead', dict(bead=bead_id, viewer='pat')))
                                    external_notes.append(client.call('Notes', [bead_id]))
                            return actual_call(name, args)

                        update = 500 + index * 10 + ('AnswerAsk', 'DeferAsk', 'DeclineAsk', 'TakeBackAsk').index(external)
                        if action == 'reply':
                            response = bound_reply(row, '1 stale Telegram words', update)
                        else:
                            response = dict(update_id=update, callback_query=dict(id=f'race-{update}',
                                data=f"askcard:{row['id']}:{action}:{original['turn']}",
                                **{'from': dict(id=123, is_bot=False)}, message=dict(message_id=row['message_id'],
                                chat=dict(id=123, type='private'), **{'from': dict(id=999, is_bot=True)})))
                        with patch.object(self.bridge.run_asks.client, 'call', side_effect=racing_call):
                            self.bridge.inbound(response)
                            self.bridge.inbound(response)
                        after = client.call('Bead', dict(bead=bead_id, viewer='pat'))
                        notes = client.call('Notes', [bead_id])
                        self.assertEqual(after, external_page[0])
                        self.assertEqual(notes, external_notes[0])
                        self.assertEqual(len([note for note in notes if note['kind'] == 'answer']), 1)
                        self.assertEqual(after['asks'][0]['turn'], original['turn'] + 1)
                        if external == 'AnswerAsk':
                            self.assertEqual((after['asks'][0]['option'], after['asks'][0]['words']), (2, 'external answer'))
                        self.assertEqual([name for name, _ in writes], [method])
                        self.assertEqual(writes[0][1]['turn'], original['turn'])
                        displayed = self.bridge.run_asks.get(row['id'])
                        self.assertEqual(displayed['state'], after['asks'][0]['state'])
                        self.assertEqual(displayed['display']['keyboard'], [])

                        if action == 'reply' and external == 'DeferAsk':
                            # Real defer/reopen: the actual asker's next note brings it back.
                            cli('bead', 'note', bead_id, 'Synthetic proposer follow-up')
                            reopened = client.call('Bead', dict(bead=bead_id, viewer='pat'))
                            self.assertEqual((reopened['asks'][0]['state'], reopened['asks'][0]['turn']), ('open', 1))
                            before = client.call('Notes', [bead_id])
                            telegram_call = self.bridge.telegram

                            def poll(response):
                                def fetched(method, body, timeout=15):
                                    if method == 'getUpdates':
                                        return 200, dict(ok=True, result=[response])
                                    return telegram_call(method, body, timeout)
                                with patch.object(self.bridge, 'telegram', side_effect=fetched):
                                    self.bridge.poll_once()

                            with patch.object(self.bridge.run_asks.client, 'call', wraps=actual_call) as rpc:
                                poll(bound_reply(row, '1 queued before reopen', 610))
                                self.assertFalse(any(call.args[0].endswith('Expected') for call in rpc.call_args_list))
                                self.assertEqual(client.call('Bead', dict(bead=bead_id, viewer='pat')), reopened)
                                self.assertEqual(client.call('Notes', [bead_id]), before)
                                current = self.bridge.run_asks.get(row['id'])
                                self.assertNotEqual(current['message_id'], row['message_id'])
                                self.bridge.run_asks = n.RunAsks(self.bridge)
                            with patch.object(self.bridge.run_asks.client, 'call', wraps=actual_call) as rpc:
                                poll(bound_reply(current, '2 after real reopen', 611))
                                poll(bound_reply(current, '1 replay cannot replace', 612))
                                writes = [call.args for call in rpc.call_args_list if call.args[0].endswith('Expected')]
                                self.assertEqual(writes, [('AnswerAskExpected', dict(bead=bead_id, ask=original['ask'],
                                    turn=1, option=2, words='after real reopen', actor='pat', weighed=False))])
                            final = client.call('Bead', dict(bead=bead_id, viewer='pat'))['asks'][0]
                            self.assertEqual((final['state'], final['turn'], final['option'], final['words']),
                                             ('answered', 2, 2, 'after real reopen'))
                            answers = [note for note in client.call('Notes', [bead_id]) if note['kind'] == 'answer']
                            self.assertEqual(len(answers), 2)  # One defer note and exactly one option answer.
                            recorded = [json.loads(note['text']) for note in answers]
                            self.assertEqual(len([answer for answer in recorded if not answer.get('later')]), 1)
                            self.assertEqual([(answer['option'], answer['words']) for answer in recorded if 'option' in answer],
                                             [(2, 'after real reopen')])

            cli('bead', 'ask', 'sample-1', 'What should it say?', '--to', 'pat', '--context', 'A free-word answer')
            ordinary = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]
            self.assertEqual(ordinary['options'], [])
            assert_fixture(ordinary, 'words')
            row = publish_decision('sample-1', ordinary)
            self.bridge.inbound(bound_reply(row, 'A complete ordinary answer 🙂', 302))
            stored = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]
            self.assertEqual((stored['state'], stored['words']), ('answered', 'A complete ordinary answer 🙂'))
            for offset, action in enumerate(('later', 'decline')):
                cli('bead', 'ask', 'sample-1', 'Decide this later?', '--to', 'pat', '--context', 'Isolated primitive')
                ordinary = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]
                row = publish_decision('sample-1', ordinary)
                query = dict(id=f'ordinary-{offset}', data=f"askcard:{row['id']}:{action}:{row['turn']}",
                    **{'from': dict(id=123, is_bot=False)}, message=dict(message_id=row['message_id'],
                    chat=dict(id=123, type='private'), **{'from': dict(id=999, is_bot=True)}))
                self.bridge.inbound(dict(update_id=303 + offset, callback_query=query))
                stored = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]
                self.assertEqual(stored['state'], 'put aside' if action == 'later' else 'declined')

            cli('user', 'add', 'other-human')
            cli('group', 'add', 'humans', '--runner', 'manual', '--members', 'pat,other-human')
            flow = root / 'decision.flow'
            flow.write_text('decide -> ready on done | backlog on changes\nready -> backlog\nbacklog -> decide\n')
            cli('flow', 'set', 'synthetic', '-f', str(flow))
            stage_dir = home / 'flows' / 'synthetic'
            stage_dir.mkdir(parents=True, exist_ok=True)
            (stage_dir / 'decide.md').write_text('## Ask\nkind: choice\nquestion: Is the work ready?\ncontext:\n'
                '- Read the complete result.\nrecommend: 1\nwhy: The checks passed.\noptions:\n'
                'done: Move to ready with your note.\nchanges: Return to backlog with your note.\n')
            cli('route', 'add', '--stage', 'decide', '--group', 'humans')
            cli('bead', 'create', 'Synthetic stage decision', '--id', 'stage-1', '--stage', 'decide', '--meta', 'flow=synthetic')
            page = client.call('Bead', dict(bead='stage-1', viewer='pat'))
            self.assertEqual(len(page['asks']), 2, page)
            stage = next(a for a in page['asks'] if a['to'] == 'pat')
            assert_fixture(stage, 'stage')
            self.assertEqual(stage['asker'], 'yard')
            self.assertEqual([o['label'] for o in stage['options']], ['done', 'changes'])
            row = publish_decision('stage-1', stage)
            forged = bound_reply(row, '1 forged note', 305)
            forged['message']['reply_to_message']['message_id'] = 99999
            with patch.object(self.bridge, '_relay', return_value=self.bridge.conversation):
                self.bridge.inbound(forged)
            self.assertEqual(client.call('Bead', dict(bead='stage-1', viewer='pat')), page)
            response = bound_reply(row, '1 Reviewed stage note', 306)
            self.bridge.inbound(response)
            self.bridge.inbound(response)
            after = client.call('Bead', dict(bead='stage-1', viewer='pat'))
            self.assertEqual(after['stage'], 'ready')
            self.assertEqual(next(a for a in after['asks'] if a['to'] == 'pat')['state'], 'answered')
            self.assertEqual(next(a for a in after['asks'] if a['to'] == 'other-human')['state'], 'taken back')
            self.assertEqual(len([note for note in client.call('Notes', ['stage-1']) if note['kind'] == 'answer'
                                  and json.loads(note['text']).get('words') == 'Reviewed stage note']), 1)
            self.bridge.inbound(bound_reply(row, '2 stale stage note', 307))
            self.assertEqual(client.call('Bead', dict(bead='stage-1', viewer='pat')), after)

            # The new core supplies the carried review; Telegram must deliver it all before buttons.
            fixture = skill_proposal()
            old = root / 'old'
            old.mkdir()
            (old / 'SKILL.md').write_bytes(fixture['run']['carried'][0]['text'].encode())
            cli('skill', 'add', str(old))
            source_dir = root / 'snapshot'
            source_dir.mkdir()
            captured = (fixture['run']['carried'][0]['files'][0]['text']
                        + 'Generic long Unicode line 🙂 <&>\t\r' * 160 + '\n').encode()
            target = home / 'skills' / 'snapshot' / 'SKILL.md'
            before = target.read_bytes()
            before_identity = target.stat().st_ino
            (source_dir / 'SKILL.md').write_bytes(captured)
            result = subprocess.run([binary, '--home', str(home), '--actor', 'proposer', 'bead', 'ask', 'sample-1',
                                     'Refresh the synthetic skill?', '--to', 'pat', '--context', 'Synthetic snapshot fixture',
                                     '--run', 'skill add ' + str(source_dir)], env=env, cwd=root, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            skill = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]
            # Verify the frozen fixture still uses the real wire contract and canonical metadata.
            self.assertEqual(set(skill), set(fixture))
            self.assertEqual(set(skill['run']), set(fixture['run']))
            self.assertEqual(skill['run']['does'][:2], fixture['run']['does'][:2])
            self.assertEqual(skill['weight'], fixture['weight'])
            self.assertEqual(skill['run']['carried'][0]['files'][0]['text'].encode(), captured)
            # Source provenance is never reopened by the bridge or core carry.
            (source_dir / 'SKILL.md').unlink()
            source_dir.rmdir()
            # Enough unique Telegram IDs for a complete large bundle.
            ids = iter(range(200, 1000))
            calls_before = len(self.calls)
            receipt = self.bridge.api_run_ask(dict(bead='sample-1', ask=skill['ask'], key='real-skill'))
            self.assertTrue(receipt['delivered'], receipt)
            row = next(r for r in self.bridge.run_asks.rows() if r['ask'] == skill['ask'])
            sends = [body for url, body in self.calls[calls_before:] if url.endswith('/sendMessage')]
            self.assertGreater(len(sends), 3)
            self.assertEqual(len(sends), len(row['bundle']['parts']) + 1)
            self.assertTrue(all(not body.get('reply_markup') for body in sends[:-1]))
            self.assertTrue(sends[-1]['reply_markup']['inline_keyboard'])
            self.assertEqual(target.read_bytes(), before)
            self.assertEqual(target.stat().st_ino, before_identity)
            query['data'] = sends[-1]['reply_markup']['inline_keyboard'][0][0]['callback_data']
            query['message']['message_id'] = row['message_id']
            self.bridge.inbound(dict(update_id=50, callback_query=query))
            self.assertEqual(client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]['state'], 'open')
            self.assertEqual(target.read_bytes(), before)
            approvals = []
            actual_call = self.bridge.run_asks.client.call
            def record_call(method, args):
                reply = actual_call(method, args)
                if method == 'ApproveAsk':
                    approvals.append(reply)
                return reply
            self.bridge.run_asks.client.call = record_call
            confirmation = dict(update_id=51, message=dict(message_id=901, text=str(skill['ask']),
                chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)},
                reply_to_message=dict(message_id=row['message_id'], **{'from': dict(id=999, is_bot=True)})))
            self.bridge.inbound(confirmation)
            self.assertEqual(len(approvals), 1)
            self.assertEqual(approvals[0]['result'], n.PENDING)
            deadline = time.monotonic() + 10
            while True:
                settled = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]
                if settled.get('ran', {}).get('result') != n.PENDING:
                    break
                if time.monotonic() > deadline:
                    self.fail('real skill carry did not settle')
                time.sleep(.02)
            self.assertEqual(settled['state'], 'answered')
            self.assertFalse(settled['ran'].get('failed'))
            self.assertEqual(settled['ran']['argv'], skill['run']['argv'])
            self.assertIn('snapshot', settled['ran']['result'])
            self.assertEqual(target.read_bytes(), captured)
            installed = (target.stat().st_ino, target.stat().st_mtime_ns)
            intents = sorted(p.name for p in (home / 'skills').glob('.carry-skill-*'))
            self.assertEqual(len(intents), 1)
            self.bridge.run_asks = n.RunAsks(self.bridge)
            self.bridge.run_asks.refresh()
            self.assertEqual(next(r for r in self.bridge.run_asks.rows() if r['ask'] == skill['ask'])['state'], 'approved')
            self.bridge.inbound(confirmation)
            self.bridge.inbound(dict(update_id=52, callback_query=query))
            self.assertTrue(self.bridge.api_ask_close(dict(ask_key='real-skill', decision='invented outcome'))['delivered'])
            self.assertTrue(self.bridge.api_run_ask(dict(bead='sample-1', ask=skill['ask'], key='real-skill-repost'))['delivered'])
            self.assertEqual(target.read_bytes(), captured)
            self.assertEqual((target.stat().st_ino, target.stat().st_mtime_ns), installed)
            self.assertEqual(sorted(p.name for p in (home / 'skills').glob('.carry-skill-*')), intents)
            self.assertEqual(client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1], settled)

            for offset, scenario in enumerate(('add', 'add-quoted', 'add-block', 'decline', 'target-drift', 'unfamiliar-metadata'), 1):
                with self.subTest(real_skill=scenario):
                    name = 'snapshot-' + scenario
                    if scenario == 'target-drift':
                        cli('skill', 'add', str(old), '--name', name)
                    source_dir.mkdir()
                    proposed_bytes = captured
                    if scenario == 'add-quoted':
                        proposed_bytes = captured.replace(b'description: Generic proposed instructions', b'description: "Generic proposed instructions"')
                    elif scenario == 'add-block':
                        proposed_bytes = captured.replace(b'description: Generic proposed instructions', b'description: >-\r\n  Generic proposed\r\n  instructions')
                    elif scenario == 'unfamiliar-metadata':
                        proposed_bytes = captured.replace(b'name: snapshot', b'license: MIT\r\nname: snapshot')
                    (source_dir / 'SKILL.md').write_bytes(proposed_bytes)
                    result = subprocess.run([binary, '--home', str(home), '--actor', 'proposer', 'bead', 'ask', 'sample-1',
                        'Review a synthetic single-file skill?', '--to', 'pat', '--context', 'Isolated native refusal/add case',
                        '--run', 'skill add ' + str(source_dir) + ' --name ' + name], env=env, cwd=root, capture_output=True, text=True)
                    self.assertEqual(result.returncode, 0, result.stderr)
                    proposed = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]
                    if scenario.startswith('add'):
                        carried = proposed['run']['carried'][0]
                        self.assertEqual(carried['text'], '')
                        # Core omits empty old-state fields on an add, not on a refresh.
                        self.assertNotIn('inventory', carried)
                        self.assertNotIn('old_sha256', carried['old'][0])
                    with self.assertRaises(n.RPCRefused):
                        client.call('ApproveAsk', dict(bead='sample-1', ask=proposed['ask'], actor='proposer', weighed=True, values={}))
                    shown = self.bridge.api_run_ask(dict(bead='sample-1', ask=proposed['ask'], key=name))
                    if scenario == 'unfamiliar-metadata':
                        self.assertFalse(shown['delivered'])
                        self.assertIn('unsupported native skill metadata', shown['reason'])
                        self.assertFalse((home / 'skills' / name).exists())
                        (source_dir / 'SKILL.md').unlink()
                        source_dir.rmdir()
                        continue
                    self.assertTrue(shown['delivered'], shown)
                    row = next(r for r in self.bridge.run_asks.rows() if r['ask'] == proposed['ask'])
                    action = 'decline' if scenario == 'decline' else 'approve'
                    query['data'] = f"runask:{row['id']}:{action}"
                    query['message']['message_id'] = row['message_id']
                    self.bridge.inbound(dict(update_id=100 + offset * 2, callback_query=query))
                    target = home / 'skills' / name / 'SKILL.md'
                    if scenario == 'target-drift':
                        drifted = before.replace(b'Keep', b'Change')
                        target.write_bytes(drifted)
                    if scenario != 'decline':
                        self.bridge.inbound(dict(update_id=101 + offset * 2, message=dict(
                            message_id=1001 + offset, text=str(proposed['ask']), chat=dict(id=123, type='private'),
                            **{'from': dict(id=123, is_bot=False)}, reply_to_message=dict(message_id=row['message_id']))))
                    deadline = time.monotonic() + 10
                    while True:
                        final = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]
                        if final.get('ran', {}).get('result') != n.PENDING:
                            break
                        if time.monotonic() > deadline:
                            self.fail('real skill add did not settle')
                        time.sleep(.02)
                    if scenario.startswith('add'):
                        self.assertEqual(final['state'], 'answered')
                        self.assertEqual(target.read_bytes(), proposed_bytes)
                    elif scenario == 'decline':
                        self.assertEqual(final['state'], 'declined')
                        self.assertFalse(target.exists())
                    else:
                        self.assertEqual(final['state'], 'open')
                        self.assertEqual(target.read_bytes(), drifted)
                    (source_dir / 'SKILL.md').unlink()
                    source_dir.rmdir()
            # Reapplying the supported source pack preserves an installation's bindings and grants.
            release = str(Path(__file__).resolve().parents[1] / 'yardr')

            def rendered(name, port):
                """The release's pack as install.sh renders it for an instance on port."""
                directory = root / name
                directory.mkdir()
                b.stage_pack(b.render_pack(release, port), directory)
                return str(directory)

            # With no base= selected, the yard's calls go to the base of the instance's own pack.
            self.assertNotIn('base', client.call('GetSource', ['tgbridge']))
            cli('pack', 'apply', rendered('fresh-pack', 28941), '--yes')
            self.assertEqual(cli('source', 'show', 'tgbridge').splitlines()[0], 'tgbridge  http://127.0.0.1:28941')
            legacy = root / 'legacy-pack'
            shutil.copytree(release, legacy)
            declaration = legacy / 'tgbridge.yaml'
            legacy_text = declaration.read_text()
            declaration.write_text(legacy_text[:legacy_text.index('  run_ask:\n')] + legacy_text[legacy_text.index('  ask_close:\n'):])
            credential = root / 'synthetic.env'
            credential.write_text('TGBRIDGE_SECRET=synthetic-test-only\n')
            credential.chmod(0o600)
            cli('pack', 'apply', str(legacy), '--yes')
            for setting in ('base=imd', 'calls_per_hour=600', 'updates_per_hour=300', 'env_file=' + str(credential)):
                cli('source', 'set', 'tgbridge', setting)
            cli('source', 'grant', 'tgbridge', 'send', '--group', 'humans')
            before_source = client.call('GetSource', ['tgbridge'])
            before_group = client.call('GetGroup', ['humans'])
            instance = rendered('instance-pack', 18942)
            cli('pack', 'check', instance)
            cli('pack', 'apply', instance, '--yes', '--source-base', 'tgbridge=imd', '--source-env', 'tgbridge=' + str(credential))
            upgraded = client.call('GetSource', ['tgbridge'])
            self.assertIn('  ask_card:', upgraded['declaration'])
            self.assertIn('  run_ask:', upgraded['declaration'])
            self.assertIn('\nbase: http://127.0.0.1:18942\nbases:\n  odyssey: http://127.0.0.1:8941\n'
                          '  imd: http://127.0.0.1:8942\n', upgraded['declaration'])
            self.assertEqual(cli('source', 'show', 'tgbridge').splitlines()[0], 'tgbridge  http://127.0.0.1:8942 (base imd)')
            self.assertEqual((upgraded['base'], upgraded['keyed_host']), ('imd', 'http://127.0.0.1:8942'))
            self.assertEqual({k: v for k, v in upgraded.items() if k not in ('declaration', 'added_at')},
                             {k: v for k, v in before_source.items() if k not in ('declaration', 'added_at')})
            self.assertEqual(client.call('GetGroup', ['humans']), before_group)
            cli('source', 'grant', 'tgbridge', 'ask_card', '--group', 'humans')
            granted = client.call('GetGroup', ['humans'])
            cli('pack', 'apply', instance, '--yes', '--source-base', 'tgbridge=imd', '--source-env', 'tgbridge=' + str(credential))
            self.assertEqual(client.call('GetSource', ['tgbridge']), upgraded)
            self.assertEqual(client.call('GetGroup', ['humans']), granted)
            self.assertEqual(credential.read_text(), 'TGBRIDGE_SECRET=synthetic-test-only\n')
        finally:
            server.terminate()
            server.wait(timeout=10)
