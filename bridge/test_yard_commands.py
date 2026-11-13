#!/usr/bin/env python3
"""Native /quota on yardr limits: the render, the yardr child runner, the
settings matrix and one real-native run on an isolated yard. Fake Telegram
and stub yardr binaries; temp files only; no live yard, login or network
beyond the isolated server the real-native test starts itself."""
import datetime
import io
import json
import os
import re
import subprocess
import sys
import tempfile
import time
import unittest
from pathlib import Path
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
import yard_commands as y  # noqa: E402
import testyardr  # noqa: E402
from test_bridge import write_settings  # noqa: E402

FUTURE = '2031-06-05T02:00:00Z'
FUTURE_MODEL = '2031-06-12T02:00:00Z'
FUTURE_PAST = '2020-05-01T02:00:00Z'


def tokyo_stamp(text):
    import zoneinfo
    return y.stamp(y._parse_time(text), zoneinfo.ZoneInfo('Asia/Tokyo'))


def plain_report():
    """A fixed plain `limits --json` answer exercising every render rule."""
    return {
        'limits': [{
            'provider': 'codex', 'kinds': ['codex'],
            'burned_by': ['group builders', 'crew member quota-bot'],
            'limits': {'source': 'synthetic', 'provider': 'codex',
                       'windows': [
                           {'label': '5h', 'used_percent': 24.0, 'target_percent': 20.0,
                            'resets_at': FUTURE},
                           {'label': 'Weekly Fable', 'model': 'Fable', 'used_percent': 67.0,
                            'resets_at': FUTURE_MODEL},
                           {'label': 'Weekly k3', 'model': 'k3', 'used_percent': 30.0,
                            'resets_at': FUTURE},
                           {'label': 'Daily', 'used_percent': 10.0, 'used': 1, 'limit': 10,
                            'resets_at': '0001-01-01T00:00:00Z'},
                           {'label': 'Odd span', 'used_percent': 95.0,
                            'resets_at': FUTURE_PAST},
                           {'label': 'Mystery', 'used_percent': 5.0, 'resets_at': 'soon'}],
                       'window': '5h', 'resets_at': FUTURE},
        }, {
            'provider': 'deepseek', 'kinds': ['deepseek'], 'burned_by': [],
            'limits': None, 'limits_unknown': 'no source of this yard answers for provider deepseek',
        }],
        'sources': [{
            'source': 'machine',
            'limits': {'source': 'machine', 'provider': 'machine',
                       'windows': [
                           {'label': 'load', 'used_percent': 43.2},
                           {'label': 'swap', 'used_percent': 87.6},
                           {'label': 'disk', 'used_percent': 58.0},
                           {'label': 'mem', 'used_percent': 12.5}]},
        }, {
            'source': 'selfy', 'limits': None,
            'limits_unknown': 'source selfy: limits command selfy-limits: exit status 1',
        }],
        'unmapped': ['claude'],
    }


STUB = """#!{python}
import json, os, sys, time
argv = sys.argv[1:]
record = {record!r}
expected_home = {home!r}
mode = {mode!r}
plain = json.loads({plain!r})
future = {future!r}
with open(record, 'a') as stream:
    stream.write(json.dumps(dict(argv=argv, env=dict(os.environ), cwd=os.getcwd(),
                                pid=os.getpid(), pgid=os.getpgrp(),
                                started=time.monotonic())) + '\\n')
args = list(argv)
home = args[args.index('--home') + 1] if '--home' in args else ''
rest = args[args.index('limits') + 1:] if 'limits' in args else []
ok = (home == expected_home and args[:3] == ['--home', expected_home, 'limits'] and
      (rest == ['--json'] or
       (len(rest) == 3 and rest[0] == '--group' and rest[1] and rest[2] == '--json')))
if not ok:
    sys.stdout.write('wrong argv or home')
    sys.exit(3)
if mode == 'sleep':
    time.sleep(30)
elif mode == 'nonjson':
    sys.stdout.write('not json')
elif mode == 'empty':
    sys.stdout.write('{{"limits":[],"sources":[],"unmapped":[]}}')
elif mode == 'badshape':
    sys.stdout.write('{{"limits":{{}},"sources":[],"unmapped":[]}}')
elif mode == 'exit1':
    sys.stdout.write(json.dumps(plain))
    sys.exit(1)
elif mode == 'many':
    sys.stdout.write(json.dumps(dict(
        plain, limits=[dict(plain['limits'][0], limits=dict(
            plain['limits'][0]['limits'], windows=[
                dict(label='span-%d' % i, used_percent=10.0, resets_at=future)
                for i in range(200)]))])))
elif mode in ('group', 'slow-group'):
    if mode == 'slow-group':
        time.sleep(2)
    if rest == ['--json']:
        sys.stdout.write(json.dumps(plain))
    else:
        name = rest[1]
        sys.stdout.write(json.dumps(dict(
            limits=[dict(kind='codex', group=name, limits=dict(
                source='synthetic', provider='codex', windows=[
                    dict(label='5h ' + name, used_percent=31.0, resets_at=future),
                    dict(label='Weekly', model='Muse', used_percent=12.0,
                         resets_at='0001-01-01T00:00:00Z')]))],
            sources=[], unmapped=[])))
else:
    sys.stdout.write(json.dumps(plain))
"""


def write_stub(directory, home, mode='plain', plain=None):
    """An executable stub yardr in `directory`: answers only the argv shapes
    the bridge runs ([bin, --home, home, limits, --json] and one --group
    read each) and only its temp home, records argv, env, cwd and timing per
    call, and behaves as `mode` says. `plain` can supply a fixture answer."""
    path = directory / ('stub-yardr-' + mode)
    text = STUB.format(python=sys.executable, record=str(directory / ('record-' + mode + '.jsonl')),
                       home=str(home), mode=mode,
                       plain=json.dumps(plain_report() if plain is None else plain), future=FUTURE)
    path.write_text(text)
    path.chmod(0o700)
    return path


def read_records(directory, mode):
    path = directory / ('record-' + mode + '.jsonl')
    if not path.exists():
        return []
    return [json.loads(line) for line in path.read_text().splitlines()]


class FakeTelegram:
    """Records Telegram calls; answers sends, edits, press answers and the
    command-menu round-trip like the real service."""

    def __init__(self):
        self.calls, self.next_id, self.menu = [], 500, []

    def __call__(self, url, body, *args, **kwargs):
        method = url.rsplit('/', 1)[1]
        self.calls.append((method, body))
        if method == 'sendMessage':
            self.next_id += 1
            return 200, dict(ok=True, result=dict(message_id=self.next_id, chat=dict(id=123)))
        if method == 'editMessageText':
            return 200, dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=123)))
        if method == 'answerCallbackQuery':
            return 200, dict(ok=True, result=True)
        if method == 'setMyCommands':
            self.menu = body['commands']
            return 200, dict(ok=True, result=True)
        if method == 'getMyCommands':
            return 200, dict(ok=True, result=self.menu)
        raise AssertionError('unexpected call ' + method)

    def of(self, method):
        return [body for name, body in self.calls if name == method]


class QuotaCase(unittest.TestCase):
    """A bridge with native /quota on a stub yardr; carries no tests."""

    MODE = 'plain'

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / 'yard'
        self.home.mkdir(mode=0o700)
        self.stub = write_stub(self.root, self.home, mode=self.MODE)
        self.store = b.Store(self.root / 'state.sqlite3', 10)
        self.telegram = FakeTelegram()
        self.env = self.enterContext(patch.dict(os.environ, {'LANG': 'C.UTF-8'}))
        self.bridge = self.make_bridge()

    def quota(self, **fields):
        entry = dict(timeout=30, groups=(), machine_login=True, machine=False)
        entry.update(fields)
        return entry

    def make_bridge(self, quota=None, commands=True, **config):
        quota = self.quota() if quota is None else quota
        native = [dict(y.QUOTA_COMMAND, timeout=quota['timeout'])] if commands else []
        full = dict(bot_id=999, user_id=123, chat_id=123, account_id='test_bot', yard='test-city',
                    hook_url='http://127.0.0.1:8797/hooks/tgbridge', hook_secret='HOOKSECRET',
                    user_display_name='Pat', timezone='Asia/Tokyo', menu=(), agent='yardmaster',
                    yardr_home=str(self.home), yardr_operator='pat', yardr_bin=str(self.stub),
                    yardr_quota=quota, commands=tuple(native))
        full.update(config)
        return b.Bridge(full, self.store, 'SECRET', 'CAPABILITY', request=self.telegram,
                        state_dir=self.root)

    def message(self, text, uid=40, **extra):
        note = dict(message_id=uid + 1000, text=text, chat=dict(id=123, type='private'),
                    **{'from': dict(id=123, is_bot=False)})
        note.update(extra)
        return dict(update_id=uid, message=note)

    def drain(self, *updates):
        with patch('sys.stderr', new_callable=io.StringIO):
            self.bridge.drain(list(updates))

    def sends(self):
        return self.telegram.of('sendMessage')

    def answer_text(self):
        self.assertEqual(len(self.sends()), 1)
        return self.sends()[0]['text']


class RenderTests(QuotaCase):
    def test_render_shows_providers_sources_and_unmapped(self):
        self.drain(self.message('/quota'))
        text = self.answer_text()
        self.assertTrue(text.startswith('<pre>') and text.endswith('</pre>'))
        # The provider under its display name alone, the yellow dot ahead of
        # target, the Ddd HH:MM reset in the bridge's timezone and the time left.
        self.assertTrue(text.startswith('<pre>Codex\n🟡 5h       24%  ' + tokyo_stamp(FUTURE)))
        self.assertNotIn('burned by', text)
        # A per-model window shows its model unless its label names it: 'Weekly
        # Fable' reads 'Fable wk'; a used figure with a year-1 reset shows its
        # figure and 'no reset'; a past reset shows no left.
        self.assertIn('Fable wk', text)
        self.assertNotIn('model Fable', text)
        # Shortening can remove a model name, so keep the suffix in that case.
        self.assertIn('Weekly k', text)
        self.assertIn('model k3', text)
        self.assertIn('no reset', text)
        self.assertIn('Odd span', text)
        # An unparseable reset is unknown, not dropped.
        self.assertIn('unknown', text)
        # A provider with no answer and a failing source render one
        # unavailable row each, with the reason scrubbed of nothing here.
        self.assertIn('DeepSeek\n⚪ limits unknown for deepseek', text)
        self.assertIn('selfy\n⚪ limits unknown for selfy', text)
        # The machine source is left out by default; unmapped is one line.
        self.assertNotIn('load', text)
        self.assertIn('not asked: claude', text)
        # One message of at most 4096 UTF-16 units, html, a send.
        self.assertLessEqual(y.utf16_units(text), 4096)
        self.assertEqual(self.sends()[0].get('parse_mode'), 'HTML')
        self.assertNotIn('reply_parameters', self.sends()[0])

    def test_antigravity_rows_render_as_compact_shared_quotas(self):
        fixtures = Path(__file__).parent / 'fixtures'
        plain = json.loads((fixtures / 'limits_antigravity.json').read_text(encoding='utf-8'))

        class FixedDateTime(datetime.datetime):
            @classmethod
            def now(cls, tz=None):
                value = cls(2026, 11, 13, 8, 8, tzinfo=datetime.timezone.utc)
                return value if tz is None else value.astimezone(tz)

        def render(report, uid):
            self.stub = write_stub(self.root, self.home, plain=report)
            self.bridge = self.make_bridge(timezone='Asia/Yekaterinburg')
            with patch.object(y.datetime, 'datetime', FixedDateTime):
                self.drain(self.message('/quota', uid=uid))
            text = self.sends()[-1]['text']
            return text.removeprefix('<pre>').removesuffix('</pre>')

        rendered = render(plain, 40)
        expected = (fixtures / 'limits_antigravity.rendered.txt').read_text(encoding='utf-8').rstrip('\n')
        self.assertEqual(rendered, expected)
        for index, (field, value) in enumerate((('used_percent', 99),
                                                ('resets_at', '2026-11-15T13:03:00Z'))):
            variant = json.loads(json.dumps(plain))
            variant['limits'][0]['limits']['windows'][5][field] = value
            lines = render(variant, 41 + index).splitlines()
            self.assertEqual(sum('Argon' in line for line in lines), 2, field)
            quota_rows = [line for line in lines if line.startswith(('🟢 ', '🟡 ', '🔴 ', '⚪ '))]
            self.assertEqual([line[2:10] for line in quota_rows], [
                'Gem wk  ', 'Gem 5h  ', 'C+GPT wk', 'C+GPT 5h', 'Argon wk', 'Argon wk',
            ], field)

    def test_machine_block_shows_figures_only_when_asked(self):
        self.bridge = self.make_bridge(quota=self.quota(machine=True))
        self.drain(self.message('/quota'))
        text = self.answer_text()
        # The machine source's rows show the figure alone, never 'no reset'.
        self.assertIn('machine\n🟢 load     43%\n🟡 swap     88%\n🟢 disk     58%\n🟢 mem      13%\n', text)
        self.assertEqual(text.count('no reset'), 1)  # the codex Daily window only

    def test_text_arguments_are_ignored(self):
        self.drain(self.message('/quota extra words here'))
        first = self.answer_text()
        self.drain(self.message('/quota', uid=41))
        self.assertEqual(self.sends()[1]['text'], first)

    def test_redelivered_update_sends_nothing_twice(self):
        update = self.message('/quota')
        self.drain(update)
        self.drain(update)
        self.assertEqual(len(self.sends()), 1)

    def test_in_a_topic_the_answer_goes_to_the_thread(self):
        self.drain(self.message('/quota', is_topic_message=True, message_thread_id=7))
        self.assertEqual(self.sends()[0].get('message_thread_id'), 7)

    def test_set_commands_lists_quota(self):
        # The native config also carries home and operator, so /decide is
        # listed too; /quota's menu text is the registry's description.
        self.assertEqual(self.bridge.set_commands(),
                         [dict(command='quota', description='Usage limits and time to each reset'),
                          dict(command='decide', description='Open decisions waiting on you')])

    def test_callback_prefix_is_registered(self):
        self.assertIs(self.bridge._callback_command('quota:anything'),
                      self.bridge.commands['quota'])
        keyboard = [[dict(text='q', callback_data='quota:x')]]
        self.assertEqual(len(self.bridge._command_keyboard(keyboard)), 1)

    def test_callback_press_is_answered_empty(self):
        update_id = 41
        self.bridge.run_command(self.bridge.commands['quota'], update_id, dict(
            kind='callback', command='quota', args=None, update_id=update_id, message_id=None,
            callback_data='quota:x', callback_message_id=501, received_at='2026-11-12T00:00:00+00:00',
            user_display_name='Pat'), query_id='query-41')
        self.assertEqual(self.telegram.of('answerCallbackQuery'), [dict(callback_query_id='query-41')])
        self.assertEqual(self.sends(), [])


class ProviderOrderTests(QuotaCase):
    def test_providers_lead_in_the_pinned_order_then_the_yards(self):
        """Claude, Codex, Muse, Antigravity (agy) and Grok lead, each under its
        display name alone; the other providers follow in the yard's order."""
        yard_order = ['deepseek', 'grok', 'agy', 'zeta', 'codex', 'claude', 'muse']
        answer = dict(limits=[dict(provider=name, kinds=[name], burned_by=['group ops'],
                                   limits=dict(source='synthetic', provider=name, windows=[
                                       dict(label='5h', used_percent=10.0, resets_at=FUTURE)]))
                              for name in yard_order], sources=[], unmapped=[])
        with patch.object(y, 'run_yardr', return_value=(0, json.dumps(answer).encode(), b'', None)):
            self.drain(self.message('/quota'))
        text = self.answer_text()
        headers = ['Claude', 'Codex', 'Muse', 'Antigravity', 'Grok', 'DeepSeek', 'zeta']
        positions = [text.index(header + '\n') for header in headers]
        self.assertEqual(positions, sorted(positions))
        self.assertNotIn('agy', text)
        self.assertNotIn('burned by', text)


class ExitStatusTests(QuotaCase):
    MODE = 'exit1'

    def test_exit_1_with_json_renders(self):
        """yardr exits 1 with the full JSON when an entry is not known: the
        stdout still parses, whatever the exit status."""
        self.drain(self.message('/quota'))
        text = self.answer_text()
        self.assertTrue(text.startswith('<pre>Codex\n'))
        self.assertIn('not asked: claude', text)

    def test_unknown_entries_render_a_fixed_row(self):
        """An entry yardr does not know renders a fixed row naming only the
        structured provider or source, never its diagnostic string."""
        answer = plain_report()
        answer['sources'][-1]['limits_unknown'] = 'source selfy: {"api_key": "CREDMARKVALUE"}'
        answer['limits'][1]['limits_unknown'] = 'no source of this yard answers for provider deepseek'
        with patch.object(y, 'run_yardr', return_value=(
                1, json.dumps(answer).encode(), b'', None)):
            self.drain(self.message('/quota'))
        text = self.answer_text()
        self.assertIn('selfy\n⚪ limits unknown for selfy', text)
        self.assertIn('DeepSeek\n⚪ limits unknown for deepseek', text)
        self.assertNotIn('CREDMARKVALUE', text)
        self.assertNotIn('no source of this yard', text)

    def test_exited_nonzero_with_unparseable_stdout_names_the_code(self):
        with patch.object(y, 'run_yardr', return_value=(
                3, b'wrong argv or home', b'', None)):
            self.drain(self.message('/quota'))
        self.assertIn('⚪ quota unavailable: yardr exited 3', self.answer_text())


class NonJsonTests(QuotaCase):
    MODE = 'nonjson'

    def test_non_json_output_is_unavailable(self):
        self.drain(self.message('/quota'))
        self.assertIn('⚪ quota unavailable: answer not valid JSON', self.answer_text())


class ShapeTests(QuotaCase):
    MODE = 'badshape'

    def test_wrong_top_shape_is_unavailable(self):
        self.drain(self.message('/quota'))
        self.assertIn('⚪ quota unavailable: answer not valid JSON', self.answer_text())


class EmptyTests(QuotaCase):
    MODE = 'empty'

    def test_empty_answer_says_no_sources(self):
        self.drain(self.message('/quota'))
        self.assertIn('⚪ quota unavailable: no sources configured', self.answer_text())


class TimeoutTests(QuotaCase):
    MODE = 'sleep'

    def test_timeout_kills_the_group_and_answers_timeout(self):
        self.bridge = self.make_bridge(quota=self.quota(timeout=1))
        self.drain(self.message('/quota'))
        self.assertIn('⚪ quota unavailable: timed out', self.answer_text())
        records = read_records(self.root, 'sleep')
        self.assertEqual(len(records), 1)
        pgid, pid = records[0]['pgid'], records[0]['pid']
        # Its process group is gone: the run was killed, not left behind.
        with self.assertRaises(ProcessLookupError):
            os.killpg(pgid, 0)
        with self.assertRaises(ProcessLookupError):
            os.kill(pid, 0)


class RunnerCleanupTests(unittest.TestCase):
    def test_exited_leader_leaves_no_group_behind(self):
        """A leader that forks a helper and exits 0 still loses its group:
        the helper is killed, never left to run past the deadline."""
        import signal
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        script = root / 'forking-yardr.py'
        script.write_text(
            'import os, sys\n'
            'pid = os.fork()\n'
            'if pid == 0:\n'
            '    import time\n'
            '    time.sleep(30)\n'
            '    os._exit(0)\n'
            'open(' + json.dumps(str(root / 'pgid')) + ', "w").write(str(os.getpgrp()))\n'
            'sys.stdout.write(\'{"limits": [], "sources": [], "unmapped": []}\')\n'
            'sys.stdout.flush()\n')
        pgid = None
        try:
            status, stdout, _stderr, problem = y.run_yardr(
                [sys.executable, str(script)], str(root),
                {'PATH': '/usr/bin:/bin', 'HOME': str(root),
                 'LANG': 'C.UTF-8', 'TZ': 'UTC'},
                time.monotonic() + 10)
            self.assertIsNone(problem)
            self.assertEqual(status, 0)
            self.assertEqual(json.loads(stdout),
                             {'limits': [], 'sources': [], 'unmapped': []})
            pgid = int((root / 'pgid').read_text())
            # SIGKILL lands async and init reaps the orphan: poll, not race.
            for _ in range(200):
                try:
                    os.killpg(pgid, 0)
                except ProcessLookupError:
                    break
                time.sleep(0.01)
            else:
                self.fail('the helper process group survived the run')
        finally:
            if pgid is not None:
                try:
                    os.killpg(pgid, signal.SIGKILL)
                except ProcessLookupError:
                    pass

    def test_exited_leader_drain_is_bounded_by_deadline_and_cap(self):
        """A helper keeping an inherited pipe readable cannot hold the
        exited-leader drain past the deadline or the output cap."""
        from types import SimpleNamespace

        class Stream:
            def close(self):
                pass

        class Proc:
            def __init__(self):
                self.stdout, self.stderr = Stream(), Stream()
                self.pid, self.returncode = 999999999, 0

            def poll(self):
                return 0

            def wait(self):
                return 0

        class Selector:
            def __init__(self):
                self.keys, self.reads, self.now = {}, 0, 0

            def register(self, fileobj, events, data):
                self.keys[data] = SimpleNamespace(fileobj=fileobj, fd=1, data=data)

            def unregister(self, fileobj):
                self.keys = {k: v for k, v in self.keys.items()
                             if v.fileobj is not fileobj}

            def get_map(self):
                return self.keys

            def select(self, timeout):
                self.reads += 1
                self.now += 0.25
                return [(self.keys['out'], 1)] if self.reads <= 67 else []

            def close(self):
                pass

        proc, selector = Proc(), Selector()
        deadline = 5
        with patch.object(y.subprocess, 'Popen', return_value=proc), \
                patch.object(y.selectors, 'DefaultSelector', return_value=selector), \
                patch.object(y.os, 'read', return_value=b'x' * 65536), \
                patch.object(y.os, 'killpg'):
            _status, out, _err, problem = y.run_yardr(
                ['synthetic'], '/tmp', {}, deadline,
                monotonic=lambda: selector.now)
        self.assertLessEqual(len(out), y.YARDR_OUTPUT_MAX_BYTES + 65536)
        self.assertLessEqual(selector.now, deadline + 0.25)
        self.assertIsNotNone(problem)


class UnreachableTests(QuotaCase):
    def test_missing_binary_is_unreachable(self):
        self.bridge = self.make_bridge()
        self.bridge.config['yardr_bin'] = str(self.root / 'no-such-yardr')
        self.drain(self.message('/quota'))
        self.assertIn('⚪ quota unavailable: yardr not reachable', self.answer_text())


class EnvTests(QuotaCase):
    def test_child_env_is_fresh_and_names_the_bridge(self):
        with patch.dict(os.environ, {'PATH': '/tmp/custom-bin:/usr/bin:/bin', 'HOME': '/tmp/custom-home',
                                     'LANG': 'xx.UTF-8', 'USER': 'customuser',
                                     'YARDR_FOO': 'scrubbed', 'YARDR_HOME': '/srv/yard',
                                     'TELEGRAM_BRIDGE_COMMAND': 'quota'}):
            self.drain(self.message('/quota'))
        self.assertEqual(len(self.sends()), 1)  # the run itself worked
        records = read_records(self.root, 'plain')
        self.assertEqual(len(records), 1)
        env = records[0]['env']
        self.assertEqual(env['PATH'], '/tmp/custom-bin:/usr/bin:/bin')
        self.assertEqual(env['HOME'], '/tmp/custom-home')
        self.assertEqual(env['LANG'], 'xx.UTF-8')
        self.assertEqual(env['TZ'], 'Asia/Tokyo')
        self.assertEqual(env['USER'], 'customuser')
        self.assertEqual(env['LOGNAME'], 'customuser')
        self.assertNotIn('TELEGRAM_BRIDGE_COMMAND', env)
        for key in env:
            self.assertFalse(key.startswith('YARDR_'), key)
        self.assertEqual(records[0]['cwd'], str(self.home))
        argv = records[0]['argv']
        self.assertEqual(argv[:4], ['--home', str(self.home), 'limits', '--json'])

    def test_path_falls_back_to_command_path(self):
        with patch.dict(os.environ, {'YARDR_FOO': 'scrubbed'}, clear=False):
            saved = os.environ.pop('PATH', None)
            try:
                self.drain(self.message('/quota'))
            finally:
                if saved is not None:
                    os.environ['PATH'] = saved
        records = read_records(self.root, 'plain')
        self.assertEqual(records[0]['env']['PATH'], b.Bridge.COMMAND_PATH)


class GroupTests(QuotaCase):
    MODE = 'group'

    def test_group_reads_render_under_provider_and_group(self):
        self.bridge = self.make_bridge(quota=self.quota(groups=('ops',)))
        self.drain(self.message('/quota'))
        text = self.answer_text()
        # The plain read first, then the group read under '<provider> · <group>'.
        self.assertLess(text.index('Codex\n'), text.index('codex · ops'))
        self.assertIn('codex · ops', text)
        self.assertIn('5h ops', text)
        self.assertIn('model Muse', text)
        records = read_records(self.root, 'group')
        self.assertEqual(len(records), 2)
        plain = [record['argv'] for record in records if '--group' not in record['argv']]
        grouped = [record['argv'] for record in records if '--group' in record['argv']]
        self.assertEqual(plain, [['--home', str(self.home), 'limits', '--json']])
        self.assertEqual(grouped, [['--home', str(self.home), 'limits', '--group', 'ops', '--json']])

    def test_machine_login_false_hides_the_plain_providers(self):
        self.bridge = self.make_bridge(quota=self.quota(groups=('ops',), machine_login=False))
        self.drain(self.message('/quota'))
        text = self.answer_text()
        self.assertNotIn('Codex\n', text)
        self.assertNotIn('DeepSeek', text)
        # The self-reporting sources are shown either way; the machine's
        # figures wait for [yardr.quota] machine.
        self.assertNotIn('machine', text)
        self.assertIn('selfy', text)
        self.assertIn('not asked: claude', text)
        self.assertIn('codex · ops', text)


class ParallelTests(QuotaCase):
    MODE = 'slow-group'

    def test_groups_run_in_parallel(self):
        self.bridge = self.make_bridge(quota=self.quota(timeout=30, groups=('ga', 'gb')))
        started = time.monotonic()
        self.drain(self.message('/quota'))
        wall = time.monotonic() - started
        text = self.answer_text()
        self.assertIn('codex · ga', text)
        self.assertIn('codex · gb', text)
        records = read_records(self.root, 'slow-group')
        self.assertEqual(len(records), 3)
        starts = sorted(record['started'] for record in records)
        # Three 2 s runs back to back take 6 s; in parallel they start together.
        self.assertLess(starts[-1] - starts[0], 1.0)
        self.assertLess(wall, 5.0)


class FitTests(QuotaCase):
    MODE = 'many'

    def test_long_answer_drops_whole_blocks_with_ellipsis(self):
        self.drain(self.message('/quota'))
        text = self.answer_text()
        inner = text.removeprefix('<pre>').removesuffix('</pre>')
        self.assertLessEqual(y.utf16_units(inner), 4096)
        self.assertTrue(text.endswith('…</pre>'), text[-20:])
        self.assertIn('span-0', text)
        self.assertNotIn('span-199', text)


class FixedReasonTests(unittest.TestCase):
    """The closed set of failure reasons: no scrubber, no free text."""

    def test_no_scrubber_left(self):
        self.assertFalse(hasattr(y, 'reason'))
        self.assertFalse(hasattr(y, 'stderr_last_line'))
        self.assertFalse(hasattr(y, '_SECRET_SHAPES'))

    def test_exit_reason_names_a_plain_integer_code(self):
        self.assertEqual(y._exit_reason(1), 'yardr exited 1')
        self.assertEqual(y._exit_reason(3), 'yardr exited 3')
        self.assertEqual(y._exit_reason(0), 'answer not valid JSON')
        self.assertEqual(y._exit_reason(None), 'answer not valid JSON')
        self.assertEqual(y._exit_reason(True), 'answer not valid JSON')
        self.assertEqual(y._exit_reason('1'), 'answer not valid JSON')
        self.assertEqual(y._exit_reason(9999), 'answer not valid JSON')
        self.assertEqual(y._exit_reason(1.0), 'answer not valid JSON')

    def test_safe_name_falls_back(self):
        self.assertEqual(y._safe_name('codex', 'provider'), 'codex')
        self.assertEqual(y._safe_name('quota-builders', 'group'), 'quota-builders')
        self.assertEqual(y._safe_name('has space', 'group'), 'group')
        self.assertEqual(y._safe_name('a@b', 'source'), 'source')
        self.assertEqual(y._safe_name('', 'provider'), 'provider')
        self.assertEqual(y._safe_name(None, 'source'), 'source')


class DotTests(unittest.TestCase):
    def row(self, used, target):
        import datetime
        import zoneinfo
        window = dict(label='5h', model='', used_percent=used, target_percent=target,
                      resets_at=FUTURE, used=None, limit=None)
        return y.window_row(window, zoneinfo.ZoneInfo('Asia/Tokyo'),
                            datetime.datetime(2030, 1, 1, tzinfo=datetime.timezone.utc))

    def test_dot_is_yellow_when_ahead_of_target_before_rounding(self):
        self.assertTrue(self.row(20.4, 20.0).startswith(y.YELLOW))

    def test_dot_is_green_when_level_with_target(self):
        self.assertTrue(self.row(20.0, 20.0).startswith(y.GREEN))


class NoFreeTextTests(QuotaCase):
    """No byte sequence from a free-text field (limits_unknown, yardr
    stderr) ever reaches the reply or the bridge log."""

    # The reviewers' synthetic diagnostics: one word (note 4037), spaced
    # (note 4088), escaped quote (note 4148).
    DIAGNOSTICS = [
        ('CREDMARKVALUE', 'source selfy: {"api_key": "CREDMARKVALUE"}',
         b'source selfy: {"api_key": "CREDMARKVALUE"}\n'),
        ('FIRST SECOND', 'helper failed: {"password": "SYNTHETIC FIRST SECOND"}',
         b'helper failed: {"password": "SYNTHETIC FIRST SECOND"}\n'),
        ('ESCAPED TAIL', 'helper failed: {"password": "SYNTHETIC \\"ESCAPED\\" TAIL"}',
         b'helper failed: {"password": "SYNTHETIC \\"ESCAPED\\" TAIL"}\n'),
    ]

    def drain_capturing(self, bridge, update):
        import io as _io
        with patch('sys.stderr', new_callable=_io.StringIO) as logged:
            bridge.drain([update])
        (send,) = [body for name, body in self.telegram.calls
                   if name == 'sendMessage'][-1:]
        return send['text'], logged.getvalue()

    def fake(self, plain_json, group_json, stderr):
        def run(argv, home, env, deadline):
            return (1, group_json if '--group' in argv else plain_json,
                    stderr, None)
        return run

    def test_reviewers_diagnostics_never_reach_send_or_log(self):
        for index, (marker, unknown, stderr) in enumerate(self.DIAGNOSTICS):
            answer = plain_report()
            answer['sources'][-1]['limits_unknown'] = unknown
            answer['limits'][1]['limits_unknown'] = unknown
            group = {'kind': 'codex', 'group': 'ops', 'limits': None,
                     'limits_unknown': unknown}
            group_json = json.dumps(
                {'limits': [group], 'sources': [], 'unmapped': []}).encode()
            plain_json = json.dumps(answer).encode()
            with patch.object(y, 'run_yardr', side_effect=self.fake(
                    plain_json, group_json, stderr)):
                bridge = self.make_bridge(quota=self.quota(groups=('ops',)))
                text, logged = self.drain_capturing(
                    bridge, self.message('/quota', uid=90 + index))
            for word in marker.split():
                self.assertNotIn(word, text, unknown)
                self.assertNotIn(word, logged, unknown)
            self.assertIn('limits unknown for selfy', text)
            self.assertIn('limits unknown for ops', text)

    def test_random_free_text_never_reaches_send_or_log(self):
        import random
        import string
        rng = random.Random(20261113)
        alphabet = string.ascii_letters + string.digits + ' @/:.=+_"\'\\{}[]$<>-'
        for round in range(25):
            marker = 'FX' + ''.join(rng.choice(string.ascii_letters + string.digits)
                                    for _ in range(12))
            noise = ''.join(rng.choice(alphabet) for _ in range(rng.randint(8, 64)))
            unknown = f'{noise} {marker} {noise}'
            stderr = f'{noise}\n{marker}-err {noise}\n'.encode()
            answer = plain_report()
            answer['sources'][-1]['limits_unknown'] = unknown
            answer['limits'][1]['limits_unknown'] = unknown
            group = {'kind': 'codex', 'group': 'ops', 'limits': None,
                     'limits_unknown': unknown}
            group_json = json.dumps(
                {'limits': [group], 'sources': [], 'unmapped': []}).encode()
            plain_json = json.dumps(answer).encode()
            with patch.object(y, 'run_yardr', side_effect=self.fake(
                    plain_json, group_json, stderr)):
                bridge = self.make_bridge(quota=self.quota(groups=('ops',)))
                text, logged = self.drain_capturing(
                    bridge, self.message('/quota', uid=100 + round))
            self.assertNotIn(marker, text, unknown)
            self.assertNotIn(marker, logged, unknown)
            self.assertNotIn(marker + '-err', logged, unknown)


class SettingsQuotaCase(unittest.TestCase):
    """The settings matrix: no table loads as on main; the table turns /quota
    on; the table beside a [[command]] or [[menu]] quota, or an overlapping
    prefix, is refused with the migration message."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / 'yard'
        self.home.mkdir(mode=0o700)
        self.stub = self.root / 'yardr-stub'
        self.stub.write_text('#!/bin/sh\necho {}\n')
        self.stub.chmod(0o700)

    def settings(self, quota=None, menu=True, command=None, native=True, path_bin=False):
        """A settings file: `quota` is the [yardr.quota] body (None: no
        table); `menu` keeps the default quota [[menu]] entry; `command` is
        an extra [[command]] table body; without `native` the [yardr]
        home/operator lines are dropped; `path_bin` resolves bin from PATH."""
        path = write_settings(self.root)
        text = path.read_text()
        if not menu:
            text = re.sub(r'\n\[\[menu\]\]\ncommand = "quota"\ndescription = [^\n]*\n', '\n', text)
        lines = ['bin = "%s"' % self.stub]
        if native:
            lines.append('home = "%s"' % self.home)
            lines.append('operator = "pat"')
        marker = '\n[yardr]\n'
        start = text.index(marker)
        text = text[:start] + marker + '\n'.join(lines) + '\n' + text[start + len(marker):]
        if quota is not None:
            text += '\n[yardr.quota]\n' + quota
        if command is not None:
            text += '\n' + command
        if path_bin:
            text = text.replace('bin = "%s"\n' % self.stub, '')
        path.write_text(text)
        return path

    def entry(self, name='quota', **fields):
        fields.setdefault('description', 'Quota via add-on')
        if 'url' not in fields and 'handler' not in fields:
            fields['handler'] = str(self.stub)
        return '\n[[command]]\nname = "%s"\n' % name + ''.join(
            '%s = %s\n' % (key, json.dumps(value)) for key, value in fields.items())

    def test_no_table_keeps_a_command_quota(self):
        settings = b.load_settings(self.settings(command=self.entry(), menu=False))
        self.assertEqual(settings.config['commands'][0]['name'], 'quota')
        self.assertEqual(settings.config['commands'][0]['handler'], str(self.stub))
        self.assertNotIn('native', settings.config['commands'][0])
        self.assertNotIn('yardr_bin', settings.config)
        self.assertNotIn('yardr_quota', settings.config)

    def test_table_registers_the_native_command(self):
        settings = b.load_settings(self.settings(quota='', menu=False))
        self.assertEqual(settings.config['yardr_bin'], str(self.stub))
        self.assertEqual(settings.config['yardr_quota'],
                         dict(timeout=30, groups=(), machine_login=True, machine=False))
        (entry,) = [command for command in settings.config['commands']
                     if command.get('native')]
        self.assertEqual(entry, dict(name='quota', description='Usage limits and time to each reset',
                                     menu=True, callback_prefix='quota:', native='quota', timeout=30))

    def test_table_with_explicit_keys(self):
        settings = b.load_settings(self.settings(
            quota='timeout = 45\ngroups = ["nightly", "builders"]\nmachine_login = false\nmachine = true\n',
            menu=False))
        self.assertEqual(settings.config['yardr_quota'],
                         dict(timeout=45, groups=('nightly', 'builders'), machine_login=False, machine=True))

    def test_default_bin_comes_from_path(self):
        bindir = self.root / 'bin'
        bindir.mkdir()
        link = bindir / 'yardr'
        link.write_text('#!/bin/sh\necho {}\n')
        link.chmod(0o700)
        with patch.dict(os.environ, {'PATH': str(bindir)}):
            settings = b.load_settings(self.settings(quota='', menu=False, path_bin=True))
        self.assertEqual(settings.config['yardr_bin'], str(link))

    def test_table_with_command_quota_is_refused(self):
        with self.assertRaisesRegex(b.SettingsError, r'delete the \[\[command\]\] quota table'):
            b.load_settings(self.settings(quota='', menu=False, command=self.entry()))

    def test_table_with_menu_quota_is_refused(self):
        with self.assertRaisesRegex(b.SettingsError, r'\[\[menu\]\] quota clashes.*delete the \[\[command\]\] quota table'):
            b.load_settings(self.settings(quota=''))

    def test_table_with_overlapping_prefix_is_refused(self):
        with self.assertRaisesRegex(b.SettingsError, r'callback_prefix overlaps.*delete the \[\[command\]\] quota table'):
            b.load_settings(self.settings(
                quota='', menu=False,
                command=self.entry('other', callback_prefix='quota:reset')))
        with self.assertRaisesRegex(b.SettingsError, r'callback_prefix overlaps.*delete the \[\[command\]\] quota table'):
            b.load_settings(self.settings(
                quota='', menu=False, command=self.entry('other', callback_prefix='qu')))
        # A 'qu:' prefix is no overlap: no quota: press starts with it.
        settings = b.load_settings(self.settings(
            quota='', menu=False, command=self.entry('qu', callback_prefix='qu:')))
        self.assertTrue(any(command.get('native') == 'quota' for command in settings.config['commands']))

    def test_table_without_native_keys_is_refused(self):
        with self.assertRaisesRegex(b.SettingsError, r'\[yardr\.quota\] needs \[yardr\] home and operator'):
            b.load_settings(self.settings(quota='', menu=False, native=False))

    def test_table_with_bad_values_is_refused(self):
        for quota in ('timeout = 4\n', 'timeout = 121\n', 'timeout = "30"\n',
                      'groups = ["ok", "ok"]\n', 'groups = ["Has Space"]\n',
                      'groups = ["' + 'g' * 65 + '"]\n', 'machine_login = "yes"\n', 'machine = "yes"\n',
                      'unknown_key = true\n', 'bin = 1\n'):
            with self.subTest(quota=quota):
                if 'bin' in quota:
                    with self.assertRaisesRegex(b.SettingsError, r'unknown key yardr\.quota\.bin'):
                        b.load_settings(self.settings(quota=quota, menu=False))
                else:
                    with self.assertRaisesRegex(b.SettingsError, r'settings: '):
                        b.load_settings(self.settings(quota=quota, menu=False))

    def test_table_without_bin_on_path_is_refused(self):
        with patch.dict(os.environ, {'PATH': str(self.root / 'empty')}):
            (self.root / 'empty').mkdir()
            with self.assertRaisesRegex(b.SettingsError, r'\[yardr\] bin must name the yardr executable'):
                b.load_settings(self.settings(quota='', menu=False, path_bin=True))

    def test_bin_without_table_is_ignored(self):
        settings = b.load_settings(self.settings())
        self.assertNotIn('yardr_bin', settings.config)
        self.assertNotIn('yardr_quota', settings.config)


class RealNativeTests(QuotaCase):
    """One real-native run on an isolated server of the aligned build: a
    declared limits source with a stub command, a crew member of its kind, a
    group of that kind whose env_file the stub echoes into a window label
    (which proves --group reads that group's env), a second self-answering
    source that exits 1, and the built-in machine source."""

    GROUP = 'quota-builders'

    def setUp(self):
        super().setUp()
        self.binary = testyardr.binary()
        if self.binary is None:
            self.skipTest('yardr binary required for isolated native integration')

    def cli(self, home, command, env, *args):
        result = subprocess.run(command + list(args), env=env, capture_output=True, text=True)
        self.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def write_source(self, directory, name, command_lines, kinds=None):
        directory.mkdir(parents=True, exist_ok=True)
        lines = ['name: ' + name, 'about: Synthetic limits source for the native quota test.',
                 'limits:', '  command: [' + ', '.join(command_lines) + ']']
        if kinds is not None:
            lines.append('  kinds:')
            lines.extend('    %s: %s' % pair for pair in kinds)
        (directory / (name + '.yaml')).write_text('\n'.join(lines) + '\n')

    def test_real_native_quota(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        server, home, command, env = testyardr.start(root, self.binary)
        try:
            decl = root / 'decl'
            decl.mkdir(parents=True, exist_ok=True)
            stub = decl / 'synthetic-limits'
            stub.write_text(
                '#!/bin/sh\n'
                'echo "{\\"provider\\":\\"codex\\",\\"plan\\":\\"Test\\",\\"windows\\":'
                '[{\\"label\\":\\"5h $QUOTA_SPAN\\",\\"used_percent\\":24,'
                '\\"target_percent\\":20,\\"resets_at\\":\\"2031-06-05T02:00:00Z\\"},'
                '{\\"label\\":\\"Weekly\\",\\"model\\":\\"Fable\\",\\"used_percent\\":67,'
                '\\"resets_at\\":\\"2031-06-12T02:00:00Z\\"}]}"\n')
            stub.chmod(0o700)
            self.cli(home, command, env, 'init', '--harness', 'claude')
            self.write_source(decl, 'synthetic', ['synthetic-limits', '"{provider}"'],
                              kinds=[('codex', 'codex')])
            failing = decl / 'selfy-limits'
            failing.write_text('#!/bin/sh\nexit 1\n')
            failing.chmod(0o700)
            self.write_source(decl, 'selfy', ['selfy-limits'])
            self.cli(home, command, env, 'source', 'add', str(decl / 'synthetic.yaml'))
            self.cli(home, command, env, 'source', 'add', str(decl / 'selfy.yaml'))
            self.cli(home, command, env, 'crew', 'add', 'quota-bot', '--set', 'kind=codex')
            self.cli(home, command, env, 'group', 'add', self.GROUP,
                     '--runner', 'herdr', '--set', 'kind=codex')
            group_env = root / 'group.env'
            group_env.write_text('QUOTA_SPAN=night\n')
            group_env.chmod(0o600)
            self.cli(home, command, env, 'group', 'set', self.GROUP,
                     'env_file=%s' % group_env)
            bridge = self.make_bridge(
                quota=self.quota(timeout=30, groups=(self.GROUP,), machine=True),
                yardr_home=str(home), yardr_bin=self.binary)
            with patch('sys.stderr', new_callable=io.StringIO):
                bridge.drain([self.message('/quota')])
            (send,) = self.telegram.of('sendMessage')
            text = send['text']
            # The provider under its name, the group read under '<provider> · <group>'
            # with the label the group's env produced, the unknown self source,
            # the machine's figures with no 'no reset' text, and unmapped.
            self.assertTrue(text.startswith('<pre>Codex\n'))
            self.assertIn('codex · %s' % self.GROUP, text)
            self.assertIn('5h night', text)
            self.assertIn('selfy\n⚪ limits unknown for selfy', text)
            # The machine's figures are live, so any dot; the row shows no reset.
            self.assertRegex(text, r'machine\n[🟢🟡🔴⚪] load ')
            self.assertNotIn('no reset', text)
            self.assertIn('not asked: ', text)
            self.assertLessEqual(y.utf16_units(text), 4096)
        finally:
            testyardr.stop(server)


# --- /mayor_model -----------------------------------------------------------

CREW_STUB = """#!{python}
import json, os, sys, time
argv = sys.argv[1:]
state_path, control_path, record = {state!r}, {control!r}, {record!r}
expected_home = {home!r}
with open(record, 'a') as stream:
    stream.write(json.dumps(dict(argv=argv, env=dict(os.environ), cwd=os.getcwd())) + '\\n')
control = json.load(open(control_path)) if os.path.exists(control_path) else {{}}
state = json.load(open(state_path))
if argv[:2] != ['--home', expected_home]:
    sys.stdout.write('wrong home')
    sys.exit(9)
rest = argv[2:]
actor = None
if rest[:1] == ['--actor']:
    actor, rest = rest[1], rest[2:]
def save():
    state['revision'] += 1
    json.dump(state, open(state_path, 'w'))
if rest[:2] == ['crew', 'show'] and rest[3:] == ['--json'] and actor is None:
    if rest[2] != state['name']:
        sys.stdout.write(json.dumps(dict(error='crew member %s: not found' % rest[2])))
        sys.exit(3)
    if control.get('show') == 'fail' or (control.get('show') == 'fail-after-set' and state.get('was_set')):
        sys.stderr.write('yardr: secret=hunter2 broke\\n')
        sys.exit(1)
    config = dict(kind=state['kind'])
    if state['model']:
        config['model'] = state['model']
    if state['args']:
        config['args'] = state['args']
    if state['env_file']:
        config['env_file'] = state['env_file']
    crew = dict(name=state['name'], runner='herdr', config=config, revision='R%d' % state['revision'],
                kind=state['kind'], state=state['session_state'], journal='j')
    if state['model']:
        crew['model'] = state['model']
    if state.get('session_status'):
        crew['status'] = state['session_status']
    if state.get('session_kind'):
        crew['handle'] = dict(kind=state['session_kind'])
    sys.stdout.write(json.dumps(dict(crew=crew, inbox=0, journal=[], timers=[])))
    sys.exit(0)
if rest[:2] == ['crew', 'set'] and actor == {operator!r}:
    words = rest[3:]
    revision = words[words.index('--revision') + 1]
    if control.get('set') == 'sleep':
        time.sleep(30)
    if control.get('set') == 'fail':
        sys.stderr.write('yardr: refused, token=abc123\\n')
        sys.exit(1)
    if revision != 'R%d' % state['revision']:
        sys.stderr.write('yardr: stale: read it again\\n')
        sys.exit(1)
    for word in words[:words.index('--revision')]:
        key, _, value = word.partition('=')
        state[key] = json.loads(value) if key == 'args' else value
    if control.get('set') == 'half':
        state['model'] = 'half-written'
    state['was_set'] = True
    save()
    sys.stdout.write('crew member %s saved\\nOUT-SECRET\\n' % state['name'])
    sys.exit(1 if control.get('set') == 'nonzero-after-write' else 0)
if rest[:2] == ['crew', 'restart'] and actor == {operator!r}:
    if control.get('restart') == 'fail':
        sys.stderr.write('yardr: restart failed\\n')
        sys.exit(1)
    sys.stdout.write('%s: restart\\n' % rest[2])
    sys.exit(0)
sys.stdout.write('unexpected argv')
sys.exit(8)
"""

SOL_ARGS = ('--dangerously-bypass-approvals-and-sandbox', '-c', 'model_reasoning_effort="high"')
OPUS_ARGS = ('--permission-mode', 'auto', '--effort', 'high')
WONDER_ARGS = ('--yolo', '--reasoning-effort', 'high')


def mayor_picks(env_file=None):
    return (dict(id='sol', label='GPT-6.1 Sol high', kind='codex', model='gpt-6.1-sol', args=SOL_ARGS,
                 env_file=None, effort='high'),
            dict(id='opus', label='Opus high', kind='claude', model='opus', args=OPUS_ARGS,
                 env_file=env_file, effort='high'),
            dict(id='wonder', label='Muse Wonder high', kind='muse', model='muse-wonder-1.0', args=WONDER_ARGS,
                 env_file=None, effort='high'))


class Crash(BaseException):
    """The bridge process dying mid-handler: no except Exception catches it."""


class FakeYard:
    """GetUser as the native socket answers it: `mapped` is the Telegram id
    the operator's tgbridge identity names."""

    def __init__(self, mapped='123', role='operator'):
        self.mapped, self.role, self.calls = mapped, role, []

    def call(self, method, arguments):
        self.calls.append((method, arguments))
        if method != 'GetUser':
            raise AssertionError('unexpected native call ' + method)
        return dict(name=arguments['name'], role=self.role, identities=dict(tgbridge=dict(id=self.mapped)))


class MayorCase(unittest.TestCase):
    """A bridge with native /mayor_model on a stub yardr that keeps one crew
    member's settings in a file, refuses a stale --revision and records
    every argv; carries no tests."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        self.home = self.root / 'yard'
        self.home.mkdir(mode=0o700)
        self.state = self.root / 'crew.json'
        self.control = self.root / 'control.json'
        self.record = self.root / 'record.jsonl'
        self.set_crew(kind='codex', model='gpt-6.1-sol', args=list(SOL_ARGS))
        self.stub = self.root / 'stub-yardr'
        self.stub.write_text(CREW_STUB.format(python=sys.executable, state=str(self.state),
                                              control=str(self.control), record=str(self.record),
                                              home=str(self.home), operator='pat'))
        self.stub.chmod(0o700)
        self.store = b.Store(self.root / 'state.sqlite3', 10)
        self.telegram = FakeTelegram()
        self.yard = FakeYard()
        self.enterContext(patch.dict(os.environ, {'LANG': 'C.UTF-8'}))
        self.log = io.StringIO()
        self.bridge = self.make_bridge()

    def set_crew(self, **fields):
        state = json.loads(self.state.read_text()) if self.state.exists() else dict(
            name='yardmaster', kind='codex', model='', args=[], env_file='', revision=1,
            session_state='running', session_status='idle', session_kind='codex')
        state.update(fields)
        self.state.write_text(json.dumps(state))

    def crew(self):
        return json.loads(self.state.read_text())

    def mayor(self, **fields):
        entry = dict(crew='yardmaster', timeout=30, picks=mayor_picks(env_file=str(self.root / 'crew.env')))
        entry.update(fields)
        return entry

    def make_bridge(self, mayor=None, **config):
        mayor = self.mayor() if mayor is None else mayor
        full = dict(bot_id=999, user_id=123, chat_id=123, account_id='test_bot', yard='test-city',
                    hook_url='http://127.0.0.1:8797/hooks/tgbridge', hook_secret='HOOKSECRET',
                    user_display_name='Pat', timezone='Asia/Tokyo', menu=(), agent='yardmaster',
                    yardr_home=str(self.home), yardr_operator='pat', yardr_bin=str(self.stub),
                    yardr_mayor_model=mayor, commands=(y.mayor_model_command(mayor),))
        full.update(config)
        bridge = b.Bridge(full, self.store, 'SECRET', 'CAPABILITY', request=self.telegram,
                          state_dir=self.root)
        bridge.run_asks.client = self.yard
        return bridge

    def control_with(self, **modes):
        self.control.write_text(json.dumps(modes))

    def calls(self, verb):
        if not self.record.exists():
            return []
        return [entry for entry in map(json.loads, self.record.read_text().splitlines())
                if verb in entry['argv'][2:6]]

    def tap(self, pick, uid=60, message_id=777):
        return dict(update_id=uid, callback_query=dict(
            id='query-%d' % uid, data='mayor-model:' + pick, **{'from': dict(id=123, is_bot=False)},
            message=dict(message_id=message_id, chat=dict(id=123, type='private'))))

    def text(self, value, uid=40):
        return dict(update_id=uid, message=dict(message_id=uid + 1000, text=value,
                                                chat=dict(id=123, type='private'),
                                                **{'from': dict(id=123, is_bot=False)}))

    def drain(self, *updates, bridge=None):
        with patch('sys.stderr', self.log):
            (bridge or self.bridge).drain(list(updates))

    def answers(self):
        return self.telegram.of('answerCallbackQuery')

    def edits(self):
        return self.telegram.of('editMessageText')

    def rows(self):
        with self.store.connect() as db:
            return db.execute('SELECT update_id, pick, crew, state FROM mayor_model_taps ORDER BY update_id').fetchall()


class MayorViewTests(MayorCase):
    def test_view_shows_the_assignment_and_marks_the_current_pick(self):
        self.set_crew(session_kind='claude', env_file='/secret/crew.env')
        self.drain(self.text('/mayor_model'))
        (send,) = self.telegram.of('sendMessage')
        self.assertEqual(send['text'], '<pre>yardmaster model: GPT-6.1 Sol high\n'
                                       'codex · gpt-6.1-sol · effort high\n'
                                       'session: running · idle · on claude until it restarts</pre>')
        self.assertEqual(send['reply_markup'], {'inline_keyboard': [
            [dict(text='✓ GPT-6.1 Sol high', callback_data='mayor-model:sol')],
            [dict(text='Opus high', callback_data='mayor-model:opus')],
            [dict(text='Muse Wonder high', callback_data='mayor-model:wonder')]]})
        self.assertNotIn('crew.env', send['text'])
        (show,) = self.calls('show')
        self.assertEqual(show['argv'], ['--home', str(self.home), 'crew', 'show', 'yardmaster', '--json'])
        self.assertEqual(show['cwd'], str(self.home))
        self.assertFalse(any(key.startswith('YARDR_') for key in show['env']))

    def test_view_of_an_assignment_no_pick_has(self):
        self.set_crew(kind='grok', model='', args=[], session_kind='grok', session_status='')
        self.drain(self.text('/mayor_model'))
        (send,) = self.telegram.of('sendMessage')
        self.assertEqual(send['text'], '<pre>yardmaster model: default model (grok, effort default)\n'
                                       'grok · default model · effort default\nsession: running</pre>')
        self.assertFalse(any(button['text'].startswith('✓') for row in send['reply_markup']['inline_keyboard']
                             for button in row))

    def test_text_arguments_run_nothing_but_the_view(self):
        self.drain(self.text('/mayor_model sol'), self.text('/mayor_model opus', uid=41))
        self.assertEqual(len(self.calls('show')), 2)
        self.assertEqual(self.calls('set'), [])
        self.assertEqual(self.calls('restart'), [])
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.yard.calls, [])

    def test_missing_crew_member_is_unavailable(self):
        self.set_crew(name='someone-else')
        self.drain(self.text('/mayor_model'))
        (send,) = self.telegram.of('sendMessage')
        self.assertEqual(send['text'], '<pre>⚪ mayor model unavailable: the crew member does not exist</pre>')
        self.assertNotIn('reply_markup', send)

    def test_set_commands_lists_mayor_model(self):
        self.assertEqual(self.bridge.set_commands(),
                         [dict(command='mayor_model', description='Show the yardmaster model, tap to switch it'),
                          dict(command='decide', description='Open decisions waiting on you')])

    def test_publish_keyboard_refuses_the_bridge_owned_prefix(self):
        keyboard = [[dict(text='switch', callback_data='mayor-model:opus')]]
        self.assertEqual(self.bridge._publish_keyboard(keyboard), (None, 'foreign_callback_data'))
        # Without the table the prefix is nobody's: a publish naming it has no command.
        bare = b.Bridge(dict(self.bridge.config, commands=()), self.store, 'SECRET', 'CAPABILITY',
                        request=self.telegram, state_dir=self.root)
        del bare.config['yardr_mayor_model']
        self.assertEqual(bare._publish_keyboard(keyboard), (None, 'foreign_callback_data'))


class MayorOwnedPrefixTests(MayorCase):
    def test_another_handler_cannot_send_mayor_model_buttons(self):
        handler = self.root / 'other'
        handler.write_text('#!/bin/sh\ncat >/dev/null\necho \'{"actions":[{"type":"send","text":"x",'
                           '"keyboard":[[{"text":"OK","callback_data":"mayor-model:opus"}]]}]}\'\n')
        handler.chmod(0o700)
        other = dict(name='other', description='x', menu=True, timeout=10, handler=str(handler),
                     callback_prefix='other:')
        mayor = self.mayor()
        self.bridge = self.make_bridge(commands=(other, y.mayor_model_command(mayor)))
        self.drain(self.text('/other'))
        (send,) = self.telegram.of('sendMessage')
        self.assertEqual(send['text'], 'command other failed')
        self.assertIn('command other failed (update 40): foreign_callback_data', self.log.getvalue())


class MayorTapTests(MayorCase):
    def test_tap_switches_once_restarts_and_edits_the_message(self):
        self.drain(self.tap('opus'))
        crew = self.crew()
        self.assertEqual((crew['kind'], crew['model'], crew['args'], crew['env_file']),
                         ('claude', 'opus', list(OPUS_ARGS), str(self.root / 'crew.env')))
        (set_call,) = self.calls('set')
        self.assertEqual(set_call['argv'], [
            '--home', str(self.home), '--actor', 'pat', 'crew', 'set', 'yardmaster',
            'kind=claude', 'model=opus', 'args=["--permission-mode","auto","--effort","high"]',
            'env_file=%s' % (self.root / 'crew.env'), '--revision', 'R1'])
        (restart,) = self.calls('restart')
        self.assertEqual(restart['argv'], ['--home', str(self.home), '--actor', 'pat',
                                           'crew', 'restart', 'yardmaster'])
        self.assertEqual(self.answers(), [dict(callback_query_id='query-60', text='Model set')])
        (edit,) = self.edits()
        self.assertEqual(edit['message_id'], 777)
        self.assertEqual(edit['text'], '<pre>✅ yardmaster set to Opus high; it restarts on it once idle.\n'
                                       'yardmaster model: Opus high\nclaude · opus · effort high\n'
                                       'session: running · idle · on codex until it restarts</pre>')
        self.assertEqual(edit['reply_markup']['inline_keyboard'][1],
                         [dict(text='✓ Opus high', callback_data='mayor-model:opus')])
        self.assertEqual(self.rows(), [(60, 'opus', 'yardmaster', 'done')])
        self.assertEqual(self.yard.calls, [('GetUser', {'name': 'pat'})])

    def test_redelivered_tap_after_done_never_sets_twice(self):
        update = self.tap('opus')
        self.drain(update)
        self.store.advance(0)  # Telegram hands the same update over again
        self.drain(update, bridge=self.make_bridge())
        self.assertEqual(len(self.calls('set')), 1)
        self.assertEqual(len(self.calls('restart')), 1)
        self.assertEqual(len(self.answers()), 1)
        self.assertEqual(self.rows(), [(60, 'opus', 'yardmaster', 'done')])

    def test_redelivered_tap_after_the_reservation_only_reads_unknown(self):
        update = self.tap('opus')
        real = y.run_yardr

        def crash_before_set(argv, *args, **kwargs):
            if 'set' in argv:
                raise Crash()
            return real(argv, *args, **kwargs)
        with patch.object(y, 'run_yardr', crash_before_set), self.assertRaises(Crash):
            self.drain(update)
        self.assertEqual(self.rows(), [(60, 'opus', 'yardmaster', 'reserved')])
        self.assertEqual(self.answers(), [])
        self.drain(update, bridge=self.make_bridge())
        self.assertEqual(self.calls('set'), [])
        self.assertEqual(self.calls('restart'), [])
        self.assertEqual(self.rows(), [(60, 'opus', 'yardmaster', 'unknown')])
        self.assertEqual(self.answers(), [dict(callback_query_id='query-60', text='Switch outcome unknown',
                                               show_alert=True)])
        self.assertTrue(self.edits()[0]['text'].startswith(
            '<pre>⚪ That switch outcome is unknown; not retried. The state below is re-read.\n'))

    def test_redelivered_tap_after_the_set_before_the_record_reads_unknown(self):
        update = self.tap('opus')
        with patch.object(b.Store, 'record_mayor_tap', side_effect=Crash()), self.assertRaises(Crash):
            self.drain(update)
        self.assertEqual(len(self.calls('set')), 1)
        self.assertEqual(self.rows(), [(60, 'opus', 'yardmaster', 'reserved')])
        self.assertEqual(self.answers(), [])
        self.drain(update, bridge=self.make_bridge())
        self.assertEqual(len(self.calls('set')), 1)
        self.assertEqual(self.calls('restart'), [])
        self.assertEqual(self.rows(), [(60, 'opus', 'yardmaster', 'unknown')])
        self.assertEqual(len(self.answers()), 1)
        self.assertEqual(self.answers()[0]['text'], 'Switch outcome unknown')

    def test_set_killed_at_the_deadline_is_unknown(self):
        self.bridge = self.make_bridge(mayor=self.mayor(timeout=2))
        self.control_with(set='sleep')
        started = time.monotonic()
        self.drain(self.tap('opus'))
        self.assertLess(time.monotonic() - started, 10)
        self.assertEqual(self.rows(), [(60, 'opus', 'yardmaster', 'unknown')])
        self.assertEqual(self.calls('restart'), [])
        self.assertEqual(self.answers()[0]['text'], 'Switch outcome unknown')
        self.assertTrue(self.edits()[0]['text'].startswith('<pre>⚪ Switch outcome unknown; not retried.'))

    def test_restart_failure_is_done_and_says_restart_not_confirmed(self):
        self.control_with(restart='fail')
        self.drain(self.tap('opus'))
        self.assertEqual(self.rows(), [(60, 'opus', 'yardmaster', 'done')])
        self.assertEqual(self.answers(), [dict(callback_query_id='query-60',
                                               text='Model set; restart not confirmed', show_alert=True)])
        self.assertTrue(self.edits()[0]['text'].startswith(
            '<pre>✅ yardmaster set to Opus high; the restart was not confirmed, so its next session starts on it.\n'))

    def test_stale_revision_is_refused_with_nothing_changed(self):
        real = y.run_yardr

        def move_between(argv, *args, **kwargs):
            if 'set' in argv:
                self.set_crew(revision=self.crew()['revision'] + 1)  # another writer, same settings
            return real(argv, *args, **kwargs)
        with patch.object(y, 'run_yardr', move_between):
            self.drain(self.tap('opus'))
        crew = self.crew()
        self.assertEqual((crew['kind'], crew['model']), ('codex', 'gpt-6.1-sol'))
        self.assertEqual(self.rows(), [(60, 'opus', 'yardmaster', 'refused')])
        self.assertEqual(self.calls('restart'), [])
        self.assertEqual(self.answers()[0]['text'], 'Switch refused')
        self.assertTrue(self.edits()[0]['text'].startswith(
            '<pre>⚪ Switch refused: yardr refused the change; nothing was changed.\n'))

    def test_nonzero_set_that_wrote_the_pick_is_done(self):
        # The re-read decides: the pick read back is done and restarted.
        self.control_with(set='nonzero-after-write')
        self.drain(self.tap('opus'))
        self.assertEqual(self.rows(), [(60, 'opus', 'yardmaster', 'done')])
        self.assertEqual(len(self.calls('restart')), 1)
        self.assertEqual([a['text'] for a in self.answers()], ['Model set'])

    def test_set_that_reads_as_neither_is_unknown(self):
        self.control_with(set='half')
        self.drain(self.tap('opus'))
        self.assertEqual(self.rows(), [(60, 'opus', 'yardmaster', 'unknown')])
        self.assertEqual(self.calls('restart'), [])

    def test_failed_reread_after_set_is_unknown(self):
        self.control_with(show='fail-after-set')
        self.drain(self.tap('opus'))
        self.assertEqual(self.rows(), [(60, 'opus', 'yardmaster', 'unknown')])
        self.assertEqual(self.calls('restart'), [])
        self.assertEqual(self.edits()[0]['text'],
                         '<pre>⚪ Switch outcome unknown; not retried. The state below is re-read.\n'
                         '⚪ mayor model unavailable: yardr exited 1</pre>')
        self.assertNotIn('reply_markup', self.edits()[0])

    def test_out_of_date_pick_runs_nothing(self):
        self.drain(self.tap('grok'))
        self.assertEqual(self.answers(), [dict(callback_query_id='query-60',
                                               text='That model button is out of date.', show_alert=True)])
        self.assertFalse(self.record.exists())
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.yard.calls, [])
        self.assertEqual(self.edits(), [])

    def test_tap_on_the_current_pick_changes_nothing(self):
        self.drain(self.tap('sol'))
        self.assertEqual(self.calls('set'), [])
        self.assertEqual(self.calls('restart'), [])
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.answers()[0]['text'], 'Already on that model')
        self.assertTrue(self.edits()[0]['text'].startswith(
            '<pre>⚪ yardmaster already runs GPT-6.1 Sol high; nothing was changed.\n'))

    def test_two_taps_on_two_picks_in_a_row(self):
        self.drain(self.tap('opus', uid=60), self.tap('wonder', uid=61))
        crew = self.crew()
        self.assertEqual((crew['kind'], crew['model'], crew['args']), ('muse', 'muse-wonder-1.0', list(WONDER_ARGS)))
        # The opus env_file stays: a pick without one keeps the crew member's.
        self.assertEqual(crew['env_file'], str(self.root / 'crew.env'))
        self.assertEqual([call['argv'][-1] for call in self.calls('set')], ['R1', 'R2'])
        self.assertEqual(len(self.calls('restart')), 2)
        self.assertEqual(self.rows(), [(60, 'opus', 'yardmaster', 'done'), (61, 'wonder', 'yardmaster', 'done')])
        self.assertEqual([answer['text'] for answer in self.answers()], ['Model set', 'Model set'])

    def test_refused_operator_mapping_reserves_and_runs_nothing(self):
        for uid, yard in ((70, FakeYard(mapped='456')), (71, FakeYard(role='member'))):
            with self.subTest(mapped=yard.mapped, role=yard.role):
                self.telegram.calls.clear()
                self.yard = yard
                self.drain(self.tap('opus', uid=uid), bridge=self.make_bridge())
                self.assertEqual(self.answers(), [dict(
                    callback_query_id='query-%d' % uid, show_alert=True,
                    text="Switch refused: you are not this yard's operator; nothing was changed.")])
                self.assertEqual(yard.calls, [('GetUser', {'name': 'pat'})])
                self.assertFalse(self.record.exists())
                self.assertEqual(self.rows(), [])
                self.assertEqual(self.edits(), [])

    def test_unreachable_yard_refuses_before_any_reservation(self):
        class Down:
            def call(self, method, arguments):
                raise run_asks_module().RPCUnavailable('native GetUser unavailable before send')
        self.yard = Down()
        self.drain(self.tap('opus'), bridge=self.make_bridge())
        self.assertEqual(self.answers()[0]['text'],
                         'Switch refused: the yard did not confirm the operator; nothing was changed.')
        self.assertFalse(self.record.exists())
        self.assertEqual(self.rows(), [])

    def test_unreadable_crew_refuses_before_any_reservation(self):
        self.control_with(show='fail')
        self.drain(self.tap('opus'))
        self.assertEqual(self.rows(), [])
        self.assertEqual(self.calls('set'), [])
        self.assertEqual(self.answers()[0]['text'], 'Switch refused')

    def test_no_yardr_output_reaches_the_chat_or_the_log(self):
        self.control_with(set='fail')
        self.drain(self.tap('opus', uid=60))
        self.control_with(show='fail')
        self.drain(self.text('/mayor_model', uid=61))
        shown = json.dumps(self.telegram.calls) + self.log.getvalue()
        for secret in ('hunter2', 'abc123', 'OUT-SECRET', 'refused, token', 'crew.env'):
            self.assertNotIn(secret, shown)
        self.assertIn('command mayor_model yardr crew set: status=1 problem=None', self.log.getvalue())

    def test_tap_without_a_message_sends_the_state(self):
        update = self.tap('opus')
        del update['callback_query']['message']['message_id']
        self.drain(update)
        self.assertEqual(self.edits(), [])
        (send,) = self.telegram.of('sendMessage')
        self.assertTrue(send['text'].startswith('<pre>✅ yardmaster set to Opus high;'))


def run_asks_module():
    """The run_asks the bridge loaded, whose classes yard_commands catches."""
    return b.run_asks


class MayorDryRunTests(MayorCase):
    def settings(self):
        path = write_settings(self.root)
        text = path.read_text()
        text = re.sub(r'\n\[\[menu\]\]\ncommand = "quota"\ndescription = [^\n]*\n', '\n', text)
        marker = '\n[yardr]\n'
        start = text.index(marker)
        text = (text[:start] + marker + 'bin = "%s"\nhome = "%s"\noperator = "pat"\n' % (self.stub, self.home)
                + text[start + len(marker):])
        text += ('\n[yardr.mayor_model]\n'
                 '\n[[yardr.mayor_model.picks]]\nid = "sol"\nlabel = "GPT-6.1 Sol high"\nkind = "codex"\n'
                 'model = "gpt-6.1-sol"\nargs = ["--dangerously-bypass-approvals-and-sandbox", "-c", '
                 '"model_reasoning_effort=\\"high\\""]\n'
                 '\n[[yardr.mayor_model.picks]]\nid = "opus"\nlabel = "Opus high"\nkind = "claude"\n'
                 'model = "opus"\nargs = ["--permission-mode", "auto", "--effort", "high"]\n'
                 'env_file = "%s"\n' % (self.root / 'crew.env'))
        path.write_text(text)
        return path

    def run_cli(self, *argv):
        from test_bridge import run_main
        out, err = io.StringIO(), io.StringIO()
        with patch('sys.stdout', out), patch('sys.stderr', err):
            run_main(self, 'telegram-bridge', '--settings', str(self.settings()), *argv)
        return out.getvalue()

    def test_dry_run_prints_the_two_commands_and_writes_nothing(self):
        before = self.state.read_text()
        out = self.run_cli('mayor-model', '--dry-run', 'opus')
        self.assertEqual(out, (
            'yardmaster model: GPT-6.1 Sol high\ncodex · gpt-6.1-sol · effort high\n'
            'session: running · idle\na tap on opus runs:\n'
            "  %s --home %s --actor pat crew set yardmaster kind=claude model=opus "
            "'args=[\"--permission-mode\",\"auto\",\"--effort\",\"high\"]' env_file=%s --revision R1\n"
            '  %s --home %s --actor pat crew restart yardmaster\n'
            % (self.stub, self.home, self.root / 'crew.env', self.stub, self.home)))
        self.assertEqual(self.state.read_text(), before)
        self.assertEqual([entry['argv'][2:4] for entry in map(json.loads, self.record.read_text().splitlines())],
                         [['crew', 'show']])
        self.assertFalse((self.root / 'state').exists())

    def test_dry_run_on_the_current_pick_says_it_changes_nothing(self):
        out = self.run_cli('mayor-model', '--dry-run', 'sol')
        self.assertTrue(out.endswith('a tap on sol changes nothing: yardmaster already runs it\n'))

    def test_dry_run_of_an_unknown_pick_fails(self):
        with self.assertRaisesRegex(b.TransportError, r"mayor model unavailable: no pick 'grok'"):
            self.run_cli('mayor-model', '--dry-run', 'grok')
        self.assertFalse(self.record.exists())

    def test_mayor_model_needs_dry_run(self):
        with self.assertRaises(SystemExit):
            self.run_cli('mayor-model')


class MayorRealNativeTests(MayorCase):
    """/mayor_model on an isolated server of the real build: a codex crew
    member, the view, a tap to a claude pick that crew show then holds with
    the restart accepted, a revision moved by the test between the bridge's
    read and its set, and an operator identity that does not map to the
    paired user. The operator check is the real GetUser on the socket."""

    def setUp(self):
        super().setUp()
        self.binary = testyardr.binary()
        if self.binary is None:
            self.skipTest('yardr binary required for isolated native integration')

    def test_real_native_mayor_model(self):
        root = Path(self.enterContext(tempfile.TemporaryDirectory()))
        server, home, command, env = testyardr.start(root, self.binary)
        self.addCleanup(testyardr.stop, server)
        # The bridge's yardr children get the isolated HOME and PATH too.
        self.enterContext(patch.dict(os.environ, {'HOME': env['HOME'], 'PATH': env['PATH']}))

        def cli(*args):
            result = subprocess.run(command + list(args), env=env, capture_output=True, text=True)
            self.assertEqual(result.returncode, 0, result.stderr)
            return result.stdout

        def show():
            config = json.loads(cli('crew', 'show', 'mayor', '--json'))['crew']['config']
            return config.get('kind'), config.get('model'), config.get('args'), config.get('env_file')

        cli('init', '--harness', 'claude')
        source = root / 'tgbridge.yaml'
        source.write_text('name: tgbridge\nbase: https://example.invalid\ncalls:\n  health:\n    kind: read\n'
                          '    request: {method: GET, path: /health}\nhook:\n  bodies: unique\n  verify:\n'
                          '    scheme: secret\n    signature: {header: X-Secret}\n  normalize: [/bin/false]\n')
        cli('source', 'add', str(source))
        cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
        cli('crew', 'add', 'mayor', '--set', 'kind=codex')
        env_file = root / 'mayor.env'
        env_file.write_text('')
        env_file.chmod(0o600)
        picks = (dict(id='sol', label='GPT-6.1 Sol high', kind='codex', model='gpt-6.1-sol', args=SOL_ARGS,
                      env_file=None, effort='high'),
                 dict(id='opus', label='Opus high', kind='claude', model='opus', args=OPUS_ARGS,
                      env_file=str(env_file), effort='high'))
        mayor = dict(crew='mayor', timeout=60, picks=picks)

        def bridge():
            made = self.make_bridge(mayor=mayor, yardr_home=str(home), yardr_bin=self.binary)
            made.run_asks.client = run_asks_module().YardSocketClient(home, 'tgbridge')
            return made

        self.drain(self.text('/mayor_model', uid=50), bridge=bridge())
        (view,) = self.telegram.of('sendMessage')
        self.assertTrue(view['text'].startswith('<pre>mayor model: default model (codex, effort default)\n'
                                                'codex · default model · effort default\n'), view['text'])
        self.assertEqual([row[0]['callback_data'] for row in view['reply_markup']['inline_keyboard']],
                         ['mayor-model:sol', 'mayor-model:opus'])

        # A tap switches it once; crew show holds the pick; the restart is accepted.
        self.drain(self.tap('opus', uid=51), bridge=bridge())
        self.assertEqual(show(), ('claude', 'opus', list(OPUS_ARGS), str(env_file)))
        self.assertEqual(self.answers()[-1], dict(callback_query_id='query-51', text='Model set'))
        self.assertTrue(self.edits()[-1]['text'].startswith(
            '<pre>✅ mayor set to Opus high; it restarts on it once idle.\nmayor model: Opus high\n'))
        self.assertEqual(self.rows(), [(51, 'opus', 'mayor', 'done')])
        self.store.advance(0)
        self.drain(self.tap('opus', uid=51), bridge=bridge())
        self.assertEqual(show(), ('claude', 'opus', list(OPUS_ARGS), str(env_file)))
        self.assertEqual(len(self.answers()), 1)

        # The revision moves between the bridge's read and its set: refused.
        real = y.run_yardr

        def move_between(argv, *args, **kwargs):
            if 'set' in argv:
                cli('crew', 'set', 'mayor', '--max-age', '12h')
            return real(argv, *args, **kwargs)
        with patch.object(y, 'run_yardr', move_between):
            self.drain(self.tap('sol', uid=52), bridge=bridge())
        self.assertEqual(show(), ('claude', 'opus', list(OPUS_ARGS), str(env_file)))
        self.assertEqual(self.answers()[-1]['text'], 'Switch refused')
        self.assertTrue(self.edits()[-1]['text'].startswith(
            '<pre>⚪ Switch refused: yardr refused the change; nothing was changed.\n'))
        self.assertEqual(self.rows()[-1], (52, 'sol', 'mayor', 'refused'))

        # The operator's identity no longer maps to the paired user: refused
        # before any reservation, with nothing run.
        cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '456')
        self.drain(self.tap('sol', uid=53), bridge=bridge())
        self.assertEqual(self.answers()[-1]['text'],
                         "Switch refused: you are not this yard's operator; nothing was changed.")
        self.assertEqual([row[0] for row in self.rows()], [51, 52])
        self.assertEqual(show(), ('claude', 'opus', list(OPUS_ARGS), str(env_file)))
