"""A new crew member's own bot, installed beside a yard's primary and second bots
(yardr/SETUP.md, "Add a second crew member's bot").

The slice runs the installed release as its own process against a fake Telegram behind a
fake curl, on a real isolated yardr server; no daemon, model, real settings or secret.
"""
from contextlib import closing
import fcntl
import hashlib
import http.server
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
import run_asks as n  # noqa: E402
import testports  # noqa: E402
import testyardr  # noqa: E402
from test_settings import git_env, make_source, tree_digest  # noqa: E402

PACK = Path(__file__).resolve().parents[1]
CHAT = 123
SKILLS = ('telegram-formatting', 'telegram-attachments', 'telegram-voice-transcripts')
MAYOR_CALLS = ('send', 'react', 'pin', 'unpin', 'ask_close')
# The shape of the crew add the authority contract proposes, with synthetic paths.
MAYOR_ARGS = ['--dangerously-bypass-approvals-and-sandbox', '-c', 'model_reasoning_effort=medium',
              '-c', 'mcp_servers.figma.enabled=false', '-c', 'mcp_servers.posthog.enabled=false']
# A fake curl: Bot API calls go to a fake Telegram kept in one JSON file under a lock, which
# forgets updates below the offset a poll confirms; every other URL goes to the real curl.
FAKE_CURL = r'''#!{python}
import fcntl, json, os, subprocess, sys, time
from pathlib import Path
TOP = Path({top!r})
config = sys.stdin.read()
fields = {{}}
for line in config.splitlines():
    key, _, value = line.partition(' = ')
    fields.setdefault(key, []).append(value)
url = json.loads(fields['url'][0])
if not url.startswith('https://api.telegram.org/'):
    done = subprocess.run([{real!r}, *sys.argv[1:]], input=config, capture_output=True, text=True)
    sys.stdout.write(done.stdout)
    sys.stderr.write(done.stderr)
    sys.exit(done.returncode)
method = url.rsplit('/', 1)[1]
body = json.loads(json.loads(fields['data'][0])) if 'data' in fields else {{}}
status, wait = 200, False
with open(TOP / 'lock', 'a') as lock:
    fcntl.flock(lock, fcntl.LOCK_EX)
    state = json.loads((TOP / 'state.json').read_text())
    if method == 'getUpdates' and state.get('conflict'):
        status, answer = 409, dict(ok=False, error_code=409, description='Conflict: terminated by other getUpdates')
    elif method == 'getUpdates':
        state['polls'].append(body['offset'])
        state['updates'] = [u for u in state['updates'] if u['update_id'] >= body['offset']]
        answer, wait = dict(ok=True, result=state['updates']), not state['updates']
    elif method == 'getMe':
        answer = dict(ok=True, result=dict(id=state['bot_id'], is_bot=True, username=state['username']))
    elif method == 'getWebhookInfo':
        answer = dict(ok=True, result=dict(url=''))
    elif method == 'sendMessage':
        state['next'] += 1
        state['sent'].append(dict(method=method, body=body, message_id=state['next']))
        answer = dict(ok=True, result=dict(message_id=state['next'], chat=dict(id=body['chat_id'])))
    else:
        state['sent'].append(dict(method=method, body=body))
        answer = dict(ok=True, result=True)
    (TOP / 'state.tmp').write_text(json.dumps(state))
    os.replace(TOP / 'state.tmp', TOP / 'state.json')
if wait:
    time.sleep(.02)
print(json.dumps(answer))
print(f'{{status}} 0.001 0.002 0', end='')
'''


def install_env(home, bin_dir):
    """An installer's environment in a temporary home: no yard or bridge variable of this session."""
    env = {k: v for k, v in git_env(home).items() if not k.startswith(('YARDR_', 'TGBRIDGE_')) and k != b.SETTINGS_ENV}
    env.update(HOME=str(home), XDG_CONFIG_HOME=str(home / '.config'), TELEGRAM_PACK_PLATFORM='linux',
               PATH=f'{bin_dir}:/usr/local/bin:/usr/bin:/bin')
    return env


class FakeTelegram:
    """The fake Telegram one installed bridge process polls, through FAKE_CURL in bin."""

    def __init__(self, top, bin_dir, bot_id, username):
        self.top = Path(top)
        self.top.mkdir()
        self.write(dict(bot_id=bot_id, username=username, updates=[], polls=[], sent=[], next=500))
        real = shutil.which('curl', path='/usr/local/bin:/usr/bin:/bin')
        if real is None:
            raise unittest.SkipTest('curl required for the installed bridge')
        bin_dir.mkdir(exist_ok=True)
        curl = bin_dir / 'curl'
        curl.write_text(FAKE_CURL.format(python=sys.executable, top=str(self.top), real=real))
        curl.chmod(0o755)

    def write(self, state):
        (self.top / 'state.tmp').write_text(json.dumps(state))
        os.replace(self.top / 'state.tmp', self.top / 'state.json')

    def change(self, apply=None):
        with open(self.top / 'lock', 'a') as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            state = json.loads((self.top / 'state.json').read_text())
            if apply is not None:
                apply(state)
                self.write(state)
            return state

    def queue(self, *updates):
        self.change(lambda state: state['updates'].extend(updates))

    def sent(self):
        return self.change()['sent']


def message(update_id, text, sender=CHAT, message_id=None):
    return dict(update_id=update_id, message=dict(message_id=message_id or update_id, text=text, date=1,
                chat=dict(id=CHAT, type='private'), **{'from': dict(id=sender, is_bot=False, first_name='Pat')}))


def wait_for(condition, what, timeout=30):
    deadline = time.monotonic() + timeout
    while True:
        value = condition()
        if value:
            return value
        if time.monotonic() > deadline:
            raise AssertionError('timed out waiting for ' + what)
        time.sleep(.01)


def ledger_rows(path, query, args=()):
    with closing(sqlite3.connect(f'file:{path}?mode=ro', uri=True)) as db, db:
        return db.execute(query, args).fetchall()


class Yard:
    """A real isolated yardr server with its hook listener on a free loopback port."""

    def __init__(self, case, root):
        binary = testyardr.binary()
        if binary is None:
            case.skipTest('yardr binary required for the isolated native slice')
        self.case, self.root = case, root
        self.server, self.home, self.command, self.env = testyardr.start(root, binary)
        case.addCleanup(lambda: testyardr.stop(self.server))
        self.cli('settings', 'set', 'operator', 'pat')
        self.cli('settings', 'set', 'default_kind', 'codex')
        probe = testports.bind_test_server(http.server.BaseHTTPRequestHandler)
        port = probe.server_address[1]
        probe.server_close()
        self.cli('settings', 'set', 'hook_addr', f'127.0.0.1:{port}')
        self.restart()

    def restart(self):
        testyardr.stop(self.server)
        log_path = self.root / 'isolated-server.log'
        with log_path.open('w') as log:
            self.server = subprocess.Popen(self.command + ['server', 'run'], env=self.env, cwd=self.root,
                                           stdout=log, stderr=log)
        match = wait_for(lambda: re.search(r'hook receiver ready.*?addr[= ](?:"?)(127\.0\.0\.1:\d+)',
                                           log_path.read_text()), 'the isolated hook receiver')
        self.hook = 'http://' + match[1]

    def run(self, *args, actor='pat', env=None):
        return subprocess.run([self.command[0], '--home', str(self.home), '--actor', actor, *args],
                              env=env or self.env, cwd=self.root, text=True, capture_output=True, timeout=30)

    def refused(self, words, *args, actor='pat', env=None):
        result = self.run(*args, actor=actor, env=env)
        self.case.assertNotEqual(result.returncode, 0, result.stdout)
        self.case.assertIn(words, result.stderr)

    def supports_skill(self):
        # The crew-skills step is fork-only: the public core has no `yardr skill`.
        return self.run('skill').returncode == 0

    def cli(self, *args, actor='pat', env=None):
        result = self.run(*args, actor=actor, env=env)
        self.case.assertEqual(result.returncode, 0, result.stderr)
        return result.stdout

    def json(self, *args, **kwargs):
        return json.loads(self.cli(*args, '--json', **kwargs))

    def inbox(self, crew):
        return self.json('inbox', '--crew', crew)['events']


def private(path, text):
    path.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    path.write_text(text)
    path.chmod(0o600)
    return path


class InProcessBot:
    """One bot through the checkout's bridge in this process, with a fake Telegram transport
    (getUpdates forgets what an offset confirmed), a callback server and a rendered pack,
    relaying to the real hook listener. `before` and `after` see each Bot API call before and
    after Telegram takes it, and may raise to fail or crash there."""

    def __init__(self, case, yard, source, crew, bot_id, capability, native=False, state=None, names=None):
        self.case, self.source, self.crew, self.capability = case, source, crew, capability
        if state is None:
            state = yard.root / (source + '-state')
            state.mkdir(mode=0o700)
        self.hook_secret = source + '-hook'
        identity = names or dict(account_id=source + '_bot', yard='odyssey')
        config = dict(bot_id=bot_id, user_id=CHAT, chat_id=CHAT,
                      agent=crew, hook_url=f'{yard.hook}/hooks/{source}', hook_secret=self.hook_secret,
                      user_display_name='Pat', timezone='UTC', yardr_source=source, **identity)
        if native:
            config.update(yardr_home=str(yard.home), yardr_operator='pat', yardr_cards=True)
        self.sent, self.updates, self.next, self.before, self.after = [], [], 100, None, None
        self.bridge = b.Bridge(config, b.Store(state / 'telegram-bridge.sqlite3', 0), 'TOKEN-' + source, capability,
                               request=self.request, state_dir=state, sleep=lambda seconds: None)
        self.bridge.reactions['on_accept'], self.bridge.typing_seconds = False, 0
        handler = b._callback_handler(self.bridge)
        server = testports.bind_test_server(handler)
        threading.Thread(target=server.serve_forever, kwargs={'poll_interval': 0.01}, daemon=True).start()
        case.addCleanup(server.server_close)
        case.addCleanup(server.shutdown)
        self.port = server.server_address[1]
        pack = yard.root / (source + '-pack')
        pack.mkdir()
        b.stage_pack(b.render_pack(PACK / 'yardr', self.port, source), pack)
        self.env_file = private(yard.root / (source + '.env'), f'TGBRIDGE_SECRET={capability}\n')
        yard.cli('pack', 'apply', str(pack), '--yes', '--source-env', f'{source}={self.env_file}')
        yard.cli('source', 'set', source, 'updates_per_hour=300')
        yard.cli('user', 'identity', 'pat', '--source', source, '--id', str(CHAT))
        hook = private(yard.root / (source + '-hook.secret'), self.hook_secret)
        yard.cli('hook', 'add', source, '--source', source, '--secret-file', str(hook))

    def request(self, url, body, *args, **kwargs):
        if not url.startswith('https://api.telegram.org/'):
            req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                         headers=kwargs['headers'] | {'Content-Type': 'application/json'})
            with urllib.request.urlopen(req, timeout=10) as response:
                return response.status, json.load(response)
        method = url.rsplit('/', 1)[1]
        if self.before:
            self.before(method, body)
        if method == 'getUpdates':
            self.updates = [u for u in self.updates if u['update_id'] >= body['offset']]
            return 200, dict(ok=True, result=list(self.updates))
        self.sent.append((method, body))
        if self.after:
            self.after(method, body)
        if method in ('sendMessage', 'editMessageText'):
            self.next += 1
            return 200, dict(ok=True, result=dict(message_id=self.next, chat=dict(id=CHAT)))
        return 200, dict(ok=True, result=True)

    def texts(self):
        return [body['text'] for method, body in self.sent if method == 'sendMessage']


class SecondBotTests(unittest.TestCase):
    def setUp(self):
        # A short root: the native socket's path has a length limit.
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='mh')))

    def install_mayor(self, yard):
        """Install the mayor's instance from a fixture checkout of this pack, then remove the
        checkout; returns (settings, install root, environment, callback capability)."""
        home = self.root / 'home'
        home.mkdir()
        source = self.root / 'source'
        commit = make_source(self, source, home)
        config, state = home / '.config/odyssey-mayor-telegram', home / '.local/state/odyssey-mayor-telegram'
        token = private(config / 'telegram-bot-token', 'SYNTHETIC-MAYOR-TOKEN\n')
        state.mkdir(parents=True, mode=0o700)
        primary_locks = home / '.config/odyssey-telegram'
        primary_locks.mkdir(parents=True, mode=0o700)
        probe = testports.bind_test_server(http.server.BaseHTTPRequestHandler)
        self.mayor_port = probe.server_address[1]
        probe.server_close()
        settings = private(config / 'telegram-bridge.toml', f'''yard = "odyssey"
crew = "mayor"
unit = "odyssey-mayor-telegram.service"
state_dir = "{state}"
port = {self.mayor_port}
python = "{sys.executable}"
token_file = "{token}"
callback_secret_file = "{config / 'callback-secret'}"
lock_dir = "{primary_locks}"
user_display_name = "Pat"
timezone = "UTC"

[yardr]
source = "tgmayor"
hook_url = "{yard.hook}/hooks/tgmayor"
hook_secret_file = "{config / 'hook-secret'}"

[bot]
bot_id = 1000000013
user_id = {CHAT}
chat_id = {CHAT}
account_id = "synthetic_mayor_bot"

[transcribe]
enabled = false

[reactions]
on_accept = false

[typing]
seconds = 0
''')
        bin_dir = self.root / 'bin'
        self.telegram = FakeTelegram(self.root / 'telegram', bin_dir, 1000000013, 'synthetic_mayor_bot')
        env = install_env(home, bin_dir)

        def install(*args):
            result = subprocess.run(['sh', str(source / 'install.sh'), '--settings', str(settings), *args],
                                    env=env, capture_output=True, text=True, timeout=120)
            self.assertEqual(result.returncode, 0, result.stdout + result.stderr)
            return result.stdout

        root = home / '.local/share/yardr-telegram/odyssey-mayor-telegram'
        self.assertIn(f'would write pack {root}/pack (base http://127.0.0.1:{self.mayor_port})', install('--dry-run'))
        self.assertFalse(root.exists())
        out = install()
        self.assertIn(f'wrote pack {root}/pack (base http://127.0.0.1:{self.mayor_port})', out)
        self.assertEqual(os.readlink(root / 'current'), f'releases/{commit}')
        # The first activation has no previous release: rollback refuses and changes nothing.
        before = tree_digest(root)
        refused = subprocess.run(['sh', str(root / 'current/install.sh'), '--settings', str(settings), '--rollback'],
                                 env=env, capture_output=True, text=True, timeout=60)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('has no previous release to roll back to', refused.stderr)
        self.assertEqual(tree_digest(root), before)
        shutil.rmtree(source)
        checked = subprocess.run([str(root / 'bin/telegram-bridge'), 'check-install'], env=env, cwd=self.root,
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertIn('ok    release files match SHA256SUMS', checked.stdout)
        self.assertIn('name: "tgmayor"', (root / 'pack/tgbridge.yaml').read_text())
        self.release_sums = hashlib.sha256((root / 'current/SHA256SUMS').read_bytes()).hexdigest()
        capability = (config / 'callback-secret').read_text().strip()
        return settings, root, env, capability, state

    def start_mayor(self, root, env):
        self.starts = getattr(self, 'starts', 0) + 1
        self.mayor_log = self.root / f'mayor-bridge-{self.starts}.log'
        with self.mayor_log.open('w') as log:
            process = subprocess.Popen([str(root / 'bin/telegram-bridge'), 'run'], env=env, cwd=self.root,
                                       stdout=log, stderr=log)
        self.addCleanup(self.stop_mayor, process)
        wait_for(lambda: process.poll() is not None or 'Telegram bridge ready' in self.mayor_log.read_text(),
                 'the mayor bridge')
        self.assertIsNone(process.poll(), self.mayor_log.read_text())
        return process

    def stop_mayor(self, process):
        if process.poll() is None:
            process.send_signal(signal.SIGTERM)
            try:
                process.wait(timeout=30)
            except subprocess.TimeoutExpired:
                process.kill()
                process.wait(timeout=10)

    def test_installed_mayor_bot_reaches_its_inbox_and_answers_through_its_own_source(self):
        yard = Yard(self, self.root)
        settings, root, env, capability, state = self.install_mayor(yard)
        ledger = state / 'telegram-bridge.sqlite3'
        has_skill = yard.supports_skill()
        if has_skill:
            for name in SKILLS:
                yard.cli('skill', 'add', str(root / 'current/skills' / name))
        else:
            print('SKIP skill add: this core offers no `yardr skill`; the rest runs without installed skills')
        primary = InProcessBot(self, yard, 'tgbridge', 'yardmaster', 1000000001, 'PRIMARY-CAPABILITY', native=True)
        second = InProcessBot(self, yard, 'tgsecretary', 'secretary', 1000000003, 'SECOND-CAPABILITY')
        for bot, calls in ((primary, MAYOR_CALLS + ('run_ask', 'ask_card')), (second, MAYOR_CALLS)):
            yard.cli('crew', 'add', bot.crew, '--dir', str(self.root), '--on=', '--set', 'kind=codex',
                     '--set', 'env_file=' + str(bot.env_file))
            for call in calls:
                yard.cli('source', 'grant', bot.source, call, '--crew', bot.crew)
            yard.cli('subscribe', bot.crew, bot.source, '--scope', 'all', '--where', 'type=message')
        others = {name: yard.json('source', 'show', name) for name in ('tgbridge', 'tgsecretary')}

        # The mayor's source, credential and hook, from the installed release alone.
        mayor_env = private(self.root / 'crew-mayor.env', f'TGBRIDGE_SECRET={capability}\n')
        yard.cli('pack', 'check', str(root / 'pack'))
        yard.cli('pack', 'apply', str(root / 'pack'), '--yes', '--source-env', f'tgmayor={mayor_env}')
        yard.cli('source', 'set', 'tgmayor', 'calls_per_hour=600')
        yard.cli('source', 'set', 'tgmayor', 'updates_per_hour=300')
        hook_secret = settings.parent / 'hook-secret'
        yard.cli('hook', 'add', 'tgmayor', '--source', 'tgmayor', '--secret-file', str(hook_secret), '--unknown', 'ignore')
        yard.cli('user', 'identity', 'pat', '--source', 'tgmayor', '--id', str(CHAT))

        # Traffic before the mayor exists never reaches its inbox: its cursor starts at the head.
        yard.cli('mail', 'yardmaster', 'synthetic note before the mayor')
        head = max(event['seq'] for event in yard.inbox('yardmaster'))
        role = private(self.root / 'roles/mayor.md', '# Mayor\n\nSynthetic role for the isolated slice.\n')
        extra = ['--set', 'skills=' + json.dumps(list(SKILLS))] if has_skill else []
        if not has_skill:
            print('SKIP mayor skills: this core takes no `skills` crew key; the mayor runs skill-less')
        yard.cli('crew', 'add', 'mayor', '--runner', 'herdr', '--dir', str(self.root), '--on=',
                 '--context-handoff', '60', '--max-age', '24h', '--handoff-grace', '15m',
                 '--set', 'kind=codex', '--set', 'model=gpt-6.1-sol', '--set', 'args=' + json.dumps(MAYOR_ARGS),
                 '--set', 'prompt=' + str(role), '--set', 'env_file=' + str(mayor_env), *extra)
        crew = yard.json('crew', 'show', 'mayor')['crew']
        self.assertGreaterEqual(crew['cursor'], head)
        self.assertEqual(yard.inbox('mayor'), [])
        self.assertEqual((crew['runner'], crew['on'], crew['context_handoff'], crew['max_age'], crew['handoff_grace']),
                         ('herdr', [], '60', '24h', '15m'))
        expected = dict(kind='codex', model='gpt-6.1-sol', args=MAYOR_ARGS, prompt=str(role),
                        env_file=str(mayor_env))
        if has_skill:
            expected['skills'] = list(SKILLS)
        self.assertEqual(crew['config'], expected)
        # Configuration, not a running assistant: no daemon runs here, and the session is stopped.
        self.assertEqual(crew['state'], 'stopped')
        primed = yard.cli('prime', '--crew', 'mayor')
        self.assertIn('Synthetic role for the isolated slice.', primed)
        if has_skill:
            for name in SKILLS:
                self.assertIn(f'`{name}`', primed)
        else:
            print('SKIP primed skills: no installed skills to render on this core')
        for call in MAYOR_CALLS:
            yard.cli('source', 'grant', 'tgmayor', call, '--crew', 'mayor')
        yard.cli('subscribe', 'mayor', 'tgmayor', '--scope', 'all', '--where', 'type=message')
        for name, before in others.items():
            self.assertEqual(yard.json('source', 'show', name), before, name)

        # Subscribed before anything is delivered: a relay's mail and the bot's message both land.
        yard.cli('mail', 'mayor', 'synthetic relay for the mayor', actor='yardmaster')
        process = self.start_mayor(root, env)
        self.telegram.queue(message(95000, 'tgmayor inbound'))
        wait_for(lambda: ledger_rows(ledger, 'SELECT offset FROM cursor')[0][0] == 95001, 'the mayor poll')
        for bot in (primary, second):
            bot.bridge.drain([message(95000, bot.source + ' inbound')])
            self.assertEqual(bot.bridge.store.offset(), 95001)
        events = yard.inbox('mayor')
        self.assertEqual([event['kind'] for event in events], ['mail', 'hook'], events)
        delivery = events[1]
        self.assertIn('tgmayor inbound', json.dumps(delivery))
        for bot in (primary, second):
            seen = json.dumps(yard.inbox(bot.crew))
            self.assertIn(bot.source + ' inbound', seen)
            self.assertNotIn('tgmayor inbound', seen)
        self.assertNotIn('tgbridge inbound', json.dumps(events))
        self.assertNotIn('tgsecretary inbound', json.dumps(events))
        # A restart of the yard keeps what it accepted, unread; one of the bridge relays nothing twice.
        yard.restart()
        self.assertEqual(yard.inbox('mayor'), events)
        self.stop_mayor(process)
        process = self.start_mayor(root, env)
        wait_for(lambda: self.telegram.change()['polls'][-1:] == [95001], 'the restarted poll')
        self.assertEqual(yard.inbox('mayor'), events)

        # The reply goes out through the mayor's bot alone, with a durable receipt.
        caller = dict(yard.env, TGBRIDGE_SECRET=capability)
        before = (list(primary.sent), list(second.sent))
        yard.cli('reply', str(delivery['seq']), '--crew', 'mayor', 'synthetic mayor reply', env=caller)
        sends = [call for call in self.telegram.sent() if call['method'] == 'sendMessage']
        self.assertEqual([(call['body']['chat_id'], call['body']['text']) for call in sends],
                         [(CHAT, 'synthetic mayor reply')])
        self.assertEqual((primary.sent, second.sent), before)
        receipts = [json.loads(row[0]) for row in ledger_rows(ledger, 'SELECT receipt FROM deliveries')]
        self.assertEqual([r['message_id'] for r in receipts if r.get('delivered') is True], [str(sends[0]['message_id'])])
        self.assertIn(('out', 'synthetic mayor reply'),
                      ledger_rows(ledger, 'SELECT direction, text FROM messages'))

        # Each bot's credential opens its own API only; a crew member calls its own source only.
        for port, secret in ((self.mayor_port, 'PRIMARY-CAPABILITY'), (primary.port, capability),
                             (second.port, capability)):
            req = urllib.request.Request(f'http://127.0.0.1:{port}/v1/send', data=b'{"text":"x"}',
                                         headers={'Authorization': 'Bearer ' + secret,
                                                  'Content-Type': 'application/json'})
            with self.assertRaises(urllib.error.HTTPError) as refused:
                urllib.request.urlopen(req, timeout=10)
            self.assertEqual(refused.exception.code, 403)
            refused.exception.close()
        # Answering the primary's delivery with the mayor's credential reaches the primary, which refuses it.
        yard.refused('HTTP 403', 'reply', str(yard.inbox('yardmaster')[-1]['seq']), '--crew', 'mayor', 'cross reply',
                     env=caller)
        for source in ('tgbridge', 'tgsecretary'):
            yard.refused('crew member mayor is not granted it', 'call', source, 'send', 'text=taken', actor='mayor')
        for call, fields in (('run_ask', ['bead=sample-1', 'ask=1']), ('ask_card', ['bead=sample-1', 'ask=1']),
                             ('siding_card', ['depot=synthetic'])):
            yard.refused('crew member mayor is not granted it', 'call', 'tgmayor', call, *fields, actor='mayor')
        self.assertTrue(yard.json('call', 'tgmayor', 'send', 'text=granted send', 'key=mayor-granted',
                                  actor='mayor')['delivered'])
        yard.cli('source', 'revoke', 'tgmayor', 'send', '--crew', 'mayor')
        yard.refused('crew member mayor is not granted it', 'call', 'tgmayor', 'send', 'text=revoked', actor='mayor')
        yard.cli('source', 'grant', 'tgmayor', 'send', '--crew', 'mayor')
        self.assertEqual([c['body']['text'] for c in self.telegram.sent() if c['method'] == 'sendMessage'],
                         ['synthetic mayor reply', 'granted send'])
        self.assertEqual((primary.sent, second.sent), before)

        # A native proposal is the primary's: the mayor's bot can neither see nor approve it.
        yard.cli('user', 'add', 'proposer')
        yard.cli('bead', 'create', 'Synthetic operation', '--id', 'sample-1')
        yard.cli('bead', 'ask', 'sample-1', 'Lower the mayor limit?', '--to', 'pat', '--context', 'Isolated slice',
                 '--run', 'source set tgmayor calls_per_hour=60', actor='proposer')
        client = n.YardSocketClient(yard.home, 'tgbridge')
        ask = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][0]
        self.assertTrue(primary.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key='slice-card'))['delivered'])
        row = primary.bridge.run_asks.rows()[0]
        tap = dict(update_id=95010, callback_query=dict(
            id='tap', data=f"runask:{row['id']}:approve", **{'from': dict(id=CHAT, is_bot=False)},
            message=dict(message_id=row['message_id'], chat=dict(id=CHAT, type='private'),
                         **{'from': dict(id=1000000013, is_bot=True)})))
        self.telegram.queue(tap, message(95011, 'from someone else', sender=999))
        wait_for(lambda: ledger_rows(ledger, 'SELECT offset FROM cursor')[0][0] == 95012, 'the stale taps')
        self.assertEqual(ledger_rows(ledger, 'SELECT count(*) FROM run_asks'), [(0,)])
        self.assertEqual(client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][0]['state'], 'open')
        self.assertEqual(client.call('GetSource', ['tgmayor'])['calls_per_hour'], 600)
        self.assertEqual(yard.inbox('mayor'), events)
        tap['update_id'] = 95012
        tap['callback_query']['message']['from']['id'] = 1000000001
        primary.bridge.inbound(tap)
        self.assertEqual(client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][0]['state'], 'answered')
        self.assertEqual(client.call('GetSource', ['tgmayor'])['calls_per_hour'], 60)

        # One poller per bot on this host: a second installed one is refused by the lock; one
        # that does not take it (a previous owner) is stopped only by Telegram's conflict.
        second_settings = private(self.root / 'second-poller.toml', settings.read_text().replace(
            str(state), str(self.root / 'second-state')).replace(str(self.mayor_port), str(self.mayor_port + 1)))
        refused = subprocess.run([sys.executable, '-B', str(root / 'current/bridge/bridge.py'), '--settings',
                                  str(second_settings), 'run'], env=env, cwd=self.root, capture_output=True,
                                 text=True, timeout=60)
        self.assertEqual(refused.returncode, 1)
        self.assertIn('another bridge already owns this bot', refused.stderr)
        # The stale tap is refused as a native card; the foreign sender's message is dropped unrelayed.
        self.assertEqual([(c['method'], c['body'].get('text')) for c in self.telegram.sent()][2:],
                         [('answerCallbackQuery', 'Native card not authorized')])
        polls = len(self.telegram.change()['polls'])
        self.telegram.change(lambda state: state.update(conflict=True))
        self.assertEqual(process.wait(timeout=30), 2)
        self.assertIn('another Telegram poller or webhook owns this bot', self.mayor_log.read_text())
        sent = self.telegram.sent()
        self.assertEqual(len(self.telegram.change()['polls']), polls)
        print(f'Mayor slice: release SHA256SUMS {self.release_sums}; inbox {len(events)} events (relay + bot) kept '
              f'through a yard and a bridge restart; {len(sent)} Telegram writes, '
              f'{len(receipts)} durable reply receipt(s); cross-credential 403 x3; ungranted/revoked/native calls '
              'refused; stale and foreign taps ran nothing; second poller refused by the lock, previous owner by 409')
