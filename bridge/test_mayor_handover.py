"""A new crew member's own bot, installed beside a yard's primary and second bots, and the
same bot's state handed over from its previous owner and back (yardr/SETUP.md, "Add a
second crew member's bot" and "Hand a bot's state to a new installation").

The slice runs the installed release as its own process against a fake Telegram behind a
fake curl, on a real isolated yardr server; no daemon, model, real settings or secret.
"""
from contextlib import closing, contextmanager, nullcontext
import ctypes
import fcntl
import hashlib
import http.server
import importlib.util
import json
import os
from pathlib import Path
import re
import shutil
import signal
import sqlite3
import stat
import subprocess
import sys
import tempfile
import threading
import time
import types
import unittest
from unittest.mock import patch
import urllib.error
import urllib.request

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b  # noqa: E402
import run_asks as n  # noqa: E402
import testports  # noqa: E402
import testyardr  # noqa: E402
from test_settings import git_env, make_source, tree_digest  # noqa: E402

PACK = Path(__file__).resolve().parents[1]
HELPER = PACK / 'scripts/handover_state.py'
CHAT = 123
SKILLS = ('telegram-formatting', 'telegram-attachments', 'telegram-voice-transcripts')
MAYOR_CALLS = ('send', 'react', 'pin', 'unpin', 'ask_close')
# The shape of the crew add the authority contract proposes, with synthetic paths.
MAYOR_ARGS = ['--dangerously-bypass-approvals-and-sandbox', '-c', 'model_reasoning_effort=medium',
              '-c', 'mcp_servers.figma.enabled=false', '-c', 'mcp_servers.posthog.enabled=false']
# The ledger's tables as a previous owner wrote them, before the native and message-log tables.
ORIGINAL_SCHEMA = '''
CREATE TABLE cursor (id INTEGER PRIMARY KEY, offset INTEGER NOT NULL);
CREATE TABLE deliveries (key TEXT PRIMARY KEY, fingerprint TEXT NOT NULL, receipt TEXT NOT NULL);
CREATE TABLE asks (ask_id TEXT PRIMARY KEY, question TEXT NOT NULL, format TEXT NOT NULL, options TEXT NOT NULL,
  message_id INTEGER, answered INTEGER NOT NULL DEFAULT 0, answer TEXT, update_id INTEGER,
  relayed INTEGER NOT NULL DEFAULT 0, kind TEXT NOT NULL DEFAULT 'text');
ALTER TABLE asks ADD COLUMN decided INTEGER NOT NULL DEFAULT 0;
ALTER TABLE asks ADD COLUMN conversation_id TEXT;
CREATE TABLE accepted (id INTEGER PRIMARY KEY CHECK (id = 1), message_id INTEGER NOT NULL, accepted_at TEXT NOT NULL);
CREATE TABLE accepted_conversations (conversation_id TEXT PRIMARY KEY, message_id INTEGER NOT NULL,
  accepted_at TEXT NOT NULL);
CREATE TABLE pins (slot TEXT PRIMARY KEY, generation INTEGER NOT NULL DEFAULT 1, message_id INTEGER,
  revision INTEGER NOT NULL DEFAULT 0, fingerprint TEXT);
CREATE TABLE topics (name TEXT PRIMARY KEY, thread_id INTEGER NOT NULL UNIQUE, source TEXT NOT NULL,
  recorded_at TEXT NOT NULL);
CREATE TABLE topic_attempts (name TEXT PRIMARY KEY, attempt INTEGER NOT NULL);
CREATE TABLE auto_topics (thread_id INTEGER PRIMARY KEY, name TEXT, name_implicit INTEGER NOT NULL DEFAULT 0,
  agent TEXT NOT NULL, detected_at TEXT NOT NULL, bound_at TEXT);
'''

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
    time.sleep(.2)
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
        time.sleep(.05)


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
            config.update(yardr_home=str(yard.home), yardr_operator='pat')
        self.sent, self.updates, self.next, self.before, self.after = [], [], 100, None, None
        self.bridge = b.Bridge(config, b.Store(state / 'telegram-bridge.sqlite3', 0), 'TOKEN-' + source, capability,
                               request=self.request, state_dir=state, sleep=lambda seconds: None)
        self.bridge.reactions['on_accept'], self.bridge.typing_seconds = False, 0
        handler = b._callback_handler(self.bridge)
        server = testports.bind_test_server(handler)
        threading.Thread(target=server.serve_forever, daemon=True).start()
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


class MayorSliceTests(unittest.TestCase):
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
        helped = subprocess.run([sys.executable, '-B', str(root / 'current/scripts/handover_state.py'), 'capture',
                                 '--help'], env=env, cwd=self.root, capture_output=True, text=True, timeout=60)
        self.assertEqual(helped.returncode, 0, helped.stderr)
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
        for name in SKILLS:
            yard.cli('skill', 'add', str(root / 'current/skills' / name))
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
        yard.cli('crew', 'add', 'mayor', '--runner', 'herdr', '--dir', str(self.root), '--on=',
                 '--context-handoff', '60', '--max-age', '24h', '--handoff-grace', '15m',
                 '--set', 'kind=codex', '--set', 'model=gpt-6.1-sol', '--set', 'args=' + json.dumps(MAYOR_ARGS),
                 '--set', 'prompt=' + str(role), '--set', 'env_file=' + str(mayor_env),
                 '--set', 'skills=' + json.dumps(list(SKILLS)))
        crew = yard.json('crew', 'show', 'mayor')['crew']
        self.assertGreaterEqual(crew['cursor'], head)
        self.assertEqual(yard.inbox('mayor'), [])
        self.assertEqual((crew['runner'], crew['on'], crew['context_handoff'], crew['max_age'], crew['handoff_grace']),
                         ('herdr', [], '60', '24h', '15m'))
        self.assertEqual(crew['config'], dict(kind='codex', model='gpt-6.1-sol', args=MAYOR_ARGS, prompt=str(role),
                                              env_file=str(mayor_env), skills=list(SKILLS)))
        # Configuration, not a running assistant: no daemon runs here, and the session is stopped.
        self.assertEqual(crew['state'], 'stopped')
        primed = yard.cli('prime', '--crew', 'mayor')
        self.assertIn('Synthetic role for the isolated slice.', primed)
        for name in SKILLS:
            self.assertIn(f'`{name}`', primed)
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


# Runs handback (argv: helper, settings, previous state) and dies inside its transaction, after the
# ledger's pages spilled to the file, as a killed process leaves it: a hot journal and changed pages.
DIE_IN_TRANSACTION = '''
import importlib.util, os, sqlite3, sys, types
spec = importlib.util.spec_from_file_location('handover_state', sys.argv[1])
module = importlib.util.module_from_spec(spec)
spec.loader.exec_module(module)


class Dying:
    def __init__(self, db):
        self.db = db
        db.execute('PRAGMA cache_size=1')

    def __getattr__(self, name):
        return getattr(self.db, name)

    def __enter__(self):
        return self.db.__enter__()

    def __exit__(self, *args):
        return self.db.__exit__(*args)

    def execute(self, sql, *args):
        result = self.db.execute(sql, *args)
        if sql.startswith('INSERT INTO main."pins"'):
            os._exit(9)
        return result


module.sqlite3 = types.SimpleNamespace(connect=lambda *a, **k: Dying(sqlite3.connect(*a, **k)), Error=sqlite3.Error)
module.handback(sys.argv[2], sys.argv[3])
'''


class Crash(BaseException):
    """The process dying at a chosen point: nothing in the bridge catches it."""


def helper(*args, script=None):
    return subprocess.run([sys.executable, '-I', '-B', *(['-c', script] if script else []), str(HELPER), *args],
                          capture_output=True, text=True, timeout=60, cwd=tempfile.gettempdir(),
                          env={k: v for k, v in os.environ.items() if not k.startswith(('YARDR_', 'TGBRIDGE_'))})


def digest(path):
    return hashlib.sha256(Path(path).read_bytes()).hexdigest()


class HandoverTests(unittest.TestCase):
    """capture and handback of scripts/handover_state.py around an adopted ledger in real use."""

    def setUp(self):
        self.root = Path(self.enterContext(tempfile.TemporaryDirectory(prefix='mh')))

    def seed(self):
        """The previous owner's state directory: an original-schema ledger with sends that were
        confirmed, never sent, reserved when it died and ambiguous, one answered and one open ask,
        a pin, a queued voice note and files of other owners beside them."""
        rows, sent = self.root / 'seed', []
        rows.mkdir()

        def transport(url, body, *args, **kwargs):
            if not url.startswith('https://api.telegram.org/'):
                return 200, dict(seq=1)
            method, text = url.rsplit('/', 1)[1], body.get('text', '')
            if 'never left' in text:
                raise b.RequestNotSent('synthetic', 7, 'connect')
            if 'lost answer' in text:
                raise b.RequestUnconfirmed('synthetic', 28, 'after-send')
            if 'reserved' in text:
                raise Crash()
            sent.append(method)
            if method not in ('sendMessage', 'editMessageText'):
                return 200, dict(ok=True, result=True)
            return 200, dict(ok=True, result=dict(message_id=body.get('message_id', 40 + len(sent)), chat=dict(id=CHAT)))

        config = dict(bot_id=999, user_id=CHAT, chat_id=CHAT, account_id='test_bot', yard='test-city',
                      agent='mayor', hook_url='http://127.0.0.1:9/hooks/tgmayor', hook_secret='seed',
                      user_display_name='Pat', timezone='UTC')
        seed = b.Bridge(config, b.Store(rows / 'ledger', 7001), 'TOKEN', 'CAPABILITY', request=transport,
                        sleep=lambda seconds: None, state_dir=rows)
        seed.reactions['on_accept'], seed.typing_seconds = False, 0
        for key, text in (('sent-key', 'already sent'), ('not-sent-key', 'never left'),
                          ('ambiguous-key', 'lost answer')):
            seed.api_send(dict(text=text, key=key))
        with self.assertRaises(Crash):
            seed.api_send(dict(text='reserved then the owner died', key='reserved-key'))
        for key in ('answered-ask', 'open-ask'):
            ask = b.Bridge._ask_id_for_key(key)
            seed.api_send(dict(text='Pick one', key=key, keyboard=[[dict(text='Yes', callback_data=f'ask:{ask}:yes')]]))
        answered = b.Bridge._ask_id_for_key('answered-ask')
        seed.inbound(dict(update_id=7001, callback_query=dict(
            id='q', data=f'ask:{answered}:yes', **{'from': dict(id=CHAT, is_bot=False)},
            message=dict(message_id=seed.store.get_ask(answered)['message_id'], chat=dict(id=CHAT, type='private')))))
        pinned = seed.pin('status', 'Status one', 'plain')
        self.assertTrue(pinned['delivered'], pinned)
        seed.store.record_accepted(7000, 'at')

        previous = self.root / 'previous'
        previous.mkdir(mode=0o700)
        ledger = previous / 'telegram-bridge.sqlite3'
        with closing(sqlite3.connect(ledger)) as db, db:
            db.executescript(ORIGINAL_SCHEMA)
            db.execute(f"ATTACH DATABASE '{rows / 'ledger'}' AS seed")
            for table in ('cursor', 'deliveries', 'asks', 'accepted', 'pins'):
                columns = ', '.join(row[1] for row in db.execute(f'PRAGMA main.table_info({table})'))
                db.execute(f'INSERT INTO main.{table} ({columns}) SELECT {columns} FROM seed.{table}')
        day = previous / 'telegram-inbound/2026-11-13'
        day.mkdir(parents=True)
        (day / '7000-voice.ogg').write_bytes(b'OggS synthetic')
        (day / '7000-transcript.pending').write_text(json.dumps(dict(
            audio_name='7000-voice.ogg', attempts=0, kind='voice', duration='0:03')))
        (previous / 'telegram-bridge-heartbeat.json').write_text(json.dumps(dict(last_poll_ok_at='x', pid=1)))
        (previous / 'mayor-notes.md').write_text('not the bridge')
        (previous / 'capture.png').write_bytes(b'not the bridge')
        old = private(self.root / 'previous.toml', f'city = "old"\nagent = "mayor"\n\n[bot]\nbot_id = 999\n'
                                                   f'user_id = {CHAT}\nchat_id = {CHAT}\naccount_id = "test_bot"\n')
        from test_bridge import write_settings
        (self.root / 'new').mkdir()
        new = write_settings(self.root / 'new')
        return previous, old, new

    def test_capture_adopts_only_the_same_bot_s_stopped_state_and_handback_moves_it_forward(self):
        previous, old, new = self.seed()
        ledger = previous / 'telegram-bridge.sqlite3'
        state = self.root / 'new/state'
        untouched = tree_digest(previous)
        # SETUP's order: the new instance is installed with only its state directory's parent made, the
        # previous owner is stopped, then the installed release captures, its checkout gone.
        new.write_text(new.read_text().replace('port = 18930\n', f'port = 18930\npython = "{sys.executable}"\n'))
        home, source = self.root / 'home', self.root / 'source'
        home.mkdir()
        make_source(self, source, home)
        env = install_env(home, Path(sys.executable).parent)
        installed = subprocess.run(['sh', str(source / 'install.sh'), '--settings', str(new)], env=env,
                                   capture_output=True, text=True, timeout=120)
        self.assertEqual(installed.returncode, 0, installed.stdout + installed.stderr)
        shutil.rmtree(source)
        root = home / '.local/share/yardr-telegram/test-city-telegram'
        checked = subprocess.run([str(root / 'bin/telegram-bridge'), 'check-install'], env=env, cwd=self.root,
                                 capture_output=True, text=True, timeout=60)
        self.assertEqual(checked.returncode, 0, checked.stdout + checked.stderr)
        self.assertFalse(os.path.lexists(state))
        done = subprocess.run([sys.executable, '-B', str(root / 'current/scripts/handover_state.py'), 'capture',
                               '--settings', str(new), '--from-settings', str(old), '--from', str(previous),
                               '--close-pending', 'Closed at the handover'], env=env, cwd=self.root,
                              capture_output=True, text=True, timeout=60)
        self.assertEqual(done.returncode, 0, done.stderr)
        self.assertIn('1 open ask(s) archived', done.stdout)
        self.assertEqual(tree_digest(previous), untouched)
        self.assertEqual(sorted(p.relative_to(state).as_posix() for p in state.rglob('*') if p.is_file()),
                         ['handover.json', 'telegram-bridge-heartbeat.json', 'telegram-bridge.sqlite3',
                          'telegram-inbound/2026-11-13/7000-transcript.pending',
                          'telegram-inbound/2026-11-13/7000-voice.ogg'])
        manifest = json.loads((state / 'handover.json').read_text())
        self.assertEqual((manifest['source'], manifest['source_sha256'], manifest['offset']),
                         (str(previous.resolve()), digest(ledger), 7001))
        self.assertEqual(manifest['counts'], {table: ledger_rows(ledger, f'SELECT count(*) FROM {table}')[0][0]
                                              for table in manifest['schema']})
        self.assertGreaterEqual(manifest['counts']['deliveries'], 8)

        # The bridge opens the adopted ledger as its own, adding only its newer tables.
        yard = Yard(self, self.root)
        bot = InProcessBot(self, yard, 'tgmayor', 'mayor', 999, 'MAYOR-CAPABILITY', state=state,
                           names=dict(yard='test-city', account_id='test_bot'))
        yard.cli('crew', 'add', 'mayor', '--dir', str(self.root), '--on=', '--set', 'kind=codex')
        yard.cli('subscribe', 'mayor', 'tgmayor', '--scope', 'all', '--where', 'type=message')
        store = bot.bridge.store
        self.assertEqual(store.offset(), 7001)
        tables = {row[0] for row in ledger_rows(state / 'telegram-bridge.sqlite3',
                                                "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertTrue({'messages', 'run_asks', 'mayor_model_taps'} <= tables)
        receipt = store.saved_receipt('sent-key')
        self.assertEqual(bot.bridge.api_send(dict(text='already sent', key='sent-key'))['message_id'],
                         str(receipt['message_id']))
        for key, text in (('ambiguous-key', 'lost answer'), ('reserved-key', 'reserved then the owner died')):
            answer = bot.bridge.api_send(dict(text=text, key=key))
            self.assertEqual((answer['delivered'], answer['reason']),
                             (False, 'ambiguous_delivery_requires_manual_reconciliation'), key)
        self.assertEqual(bot.bridge.api_send(dict(text='changed text', key='sent-key'))['reason'],
                         'idempotency_key_conflict')
        # Under another yard or account name the same request is another payload: it fails closed too.
        renamed = b.Bridge(dict(bot.bridge.config, yard='odyssey'), store, 'TOKEN', 'MAYOR-CAPABILITY',
                           request=bot.request)
        self.assertEqual(renamed.api_send(dict(text='already sent', key='sent-key'))['reason'],
                         'idempotency_key_conflict')
        self.assertEqual(bot.sent, [])
        self.assertTrue(bot.bridge.api_send(dict(text='never left', key='not-sent-key'))['delivered'])
        self.assertTrue(bot.bridge.api_send(dict(text='never left', key='not-sent-key'))['delivered'])
        self.assertEqual(bot.texts(), ['never left'])

        # Crashes around a new send: before reserving (nothing kept, sent once on retry), after
        # reserving or after Telegram took it but before the receipt (kept for a person), and an
        # answer lost on the way back (kept for a person).
        saved_reserve = store.reserve

        def die_reserving(*args, **kwargs):
            raise Crash()
        store.reserve = die_reserving
        with self.assertRaises(Crash):
            bot.bridge.api_send(dict(text='before', key='crash-before'))
        store.reserve = saved_reserve
        self.assertTrue(bot.bridge.api_send(dict(text='before', key='crash-before'))['delivered'])

        def crash_before(method, body):
            raise Crash()
        bot.before = crash_before
        with self.assertRaises(Crash):
            bot.bridge.api_send(dict(text='reserved now', key='crash-reserved'))
        bot.before = None

        def crash_after(method, body):
            raise Crash()
        bot.after = crash_after
        with self.assertRaises(Crash):
            bot.bridge.api_send(dict(text='taken', key='crash-after'))
        bot.after = None

        def lost(method, body):
            raise b.RequestUnconfirmed('synthetic', 28, 'after-send')
        bot.after = lost
        self.assertFalse(bot.bridge.api_send(dict(text='lost', key='lost-answer'))['delivered'])
        bot.after = None
        for key, text in (('crash-reserved', 'reserved now'), ('crash-after', 'taken'), ('lost-answer', 'lost')):
            self.assertEqual(bot.bridge.api_send(dict(text=text, key=key))['reason'],
                             'ambiguous_delivery_requires_manual_reconciliation')
        self.assertEqual(bot.texts(), ['never left', 'before', 'taken', 'lost'])

        # Late taps on archived asks relay nothing; the yard's event head does not move.
        mid = {key: store.get_ask(b.Bridge._ask_id_for_key(key))['message_id'] for key in ('answered-ask', 'open-ask')}
        for update, key in ((7001, 'answered-ask'), (7002, 'open-ask')):
            ask = b.Bridge._ask_id_for_key(key)
            bot.updates.append(dict(update_id=update, callback_query=dict(
                id=f'late-{update}', data=f'ask:{ask}:yes', **{'from': dict(id=CHAT, is_bot=False)},
                message=dict(message_id=mid[key], chat=dict(id=CHAT, type='private')))))
        # Accepted by the yard, then the process dies before the cursor moves: the replay is one delivery.
        bot.updates.append(message(7003, 'accepted before the crash'))
        saved_advance = store.advance

        def die(offset):
            if offset == 7004:
                raise Crash()
            saved_advance(offset)
        store.advance = die
        with self.assertRaises(Crash):
            bot.bridge.poll_once()
        store.advance = saved_advance
        self.assertEqual(store.offset(), 7003)
        bot.bridge.poll_once()
        self.assertEqual(store.offset(), 7004)
        events = yard.inbox('mayor')
        self.assertEqual([e['kind'] for e in events], ['hook'], events)
        self.assertIn('accepted before the crash', json.dumps(events))
        self.assertNotIn('ask answer', json.dumps(events))
        self.assertTrue(store.get_ask(b.Bridge._ask_id_for_key('open-ask'))['decided'])

        # Updates Telegram still holds when the new owner stops go back with its cursor.
        bot.updates += [message(7004, 'queued one'), message(7005, 'queued two')]
        answer_counts = ledger_rows(state / 'telegram-bridge.sqlite3', 'SELECT count(*) FROM deliveries')[0][0]
        # The new owner finished the adopted voice job and queued one of its own.
        day = state / 'telegram-inbound/2026-11-13'
        (day / '7000-transcript.pending').unlink()
        (day / '7003-voice.ogg').write_bytes(b'OggS second')
        (day / '7003-transcript.pending').write_text(json.dumps(dict(
            audio_name='7003-voice.ogg', attempts=0, kind='voice', duration='0:02')))
        # The process dies before its transaction, inside it, after its commit and while copying files:
        # each next run finishes from where the last stopped.
        spec = importlib.util.spec_from_file_location('handover_state', HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)
        saved_json = module.write_json

        def die_recording(state_name):
            def write_json(path, value):
                if (value.get('handback') or {}).get('state') == state_name:
                    raise Crash()
                saved_json(path, value)
            return write_json
        module.write_json = die_recording('writing')
        with self.assertRaises(Crash):
            module.handback(new, previous)
        self.assertEqual(digest(previous / 'telegram-bridge.sqlite3'), manifest['source_sha256'])
        self.assertEqual(digest(previous / 'telegram-bridge.sqlite3.before-handback'), manifest['source_sha256'])
        self.assertNotIn('handback', json.loads((state / 'handover.json').read_text()))
        module.write_json = saved_json
        # A module in the shared temp cwd must not run in the crash subprocess.
        shared = self.root / 'shared'
        shared.mkdir()
        (shared / 'sqlite3.py').write_text('open(__file__ + ".loaded", "w").close()\n'
                                         'raise RuntimeError("untrusted cwd import")\n')
        with patch.object(tempfile, 'gettempdir', return_value=str(shared)):
            dying = helper(str(new), str(previous), script=DIE_IN_TRANSACTION)
        self.assertFalse((shared / 'sqlite3.py.loaded').exists(), dying.stderr)
        self.assertEqual(dying.returncode, 9, dying.stderr)
        self.assertTrue(Path(str(previous / 'telegram-bridge.sqlite3') + '-journal').exists())
        self.assertNotEqual(digest(previous / 'telegram-bridge.sqlite3'), manifest['source_sha256'])
        self.assertEqual(json.loads((state / 'handover.json').read_text())['handback']['state'], 'writing')
        module.write_json = die_recording('ledger')
        with self.assertRaises(Crash):
            module.handback(new, previous)
        module.write_json = saved_json
        self.assertEqual(ledger_rows(previous / 'telegram-bridge.sqlite3', 'SELECT offset FROM cursor'), [(7004,)])
        self.assertEqual(json.loads((state / 'handover.json').read_text())['handback']['state'], 'writing')
        saved_copy = module.copy_private

        def die(source, target):
            if 'telegram-inbound' in str(target):
                raise Crash()
            saved_copy(source, target)
        module.copy_private = die
        with self.assertRaises(Crash):
            module.handback(new, previous)
        self.assertEqual(ledger_rows(previous / 'telegram-bridge.sqlite3', 'SELECT offset FROM cursor'), [(7004,)])
        self.assertTrue((previous / 'telegram-inbound/2026-11-13/7000-transcript.pending').exists())
        result = helper('handback', '--settings', str(new), '--to', str(previous))
        self.assertEqual(result.returncode, 0, result.stderr)
        self.assertEqual(sorted(p.name for p in (previous / 'telegram-inbound/2026-11-13').iterdir()),
                         ['7000-voice.ogg', '7003-transcript.pending', '7003-voice.ogg'])
        self.assertEqual(digest(previous / 'telegram-bridge.sqlite3.before-handback'), manifest['source_sha256'])
        original = {row[0] for row in ledger_rows(previous / 'telegram-bridge.sqlite3',
                                                  "SELECT name FROM sqlite_master WHERE type='table'")}
        self.assertEqual(original, {'cursor', 'deliveries', 'asks', 'accepted', 'accepted_conversations', 'pins',
                                    'topics', 'topic_attempts', 'auto_topics'})
        handed = previous / 'telegram-bridge.sqlite3'
        self.assertEqual(ledger_rows(handed, 'SELECT offset FROM cursor'), [(7004,)])
        self.assertEqual(ledger_rows(handed, 'SELECT key, fingerprint, receipt FROM deliveries ORDER BY key'),
                         ledger_rows(state / 'telegram-bridge.sqlite3',
                                     'SELECT key, fingerprint, receipt FROM deliveries ORDER BY key'))
        self.assertEqual(len(ledger_rows(handed, 'SELECT key FROM deliveries')), answer_counts)
        self.assertEqual(ledger_rows(handed, 'SELECT answered, decided FROM asks ORDER BY ask_id'),
                         ledger_rows(state / 'telegram-bridge.sqlite3',
                                     'SELECT answered, decided FROM asks ORDER BY ask_id'))
        self.assertEqual((previous / 'mayor-notes.md').read_text(), 'not the bridge')
        # The previous owner resumes exactly after what the new one accepted.
        resumed = [u['update_id'] for u in bot.updates if u['update_id'] >= ledger_rows(handed,
                   'SELECT offset FROM cursor')[0][0]]
        self.assertEqual(resumed, [7004, 7005])
        print(f'Handover: captured {manifest["counts"]["deliveries"]} receipts at offset 7001 from ledger '
              f'{manifest["source_sha256"][:12]}; adopted sent/not-sent/reserved/ambiguous kept, 5 crash points, '
              f'late taps relayed nothing; handed back {answer_counts} receipts at offset 7004, queued 7004-7005 '
              'left for the previous owner, resumed after dying before, inside and after its transaction and '
              'while copying files')

    def test_a_refused_capture_or_handback_leaves_both_trees_as_they_were(self):
        previous, old, new = self.seed()
        state = self.root / 'new/deep/state'
        new.write_text(new.read_text().replace(f'"{self.root / "new/state"}"', f'"{state}"'))
        ledger, beat = previous / 'telegram-bridge.sqlite3', previous / 'telegram-bridge-heartbeat.json'
        other = private(self.root / 'other.toml', old.read_text().replace('bot_id = 999', 'bot_id = 998'))
        live = subprocess.Popen([sys.executable, '-c', 'import time; time.sleep(60)', 'bridge.py'])
        self.addCleanup(live.wait)
        self.addCleanup(live.kill)
        elsewhere = self.root / 'elsewhere'
        elsewhere.mkdir()
        (elsewhere / 'other-owner.txt').write_text('not this bot')
        (elsewhere / 'secret.json').write_text(json.dumps(dict(pid=None, token='SYNTHETIC-ONLY')))
        strange, surprising, linked, beat_linked, piped = (
            self.root / name for name in ('strange', 'surprising', 'linked', 'linked-beat', 'piped'))
        for copy in (strange, surprising, linked, beat_linked, piped):
            shutil.copytree(previous, copy)
        with closing(sqlite3.connect(strange / 'telegram-bridge.sqlite3')) as db, db:
            db.execute('CREATE TABLE unknown_rows (x)')
        # A known later table in a shape the bridge's upgrade would leave, and then fail to write.
        with closing(sqlite3.connect(surprising / 'telegram-bridge.sqlite3')) as db, db:
            db.execute('CREATE TABLE messages (seq INTEGER PRIMARY KEY, surprise)')
        shutil.rmtree(linked / 'telegram-inbound')
        (linked / 'telegram-inbound').symlink_to(elsewhere, target_is_directory=True)
        (beat_linked / 'telegram-bridge-heartbeat.json').unlink()
        (beat_linked / 'telegram-bridge-heartbeat.json').symlink_to(elsewhere / 'secret.json')
        # A FIFO no writer opens: hashing it as a file would block forever.
        (piped / 'telegram-bridge.sqlite3').unlink()
        os.mkfifo(piped / 'telegram-bridge.sqlite3', 0o600)
        spec = importlib.util.spec_from_file_location('handover_state', HELPER)
        module = importlib.util.module_from_spec(spec)
        spec.loader.exec_module(module)

        @contextmanager
        def replaced(path, data):
            saved = path.read_bytes() if path.exists() else None
            path.write_bytes(data)
            try:
                yield
            finally:
                path.write_bytes(saved) if saved is not None else path.unlink()

        @contextmanager
        def locked():
            with (self.root / 'new/private/telegram-bot-999.lock').open('a') as lock:
                fcntl.flock(lock, fcntl.LOCK_EX)
                yield

        @contextmanager
        def linked_beat(top):
            path = top / 'telegram-bridge-heartbeat.json'
            saved = path.read_bytes()
            path.unlink()
            path.symlink_to(elsewhere / 'secret.json')
            try:
                yield
            finally:
                path.unlink()
                path.write_bytes(saved)

        @contextmanager
        def swapped_after_check():
            # The heartbeat becomes a link between the check and its copy: the last write refuses.
            def copy(source, target):
                if target.name == 'telegram-bridge-heartbeat.json':
                    raise module.Refusal(f'{source} is not a regular file')
                saved_copy(source, target)
            saved_copy, module.copy_private = module.copy_private, copy
            try:
                yield
            finally:
                module.copy_private = saved_copy

        @contextmanager
        def darwin_libc():
            # A libc as darwin's: renamex_np(from, to, RENAME_EXCL) and no renameat2, served by this one's.
            linux = ctypes.CDLL(None, use_errno=True)
            if not hasattr(linux, 'renameat2'):
                yield  # darwin itself
                return
            linux.renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)

            class Call:
                def __call__(self, source, target, flags):
                    self.case.assertEqual((type(source), type(target), flags), (bytes, bytes, 4))
                    return linux.renameat2(-100, source, -100, target, 1)
            Call.case = self
            libc = types.SimpleNamespace(renamex_np=Call())
            saved_ctypes = module.ctypes
            module.ctypes = types.SimpleNamespace(CDLL=lambda *args, **kwargs: libc, c_char_p=ctypes.c_char_p,
                                                  c_uint=ctypes.c_uint, c_int=ctypes.c_int, get_errno=ctypes.get_errno)
            try:
                yield
            finally:
                module.ctypes = saved_ctypes

        def capture(source=previous, previous_settings=old, close=True):
            return ['capture', '--settings', str(new), '--from-settings', str(previous_settings),
                    '--from', str(source), *(['--close-pending', 'Closed at the handover'] if close else [])]

        def run(command):
            if command[0] != 'in-process':
                result = helper(*command)
                return result.returncode, result.stdout + result.stderr
            try:
                module.capture(new, old, previous, 'Closed at the handover')
            except module.Refusal as error:
                return 1, 'handover refused: ' + str(error)
            return 0, ''

        def refused(words, command, trees):
            before = {top: tree(top) for top in trees}
            code, output = run(command)
            self.assertEqual(code, 1, output)
            self.assertIn(words, output)
            self.assertNotIn('Pick one', output)
            self.assertNotIn('SYNTHETIC-ONLY', output)
            self.assertEqual({top: tree(top) for top in trees}, before)

        captures = [
            ('only the same bot is handed over', nullcontext, previous, capture(previous_settings=other)),
            ('still open at the freeze', nullcontext, previous, capture(close=False)),
            ('still running', lambda: replaced(beat, json.dumps(dict(pid=live.pid)).encode()), previous,
             capture()),
            ('a bridge of this bot is running here', locked, previous, capture()),
            ('did not stop cleanly', lambda: replaced(Path(str(ledger) + '-journal'), b''), previous, capture()),
            ('unknown table unknown_rows', nullcontext, strange, capture(source=strange)),
            ('ledger table messages has unknown columns', nullcontext, surprising, capture(source=surprising)),
            ('telegram-inbound is not a plain directory', nullcontext, linked, capture(source=linked)),
            ('telegram-bridge-heartbeat.json is not a regular file', nullcontext, beat_linked,
             capture(source=beat_linked)),
            ('telegram-bridge.sqlite3 is not a regular file', nullcontext, piped, capture(source=piped)),
            ('telegram-bridge-heartbeat.json is not a regular file', swapped_after_check, previous, ['in-process']),
        ]
        # Into a state directory that does not exist, its parent neither, then into an empty one made for it.
        for made in (False, True):
            if made:
                state.mkdir(parents=True, mode=0o700)
            for words, setup, source, command in captures:
                if made and command == ['in-process']:
                    continue  # its refusal is in the write phase, which an existing destination never reaches
                with self.subTest(words, made=made), setup():
                    refused(words, command, (source, state))
                    self.assertEqual(os.path.lexists(state), made)
                    self.assertEqual(os.path.lexists(state.parent), made)
        # An empty destination is refused like a captured one.
        refused('capture runs once', capture(), (previous, state))
        state.rmdir()
        state.parent.rmdir()
        # A bridge that starts between the checks and the rename makes the destination, with its ledger or
        # still empty: the rename refuses, the appeared tree stays as it was and no staging is left. On
        # darwin's libc, which has renamex_np and no renameat2, as on Linux.
        saved_rename = module.rename_new
        for ledgered, darwin in ((True, False), (False, False), (True, True)):
            appeared = {}

            def appear(source, target):
                target.mkdir(mode=0o700)
                if ledgered:
                    b.Store(target / 'telegram-bridge.sqlite3', 9000)
                appeared['tree'] = tree(target)
                saved_rename(source, target)
            module.rename_new = appear
            try:
                with self.subTest('appeared during capture', ledgered=ledgered, darwin=darwin), \
                        (darwin_libc() if darwin else nullcontext()):
                    before = tree(previous)
                    code, output = run(['in-process'])
                    self.assertEqual(code, 1, output)
                    self.assertIn('appeared during capture', output)
                    self.assertEqual(tree(state), appeared['tree'])
                    self.assertEqual(os.listdir(state.parent), ['state'])
                    self.assertEqual(tree(previous), before)
                    if ledgered:
                        self.assertEqual(ledger_rows(state / 'telegram-bridge.sqlite3',
                                                     'SELECT offset FROM cursor')[0][0], 9000)
            finally:
                module.rename_new = saved_rename
                shutil.rmtree(state.parent)
        with self.subTest('captured on darwin'), darwin_libc():
            code, output = run(['in-process'])
            self.assertEqual(code, 0, output)
            self.assertEqual(ledger_rows(state / 'telegram-bridge.sqlite3', 'SELECT offset FROM cursor')[0][0], 7001)
            self.assertEqual(os.listdir(state.parent), ['state'])
        shutil.rmtree(state.parent)
        # A destination configured as a link, even to nothing yet, is refused: nothing is written at its target.
        state.parent.mkdir(mode=0o700)
        missing = elsewhere / 'other-city/state'
        state.symlink_to(missing, target_is_directory=True)
        with self.subTest('linked destination'):
            refused('capture runs once', capture(), (previous, state, elsewhere))
            self.assertEqual(os.readlink(state), str(missing))
            self.assertFalse(os.path.lexists(missing.parent))
            self.assertEqual(os.listdir(state.parent), ['state'])
        state.unlink()
        state.parent.rmdir()
        captured = helper(*capture())
        self.assertEqual(captured.returncode, 0, captured.stderr)
        refused('capture runs once', capture(), (previous, state))

        # The bridge's own upgrade adds the native tables a handback refuses rows in.
        config = dict(bot_id=999, user_id=CHAT, chat_id=CHAT, account_id='test_bot', yard='test-city',
                      agent='mayor', hook_url='http://127.0.0.1:9/hooks/tgmayor', hook_secret='seed',
                      user_display_name='Pat', timezone='UTC')
        b.Bridge(config, b.Store(state / 'telegram-bridge.sqlite3', 0), 'TOKEN', 'CAPABILITY',
                 request=lambda *args, **kwargs: (200, {}), sleep=lambda seconds: None, state_dir=state)
        changed = self.root / 'changed.sqlite3'
        shutil.copyfile(ledger, changed)
        with closing(sqlite3.connect(changed)) as db, db:
            db.execute('UPDATE cursor SET offset=offset+1')

        @contextmanager
        def native_row():
            with closing(sqlite3.connect(state / 'telegram-bridge.sqlite3')) as db, db:
                db.execute("INSERT INTO run_asks VALUES ('native', '{}')")
            try:
                yield
            finally:
                with closing(sqlite3.connect(state / 'telegram-bridge.sqlite3')) as db, db:
                    db.execute('DELETE FROM run_asks')
        @contextmanager
        def piped_ledger():
            aside = previous.parent / 'ledger-aside'
            ledger.rename(aside)
            os.mkfifo(ledger, 0o600)
            try:
                yield
            finally:
                ledger.unlink()
                aside.rename(ledger)

        @contextmanager
        def linked_inbound(top):
            inbound, aside = top / 'telegram-inbound', top.parent / (top.name + '-inbound-aside')
            inbound.rename(aside)
            inbound.symlink_to(elsewhere, target_is_directory=True)
            try:
                yield
            finally:
                inbound.unlink()
                aside.rename(inbound)
        lacking = self.root / 'lacking.sqlite3'
        shutil.copyfile(state / 'telegram-bridge.sqlite3', lacking)
        with closing(sqlite3.connect(lacking)) as db, db:
            db.execute('DROP TABLE topics')
        widened = self.root / 'widened.sqlite3'
        shutil.copyfile(state / 'telegram-bridge.sqlite3', widened)
        with closing(sqlite3.connect(widened)) as db, db:
            db.execute('ALTER TABLE run_asks ADD COLUMN surprise')
        # A handback that recorded later traffic, and then both ledgers put back as they were at capture.
        later = self.root / 'later.sqlite3'
        shutil.copyfile(state / 'telegram-bridge.sqlite3', later)
        with closing(sqlite3.connect(later)) as db, db:
            db.execute('UPDATE cursor SET offset=offset+3')
        with closing(sqlite3.connect(module.uri(ledger, 'ro'), uri=True)) as db:
            db.execute('ATTACH DATABASE ? AS new', (str(later),))
            recorded = module.projection(db, module.schema(db), 'new')
        manifest_text = json.loads((state / 'handover.json').read_text())
        begun = json.dumps(dict(manifest_text, handback=dict(
            state='ledger', projection=recorded, offset=manifest_text['offset'] + 3, at='then'))).encode()
        settings_text = new.read_bytes()
        handback = ['handback', '--settings', str(new), '--to', str(previous)]
        handbacks = [
            ('native card rows', native_row),
            ('another bot or paired chat than the one captured',
             lambda: replaced(new, settings_text.replace(b'bot_id = 999', b'bot_id = 998', 1))),
            ('another bot or paired chat than the one captured',
             lambda: replaced(new, settings_text.replace(f'user_id = {CHAT}\nchat_id = {CHAT}'.encode(),
                                                         f'user_id = {CHAT + 1}\nchat_id = {CHAT + 1}'.encode()))),
            ('telegram-bridge-heartbeat.json is not a regular file', lambda: linked_beat(state)),
            ('the previous owner is running', lambda: replaced(beat, json.dumps(dict(pid=live.pid)).encode())),
            ('a bridge of this bot is running here', locked),
            ('changed since capture', lambda: replaced(ledger, changed.read_bytes())),
            ('telegram-bridge.sqlite3 is not a regular file', piped_ledger),
            ('lacks a table of the previous one',
             lambda: replaced(state / 'telegram-bridge.sqlite3', lacking.read_bytes())),
            ('ledger table run_asks has unknown columns',
             lambda: replaced(state / 'telegram-bridge.sqlite3', widened.read_bytes())),
            ('not the one the handback began with', lambda: replaced(state / 'handover.json', begun)),
            ('telegram-inbound is not a plain directory', lambda: linked_inbound(previous)),
            ('telegram-inbound is not a plain directory', lambda: linked_inbound(state)),
        ]
        for words, setup in handbacks:
            with self.subTest(words), setup():
                refused(words, handback, (previous, state))
        handed = helper(*handback)
        self.assertEqual(handed.returncode, 0, handed.stderr)
        refused('already handed back', handback, (previous, state))
        print(f'Refusals: {2 * len(captures) + 4} capture and {len(handbacks) + 1} handback refusals changed '
              'neither tree; an absent destination stayed absent, one that appeared before the rename unchanged')


def tree(top):
    """{path: mode, SHA-256 or link target} of every entry under top, directories and top included,
    without following links; None when top does not exist."""
    top = Path(top)
    if not os.path.lexists(top):
        return None
    found = {}
    for directory, dirs, files in os.walk(top):
        for path in (Path(directory), *(Path(directory) / name for name in dirs + files)):
            info, name = os.lstat(path), path.relative_to(top).as_posix()
            if stat.S_ISLNK(info.st_mode):
                found[name] = 'link ' + os.readlink(path)
            elif stat.S_ISREG(info.st_mode):
                found[name] = digest(path) + oct(info.st_mode)
            else:
                found[name] = oct(info.st_mode)
    return found
