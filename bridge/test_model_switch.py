"""Native handoff switches through production poll/drain, SQLite and fake Telegram."""
import copy
import ctypes
import fcntl
import io
import json
import os
from pathlib import Path
import secrets
import subprocess
import struct
import sys
import tempfile
import unittest
from unittest.mock import patch

import bridge as b
import run_asks as n
import yard_commands as y
import testyardr
from test_yard_commands import FakeTelegram, switch_picks

import model_switch as m


class Telegram(FakeTelegram):
    def __init__(self):
        super().__init__()
        self.updates = []

    def __call__(self, url, body, *args, **kwargs):
        if url.endswith('/getUpdates'):
            self.calls.append(('getUpdates', body))
            updates, self.updates = self.updates, []
            return 200, dict(ok=True, result=updates)
        return super().__call__(url, body, *args, **kwargs)


class NativeSwitchTests(unittest.TestCase):
    def setUp(self):
        binary = testyardr.binary()
        if binary is None:
            self.skipTest('native model switch tests require a core with crew_handoff_switch v1; set TGBRIDGE_TEST_YARDR')
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='ms')))
        self.server, self.home, self.command, self.env = testyardr.start(self.root, binary)
        self.addCleanup(testyardr.stop, self.server)
        self.enterContext(patch.dict(os.environ, {'HOME': self.env['HOME'], 'PATH': self.env['PATH']}))
        self.cli('init', '--harness', 'claude')
        try:
            m.capability(dict(yardr_bin=binary, yardr_home=str(self.home)), m.time.monotonic() + 10, lambda message: None)
        except y.SwitchUnavailable:
            self.fail('supplied core requires crew_handoff_switch v1; native model switch tests cannot run')
        source = self.root / 'tgbridge.yaml'
        source.write_text('name: tgbridge\nbase: https://example.invalid\ncalls:\n  health:\n    kind: read\n'
                          '    request: {method: GET, path: /health}\nhook:\n  bodies: unique\n  verify:\n'
                          '    scheme: secret\n    signature: {header: X-Secret}\n  normalize: [/bin/false]\n')
        self.cli('source', 'add', str(source))
        self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
        self.cli('crew', 'add', 'mayor', '--set', 'kind=codex', '--set', 'model=old')
        self.native = n.YardSocketClient(self.home, 'tgbridge')
        # This isolated test process is the fake dispatcher, holding only this home's daemon lock.
        self.lock = open(self.home / 'daemon.lock', 'w')
        self.addCleanup(self.lock.close)
        fcntl.flock(self.lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        start = testyardr.process_start(os.getpid())
        self.lock.write(f'{os.getpid()} {start}\n')
        self.lock.flush()
        self.old = secrets.token_hex(12)
        self.runner = subprocess.Popen([sys.executable, '-u', '-c',
            'import json,subprocess,sys\ncommand=json.loads(sys.argv[1]); env=json.loads(sys.argv[2])\n'
            'for line in sys.stdin:\n'
            ' r=subprocess.run(command+["crew","handoff","mayor","--session-generation",line.strip(),"safe synthetic handoff"],'
            'env=env,capture_output=True,text=True)\n'
            ' print(json.dumps({"status":r.returncode,"error":r.stderr}),flush=True)\n',
            json.dumps(self.command), json.dumps(self.env)], env=self.env, cwd=self.root,
            stdin=subprocess.PIPE, stdout=subprocess.PIPE, stderr=subprocess.DEVNULL, text=True)
        self.addCleanup(self.stop_runner)
        self.rpc('CrewLaunchedFor', name='mayor', user='', previous='',
                 launch=dict(kind='codex', model='old', fingerprint='f' * 64),
                 handle={'crew_generation': self.old, 'shell_pid': self.runner.pid})
        self.rpc('CrewStatusFor', name='mayor', user='', status='working')
        self.telegram = Telegram()
        self.store = b.Store(self.root / 'bridge.sqlite3', 10)
        self.picks = switch_picks()
        self.config = dict(bot_id=999, user_id=123, chat_id=123, account_id='test_bot', yard='test-city',
                           user_display_name='Pat', timezone='UTC', menu=(), agent='mayor',
                           yardr_home=str(self.home), yardr_source='tgbridge', yardr_operator='pat', yardr_bin=binary,
                           yardr_cards=True,
                           yardr_switch_model=dict(crew='mayor', timeout=10, picks=self.picks), commands=())
        self.config['commands'] = (y.switch_model_command(self.config['yardr_switch_model']),)
        self.log = self.enterContext(patch('sys.stderr', io.StringIO()))
        self.bridge = self.reopen()
        self.uid = 40

    def stop_runner(self):
        self.runner.terminate()
        self.runner.wait(timeout=5)
        self.runner.stdin.close()
        self.runner.stdout.close()

    def cli(self, *args):
        result = subprocess.run(self.command + list(args), env=self.env, cwd=self.root,
                                capture_output=True, text=True, timeout=15)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def rpc(self, method, **arguments):
        return self.native.call(method, list(arguments.values()))

    def crew(self):
        return self.rpc('GetCrew', name='mayor')

    def reopen(self, **config):
        bridge = b.Bridge(dict(self.config, **config), self.store, 'SECRET', 'CAPABILITY', request=self.telegram, state_dir=self.root)
        return bridge

    def poll(self, *updates):
        self.telegram.updates = list(updates)
        self.bridge.poll_once()

    def text(self, value='/switch_model'):
        self.uid += 1
        return dict(update_id=self.uid, message=dict(message_id=self.uid + 1000, text=value,
                    chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)}))

    def selector(self):
        self.poll(self.text())
        sent = self.telegram.of('sendMessage')[-1]
        return sent, self.telegram.next_id

    def tap(self, data, mid):
        self.uid += 1
        return dict(update_id=self.uid, callback_query=dict(id=f'q{self.uid}', data=data,
            **{'from': dict(id=123, is_bot=False)}, message=dict(message_id=mid,
            chat=dict(id=123, type='private'), **{'from': dict(id=999, is_bot=True)})))

    def pick(self, selector, pick='opus'):
        sent, mid = selector
        data = next(button['callback_data'] for row in sent['reply_markup']['inline_keyboard']
                    for button in row if button['callback_data'].endswith(':' + pick))
        return self.tap(data, mid)

    def operations(self):
        with self.store.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT value FROM model_operations')]

    def last(self):
        return self.operations()[-1]

    def edit(self):
        return self.telegram.of('editMessageText')[-1]

    def force_tap(self):
        data = self.edit()['reply_markup']['inline_keyboard'][0][0]['callback_data']
        return self.tap(data, self.edit()['message_id'])

    def handoff(self):
        self.runner.stdin.write(self.old + '\n')
        self.runner.stdin.flush()
        result = json.loads(self.runner.stdout.readline())
        self.assertEqual(result['status'], 0, result)

    def launch(self):
        row = self.last()
        op = row['observed']
        self.rpc('CrewEndedFor', name='mayor', user='', generation=self.old, reason='restart: ' + self.crew().get('restart_requested', 'handoff'), gone=False)
        new = secrets.token_hex(12)
        self.rpc('CrewLaunchedFor', name='mayor', user='', previous=self.old, launch=op['target'], handle={'crew_generation': new})
        return new

    def native_launch_pick(self, pick):
        crew = self.crew()
        target = dict(kind=pick['kind'], model=pick['model'], args=list(pick['args']))
        if pick['env_file'] is not None:
            target['env_file'] = pick['env_file']
        value = self.rpc('RequestCrewSwitch', req=dict(request_id='synthetic-' + secrets.token_hex(12),
            crew='mayor', revision=crew['revision'], session_generation=self.old,
            target=target, wait_seconds=600), actor='pat')
        self.rpc('CrewEndedFor', name='mayor', user='', generation=self.old, reason='natural exit', gone=True)
        new = secrets.token_hex(12)
        self.rpc('CrewLaunchedFor', name='mayor', user='', previous=self.old, launch=value['operation']['target'],
                 handle={'crew_generation': new})
        self.old = new
        self.rpc('CrewStatusFor', name='mayor', user='', status='working')
        return value

    def test_complete_real_native_poll_slice(self):
        self.bridge = self.reopen(yardr_cards=False)
        self.assertIsNone(self.bridge.run_asks.client)
        selector = self.selector()
        self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '456')
        with self.assertRaisesRegex(n.NativeError, 'operator_not_authorized'):
            self.bridge.model_switch.verify()
        self.poll(self.pick(selector))
        self.assertEqual(self.operations(), [])
        self.assertIn('Switch unavailable', str(self.telegram.of('answerCallbackQuery')))
        self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
        self.poll(self.pick(selector))
        row = self.last()
        self.assertEqual(row['observed']['state'], 'waiting')
        self.assertIn('Please wait for the handoff', self.edit()['text'])
        self.assertEqual(self.edit()['message_id'], selector[1])
        self.assertIn('Force apply', json.dumps(self.edit()))
        self.assertEqual(self.crew()['launch']['model'], 'old')
        self.assertEqual(self.crew()['config']['model'], 'opus')
        op = row['observed']
        self.rpc('StepCrewSwitch', name='mayor', id=op['id'], generation=op['generation'], session=self.old, step='nudged')
        self.handoff()
        self.launch()
        self.poll()
        self.assertEqual(self.last()['observed']['state'], 'running')
        self.assertIn('Current: Opus high', self.edit()['text'])
        self.assertIn('handoff requested from the session → handoff written → restarting → running on target', self.edit()['text'])
        self.assertEqual(self.edit()['reply_markup']['inline_keyboard'], [])
        self.assertEqual({edit['message_id'] for edit in self.telegram.of('editMessageText')}, {selector[1]})
        self.assertEqual(len(self.telegram.of('sendMessage')), 1)
        self.assertEqual(self.store.offset(), self.uid + 1)
        events = self.rpc('Events', bead=self.crew()['journal'], limit=100)
        counts = {kind: sum(event['kind'] == kind for event in events) for kind in
                  ('crew_started', 'crew_stopped', 'crew_switch_requested', 'crew_restart_requested')}
        self.assertEqual(counts, dict(crew_started=2, crew_stopped=1, crew_switch_requested=1, crew_restart_requested=0))
        # Exercise the macOS fixture route without procfs, retaining real native PID/start authentication.
        if sys.platform == 'linux':
            start = int(Path('/proc/self/stat').read_text().rsplit(')', 1)[1].split()[19])
            fields = [0] * 12
            fields[3] = os.getpid()
            raw = struct.pack('=12I48s6I2Q', *fields, b'', *([0] * 6), *divmod(start, 1000000))
            def pidinfo(pid, flavor, arg, buffer, size):
                self.assertEqual((pid, flavor, arg, size), (os.getpid(), 3, 0, len(raw)))
                ctypes.memmove(buffer, raw, len(raw))
                return len(raw)
            read_text = Path.read_text
            def no_proc(path, *args, **kwargs):
                if str(path).startswith('/proc/'):
                    raise FileNotFoundError('no procfs on macOS')
                return read_text(path, *args, **kwargs)
            case = NativeSwitchTests(methodName='test_complete_real_native_poll_slice')
            self.addCleanup(case.doCleanups)
            with patch.object(sys, 'platform', 'darwin'), patch.object(Path, 'read_text', no_proc), \
                    patch.object(testyardr, 'binary', return_value=self.command[0]), \
                    patch.object(ctypes, 'CDLL') as library:
                library.return_value.proc_pidinfo.side_effect = pidinfo
                case.setUp()
                case.poll(case.pick(case.selector()))
                case.handoff()
                case.launch()
                case.poll()
                self.assertEqual(case.last()['observed']['state'], 'running')
                self.assertEqual({edit['message_id'] for edit in case.telegram.of('editMessageText')}, {case.edit()['message_id']})
                library.assert_called_once_with('/usr/lib/libproc.dylib', use_errno=True)
                library.return_value.proc_pidinfo.side_effect = lambda *args: 0
                with self.assertRaisesRegex(AssertionError, 'identity unavailable'):
                    testyardr.process_start(os.getpid())
                fields[3] += 1
                raw = struct.pack('=12I48s6I2Q', *fields, b'', *([0] * 6), *divmod(start, 1000000))
                library.return_value.proc_pidinfo.side_effect = pidinfo
                with self.assertRaisesRegex(AssertionError, 'identity unavailable'):
                    testyardr.process_start(os.getpid())

    def test_force_immediate_duplicate_and_stale_after_launch(self):
        selector = self.selector()
        self.poll(self.pick(selector))
        tap = self.force_tap()
        native = y.run_yardr
        writes = []
        before_native = []
        def tracked(argv, *args, **kwargs):
            if 'restart' in argv:
                before_native.append(dict(tap=self.store.switch_tap(tap['update_id']),
                    operation=self.last(), restart=self.crew().get('restart_requested', ''),
                    calls=copy.deepcopy(self.telegram.calls)))
                writes.append(argv)
            return native(argv, *args, **kwargs)
        with patch.object(y, 'run_yardr', tracked):
            self.poll(tap)
            self.assertEqual(len(before_native), 1)
            before = before_native[0]
            self.assertEqual(before['tap']['state'], 'reserved')
            self.assertEqual(before['operation']['force_action']['action_id'], writes[0][writes[0].index('--action-id') + 1])
            self.assertEqual(before['operation']['observed']['mode'], 'handoff')
            self.assertEqual(before['restart'], '')
            calls = before['calls']
            answer = [body for method, body in calls if method == 'answerCallbackQuery'][-1]
            self.assertEqual(answer['callback_query_id'], tap['callback_query']['id'])
            edit = [body for method, body in calls if method == 'editMessageText'][-1]
            self.assertIn('Force apply accepted, please wait', edit['text'])
            self.assertIn('waits for idle', edit['text'])
            self.assertEqual(edit['reply_markup']['inline_keyboard'], [])
            self.assertEqual(edit['message_id'], selector[1])
            self.assertEqual(sum(method == 'sendMessage' for method, _ in calls), 1)
            self.assertLess(calls.index(('answerCallbackQuery', answer)), calls.index(('editMessageText', edit)))
            self.assertIn('Force apply accepted, please wait', self.edit()['text'])
            self.assertIn('Status: restart_requested', self.edit()['text'])
            self.assertGreater(self.last()['revision'], before['operation']['revision'])
            self.assertIsNone(self.last()['display_failure'])
            self.assertIn('waits for idle', self.edit()['text'])
            self.assertEqual(self.edit()['reply_markup']['inline_keyboard'], [])
            self.assertEqual(self.crew()['session_generation'], self.old)
            self.assertEqual(self.last()['observed']['mode'], 'force')
            self.bridge.inbound(tap)
            self.launch()
            self.poll()
            stale = copy.deepcopy(tap)
            stale['update_id'] = self.uid + 1
            stale['callback_query']['id'] = 'stale-force'
            self.poll(stale)
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.last()['observed']['state'], 'running')
        self.assertEqual(self.crew().get('restart_requested', ''), '')

    def test_pending_same_and_different_picks_and_replayed_update(self):
        first, second = self.selector(), self.selector()
        tap = self.pick(first)
        native = y.run_yardr
        writes = []
        def tracked(argv, *args, **kwargs):
            if '--handoff' in argv:
                writes.append(argv)
            return native(argv, *args, **kwargs)
        with patch.object(y, 'run_yardr', tracked):
            self.poll(tap)
            original = self.last()
            self.bridge.inbound(tap)
            self.poll(self.pick(second), self.pick(second, 'sol'))
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.last()['deadline'], original['deadline'])
        self.assertEqual(len(self.operations()), 1)
        self.assertIn('different pick is refused', self.telegram.of('answerCallbackQuery')[-1]['text'])
        self.assertEqual({edit['message_id'] for edit in self.telegram.of('editMessageText')}, {first[1]})

        # A native-only pending refusal survives a crash before the inbound cursor commits.
        case = NativeSwitchTests(methodName=self._testMethodName)
        try:
            case.setUp()
            selector = case.selector()
            crew = case.crew()
            case.rpc('RequestCrewSwitch', req=dict(request_id='external-switch', crew='mayor',
                revision=crew['revision'], session_generation=case.old,
                target=dict(kind='codex', model='external', args=[]), wait_seconds=600), actor='pat')
            tap = case.pick(selector)
            with patch.object(case.store, 'advance', side_effect=SystemExit), self.assertRaises(SystemExit):
                case.poll(tap)
            self.assertEqual(case.store.offset(), tap['update_id'])
            with self.subTest(stage='refusal reservation'):
                self.assertEqual((case.store.switch_tap(tap['update_id']) or {}).get('state'), 'refused')
            case.cli('crew', 'set', 'mayor', 'model=changed-externally')
            settled = case.crew()
            case.store = b.Store(case.root / 'bridge.sqlite3', 0)
            case.bridge = case.reopen()
            with patch.object(y, '_run_logged', wraps=y._run_logged) as run:
                case.poll(tap)
            self.assertFalse(any('--handoff' in call.args[1] or 'restart' in call.args[1]
                                 for call in run.call_args_list))
            self.assertEqual(case.operations(), [])
            self.assertEqual(case.crew(), settled)
            self.assertEqual(case.store.switch_tap(tap['update_id'])['state'], 'refused')
            self.assertEqual(case.store.offset(), tap['update_id'] + 1)
        finally:
            case.doCleanups()

    def test_legacy_foreign_and_changed_selectors_reserve_nothing(self):
        selector = self.selector()
        original = self.pick(selector)
        cases = []
        for change in ('user', 'bot', 'chat', 'type', 'topic', 'message', 'sender_bot', 'legacy', 'token', 'invalid-topic'):
            tap = copy.deepcopy(original)
            q = tap['callback_query']
            if change == 'user': q['from']['id'] = 456
            if change == 'sender_bot': q['from']['is_bot'] = True
            if change == 'bot': q['message']['from']['id'] = 111
            if change == 'chat': q['message']['chat']['id'] = 456
            if change == 'type': q['message']['chat']['type'] = 'group'
            if change == 'invalid-topic': q['message']['is_topic_message'] = True
            if change == 'topic': q['message'].update(message_thread_id=99, is_topic_message=True)
            if change == 'message': q['message']['message_id'] += 1
            if change == 'legacy': q['data'] = 'switch-model:opus'
            if change == 'token': q['data'] = 'switch-model:s:' + 'A' * 16 + ':opus'
            cases.append((change, tap))
        for change, tap in cases:
            with self.subTest(change=change):
                self.uid += 1
                tap['update_id'] = self.uid
                tap['callback_query']['id'] = str(self.uid)
                self.poll(tap)
        self.assertEqual(self.operations(), [])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM switch_model_taps').fetchone()[0], 0)
        for change in ('label', 'args', 'env_file', 'crew', 'timeout'):
            with self.subTest(change=change):
                config = copy.deepcopy(self.config)
                if change in ('crew', 'timeout'):
                    config['yardr_switch_model'][change] = 'other' if change == 'crew' else 20
                else:
                    picks = list(config['yardr_switch_model']['picks'])
                    picks[1][change] = {'label': 'different', 'args': ['--effort', 'low'], 'env_file': '/new.env'}[change]
                    config['yardr_switch_model']['picks'] = picks
                self.bridge = self.reopen(yardr_switch_model=config['yardr_switch_model'])
                self.poll(self.pick(selector))
                self.assertEqual(self.operations(), [])

    def test_revoked_operator_and_binding_changes_disable_force(self):
        selector = self.selector()
        other = NativeSwitchTests(methodName=self._testMethodName)
        self.addCleanup(other.doCleanups)
        other.setUp()
        other.store = self.store
        other.uid = self.uid
        other.bridge = other.reopen()
        other_selector = other.selector()
        self.uid = other.uid
        original_read = m.read_current
        def revoke_during_read(*args, **kwargs):
            value = original_read(*args, **kwargs)
            self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '456')
            return value
        with patch.object(m, 'read_current', revoke_during_read):
            self.bridge.drain([self.pick(selector)])
        self.assertEqual(self.operations(), [])
        self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
        self.poll(self.pick(selector))
        force = self.force_tap()
        self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '456')
        self.poll(force)
        self.assertIsNone(self.last()['force_action'])
        self.assertEqual(self.crew().get('restart_requested', ''), '')

        # Reopening the same ledger on another real native home cannot hide its pending owner.
        other.uid = self.uid
        before = other.crew()
        with self.store.connect() as db:
            selectors = db.execute('SELECT count(*) FROM model_selectors').fetchone()[0]
        other.poll(other.text())
        with self.subTest(stage='fresh selector on changed home'), self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM model_selectors').fetchone()[0], selectors)
            self.assertIn('already pending', other.telegram.of('sendMessage')[-1]['text'])
        with patch.object(y, '_run_logged', wraps=y._run_logged) as run:
            other.poll(other.pick(other_selector))
        self.assertFalse(any('--handoff' in call.args[1] or 'restart' in call.args[1]
                             for call in run.call_args_list))
        self.assertEqual(len(self.operations()), 1)
        self.assertEqual(other.crew(), before)
        self.assertEqual(self.store.switch_tap(other.uid)['state'], 'refused')
        self.assertIn('already pending', other.telegram.of('answerCallbackQuery')[-1]['text'])
        self.uid = other.uid
        self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
        for key, value in (('yardr_source', 'other'), ('yardr_home', str(self.root)), ('yardr_operator', 'other'),
                           ('user_id', 456), ('bot_id', 111), ('account_id', 'other')):
            with self.subTest(key=key):
                self.bridge = self.reopen(**({key: value, 'chat_id': value} if key == 'user_id' else {key: value}))
                self.uid += 1
                changed = copy.deepcopy(force)
                changed['update_id'] = self.uid
                changed['callback_query']['id'] = str(self.uid)
                self.poll(changed)
                self.assertIsNone(self.last()['force_action'])
        self.assertEqual(self.crew().get('restart_requested', ''), '')
        self.bridge = self.reopen()
        original_lookup = self.bridge.model_switch.lookup
        def revoke_during_lookup(*args, **kwargs):
            result = original_lookup(*args, **kwargs)
            self.cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '456')
            return result
        force = self.force_tap()
        with patch.object(self.bridge.model_switch, 'lookup', revoke_during_lookup):
            self.bridge.drain([force])
        self.assertIsNone(self.last()['force_action'])
        self.assertEqual(self.crew().get('restart_requested', ''), '')

    def test_crashes_before_and_after_native_acceptance_never_retry(self):
        case = NativeSwitchTests(methodName=self._testMethodName)
        try:
            case.setUp()
            selector = case.selector()
            tap = case.pick(selector)
            original = y._run_logged
            class Crash(BaseException): pass
            def crash(config, argv, *args):
                if '--handoff' in argv:
                    original(config, argv, *args)
                    raise Crash()
                return original(config, argv, *args)
            with patch.object(y, '_run_logged', crash), case.assertRaises(Crash):
                case.poll(tap)
            case.assertIsNone(case.last()['receipt'])
            case.assertEqual(case.crew()['config']['model'], 'opus')
            case.store = b.Store(case.root / 'bridge.sqlite3', 0)
            case.bridge = case.reopen()
            with patch.object(y, '_run_logged', wraps=original) as run:
                case.poll(tap)
                case.assertFalse(any('--handoff' in call.args[1] for call in run.call_args_list))
            case.assertIsNone(case.last()['observed'])
            case.assertIsNone(case.last()['receipt'])
            case.assertIn('request could not be matched', case.edit()['text'])
            case.assertEqual(case.edit()['reply_markup']['inline_keyboard'], [])
            case.assertEqual(case.store.switch_tap(tap['update_id'])['state'], 'reserved')

        finally:
            case.doCleanups()
        selector = self.selector()
        self.poll(self.pick(selector))
        original = y._run_logged
        class Crash(BaseException): pass

        # Each abandoned Force remains recorded; only a fresh tap may act after reconciliation.
        import datetime
        writes = []
        accepted = False
        def crash_force(config, argv, *args):
            if 'restart' in argv:
                if accepted:
                    writes.append(argv)
                    original(config, argv, *args)
                raise Crash()
            return original(config, argv, *args)
        for timed_out in (False, True):
            force = self.force_tap()
            before = self.last()
            with patch.object(y, '_run_logged', crash_force), self.assertRaises(Crash):
                self.poll(force)
            action = self.last()['force_action']
            self.assertIsNotNone(action)
            self.assertEqual(self.store.switch_tap(force['update_id'])['state'], 'reserved')
            self.assertEqual(self.crew().get('restart_requested', ''), '')
            self.store = b.Store(self.root / 'bridge.sqlite3', 0)
            self.bridge = self.reopen()
            with patch.object(y, 'run_yardr', return_value=(1, b'', b'', None)):
                self.poll(force)
            self.assertEqual(self.last()['force_action'], action)
            self.assertEqual(self.edit()['reply_markup']['inline_keyboard'], [])
            later = m.timestamp(before['deadline']) + datetime.timedelta(seconds=1) if timed_out else m.now()
            with patch.object(m, 'now', return_value=later), patch.object(y, '_run_logged', wraps=original) as run:
                self.poll(force)
            self.assertFalse(any('restart' in call.args[1] or '--handoff' in call.args[1] for call in run.call_args_list))
            self.assertIn('Force apply', json.dumps(self.edit()))
            self.assertIsNone(self.last()['force_action'])
            self.assertEqual(self.last()['generation'], before['generation'] + 1)
            self.assertEqual(self.last()['deadline'], before['deadline'])
            self.assertEqual(self.last()['request'], before['request'])
            self.assertEqual(self.edit()['message_id'], selector[1])
            self.assertEqual(self.bridge.model_switch.load('model_actions', 'update_id', force['update_id']), action)
            if timed_out:
                self.assertIn('timed out', self.edit()['text'])
            stale = self.tap(force['callback_query']['data'], selector[1])
            with patch.object(y, '_run_logged', wraps=original) as run:
                self.bridge.inbound(force)
                self.poll(stale)
            self.assertFalse(any('restart' in call.args[1] for call in run.call_args_list))
            self.assertIn('out of date', self.telegram.of('answerCallbackQuery')[-1]['text'])
            self.assertEqual(self.store.switch_tap(force['update_id'])['state'], 'reserved')

        force = self.force_tap()
        accepted = True
        with patch.object(y, '_run_logged', crash_force), self.assertRaises(Crash):
            self.poll(force)
        action = self.last()['force_action']
        self.store = b.Store(self.root / 'bridge.sqlite3', 0)
        self.bridge = self.reopen()
        with patch.object(y, '_run_logged', wraps=original) as run:
            self.poll(force)
        self.assertFalse(any('restart' in call.args[1] or '--handoff' in call.args[1] for call in run.call_args_list))
        self.assertEqual(self.last()['force_action'], action)
        self.assertEqual(self.last()['observed']['mode'], 'force')
        self.assertIn('Force apply accepted, please wait', self.edit()['text'])
        self.assertEqual(self.edit()['reply_markup']['inline_keyboard'], [])
        self.assertEqual(len(writes), 1)
        self.launch()
        self.poll()
        events = self.rpc('Events', bead=self.crew()['journal'], limit=100)
        counts = {kind: sum(event['kind'] == kind for event in events) for kind in
                  ('crew_started', 'crew_stopped', 'crew_switch_requested', 'crew_restart_requested')}
        self.assertEqual(counts, dict(crew_started=2, crew_stopped=1, crew_switch_requested=1, crew_restart_requested=1))
        self.assertEqual(self.last()['observed']['state'], 'running')
        self.assertEqual(self.edit()['reply_markup']['inline_keyboard'], [])
        self.assertEqual({edit['message_id'] for edit in self.telegram.of('editMessageText')}, {selector[1]})

    def test_reserved_absent_receipt_recovers_on_store_copy_without_retry(self):
        selector = self.selector()
        tap = self.pick(selector)
        original = y._run_logged
        class Crash(BaseException): pass
        def crash(config, argv, *args):
            if '--handoff' in argv:
                raise Crash()
            return original(config, argv, *args)
        with patch.object(y, '_run_logged', crash), self.assertRaises(Crash):
            self.poll(tap)
        source = self.store
        self.store = b.Store(self.root / 'recovery.sqlite3', 0)
        with source.connect() as before, self.store.connect() as after:
            before.backup(after)
        self.bridge = self.reopen()
        native = y.run_yardr
        # Only the exact native not-found response can free an unconfirmed reservation.
        for status, out, problem in ((1, b'{"error":"crew switch request: not found"}', None),
                                     (3, b'{"error":"other"}', None),
                                     (3, b'{"error":"crew switch request: not found","extra":1}', None),
                                     (3, b'not JSON', None),
                                     (3, b'{"error":"crew switch request: not found"}', 'timeout')):
            with self.subTest(status=status, out=out, problem=problem):
                with patch.object(y, 'run_yardr', return_value=(status, out, b'PRIVATE', problem)):
                    self.poll()
                self.assertTrue(self.last()['active'])
                self.assertIn('unknown', self.edit()['text'])
        with patch.object(y, 'run_yardr', wraps=native) as calls:
            self.poll(tap)
        self.assertFalse(any('--handoff' in call.args[0] or 'restart' in call.args[0] for call in calls.call_args_list))
        self.assertIsNone(self.last()['receipt'])
        self.assertFalse(self.last()['active'])
        self.assertTrue(self.last()['refused'])
        self.assertEqual(self.store.switch_tap(tap['update_id'])['state'], 'refused')
        self.assertEqual(self.edit()['reply_markup']['inline_keyboard'], [])
        self.assertEqual(self.crew()['config']['model'], 'old')
        self.assertIn('not admitted', self.edit()['text'])
        with source.connect() as db:
            self.assertEqual(db.execute('SELECT active FROM model_operations').fetchone()[0], 1)
        self.poll(self.pick(self.selector()))
        self.assertEqual(self.last()['observed']['state'], 'waiting')
        pending = self.last()
        self.bridge = self.reopen()
        with patch.object(y, 'run_yardr', return_value=(3, b'{"error":"crew switch request: not found"}', b'', None)):
            self.poll()
        self.assertTrue(self.last()['active'])
        self.assertEqual(self.last()['receipt'], pending['receipt'])
        self.assertEqual(self.last()['observed'], pending['observed'])
        self.poll()
        self.assertEqual(self.last()['observed']['state'], 'waiting')

    def test_native_refusal_stale_revision_and_unknown_receipt_collision(self):
        # A real definition change since display refuses before any reservation.
        for setting in ('model=changed', 'args=["--effort","low"]', 'prompt=changed', 'env_file=other.env', 'session'):
            with self.subTest(setting=setting):
                case = NativeSwitchTests(methodName=self._testMethodName)
                try:
                    case.setUp()
                    before = case.crew()
                    selector = case.selector()
                    if setting == 'session':
                        crew = case.crew()
                        case.rpc('CrewEndedFor', name='mayor', user='', generation=case.old, reason='exit', gone=True)
                        case.rpc('CrewLaunchedFor', name='mayor', user='', previous=case.old,
                                 launch=crew['launch'], handle={'crew_generation': secrets.token_hex(12)})
                    else:
                        case.cli('crew', 'set', 'mayor', setting)
                    native = y.run_yardr
                    tap = case.pick(selector)
                    with patch.object(y, 'run_yardr', wraps=native) as calls:
                        with patch.object(case.store, 'advance', side_effect=SystemExit), case.assertRaises(SystemExit):
                            case.poll(tap)
                    case.assertFalse(any('--handoff' in call.args[0] for call in calls.call_args_list))
                    case.assertEqual(case.operations(), [])
                    case.assertIn('configuration or session changed', case.telegram.of('answerCallbackQuery')[-1]['text'])
                    case.assertEqual((case.store.switch_tap(tap['update_id']) or {}).get('state'), 'refused')
                    if setting != 'session':
                        key = setting.split('=', 1)[0]
                        if key in before['config']:
                            case.cli('crew', 'set', 'mayor', key + '=' + str(before['config'][key]))
                        else:
                            case.cli('crew', 'set', 'mayor', '--unset', key)
                    case.bridge = case.reopen()
                    with patch.object(y, 'run_yardr', wraps=native) as calls:
                        case.poll(tap)
                    case.assertFalse(any('--handoff' in call.args[0] for call in calls.call_args_list))
                    case.assertEqual(case.operations(), [])
                finally:
                    case.doCleanups()

        # JSON errors are bounded, escaped and redacted; malformed replies remain ambiguous.
        for output in (json.dumps(dict(error='<refused & unsafe>\n/synthetic/secret.env ' + 'x' * 500)).encode(),
                       b'not JSON', b'{"error":false}', b'{"error":"bad","extra":1}'):
            with self.subTest(admission=output):
                case = NativeSwitchTests(methodName=self._testMethodName)
                try:
                    case.setUp()
                    config = copy.deepcopy(case.config['yardr_switch_model'])
                    config['picks'][1]['env_file'] = '/synthetic/secret.env'
                    case.bridge = case.reopen(yardr_switch_model=config)
                    selector = case.selector()
                    native = y.run_yardr
                    def refuse(argv, *args, **kwargs):
                        if '--handoff' in argv:
                            return 1, output, b'PRIVATE', None
                        return native(argv, *args, **kwargs)
                    with patch.object(y, 'run_yardr', refuse):
                        case.bridge.drain([case.pick(selector)])
                    if output.startswith(b'{"error": "'):
                        case.assertFalse(case.last()['active'])
                        case.assertIn('&lt;refused &amp; unsafe&gt;', case.edit()['text'])
                        case.assertLessEqual(len(case.last()['admission_error']), 320)
                        case.assertNotIn('/synthetic/secret.env', case.edit()['text'] + case.log.getvalue())
                        case.assertIn('<refused & unsafe>', case.log.getvalue())
                    else:
                        case.assertTrue(case.last()['active'])
                        case.assertIn('unknown', case.edit()['text'])
                    case.assertNotIn('PRIVATE', case.edit()['text'] + case.log.getvalue())
                finally:
                    case.doCleanups()
        selector = self.selector()
        original = y.run_yardr
        errors = []
        def stale(argv, *args, **kwargs):
            if '--handoff' in argv:
                self.rpc('CrewStatusFor', name='mayor', user='', status='idle')
            result = original(argv, *args, **kwargs)
            if '--handoff' in argv:
                errors.append((result[0], json.loads(result[1])))
            return result
        with patch.object(y, 'run_yardr', stale):
            self.poll(self.pick(selector))
        self.assertIsNone(self.last()['receipt'])
        self.assertEqual(self.crew()['config']['model'], 'old')
        error = 'crew switch expected revision changed: stale'
        self.assertEqual(errors, [(1, dict(error=error))])
        self.assertFalse(self.last()['active'])
        self.assertTrue(self.last()['refused'])
        self.assertIn(error, self.edit()['text'])
        self.assertIn(error, self.log.getvalue())
        self.assertEqual(self.edit()['reply_markup']['inline_keyboard'], [])

        # A card survives ordinary activity, but never a changed definition or session.
        selector = self.selector()
        self.rpc('CrewStatusFor', name='mayor', user='', status='working')
        revision = self.crew()['revision']
        self.poll(self.pick(selector))
        self.assertEqual(self.last()['request']['revision'], revision)
        self.assertEqual(self.last()['observed']['state'], 'waiting')

    def test_natural_exit_external_supersession_and_failed_start(self):
        self.poll(self.pick(self.selector()))
        row = self.last()
        self.rpc('CrewEndedFor', name='mayor', user='', generation=self.old, reason='the session is gone', gone=True)
        self.rpc('CrewLaunchFailedFor', name='mayor', user='', generation=self.old, reason='synthetic', backoff=1000000000)
        self.poll()
        self.assertIn('Start failed', self.edit()['text'])
        # Let the native retry launch the saved target without a fabricated handoff.
        self.rpc('CrewLaunchedFor', name='mayor', user='', previous=self.old,
                 launch=row['observed']['target'], handle={'crew_generation': secrets.token_hex(12)})
        self.poll()
        self.assertEqual(self.last()['observed']['state'], 'running')
        self.assertIn('Previous session ended without handoff', self.edit()['text'])
        self.assertNotIn('handoff written', self.edit()['text'])
        self.assertEqual(self.edit()['reply_markup']['inline_keyboard'], [])

        for state in ('superseded', 'stopped'):
            for observation in ('poll', 'reopen', 'missing-launch'):
                with self.subTest(state=state, observation=observation):
                    case = NativeSwitchTests(methodName=self._testMethodName)
                    try:
                        case.setUp()
                        selector = case.selector()
                        case.poll(case.pick(selector))
                        force = case.force_tap()
                        if state == 'superseded':
                            case.cli('crew', 'set', 'mayor', 'model=external')
                        case.rpc('CrewEndedFor', name='mayor', user='', generation=case.old,
                                 reason='external stop', gone=False)
                        case.rpc('CrewLaunchedFor', name='mayor', user='', previous=case.old,
                                 launch=dict(kind='codex', model='external', fingerprint='e' * 64),
                                 handle={'crew_generation': secrets.token_hex(12)})
                        native = case.crew()
                        self.assertEqual(native['switch']['state'], state)
                        self.assertEqual(native['launch']['model'], 'external')
                        if observation == 'reopen':
                            case.store = b.Store(case.root / 'bridge.sqlite3', 0)
                            case.bridge = case.reopen()
                        read_current = m.read_current
                        def missing_launch(*args, **kwargs):
                            crew, parsed = read_current(*args, **kwargs)
                            crew.pop('launch', None)
                            return crew, parsed
                        with patch.object(m, 'read_current', missing_launch if observation == 'missing-launch' else read_current), \
                                patch.object(y, '_run_logged', wraps=y._run_logged) as run:
                            case.poll()
                            self.assertEqual(case.last()['observed']['state'], state)
                            self.assertFalse(case.last()['active'])
                            expected = 'Current: codex · external' if observation == 'poll' else 'Current launch unavailable.'
                            self.assertIn(expected, case.edit()['text'])
                            self.assertNotIn('Current: codex · old', case.edit()['text'])
                            self.assertEqual(case.edit()['message_id'], selector[1])
                            self.assertEqual(case.edit()['reply_markup']['inline_keyboard'], [])
                            edits = len(case.telegram.of('editMessageText'))
                            case.poll()
                            self.assertEqual(len(case.telegram.of('editMessageText')), edits)
                            case.poll(force)
                            self.assertNotIn('Current: codex · old', case.edit()['text'])
                            self.assertEqual(case.edit()['reply_markup']['inline_keyboard'], [])
                        self.assertFalse(any('--handoff' in call.args[1] or 'restart' in call.args[1]
                                             for call in run.call_args_list))
                        self.assertEqual(case.crew(), native)
                        events = case.rpc('Events', bead=native['journal'], limit=100)
                        counts = {kind: sum(event['kind'] == kind for event in events) for kind in
                                  ('crew_started', 'crew_stopped', 'crew_switch_requested', 'crew_restart_requested')}
                        self.assertEqual(counts, dict(crew_started=2, crew_stopped=1,
                                                     crew_switch_requested=1, crew_restart_requested=0))
                    finally:
                        case.doCleanups()

    def test_handoff_force_race_and_terminal_refresh_after_crash(self):
        for recovery in ('current', 'receipt', 'saved-terminal'):
            with self.subTest(recovery=recovery):
                case = NativeSwitchTests(methodName=self._testMethodName)
                try:
                    case.setUp()
                    case.rpc('SetContextFor', name='mayor', user='', pct=42, window=200000)
                    selector = case.selector()
                    case.poll(case.pick(selector))
                    self.assertIn('Session: working', case.edit()['text'])
                    self.assertIn('42% of 200000', case.edit()['text'])
                    tap = case.force_tap()
                    case.handoff()
                    case.poll(tap)
                    self.assertEqual(case.crew()['restart_requested'], 'handoff')
                    case.launch()
                    case.rpc('CrewStatusFor', name='mayor', user='', status='idle')
                    case.rpc('SetContextFor', name='mayor', user='', pct=1, window=1000000)
                    native = case.crew()
                    if recovery == 'saved-terminal':
                        with patch.object(case.bridge.model_switch, 'display', side_effect=SystemExit), self.assertRaises(SystemExit):
                            case.poll()
                        self.assertFalse(case.last()['active'])
                    if recovery != 'current':
                        case.store = b.Store(case.root / 'bridge.sqlite3', 0)
                        case.bridge = case.reopen()
                    with patch.object(y, '_run_logged', wraps=y._run_logged) as run:
                        case.poll()
                        self.assertFalse(case.last()['active'])
                        self.assertEqual(case.last()['observed']['state'], 'running')
                        self.assertEqual(case.edit()['reply_markup']['inline_keyboard'], [])
                        self.assertIn('Running on target', case.edit()['text'])
                        self.assertIn('Current: Opus high', case.edit()['text'])
                        self.assertNotIn('Session: working', case.edit()['text'])
                        self.assertNotIn('42% of 200000', case.edit()['text'])
                        if recovery == 'receipt':
                            self.assertIn('Session: unknown', case.edit()['text'])
                            self.assertIn('Context unavailable.', case.edit()['text'])
                            self.assertEqual(case.last()['telemetry'], {})
                        else:
                            self.assertIn('Session: idle', case.edit()['text'])
                            self.assertIn('1% of 1000000', case.edit()['text'])
                        settled = copy.deepcopy(case.edit())
                        reads = len(run.call_args_list)
                        self.assertEqual(reads, 0 if recovery == 'saved-terminal' else 1)
                        case.poll()
                        self.assertEqual(case.edit(), settled)
                        self.assertEqual(len(run.call_args_list), reads)
                    self.assertEqual({edit['message_id'] for edit in case.telegram.of('editMessageText')}, {selector[1]})
                    self.assertEqual(case.crew(), native)
                    events = case.rpc('Events', bead=native['journal'], limit=100)
                    counts = {kind: sum(event['kind'] == kind for event in events) for kind in
                              ('crew_started', 'crew_stopped', 'crew_switch_requested', 'crew_restart_requested')}
                    self.assertEqual(counts, dict(crew_started=2, crew_stopped=1,
                                                 crew_switch_requested=1, crew_restart_requested=0))
                finally:
                    case.doCleanups()

    def test_poll_batch_drains_force_before_refresh_and_no_resurrection(self):
        selector, other = self.selector(), self.selector()
        tap = self.pick(selector)
        self.poll(tap)
        force = self.force_tap()
        conflict = self.pick(other, 'sol')
        same = self.pick(other)
        order = []
        original = self.bridge.model_switch.refresh
        def refresh():
            order.append(('refresh', self.store.offset()))
            return original()
        self.telegram.calls.clear()
        with patch.object(self.bridge.model_switch, 'refresh', refresh):
            self.poll(tap, force, conflict, same)
        self.assertEqual(order, [('refresh', same['update_id'] + 1)])
        self.assertEqual(self.last()['observed']['mode'], 'force')
        for edit in self.telegram.of('editMessageText'):
            self.assertEqual(edit['reply_markup']['inline_keyboard'], [])
        self.assertEqual({edit['message_id'] for edit in self.telegram.of('editMessageText')}, {selector[1]})

    def test_cli_menu_and_zero_write_dry_run(self):
        self.assertEqual([c['command'] for c in self.bridge.set_commands()], ['switch_model', 'decide'])
        before = self.crew()
        for pick in ('opus', 'sol'):
            result = m.dry_run(self.config, pick, self.bridge._log)
            self.assertIn('600-second', result)
            self.assertIn('crew restart --switch', result)
        with self.assertRaisesRegex(y.SwitchUnavailable, 'unknown pick'):
            m.dry_run(self.config, 'private-value', self.bridge._log)
        self.assertEqual(self.crew(), before)
        self.assertEqual(self.operations(), [])
        with self.store.connect() as db:
            self.assertEqual(db.execute('SELECT count(*) FROM model_selectors').fetchone()[0], 0)
            self.assertEqual(db.execute('SELECT count(*) FROM switch_model_taps').fetchone()[0], 0)
        self.poll(self.pick(self.selector()))
        self.assertIn('acknowledges the existing operation', m.dry_run(self.config, 'opus', self.bridge._log))
        self.assertIn('different pick is refused', m.dry_run(self.config, 'sol', self.bridge._log))

    def test_old_ledger_positional_rows_and_reopen(self):
        with self.store.connect() as db:
            db.execute("INSERT INTO switch_model_taps VALUES (20, 'sol', 'mayor', 'done', 'old')")
        self.poll(self.pick(self.selector()))
        with self.store.connect() as db:
            self.assertEqual(len(db.execute('PRAGMA table_info(switch_model_taps)').fetchall()), 5)
            db.execute("INSERT INTO switch_model_taps VALUES (21, 'opus', 'mayor', 'unknown', 'old')")
        self.store = b.Store(self.root / 'bridge.sqlite3', 0)
        self.bridge = self.reopen()
        self.poll()
        self.assertEqual(self.store.switch_tap(20), dict(pick='sol', crew='mayor', state='done'))
        self.assertEqual(self.store.switch_tap(21)['state'], 'unknown')
        self.assertEqual(self.last()['observed']['state'], 'waiting')

    def test_message_edit_ambiguity_reuses_exact_key_and_deleted_message(self):
        self.poll(self.pick(self.selector()))
        self.handoff()
        self.launch()
        failures = []
        actual = self.bridge.telegram
        def ambiguous(method, body, *args, **kwargs):
            if method == 'editMessageText':
                failures.append(copy.deepcopy(body))
                raise b.TransportError('synthetic')
            return actual(method, body, *args, **kwargs)
        with patch.object(self.bridge, 'telegram', ambiguous):
            self.poll()
        self.assertTrue(self.last()['display_failure'])
        revision = self.last()['revision']
        self.poll()
        self.assertEqual(self.last()['revision'], revision)
        self.assertEqual(failures[-1]['text'], self.edit()['text'])
        self.assertIsNone(self.last()['display_failure'])
        count = len(self.telegram.of('editMessageText'))
        self.poll()
        self.assertEqual(len(self.telegram.of('editMessageText')), count)
        selector = self.selector()
        sends = len(self.telegram.of('sendMessage'))
        actual = self.bridge.telegram
        def deleted(method, body, *args, **kwargs):
            if method == 'editMessageText':
                return 400, dict(ok=False, error_code=400, description='message to edit not found')
            return actual(method, body, *args, **kwargs)
        with patch.object(self.bridge, 'telegram', deleted):
            self.poll(self.pick(selector, 'sol'))
        self.assertTrue(self.last()['display_failure'])
        self.assertEqual(len(self.telegram.of('sendMessage')), sends)
        # An older failed terminal card must not consume a newer switch's observation budget.
        def natural_launch():
            generation = self.crew()['session_generation']
            self.rpc('CrewEndedFor', name='mayor', user='', generation=generation, reason='the session is gone', gone=True)
            self.rpc('CrewLaunchedFor', name='mayor', user='', previous=generation,
                     launch=self.last()['target'], handle={'crew_generation': secrets.token_hex(12)})
        natural_launch()
        with patch.object(self.bridge, 'telegram', deleted):
            self.poll()
        previous = self.last()
        self.assertFalse(previous['active'])
        with patch.object(self.bridge, 'telegram', deleted):
            selector = self.selector()
            self.poll(self.pick(selector, 'opus'))
        newer = self.last()
        natural_launch()
        clock = m.time.monotonic
        elapsed, reads, failed = [0], [], []
        native = y.run_yardr
        def tracked(argv, *args, **kwargs):
            reads.append(argv)
            return native(argv, *args, **kwargs)
        def slow_deleted(method, body, *args, **kwargs):
            if method == 'editMessageText' and body['message_id'] == previous['message']:
                elapsed[0] += 4
                failed.append(body['message_id'])
                return 400, dict(ok=False, error_code=400, description='message to edit not found')
            return actual(method, body, *args, **kwargs)
        with patch.object(self.bridge, 'telegram', slow_deleted), patch.object(y, 'run_yardr', tracked), \
                patch.object(m.time, 'monotonic', side_effect=lambda: clock() + elapsed[0]):
            self.poll()
            self.assertEqual(self.last()['observed']['state'], 'running')
            self.assertTrue(reads)
            self.assertEqual(self.edit()['message_id'], newer['message'])
            self.assertIn('Running on target', self.edit()['text'])
            self.assertEqual(self.edit()['reply_markup']['inline_keyboard'], [])
            reads.clear()
            self.poll()
            self.assertEqual(reads, [])
        self.assertEqual(failed, [previous['message'], previous['message']])
        self.assertTrue(self.bridge.model_switch.load('model_operations', 'token', previous['token'])['display_failure'])
        self.assertEqual(len(self.telegram.of('sendMessage')), sends + 1)
        # A crash after terminal observation must not strand the owed edit behind historical failures.
        with patch.object(self.bridge, 'telegram', slow_deleted):
            selector = self.selector()
            self.poll(self.pick(selector, 'sol'))
        recovering = self.last()
        self.assertTrue(self.edit()['reply_markup']['inline_keyboard'])
        natural_launch()
        display = self.bridge.model_switch.display
        def crash_before_terminal_edit(row):
            if row['token'] == recovering['token']:
                raise SystemExit()
            return display(row)
        with patch.object(self.bridge.model_switch, 'display', crash_before_terminal_edit), self.assertRaises(SystemExit):
            self.poll()
        self.assertFalse(self.last()['active'])
        self.assertEqual(self.last()['observed']['state'], 'running')
        self.assertTrue(self.edit()['reply_markup']['inline_keyboard'])
        native_before = self.crew()
        events = self.rpc('Events', bead=native_before['journal'], limit=100)
        legacy = self.bridge.model_switch.load('model_operations', 'token', previous['token'])
        legacy.pop('display_attempted_at', None)
        self.bridge.model_switch.save(legacy)
        delivered = len(self.telegram.of('editMessageText'))
        reads.clear()
        elapsed[0] = 0
        failed.clear()
        for _ in range(3):
            self.store = b.Store(self.root / 'bridge.sqlite3', 0)
            self.bridge = self.reopen()
            with patch.object(self.bridge, 'telegram', slow_deleted), patch.object(y, 'run_yardr', tracked), \
                    patch.object(m.time, 'monotonic', side_effect=lambda: clock() + elapsed[0]):
                self.poll()
        recovered = [edit for edit in self.telegram.of('editMessageText')[delivered:]
                     if edit['message_id'] == recovering['message']]
        self.assertEqual(len(recovered), 1)
        self.assertIn('Running on target', recovered[0]['text'])
        self.assertEqual(recovered[0]['reply_markup']['inline_keyboard'], [])
        self.assertIsNone(self.last()['display_failure'])
        self.assertEqual(self.last()['generation'], recovering['generation'])
        self.assertGreater(self.last()['revision'], recovering['revision'])
        self.assertEqual(reads, [])
        self.assertTrue(failed)
        historical = self.bridge.model_switch.load('model_operations', 'token', previous['token'])
        self.assertTrue(historical['display_failure'])
        self.assertEqual(historical['revision'], legacy['revision'])
        self.assertEqual(self.crew(), native_before)
        self.assertEqual(self.rpc('Events', bead=native_before['journal'], limit=100), events)
        counts = {kind: sum(event['kind'] == kind for event in events) for kind in
                  ('crew_started', 'crew_stopped', 'crew_switch_requested', 'crew_restart_requested')}
        self.assertEqual(counts, dict(crew_started=5, crew_stopped=4, crew_switch_requested=4, crew_restart_requested=0))
        self.assertEqual(len(self.telegram.of('sendMessage')), sends + 2)
        self.assertEqual(self.store.offset(), self.uid + 1)

        # Slow failing selectors and historical operations must not starve later completed-selector edits.
        self.old = self.crew()['session_generation']
        self.native_launch_pick(self.picks[1])
        first_sel = self.selector()
        first_sel_id = first_sel[1]
        second_sel_id = [None]
        second_sel_attempts = []
        third_sel_id = [None]
        third_sel_attempts = []

        def slow_and_transient(method, body, *args, **kwargs):
            if method == 'editMessageText':
                mid = body['message_id']
                if mid in (previous['message'], first_sel_id):
                    elapsed[0] += 4
                    failed.append(mid)
                    return 400, dict(ok=False, error_code=400, description='message to edit not found')
                if mid == second_sel_id[0]:
                    second_sel_attempts.append(mid)
                    if len(second_sel_attempts) == 1:
                        raise b.TransportError('one transient selector delivery failure')
                if mid == third_sel_id[0]:
                    third_sel_attempts.append(mid)
                    if len(third_sel_attempts) == 1:
                        raise b.TransportError('one transient selector delivery failure vs operation')
            return actual(method, body, *args, **kwargs)

        ops_before = len(self.operations())
        def poll_slow(*updates):
            with patch.object(self.bridge, 'telegram', slow_and_transient), \
                    patch.object(m.time, 'monotonic', side_effect=lambda: clock() + elapsed[0]):
                self.poll(*updates)

        def selector_slow():
            poll_slow(self.text())
            return self.telegram.of('sendMessage')[-1], self.telegram.next_id

        poll_slow(self.pick(first_sel))
        second_sel = selector_slow()
        second_sel_id[0] = second_sel[1]
        second_tap = self.pick(second_sel)
        poll_slow(second_tap)
        self.assertEqual(len(second_sel_attempts), 1)
        for _ in range(3):
            self.store = b.Store(self.root / 'bridge.sqlite3', 0)
            self.bridge = self.reopen()
            poll_slow()
        second_edits = [edit for edit in self.telegram.of('editMessageText') if edit['message_id'] == second_sel_id[0]]
        self.assertEqual(len(second_edits), 1)
        self.assertIn('Already running Opus high; nothing to switch.', second_edits[0]['text'])
        self.assertEqual(second_edits[0]['reply_markup']['inline_keyboard'], [])
        self.assertEqual(len(self.operations()), ops_before)
        self.assertEqual(self.store.switch_tap(second_tap['update_id'])['state'], 'done')
        self.assertEqual(self.store.offset(), second_tap['update_id'] + 1)

        # Historical slow inactive operation must not starve a later completed selector either.
        third_sel = selector_slow()
        third_sel_id[0] = third_sel[1]
        third_tap = self.pick(third_sel)
        poll_slow(third_tap)
        self.assertEqual(len(third_sel_attempts), 1)
        with self.store.connect() as db:
            for token, raw in db.execute('SELECT token, value FROM model_selectors').fetchall():
                item = json.loads(raw)
                if item.get('message') == third_sel_id[0]:
                    item.pop('display_attempted_at', None)
                    db.execute('UPDATE model_selectors SET value=? WHERE token=?', (m.canonical(item), token))
        for _ in range(4):
            self.store = b.Store(self.root / 'bridge.sqlite3', 0)
            self.bridge = self.reopen()
            poll_slow()
        third_edits = [edit for edit in self.telegram.of('editMessageText') if edit['message_id'] == third_sel_id[0]]
        self.assertEqual(len(third_edits), 1)
        self.assertIn('Already running Opus high; nothing to switch.', third_edits[0]['text'])
        self.assertEqual(third_edits[0]['reply_markup']['inline_keyboard'], [])
        self.assertEqual(len(self.operations()), ops_before)

    def test_timeout_card_preserves_target_no_automatic_force_and_explicit_force(self):
        self.poll(self.pick(self.selector()))
        row = self.last()
        import datetime
        later = m.timestamp(row['deadline']) + datetime.timedelta(seconds=1)
        original = y.run_yardr
        writes = []
        def tracked(argv, *args, **kwargs):
            if '--handoff' in argv or 'restart' in argv:
                writes.append(argv)
            return original(argv, *args, **kwargs)
        with patch.object(m, 'now', return_value=later), patch.object(y, 'run_yardr', tracked):
            self.poll()
            self.assertIn('timed out', self.edit()['text'])
            self.assertIn('Force apply', json.dumps(self.edit()))
            self.assertEqual(writes, [])
            self.assertEqual(self.crew()['session_generation'], self.old)
            self.assertEqual(self.crew()['config']['model'], 'opus')
            self.poll(self.force_tap())
        self.assertEqual(len(writes), 1)
        self.assertEqual(self.last()['observed']['mode'], 'force')
        self.assertEqual(self.last()['deadline'], row['deadline'])

    def test_exact_actual_launch_noop_and_same_kind_model_effort_change(self):
        # No previous bridge receipt is needed for an exact native launch/config match.
        case = NativeSwitchTests(methodName=self._testMethodName)
        try:
            case.setUp()
            case.native_launch_pick(case.picks[1])
            crew = case.crew()
            row = dict(pick=case.picks[1], request=dict(request_id='synthetic-identical',
                revision=crew['revision'], session_generation=case.old))
            result = subprocess.run(m.admission_argv(case.config, row), env=case.env, cwd=case.root,
                                    capture_output=True, timeout=15)
            case.assertEqual(result.returncode, 0, result.stdout)
            case.assertEqual(json.loads(result.stdout)['operation']['state'], 'waiting')
            case.native_launch_pick(case.picks[1])
            tap = case.pick(case.selector())
            native = y.run_yardr
            before = case.crew()
            with patch.object(y, 'run_yardr', wraps=native) as calls:
                case.poll(tap)
            notice = 'Already running Opus high; nothing to switch.'
            case.assertFalse(any('--handoff' in call.args[0] or 'restart' in call.args[0] for call in calls.call_args_list))
            case.assertEqual(case.operations(), [])
            case.assertEqual(case.crew(), before)
            case.assertIn(notice, case.edit()['text'])
            case.assertEqual(case.edit()['reply_markup']['inline_keyboard'], [])
            case.assertEqual(case.telegram.of('answerCallbackQuery')[-1]['text'], notice)
            # Desired args equality cannot acknowledge an old launch with different effort.
            case.cli('crew', 'set', 'mayor', 'args=["--effort","low"]')
            case.poll(case.pick(case.selector()))
            case.assertEqual(case.last()['observed']['state'], 'waiting')
        finally:
            case.doCleanups()
        for mode in ('absolute', 'relative', 'tilde', 'empty-args'):
            with self.subTest(native_launch=mode):
                case = NativeSwitchTests(methodName=self._testMethodName)
                try:
                    case.setUp()
                    env = case.root / 'crew.env'
                    env.write_text('')
                    env.chmod(0o600)
                    if mode != 'empty-args':
                        path = {'absolute': str(env), 'relative': 'crew.env', 'tilde': '~/crew.env'}[mode]
                        case.cli('crew', 'set', 'mayor', 'env_file=' + path)
                    config = copy.deepcopy(case.config['yardr_switch_model'])
                    pick = config['picks'][1]
                    if mode == 'empty-args':
                        pick['args'] = []
                    case.cli('crew', 'set', 'mayor', 'prompt=<synthetic & prompt>\u2028🌲')
                    case.bridge = case.reopen(yardr_switch_model=config)
                    case.native_launch_pick(pick)
                    case.poll(case.pick(case.selector()))
                    case.assertEqual(case.operations(), [])
                    case.assertIn('nothing to switch.', case.edit()['text'])
                    case.assertNotIn('crew.env', json.dumps(case.telegram.calls) + case.log.getvalue())
                    # Matching kind/model/config args cannot hide a stale actual effort.
                    pick['args'] = ['--effort', 'low']
                    case.cli('crew', 'set', 'mayor', 'args=["--effort","low"]')
                    case.bridge = case.reopen(yardr_switch_model=config)
                    case.poll(case.pick(case.selector()))
                    case.assertEqual(case.last()['observed']['state'], 'waiting')
                finally:
                    case.doCleanups()
        for explicit_env in (False, True):
            with self.subTest(native_launch='relative-server-cwd', explicit_env=explicit_env):
                case = NativeSwitchTests(methodName=self._testMethodName)
                popen = subprocess.Popen
                server_cwd = None

                def start(argv, *args, **kwargs):
                    nonlocal server_cwd
                    if len(argv) >= 2 and argv[-2:] == ['server', 'run']:
                        server_cwd = Path(kwargs['cwd']) / 'native-cwd'
                        server_cwd.mkdir()
                        kwargs['cwd'] = server_cwd
                    return popen(argv, *args, **kwargs)

                try:
                    with patch.object(subprocess, 'Popen', start):
                        case.setUp()
                    for path in (case.root / 'crew.env', server_cwd / 'crew.env',
                                 case.root / 'other.env', server_cwd / 'other.env'):
                        path.write_text('')
                        path.chmod(0o600)
                    case.cli('crew', 'set', 'mayor', 'env_file=crew.env')
                    config = copy.deepcopy(case.config['yardr_switch_model'])
                    pick = config['picks'][1]
                    if explicit_env:
                        pick['env_file'] = 'crew.env'
                    case.bridge = case.reopen(yardr_switch_model=config)
                    case.native_launch_pick(pick)
                    selector = case.selector()
                    native = y.run_yardr
                    with patch.object(y, 'run_yardr', wraps=native) as calls:
                        case.poll(case.pick(selector))
                    notice = 'Already running Opus high; nothing to switch.'
                    case.assertFalse(any('--handoff' in call.args[0] or 'restart' in call.args[0]
                                         for call in calls.call_args_list))
                    case.assertEqual(case.operations(), [])
                    case.assertIn(notice, case.edit()['text'])
                    case.assertEqual(case.edit()['reply_markup']['inline_keyboard'], [])
                    case.assertEqual(case.telegram.of('answerCallbackQuery')[-1]['text'], notice)
                    case.assertNotIn('crew.env', json.dumps(case.telegram.calls) + case.log.getvalue())

                    # Revision/session guard still refuses if config changes after selector display.
                    stale_selector = case.selector()
                    case.cli('crew', 'set', 'mayor', 'env_file=other.env')
                    case.poll(case.pick(stale_selector))
                    case.assertEqual(case.operations(), [])
                    case.assertIn('configuration or session changed', case.telegram.of('answerCallbackQuery')[-1]['text'])

                    # Mismatched pidfile start or different launch env_file never grants false equality.
                    case.cli('crew', 'set', 'mayor', 'env_file=crew.env')
                    start_file = case.home / 'server.pid.start'
                    saved_start = start_file.read_text()
                    start_file.write_text('0\n')
                    try:
                        self.assertFalse(m.launch_matches(case.config, case.crew(), y.parse_crew(
                            json.dumps(dict(crew=case.crew())).encode())))
                    finally:
                        start_file.write_text(saved_start)
                    if sys.platform != 'darwin':
                        import builtins
                        pid = int((case.home / 'server.pid').read_text().strip())
                        start_val = int(saved_start.strip())
                        fields = [0] * 12
                        fields[3] = pid
                        raw = struct.pack('=12I48s6I2Q', *fields, b'', *([0] * 6), *divmod(start_val, 1000000))
                        def pidinfo(qpid, flavor, arg, buffer, size):
                            self.assertEqual((qpid, flavor, arg, size), (pid, 3, 0, len(raw)))
                            ctypes.memmove(buffer, raw, len(raw))
                            return len(raw)
                        orig_open = builtins.open
                        orig_readlink = m.os.readlink
                        def no_proc_open(path, *args, **kwargs):
                            if str(path).startswith('/proc/'):
                                raise FileNotFoundError('no procfs on macOS')
                            return orig_open(path, *args, **kwargs)
                        def no_proc_link(path, *args, **kwargs):
                            if str(path).startswith('/proc/'):
                                raise FileNotFoundError('no procfs on macOS')
                            return orig_readlink(path, *args, **kwargs)
                        darwin_selector = case.selector()
                        with patch.object(sys, 'platform', 'darwin'), \
                                patch('builtins.open', no_proc_open), \
                                patch.object(m.os, 'readlink', no_proc_link), \
                                patch.object(ctypes, 'CDLL') as library:
                            library.return_value.proc_pidinfo.side_effect = pidinfo
                            self.assertEqual(os.path.realpath(m.server_cwd(case.config)), os.path.realpath(server_cwd))
                            with patch.object(y, 'run_yardr', wraps=native) as calls:
                                case.poll(case.pick(darwin_selector))
                            self.assertFalse(any('--handoff' in call.args[0] or 'restart' in call.args[0]
                                                 for call in calls.call_args_list))
                            self.assertEqual(case.operations(), [])
                            self.assertIn(notice, case.edit()['text'])
                            self.assertEqual(case.edit()['reply_markup']['inline_keyboard'], [])
                            self.assertEqual(case.telegram.of('answerCallbackQuery')[-1]['text'], notice)
                            start_file.write_text('0\n')
                            try:
                                self.assertFalse(m.launch_matches(case.config, case.crew(), y.parse_crew(
                                    json.dumps(dict(crew=case.crew())).encode())))
                            finally:
                                start_file.write_text(saved_start)
                            with patch.object(m.shutil, 'which', return_value=None):
                                self.assertIsNone(m.server_cwd(case.config))
                    case.cli('crew', 'set', 'mayor', 'env_file=other.env')
                    if explicit_env:
                        pick['env_file'] = 'other.env'
                    case.bridge = case.reopen(yardr_switch_model=config)
                    case.poll(case.pick(case.selector()))
                    case.assertEqual(case.last()['observed']['state'], 'waiting')
                finally:
                    case.doCleanups()
        # Desired equality alone must still admit a switch from the old actual launch.
        self.cli('crew', 'set', 'mayor', 'kind=codex', 'model=gpt-6.1-sol',
                 'args=' + json.dumps(list(self.picks[0]['args'])))
        self.poll(self.pick(self.selector(), 'sol'))
        self.assertEqual(self.last()['observed']['state'], 'waiting')
        self.assertEqual(self.last()['observed']['target']['kind'], 'codex')
        self.handoff()
        self.launch()
        self.poll()
        before = len(self.operations())
        self.poll(self.pick(self.selector(), 'sol'))
        self.assertEqual(len(self.operations()), before)
        self.assertIn('Already running GPT-6.1 Sol high; nothing to switch.', self.telegram.of('answerCallbackQuery')[-1]['text'])

        def relaunch():
            previous = self.crew()['session_generation']
            self.rpc('CrewEndedFor', name='mayor', user='', generation=previous, reason='external exit', gone=True)
            crew = self.crew()
            launch = (crew['switch']['target'] if crew['config']['model'] == 'gpt-6.1-sol' else
                      dict(kind='codex', model='external-model', fingerprint='e' * 64))
            self.rpc('CrewLaunchedFor', name='mayor', user='', previous=previous,
                     launch=launch, handle={'crew_generation': secrets.token_hex(12)})
            self.rpc('CrewStatusFor', name='mayor', user='', status='working')

        for seam in ('cursor', 'answer', 'display'):
            with self.subTest(crash=seam):
                self.cli('crew', 'set', 'mayor', 'model=gpt-6.1-sol')
                relaunch()
                tap = self.pick(self.selector(), 'sol')
                if seam == 'cursor':
                    crash = patch.object(self.store, 'advance', side_effect=SystemExit)
                elif seam == 'answer':
                    crash = patch.object(self.bridge.model_switch, 'answer', side_effect=SystemExit)
                else:
                    crash = patch.object(self.bridge.model_switch, 'display_selector', side_effect=SystemExit)
                with crash, self.assertRaises(SystemExit):
                    self.poll(tap)
                self.assertEqual(self.store.offset(), tap['update_id'])
                self.cli('crew', 'set', 'mayor', 'model=external-model')
                relaunch()
                external = self.crew()
                events = self.rpc('Events', bead=external['journal'], limit=100)
                self.bridge = self.reopen()
                native = y.run_yardr
                with patch.object(y, 'run_yardr', wraps=native) as calls:
                    self.poll()
                    self.assertIn('Already running GPT-6.1 Sol high; nothing to switch.', self.edit()['text'])
                    self.poll(tap)
                self.assertEqual(len(self.operations()), before)
                self.assertFalse(any('--handoff' in call.args[0] or 'restart' in call.args[0]
                                     for call in calls.call_args_list))
                self.assertEqual(self.crew(), external)
                self.assertEqual(self.rpc('Events', bead=external['journal'], limit=100), events)
                self.assertEqual(self.store.switch_tap(tap['update_id']), dict(pick='sol', crew='mayor', state='done'))
                self.assertEqual(self.telegram.of('answerCallbackQuery')[-1]['text'], 'Already running GPT-6.1 Sol high; nothing to switch.')
                self.assertEqual(self.store.offset(), tap['update_id'] + 1)

    def test_missing_and_stale_telemetry_sanitized_and_payload_limits(self):
        config = copy.deepcopy(self.config['yardr_switch_model'])
        config['picks'][1]['id'] = 'x' * 32
        config['picks'][1]['label'] = '<target & model>'
        self.bridge = self.reopen(yardr_switch_model=config)
        selector = self.selector()
        for row in selector[0]['reply_markup']['inline_keyboard']:
            self.assertLessEqual(len(row[0]['callback_data'].encode()), 64)
        self.poll(self.pick(selector, 'x' * 32))
        self.assertIn('&lt;target &amp; model&gt;', self.edit()['text'])
        self.assertLessEqual(n.units(self.edit()['text']), 4096)
        self.assertIn('Context unavailable', self.edit()['text'])
        row = self.last()
        row['telemetry_at'] = '2020-01-01T00:00:00Z'
        self.bridge.model_switch.save(row)
        self.bridge.model_switch.display(row)
        self.assertIn('stale observation', self.edit()['text'])
        self.rpc('SetContextFor', name='mayor', user='', pct=42, window=200000)
        self.poll()
        self.assertIn('42% of 200000', self.edit()['text'])
        self.assertIn('read ', self.edit()['text'])

    def test_context_over_100_accepted_and_rendered(self):
        self.rpc('SetContextFor', name='mayor', user='', pct=42, window=200000)
        original = y.run_yardr
        def over_hundred(argv, *args, **kwargs):
            status, out, err, problem = original(argv, *args, **kwargs)
            if 'show' in argv:
                try:
                    value = json.loads(out)
                except ValueError:
                    return status, out, err, problem
                if isinstance(value, dict) and 'crew' in value:
                    value['crew']['context_pct'] = 102
                    value['crew']['context_window'] = 200000
                    return status, json.dumps(value).encode(), err, problem
            return status, out, err, problem
        with patch.object(y, 'run_yardr', over_hundred):
            try:
                crew, _parsed = m.read_current(self.config, m.time.monotonic() + 10, lambda message: None)
            except y.SwitchUnavailable:
                self.fail('read_current refuses context_pct 102')
            selector = self.selector()
            self.poll(self.pick(selector))
        self.assertEqual(crew['context_pct'], 102)
        self.assertIn('102% of 200000', self.edit()['text'])
        for bad in (-1, 'x'):
            def corrupt(argv, *args, bad=bad, **kwargs):
                status, out, err, problem = original(argv, *args, **kwargs)
                if 'show' in argv:
                    try:
                        value = json.loads(out)
                    except ValueError:
                        return status, out, err, problem
                    if isinstance(value, dict) and 'crew' in value:
                        value['crew']['context_pct'] = bad
                        return status, json.dumps(value).encode(), err, problem
                return status, out, err, problem
            with patch.object(y, 'run_yardr', corrupt):
                with self.assertRaises(y.SwitchUnavailable):
                    m.read_current(self.config, m.time.monotonic() + 10, lambda message: None)

    def test_bad_capability_status_and_output_never_reserve_or_disclose(self):
        selector = self.selector()
        original = y.run_yardr
        for mode in ('unsupported', 'invalid-json', 'unknown-status', 'invalid-launch', 'too-large'):
            with self.subTest(mode=mode):
                def corrupt(argv, *args, **kwargs):
                    status, out, err, problem = original(argv, *args, **kwargs)
                    if 'capabilities' in argv and mode == 'unsupported':
                        return 0, b'{"crew_handoff_switch":2}', b'PRIVATE', None
                    if 'capabilities' in argv and mode == 'invalid-json':
                        return 0, b'PRIVATE', b'PRIVATE', None
                    if 'show' in argv and mode == 'unknown-status':
                        value = json.loads(out)
                        value['crew']['status'] = 'PRIVATE'
                        return 0, json.dumps(value).encode(), b'PRIVATE', None
                    if 'show' in argv and mode == 'invalid-launch':
                        value = json.loads(out)
                        value['crew']['launch']['fingerprint'] = 'PRIVATE'
                        return 0, json.dumps(value).encode(), b'PRIVATE', None
                    if 'show' in argv and mode == 'too-large':
                        return -9, b'PRIVATE', b'PRIVATE', 'too_large'
                    return status, out, err, problem
                with patch.object(y, 'run_yardr', corrupt):
                    self.bridge.drain([self.pick(selector)])
                self.assertEqual(self.operations(), [])
                self.assertNotIn('PRIVATE', json.dumps(self.telegram.calls) + self.log.getvalue())

        def not_started(argv, *args, **kwargs):
            if '--handoff' in argv:
                return None, b'', b'', 'not_started'
            return original(argv, *args, **kwargs)
        with patch.object(y, 'run_yardr', not_started):
            tap = self.pick(selector)
            self.bridge.drain([tap])
        self.assertTrue(self.last()['refused'])
        self.assertFalse(self.last()['active'])
        self.assertIn('Waited 0s', self.edit()['text'])
        self.assertNotIn('600s', self.edit()['text'])
        self.assertEqual(self.crew()['config']['model'], 'old')
        self.bridge = self.reopen()
        with patch.object(self.bridge, '_answer_callback', wraps=self.bridge._answer_callback) as answer, \
                patch.object(y, '_run_logged', wraps=y._run_logged) as run:
            self.bridge.inbound(tap)
        self.assertIn('Switch refused;', answer.call_args.args[2])
        self.assertIn('original card', answer.call_args.args[2])
        self.assertFalse(any('--handoff' in call.args[1] or 'restart' in call.args[1]
                             for call in run.call_args_list))
        self.assertEqual(len(self.operations()), 1)
        self.assertEqual(self.store.switch_tap(tap['update_id'])['state'], 'refused')
        self.assertIn('Switch refused;', self.edit()['text'])

    def test_receipt_conflicts_stay_unknown_and_confirmed_receipts_reconcile(self):
        case = NativeSwitchTests(methodName=self._testMethodName)
        try:
            case.setUp()
            selector = case.selector()
            original = y.run_yardr
            def conflicting(argv, *args, **kwargs):
                status, out, err, problem = original(argv, *args, **kwargs)
                if '--handoff' in argv:
                    value = json.loads(out)
                    value['operation']['target']['kind'] = 'codex'
                    return status, json.dumps(value).encode(), err, problem
                return status, out, err, problem
            with patch.object(y, 'run_yardr', conflicting):
                case.bridge.drain([case.pick(selector)])
            case.assertIsNone(case.last()['receipt'])
            case.assertEqual(case.edit()['reply_markup']['inline_keyboard'], [])
            request_id = case.last()['request']['request_id']
            case.bridge = case.reopen()
            with patch.object(y, 'run_yardr', wraps=original) as calls:
                case.poll()
            case.assertTrue(any(request_id in call.args[0] for call in calls.call_args_list))
            case.assertFalse(any('--handoff' in call.args[0] for call in calls.call_args_list))
            case.assertIsNone(case.last()['receipt'])
            case.assertIn('request could not be matched', case.edit()['text'])
            case.assertEqual(case.edit()['reply_markup']['inline_keyboard'], [])

        finally:
            case.doCleanups()
        self.poll(self.pick(self.selector()))
        request_id = self.last()['request']['request_id']

        # Corrupt native JSON at the transport boundary, retaining real poll/store handling.
        waiting = json.loads(self.cli('crew', 'switch', 'show', request_id, '--json'))
        run = y._run_logged
        def crash_force(config, argv, *args):
            if 'restart' in argv:
                raise SystemExit()
            return run(config, argv, *args)
        with patch.object(y, '_run_logged', crash_force), self.assertRaises(SystemExit):
            self.bridge.drain([self.force_tap()])
        pending = self.last()
        self.handoff()
        self.launch()
        running = json.loads(self.cli('crew', 'switch', 'show', request_id, '--json'))
        timed_out = copy.deepcopy(waiting)
        timed_out['operation'].update(state='timed_out', timed_out_at=waiting['operation']['deadline'])
        refused = copy.deepcopy(waiting)
        # Refusal has no v1 operation state: a claimed refused receipt must remain unknown.
        refused['operation'].update(state='refused', unresolved=False, reason='PRIVATE')
        required = {'request_id', 'existing', 'operation', 'id', 'generation', 'crew', 'actor',
                    'previous_generation', 'target', 'kind', 'model', 'fingerprint', 'mode', 'state',
                    'unresolved', 'accepted_at', 'deadline'}
        optional = dict(nudged_at=waiting['operation']['accepted_at'],
                        timed_out_at=waiting['operation']['deadline'],
                        handoff_written_at=running['operation']['handoff_written_at'],
                        handoff_note=running['operation']['handoff_note'],
                        restarting_at=running['operation']['restarting_at'],
                        running_at=running['operation']['running_at'],
                        new_generation=running['operation']['new_generation'],
                        previous_ended_without_handoff=True, reason='PRIVATE')
        cases = []
        for state, sample in (('waiting', waiting), ('timed_out', timed_out), ('running', running), ('refused', refused)):
            if state == 'refused':
                with self.assertRaises(y.SwitchUnavailable):
                    m.receipt(sample, pending)
            else:
                self.assertEqual(m.receipt(sample, pending), sample)
            complete = copy.deepcopy(sample)
            complete['operation'].update({key: value for key, value in optional.items() if key not in complete['operation']})
            fields = [((), key) for key in complete]
            fields += [(('operation',), key) for key in complete['operation']]
            fields += [(('operation', 'target'), key) for key in complete['operation']['target']]
            for path, key in fields:
                parent = complete
                for part in path:
                    parent = parent[part]
                value = parent[key]
                wrong = [None, [], {}, 7, True, 'PRIVATE']
                for replacement in wrong:
                    if type(replacement) is type(value):
                        continue
                    changed = copy.deepcopy(complete)
                    dest = changed
                    for part in path:
                        dest = dest[part]
                    dest[key] = replacement
                    cases.append((state, path + (key,), repr(replacement), changed, None))
                changed = copy.deepcopy(complete)
                dest = changed
                for part in path:
                    dest = dest[part]
                del dest[key]
                # Optional omissions are legal until the milestone has been observed.
                history = None
                if key not in required:
                    history = copy.deepcopy(pending)
                    history['observed'][key] = value
                cases.append((state, path + (key,), 'missing', changed, history))
            for key in ('mode', 'state'):
                changed = copy.deepcopy(complete)
                changed['operation'][key] = 'PRIVATE'
                cases.append((state, ('operation', key), 'unknown enum', changed, None))
            for path, key, replacement in ((('operation',), 'id', 'another-operation'),
                                           (('operation',), 'generation', 'another-generation'),
                                           (('operation', 'target'), 'fingerprint', '0' * 64)):
                changed = copy.deepcopy(complete)
                dest = changed
                for part in path:
                    dest = dest[part]
                dest[key] = replacement
                cases.append((state, path + (key,), 'changed native identity', changed, None))
            for path in ((), ('operation',), ('operation', 'target')):
                changed = copy.deepcopy(complete)
                dest = changed
                for part in path:
                    dest = dest[part]
                dest['unexpected'] = 'PRIVATE'
                cases.append((state, path + ('unexpected',), 'unknown field', changed, None))
            for value in (-1, 2**64):
                changed = copy.deepcopy(complete)
                changed['operation']['handoff_note'] = value
                cases.append((state, ('operation', 'handoff_note'), 'outside uint64', changed, None))
        for state, field, fault, changed, history in cases:
            for route in ('lookup', 'current') if field[0] == 'operation' else ('lookup',):
                with self.subTest(state=state, field=field, fault=fault, route=route):
                    baseline = copy.deepcopy(history or pending)
                    presentation = self.last()
                    baseline['revision'], baseline['display'] = presentation['revision'], presentation['display']
                    self.bridge.model_switch.save(baseline)
                    self.bridge.model_switch.recovered = {baseline['token']} if route == 'current' else set()
                    def response(argv, *args, **kwargs):
                        self.assertNotIn('--handoff', argv)
                        self.assertNotIn('restart', argv)
                        if route == 'lookup':
                            self.assertIn(request_id, argv)
                            value = changed
                        else:
                            self.assertIn('show', argv)
                            value = dict(crew=dict(self.crew(), switch=changed.get('operation')))
                        return 0, json.dumps(value).encode(), b'PRIVATE', None
                    with patch.object(y, 'run_yardr', response):
                        self.poll()
                    saved = self.last()
                    self.assertTrue(saved['active'])
                    self.assertEqual(saved['observed'], baseline['observed'])
                    self.assertEqual(saved['receipt'], baseline['receipt'])
                    self.assertEqual(saved['force_action'], baseline['force_action'])
                    self.assertEqual(saved['generation'], baseline['generation'])
                    self.assertIn('unavailable/stale', self.edit()['text'])
                    self.assertNotIn('Running on target.', self.edit()['text'])
                    self.assertEqual(self.edit()['message_id'], pending['message'])
                    self.assertEqual(self.edit()['reply_markup']['inline_keyboard'], [])
                    self.assertNotIn('PRIVATE', json.dumps(self.telegram.calls) + self.log.getvalue())
        pending['revision'], pending['display'] = self.last()['revision'], self.last()['display']
        self.bridge.model_switch.save(pending)
        self.bridge.model_switch.recovered.clear()
        self.poll()
        self.assertFalse(self.last()['active'])
        self.assertEqual(self.last()['observed']['state'], 'running')
        events = self.rpc('Events', bead=self.crew()['journal'], limit=100)
        counts = {kind: sum(event['kind'] == kind for event in events) for kind in
                  ('crew_started', 'crew_stopped', 'crew_switch_requested', 'crew_restart_requested')}
        self.assertEqual(counts, dict(crew_started=2, crew_stopped=1, crew_switch_requested=1, crew_restart_requested=0))

    def test_crash_after_telegram_write_reapplies_only_identical_edit(self):
        self.poll(self.pick(self.selector()))
        self.handoff()
        self.launch()
        original = self.store.finish
        def crash(key, result):
            if key.startswith('model-switch:'):
                raise SystemExit()
            return original(key, result)
        with patch.object(self.store, 'finish', crash), self.assertRaises(SystemExit):
            self.poll()
        before = copy.deepcopy(self.edit())
        revision = self.last()['revision']
        self.store = b.Store(self.root / 'bridge.sqlite3', 0)
        self.bridge = self.reopen()
        self.poll()
        self.assertEqual(self.edit(), before)
        self.assertEqual(self.last()['revision'], revision)
        self.assertEqual(self.last()['observed']['state'], 'running')

    def test_concurrent_owners_and_force_actions_are_reserved_once(self):
        import concurrent.futures
        selector = self.selector()
        tap = self.pick(selector)
        bridges = [self.reopen(), self.reopen()]
        original = y.run_yardr
        writes = []
        def tracked(argv, *args, **kwargs):
            if '--handoff' in argv or 'restart' in argv:
                writes.append(argv)
            return original(argv, *args, **kwargs)
        with patch.object(y, 'run_yardr', tracked):
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                list(pool.map(lambda bridge: bridge.inbound(tap), bridges))
            self.assertEqual(len(writes), 1)
            tap = self.force_tap()
            with concurrent.futures.ThreadPoolExecutor(2) as pool:
                list(pool.map(lambda bridge: bridge.inbound(tap), bridges))
        self.assertEqual(len(writes), 2)
        self.assertEqual(len(self.operations()), 1)
        self.assertEqual(self.last()['observed']['mode'], 'force')

    def test_env_omission_preserved_and_explicit_pick_applied(self):
        for mode in ('relative', 'absent', 'explicit-relative'):
            with self.subTest(env=mode):
                case = NativeSwitchTests(methodName=self._testMethodName)
                try:
                    case.setUp()
                    self.assertNotEqual(Path.cwd(), case.root)
                    env_file = case.root / 'crew.env'
                    env_file.write_text('')
                    env_file.chmod(0o600)
                    if mode == 'relative':
                        case.cli('crew', 'set', 'mayor', 'env_file=crew.env')
                    elif mode == 'explicit-relative':
                        config = copy.deepcopy(case.config['yardr_switch_model'])
                        config['picks'][1]['env_file'] = 'crew.env'
                        case.bridge = case.reopen(yardr_switch_model=config)
                    selector = case.selector()
                    case.bridge.drain([case.pick(selector)])
                    row = case.last()
                    native = json.loads(case.cli('crew', 'switch', 'show', row['request']['request_id'], '--json'))
                    self.assertIsNotNone(row['receipt'])
                    self.assertEqual(row['receipt'], native)
                    self.assertEqual(row['observed']['target'], native['operation']['target'])
                    self.assertIn('Force apply', json.dumps(case.edit()))
                    self.assertNotIn('outcome unknown', case.edit()['text'])
                    self.assertIn('Target launch: claude · opus', case.edit()['text'])
                    self.assertIn('Target effort: unknown · env: unknown.', case.edit()['text'])
                    self.assertEqual(case.crew()['config'].get('env_file'), None if mode == 'absent' else 'crew.env')
                    if mode != 'explicit-relative':
                        self.assertNotIn('env_file', row['request']['target'])
                    case.bridge = case.reopen(**({'yardr_switch_model': config} if mode == 'explicit-relative' else {}))
                    case.poll()
                    self.assertTrue(case.last()['admission_confirmed'])
                    self.assertEqual(case.last()['receipt'], native)
                    self.assertEqual(case.last()['target'], native['operation']['target'])
                    self.assertIn('Force apply', json.dumps(case.edit()))
                    case.handoff()
                    case.launch()
                    if mode == 'relative':
                        case.cli('crew', 'set', 'mayor', 'prompt=changed before observation')
                    case.poll()
                    self.assertFalse(case.last()['active'])
                    self.assertEqual(case.last()['observed']['state'], 'running')
                    self.assertIn('Running on target.', case.edit()['text'])
                    self.assertEqual(case.edit()['reply_markup']['inline_keyboard'], [])
                    self.assertEqual({edit['message_id'] for edit in case.telegram.of('editMessageText')}, {selector[1]})
                    self.assertEqual(len(case.telegram.of('sendMessage')), 1)
                    self.assertNotIn('crew.env', json.dumps(case.telegram.calls) + case.log.getvalue())
                    events = case.rpc('Events', bead=case.crew()['journal'], limit=100)
                    counts = {kind: sum(event['kind'] == kind for event in events) for kind in
                              ('crew_started', 'crew_stopped', 'crew_switch_requested', 'crew_restart_requested')}
                    self.assertEqual(counts, dict(crew_started=2, crew_stopped=1, crew_switch_requested=1, crew_restart_requested=0))
                    before = len(case.operations())
                    case.poll(case.pick(case.selector()))
                    if mode != 'relative':
                        self.assertEqual(len(case.operations()), before)
                        self.assertIn('Already running Opus high; nothing to switch.', case.telegram.of('answerCallbackQuery')[-1]['text'])
                        # A matching kind/model/args cannot hide a changed preserved launch input.
                        case.cli('crew', 'set', 'mayor', 'prompt=synthetic changed prompt')
                        case.poll(case.pick(case.selector()))
                    self.assertEqual(len(case.operations()), before + 1)
                    self.assertEqual(case.last()['observed']['state'], 'waiting')
                finally:
                    case.doCleanups()
        env_file = self.root / 'crew.env'
        env_file.write_text('')
        env_file.chmod(0o600)
        self.cli('crew', 'set', 'mayor', 'env_file=' + str(env_file))
        self.poll(self.pick(self.selector()))
        self.assertNotIn('env_file', self.last()['request']['target'])
        self.assertEqual(self.crew()['config']['env_file'], str(env_file))
        self.assertNotIn(str(env_file), json.dumps(self.telegram.calls) + self.log.getvalue())
        self.handoff()
        self.launch()
        self.poll()
        other = self.root / 'other.env'
        other.write_text('')
        other.chmod(0o600)
        config = copy.deepcopy(self.config['yardr_switch_model'])
        config['picks'][0]['env_file'] = str(other)
        self.bridge = self.reopen(yardr_switch_model=config)
        self.poll(self.pick(self.selector(), 'sol'))
        self.assertEqual(self.last()['request']['target']['env_file'], str(other))
        self.assertEqual(self.crew()['config']['env_file'], str(other))
        self.assertNotIn(str(other), json.dumps(self.telegram.calls) + self.log.getvalue())

    def test_native_request_id_and_edit_ledger_key_collisions(self):
        for changed in ('args', 'env_file'):
            with self.subTest(competing_target=changed):
                case = NativeSwitchTests(methodName=self._testMethodName)
                try:
                    case.setUp()
                    selector = case.selector()
                    native = y.run_yardr
                    competing, statuses = [], []
                    def interleave(argv, *args, **kwargs):
                        if '--handoff' in argv:
                            request = copy.deepcopy(case.last()['request'])
                            request['target'][changed] = ['--effort', 'low'] if changed == 'args' else ''
                            competing.append(case.rpc('RequestCrewSwitch', req=request, actor='pat'))
                        result = native(argv, *args, **kwargs)
                        if '--handoff' in argv:
                            statuses.append(result[0])
                        return result
                    tap = case.pick(selector)
                    with patch.object(y, 'run_yardr', interleave):
                        case.bridge.drain([tap])
                    self.assertNotEqual(statuses, [0])
                    reserved = case.last()
                    self.assertIsNone(reserved['receipt'])
                    self.assertEqual(case.store.switch_tap(tap['update_id'])['state'], 'unknown')
                    forged = case.tap(f'switch-model:f:{reserved["token"]}:1', selector[1])
                    for launched in (False, True):
                        if launched:
                            case.handoff()
                            case.rpc('CrewEndedFor', name='mayor', user='', generation=case.old, reason='handoff', gone=False)
                            case.rpc('CrewLaunchedFor', name='mayor', user='', previous=case.old,
                                     launch=competing[0]['operation']['target'], handle={'crew_generation': secrets.token_hex(12)})
                            # A previous bridge could have adopted this lookup without admission proof.
                            legacy = case.last()
                            legacy.pop('admission_confirmed', None)
                            legacy['receipt'] = competing[0]
                            legacy['observed'] = competing[0]['operation']
                            case.bridge.model_switch.save(legacy)
                        case.store = b.Store(case.root / 'bridge.sqlite3', 0)
                        case.bridge = case.reopen()
                        with patch.object(y, 'run_yardr', wraps=native) as calls:
                            case.poll(tap, forged)
                            case.poll()
                        self.assertFalse(any('--handoff' in call.args[0] or 'restart' in call.args[0] for call in calls.call_args_list))
                        self.assertTrue(case.last()['active'])
                        self.assertIsNone(case.last()['receipt'])
                        self.assertIsNone(case.last()['observed'])
                        self.assertIsNone(case.last()['force_action'])
                        self.assertEqual(case.last()['request'], reserved['request'])
                        self.assertEqual(case.last()['deadline'], reserved['deadline'])
                        self.assertIn('request could not be matched', case.edit()['text'])
                        self.assertIn('operator reconciliation required', case.edit()['text'])
                        self.assertNotIn('Running on target.', case.edit()['text'])
                        self.assertEqual(case.edit()['reply_markup']['inline_keyboard'], [])
                        self.assertEqual(case.store.switch_tap(tap['update_id'])['state'], 'unknown')
                        self.assertIsNone(case.store.switch_tap(forged['update_id']))
                    self.assertEqual({edit['message_id'] for edit in case.telegram.of('editMessageText')}, {selector[1]})
                    self.assertEqual(len(case.telegram.of('sendMessage')), 1)
                    events = case.rpc('Events', bead=case.crew()['journal'], limit=100)
                    counts = {kind: sum(event['kind'] == kind for event in events) for kind in
                              ('crew_started', 'crew_stopped', 'crew_switch_requested', 'crew_restart_requested')}
                    self.assertEqual(counts, dict(crew_started=2, crew_stopped=1, crew_switch_requested=1, crew_restart_requested=0))
                finally:
                    case.doCleanups()
        collision = 'tg-switch-' + 'a' * 48
        crew = self.crew()
        self.rpc('RequestCrewSwitch', req=dict(request_id=collision, crew='mayor', revision=crew['revision'],
            session_generation=self.old, target=dict(kind='codex', model='external', args=[]), wait_seconds=600), actor='pat')
        self.cli('crew', 'set', 'mayor', 'model=changed-externally')
        original = self.crew()
        selector = self.selector()
        with patch.object(m.secrets, 'token_hex', return_value='a' * 48):
            self.poll(self.pick(selector))
        self.assertEqual(self.last()['request']['request_id'], collision)
        self.assertIsNone(self.last()['receipt'])
        self.assertEqual(self.crew(), original)
        self.assertEqual(self.edit()['reply_markup']['inline_keyboard'], [])
        # A conflicting edit/ask key is a local display failure, never a replacement send.
        row = self.last()
        desired = self.bridge.model_switch.render(row)
        row['display'] = None
        self.bridge.model_switch.save(row)
        key = f'model-switch:{row["token"]}:message:1:revision:{row["revision"] + 1}'
        with self.store.connect() as db:
            db.execute('INSERT INTO run_ask_keys VALUES (?, ?, ?)', (key, 'other', '{}'))
        count = len(self.telegram.of('sendMessage'))
        self.bridge.model_switch.display(row)
        self.assertTrue(self.last()['display_failure'])
        self.assertEqual(len(self.telegram.of('sendMessage')), count)

    def test_dry_run_cli_and_unsupported_zero_writes(self):
        from test_bridge import write_settings, run_main
        path = write_settings(self.root)
        text = path.read_text()
        text = text.replace('[yardr]\n', '[yardr]\nhome = ' + json.dumps(str(self.home)) + '\noperator = "pat"\ncards = true\nbin = ' + json.dumps(self.config['yardr_bin']) + '\n')
        text += '\n[yardr.switch_model]\ncrew = "mayor"\n[[yardr.switch_model.picks]]\nid = "opus"\nlabel = "Opus"\nkind = "claude"\nmodel = "opus"\nargs = []\n'
        path.write_text(text)
        native = self.crew()
        out = io.StringIO()
        with patch('sys.stdout', out), patch.object(b, 'Store', side_effect=AssertionError('dry run opened ledger')):
            run_main(self, 'telegram-bridge', '--settings', str(path), 'switch-model', '--dry-run', 'opus')
        self.assertIn('600-second', out.getvalue())
        self.assertEqual(self.crew(), native)
        self.assertEqual(self.operations(), [])
        self.assertEqual(self.telegram.calls, [])
        with patch.object(y, 'run_yardr', return_value=(0, b'{"crew_handoff_switch":0}', b'PRIVATE', None)):
            with self.assertRaisesRegex(y.SwitchUnavailable, 'requires crew_handoff_switch v1'):
                m.dry_run(self.config, 'opus', self.bridge._log)
        self.assertEqual(self.operations(), [])

    def test_dry_run_active_same_different_and_unknown_zero_writes(self):
        with self.assertRaisesRegex(y.SwitchUnavailable, 'unknown pick'):
            m.dry_run(self.config, 'missing-pick', self.bridge._log)
        self.assertEqual(self.operations(), [])
        self.assertEqual(self.telegram.calls, [])
        self.poll(self.pick(self.selector(), 'opus'))
        before_crew = self.crew()
        before_ops = self.operations()
        before_calls = list(self.telegram.calls)
        same = m.dry_run(self.config, 'opus', self.bridge._log)
        other = m.dry_run(self.config, 'sol', self.bridge._log)
        self.assertIn('acknowledges the existing operation/card', same)
        self.assertIn('different pick is refused', other)
        self.assertEqual(self.crew(), before_crew)
        self.assertEqual(self.operations(), before_ops)
        self.assertEqual(self.telegram.calls, before_calls)


class SwitchUpgradeBoundaryTests(unittest.TestCase):
    """Real Store + fake native client boundary checks that run without a native binary."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / 'yard'
        self.home.mkdir(mode=0o700)
        self.stub = self.root / 'yardr-stub'
        self.stub.write_text('#!/bin/sh\nexit 0\n')
        self.stub.chmod(0o700)
        self.store = b.Store(self.root / 'bridge.sqlite3', 10)
        self.telegram = Telegram()
        self.picks = list(switch_picks())
        self.picks[1] = dict(self.picks[1], id='p' * 32)
        self.config = dict(
            bot_id=999, user_id=123, chat_id=123, account_id='test_bot', yard='test-city',
            user_display_name='Pat', timezone='UTC', menu=(), agent='mayor',
            yardr_home=str(self.home), yardr_source='tgbridge', yardr_operator='pat', yardr_bin=str(self.stub),
            yardr_cards=True,
            yardr_switch_model=dict(crew='mayor', timeout=10, picks=tuple(self.picks)), commands=(),
        )
        self.config['commands'] = (y.switch_model_command(self.config['yardr_switch_model']),)
        self.log = self.enterContext(patch('sys.stderr', io.StringIO()))
        self.bridge = b.Bridge(self.config, self.store, 'SECRET', 'CAPABILITY',
                               request=self.telegram, state_dir=self.root)
        self.native_calls = []

    def _fake_yardr(self, argv, home, env, deadline):
        self.native_calls.append(list(argv))
        if argv[-4:] == ['crew', 'switch', 'capabilities', '--json']:
            return 0, b'{"crew_handoff_switch":1}', b'', None
        if argv[-3:] == ['crew', 'show', 'mayor'] or argv[-4:] == ['crew', 'show', 'mayor', '--json']:
            payload = {'crew': {
                'name': 'mayor', 'revision': 'rev1', 'session_generation': 'gen1',
                'state': 'running', 'status': 'working',
                'config': {'kind': 'codex', 'model': 'old', 'args': []},
                'handle': {'kind': 'codex'},
                'launch': {'kind': 'codex', 'model': 'old', 'fingerprint': 'f' * 64},
            }}
            return 0, json.dumps(payload).encode('utf-8'), b'', None
        raise AssertionError(f'unexpected mutating/unknown native call: {argv}')

    def test_selector_longest_callback_fits_64_bytes_and_replay_in_model_actions_fails_closed(self):
        selector_update = dict(
            update_id=40,
            message=dict(message_id=1040, text='/switch_model', chat=dict(id=123, type='private'),
                         **{'from': dict(id=123, is_bot=False)}),
        )
        with patch.object(y, 'run_yardr', side_effect=self._fake_yardr), patch.object(n, 'verify_operator'):
            self.bridge.drain([selector_update])
            sent = self.telegram.of('sendMessage')[-1]
            longest = sent['reply_markup']['inline_keyboard'][1][0]['callback_data']
            self.assertEqual(len(longest.encode('utf-8')), 64)
            self.assertTrue(m.PICK.fullmatch(longest))

            tap = dict(
                update_id=41,
                callback_query=dict(
                    id='cb-41', data=sent['reply_markup']['inline_keyboard'][0][0]['callback_data'],
                    **{'from': dict(id=123, is_bot=False)},
                    message=dict(message_id=self.telegram.next_id, chat=dict(id=123, type='private'),
                                 **{'from': dict(id=999, is_bot=True)}),
                ),
            )
            with self.store.connect() as db:
                db.execute('INSERT INTO model_actions VALUES (?, ?, ?)',
                           (tap['update_id'], 'prior-token', m.canonical(dict(token='prior-token', kind='admit'))))
            self.native_calls.clear()
            self.bridge.drain([tap])
        self.assertFalse(any('--handoff' in call or 'restart' in call for call in self.native_calls))
        self.assertEqual(self.bridge.model_switch.active(), [])
        self.assertIsNone(self.store.switch_tap(tap['update_id']))
        answer = self.telegram.of('answerCallbackQuery')[-1]
        self.assertTrue(answer['show_alert'])
        self.assertIn('nothing retried', answer['text'])


class PreviewSmokeTests(unittest.TestCase):
    def test_preview_scripts_render_required_states_without_secret_leak(self):
        binary = testyardr.binary()
        if binary is None:
            self.skipTest('native model switch tests require a core with crew_handoff_switch v1; set TGBRIDGE_TEST_YARDR')
        root = Path(__file__).resolve().parent.parent
        env = dict(os.environ, TGBRIDGE_TEST_YARDR=binary)
        html_proc = subprocess.run(
            [sys.executable, '-B', str(root / 'scripts' / 'preview_model_switch.py')],
            capture_output=True, text=True, check=True, env=env, timeout=120)
        html = html_proc.stdout
        self.assertIn('@media (max-width:', html)
        self.assertGreaterEqual(html.count('<article>'), 16)
        for marker in (
            '1. Initial picks',
            '2. Waiting to deliver · working',
            '3. Waiting for written handoff · blocked',
            '4. Handoff written · awaiting restart',
            '5. Restarting · old session ending',
            '6. Running on target · completed trail',
            '7. Running · skipped polls keep trail',
            '8. Timeout (600s) · Force apply kept',
            'Handoff timed out; target remains saved. Force apply is available.',
            '9. Explicit Force · waits for idle',
            'Force apply accepted, please wait. Ordinary restart waits for idle when applicable.',
            '10. Natural exit without handoff · running',
            '11. Refused · native command did not start',
            '12. Unknown · unmatched request on reopen',
            'Switch outcome unknown; operator reconciliation required. Nothing retried.',
            '13. Unavailable &amp; stale · missing context',
            'Native observation unavailable/stale; nothing retried.',
            'Context unavailable.',
            '14. Escaped long label · env_file omitted',
            '15. Refused · native admission rejected',
            'crew switch expected revision changed: stale',
            '16. Already running · no switch requested',
            'Already running Opus high; nothing to switch.',
            '&lt;Ops &amp; Prod&gt; 32-char label-pad!!',
            '<button>Force apply</button>',
            'requested → handoff requested from the session → handoff written → restarting → running on target',
        ):
            self.assertIn(marker, html)
        for forbidden in ('PRIVATE', 'crew-secret.env', 'TELEGRAM_BOT_TOKEN', '<Ops & Prod>'):
            self.assertNotIn(forbidden, html)
        cmds_proc = subprocess.run(
            [sys.executable, '-B', str(root / 'scripts' / 'preview_yard_commands.py')],
            capture_output=True, text=True, check=True, env=env, timeout=120)
        text = cmds_proc.stdout
        for marker in (
            '### 1. Initial picks',
            '### 8. Timeout (600s) · Force apply kept',
            '### 12. Unknown · unmatched request on reopen',
            '### 13. Unavailable & stale · missing context',
            '[Force apply]',
        ):
            self.assertIn(marker, text)
        for forbidden in ('PRIVATE', 'crew-secret.env', 'TELEGRAM_BOT_TOKEN'):
            self.assertNotIn(forbidden, text)


if __name__ == '__main__':
    unittest.main()
