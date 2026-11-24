"""Native paths through the bridge, a framed Unix RPC and fake Telegram."""
import copy
import fcntl
import base64
from concurrent.futures import ThreadPoolExecutor
import hashlib
from html.parser import HTMLParser
import http.server
import json
import os
import re
from pathlib import Path
import shlex
import shutil
import socketserver
import subprocess
import sys
import tempfile
import threading
import time
import unittest
import urllib.request
import testports
import testyardr
from unittest.mock import patch

sys.path.insert(0, str(Path(__file__).resolve().parent))
import bridge as b
from test_bridge import BridgeTestCase, write_settings

import run_asks as n
n = getattr(b, 'run_asks', n)


GO_ESCAPE = r'\\(?:[abfnrtv\\"]|x[0-9a-fA-F]{2}|u[0-9a-fA-F]{4}|U[0-9a-fA-F]{8})'
GO_QUOTED_CHAR = rf'(?:[^"\\\x00-\x1f\x7f]|{GO_ESCAPE})'
GO_QUOTED_NONEMPTY = rf'"{GO_QUOTED_CHAR}+"'
GO_QUOTED_ANY = rf'"{GO_QUOTED_CHAR}*"'
GO_QUOTED_TOKEN_RE = re.compile(r'"(?:[^"\\]|\\.)*"')
DISPLAY_VALUE = rf'(?:{GO_QUOTED_NONEMPTY}|[^\s,=\\"]+)'
COMMAND_TOKEN = rf'(?:{GO_QUOTED_NONEMPTY}|[^\s,\[\]\\"]+)'
COMMAND_ARGV = rf'\[{COMMAND_TOKEN}(?:, {COMMAND_TOKEN})*\]'


def and_list(items):
    return ' and '.join(items) if len(items) < 3 else ', '.join(items[:-1]) + ' and ' + items[-1]


def proposal():
    return dict(ask=7, turn=0, to='pat', asker='proposer', since='2026-01-01T00:00:00Z',
                question='Change the test?', context=['First <context>', 'Second & context'], options=[], state='open',
                run=dict(argv=['depot', 'test', 'web'], command='yardr depot test web', description='Set its test',
                         does=['current: make test', 'proposed: sh scripts/test.sh'],
                         back='propose the old command', back_by='an operator'))


def general_proposal(words=None):
    words = ['version'] if words is None else words
    command = 'yardr ' + ' '.join("'" + w.replace("'", "'\\''") + "'" if '[' in w or ']' in w else w for w in words)
    does = ['The yard has not checked this command: approving runs it as typed, as the operator.',
            'No way back is shown: nothing here says how to undo it.',
            'It is good until 12:34:56.', 'What the command prints is told to the asker.']
    for i, w in enumerate(words, 1):
        shown = w if len(w) <= 128 else w[:64] + f'…[+{len(w)-128} cut]…' + w[-64:]
        if w.removeprefix('--set=').startswith(('log=', 'prompt=')):
            shown += ' (not read by the yard)'
        does.append(f'{i}: ({len(w)}) {shown}')
    return dict(proposal(), asker='yardmaster', run=dict(argv=words[:], general=words[:], command=command,
        description='a yardr command no card fits, shown whole and unchecked', does=does, back=''),
        weight=[{'class': 'code', 'line': 'brings a program the yard will run: '
                 'a yardr command no card fits, shown whole and unchecked: ' + ' '.join(command.split())}])


def decision():
    return dict(ask=7, turn=0, to='pat', asker='proposer', since='2026-01-01T00:00:00Z',
                question='Where should the links go?', context=['First <context>', 'Second & context'],
                options=[dict(label='Move', consequence='Move every link.'),
                         dict(label='Keep', consequence='Keep the links here.'),
                         dict(label='You decide', consequence='leave it to proposer')],
                recommend=1, why='The new location is ready.',
                about=[dict(id='related-1', title='Related <work>', stage='backlog')], state='open')


def dense_decision():
    # Synthetic copy of the four-option remote-transcription decision shape.
    return dict(decision(), question='Which provider should the first remote transcription slice use?',
                context=['Synthetic review: two local tools cannot take an audio file; a third can, through a documented API. '
                         'Both example bridges transcribe in English. Provider One covers English but not Russian; '
                         'Provider Two covers Russian. Audio would leave this host only under an opt-in backend; '
                         'local transcription stays the default.'],
                options=[dict(label='Provider One API (speech-model-1)',
                              consequence='the requested model, 18 example cents per hour, English covered, Russian not; needs a new API key'),
                         dict(label='Provider Two first (4 or 18 example cents/h)',
                              consequence='speech models, Russian covered, ogg accepted; a separate account and key'),
                         dict(label='Reuse the CLI login',
                              consequence='no new key, but widens the bridge sandbox and depends on an interactive login and allowance'),
                         dict(label='You decide', consequence='leave it to the proposer')],
                why='It is the requested model, through its documented endpoint with a static owner-only key file, at cents per day.',
                about=[])


def source_proposal(kind='replace', extra=''):
    ask = json.loads(Path(__file__).with_name('fixtures').joinpath('source_' + kind + '.json').read_text())
    ask['run']['carried'][0]['text'] += extra
    return ask


def pack_proposal(kind='directory'):
    return json.loads(Path(__file__).with_name('fixtures').joinpath('pack_' + kind + '.json').read_text())


def depot_proposal(kind='first'):
    return json.loads(Path(__file__).with_name('fixtures').joinpath('depot_' + kind + '.json').read_text())


RICH_YAML = (
    'name: synthetic\n'
    'base: https://api.example.invalid\n'
    'bases:\n  eu: https://eu.example.invalid\n'
    'credential:\n'
    '  env: SRC_TOK\n'
    '  update_env: SRC_UPD\n'
    '  header: "Authorization: Bearer {credential}"\n'
    '  mint:\n    - name: MINT_A\n      secret: true\n    - MINT_B\n'
    '  mint_command: [bin/mint, --v1]\n'
    'calls:\n'
    '  fetch:\n'
    '    kind: read\n'
    '    params:\n'
    '      q: {type: enum, values: [open, closed], optional: true, log: true}\n'
    '      pat: {type: string, pattern: "^[a-z]+$", max: 20, optional: true}\n'
    '    request:\n'
    '      method: GET\n      path: /items\n      query: {q: "{q}", empty: ""}\n'
    '    answer:\n      select: [id, title]\n      max: 4096\n'
    '  mk:\n'
    '    kind: update\n'
    '    request: {method: POST, path: /items}\n'
    '    build: [bin/builder, --json]\n'
    '    params:\n      p: {type: string}\n'
    '  tellcall:\n'
    '    kind: update\n'
    '    params:\n      to: {type: string}\n      t: {type: string}\n      x: {type: string}\n      l: {type: string, optional: true}\n'
    '    request:\n'
    '      method: POST\n      path: /tell\n      body: {to: "{to}", title: "{t}", text: "{x}", link: "{l}"}\n'
    'check: fetch\n'
    'idempotency: X-Req-Id\n'
    'notify:\n  call: tellcall\n  address: {param: to, env: HEAR_ADDR}\n  title: t\n  text: x\n  link: l\n'
    'hear:\n  field: sender\n  by: identity\n'
    'limits:\n  command: [bin/report, limits]\n  kinds: {claude: pro}\n'
    'thread:\n  open: [bin/report, open]\n  place: issue\n  states: [started, review, done]\n  labels: true\n  assignee: key\n'
    'link:\n'
    '  host: example.invalid\n'
    '  issue: "^/(org/repo)/i/([0-9]+)$"\n  fixed: "https://example.invalid/{1}/i/{2}"\n  thread: "{2}"\n  fold: lower\n'
    '  pull:\n    issue: "^/(org/repo)/pr/([0-9]+)$"\n    fixed: "https://example.invalid/{1}/pr/{2}"\n    thread: "{2}"\n'
    '  repos: "^(org/repo)$"\n'
    'identity:\n  fold: lower\n'
    'events:\n  runs: [ci]\n  head_from: [push]\n  pr: [bin/report, pr]\n  ci: [bin/report, ci]\n'
    'hook:\n'
    '  verify:\n'
    '    scheme: hmac-sha256\n    algorithm: sha512\n    signed: "v0:{timestamp}:{body}"\n'
    '    signature: {header: X-Sig, encoding: hex, list: ",", prefix: "sha512="}\n'
    '    key: {encoding: hex, prefix: "whsec_"}\n'
    '    delivery: {body: deliv.id}\n    timestamp: {body: ts, unit: ms, window: 5m}\n'
    '  bodies: unique\n'
    '  normalize: [bin/report, norm]\n'
    '  keys: [sender, repo]\n'
    '  register: {call: tellcall, url: x, secret: t}\n'
)


def rich_add_proposal():
    """Rich source add covering all capability slots, mint, bases, run/build calls, params, query and answer."""
    rich_sha = n.file_hash('#!/bin/sh\necho ok\n')
    rich_mint_sha = n.file_hash('#!/bin/sh\necho mint\n')
    rich_add = source_proposal('new_public_main')
    rich_add['run']['carried'][0]['text'] = RICH_YAML
    rich_add['run']['carried'][0]['files'] = [
        dict(program='bin/mint', text='#!/bin/sh\necho mint\n', sha256=rich_mint_sha),
        dict(program='bin/report', text='#!/bin/sh\necho ok\n', sha256=rich_sha),
        dict(program='bin/builder', text='#!/bin/sh\necho ok\n', sha256=rich_sha),
    ]
    rich_add['run']['does'] = [
        "it adds one source and copies 3 command files (7 commands), and changes nothing else. It grants no call, makes no hook, and sets no credential's file.",
        (f'source synthetic: its calls go to https://eu.example.invalid (base eu). Its credential is the variable SRC_TOK (the name, never the value), '
         f'with SRC_UPD for updates, minted from MINT_A and MINT_B by the declaration\'s own mint command [bin/mint, --v1] '
         f'(command file bin/mint, copied to <home>/sources/synthetic/bin/mint: sha256 {rich_mint_sha}, mode 0700), '
         f'run with the mint\'s inputs and the operator\'s local logins where the source\'s mint_command is unset: one the operator sets wins over it, '
         f'sent in the header "Authorization: Bearer {{credential}}" on every call.'),
        'call fetch (read): GET /items. It takes q (enum, optional, one of open, closed, its value is kept in the record of each call) and pat (string, optional, pattern ^[a-z]+$, at most 20), and sends the query q={q}, empty="".',
        'call fetch sends no body. Its answer keeps id, title, at most 4096 bytes.',
        'call mk (update): POST /items. It takes p (string, required), and sends no query.',
        'call mk builds its body with the command [bin/builder, --json], run with the credential at each call and free to make its own requests with it while it builds. Its answer is returned as it comes, at most 65536 bytes.',
        'call tellcall (update): POST /tell. It takes to (string, required), t (string, required), x (string, required) and l (string, optional), and sends no query.',
        'call tellcall sends the body {"link":"{l}","text":"{x}","title":"{t}","to":"{to}"}. Its answer is returned as it comes, at most 65536 bytes.',
        'Its check call is fetch, and updates carry their key in the header X-Req-Id.',
        "it can tell a user: the call tellcall, whose to takes the person's address, read as HEAR_ADDR from the person's own env file; the title fills t and the text fills x and the link fills l.",
        "it hears deliveries: only one whose sender is the address of a user told through it, read as , and every told user's: another's, and a stranger's, is dropped after verification.",
        'its limits command [bin/report, limits] says what the subscription of claude as pro still carries.',
        "it speaks in threads: a place to open one is called issue; the words state takes are started, review, done; it takes labels (label:<name> adds one, never removes); an assignee is a person's key in the source.",
        "its issues are a bead's link: host example.invalid, the expression ^/(org/repo)/i/([0-9]+)$ reads an issue, the one fixed form https://example.invalid/{1}/i/{2} and the thread id {2}; the groups fold lower. Its pull requests read ^/(org/repo)/pr/([0-9]+)$, with the fixed form https://example.invalid/{1}/pr/{2} and the thread id {2}. A repository's path matches ^(org/repo)$.",
        'its identity keys fold to lowercase (identity fold: lower), so one spelling is stored however it was typed.',
        'what it sends in: deliveries are verified by HMAC-SHA512 over v0:{timestamp}:{body} (scheme hmac-sha256, algorithm: sha512, signed: v0:{timestamp}:{body}), the signature in the header X-Sig as hex, one or more separated by ",", each past the prefix "sha512=", any one of them passing; the HMAC key is what follows the prefix "whsec_" in the secret, read as hex (not the secret\'s own bytes); a delivery is named by deliv.id in its body; its time arrives at ts, in ms within 5m of the yard\'s clock; bodies are unique; the command [bin/report, norm] turns a verified body into the envelope, handed no credential; its events may carry the fields sender, repo; the yard registers its webhook with tellcall (POST /tell), filling x with the public URL and t with the hook\'s secret.',
        'what it sends of pull requests and CI: the envelope types ci are runs; push tell a head; the command [bin/report, pr] reads the pull request, handed the credential; the command [bin/report, ci] reads CI, handed the credential.',
        f'command file bin/report: [bin/report, limits] (limits.command, handed no credential), [bin/report, open] (thread.open, handed the credential), [bin/report, norm] (hook.normalize, handed no credential), [bin/report, pr] (events.pr, handed the credential) and [bin/report, ci] (events.ci, handed the credential): sha256 {rich_sha}, mode 0700',
        f'command file bin/builder: [bin/builder, --json] (call.mk.build, handed the credential): sha256 {rich_sha}, mode 0700',
        'still owed after it: the credential, by a secret ask for synthetic; and which user it tells, by yardr user set <user> notify=synthetic.',
    ]
    rich_add['weight'] = [dict(
        {'class': 'code'},
        line='brings a program the yard will run: the command file bin/mint of synthetic (new), bin/report of synthetic (new) and bin/builder of synthetic (new)',
    )]
    return rich_add


def nested_replace_proposal(rich_add=None):
    """Rich source as a replacement: whole new-side declaration diffs for every nested field."""
    nested_rep = copy.deepcopy(rich_add if rich_add is not None else rich_add_proposal())
    nrc = nested_rep['run']['carried'][0]
    nrc['inventory'] = [dict(path=f['program'], sha256=f['sha256'], mode='0700',
                             size=len(f['text'].encode())) for f in nrc['files']]
    nested_rep['run']['back'] = ('yardr bead ask <bead> - with Q: <question> and R: source add '
                                 '<a file holding the declaration that stands now> on stdin')
    nested_rep['run']['back_by'] = 'only the operator'
    nested_rep['run']['does'][0] = (
        'it replaces one source and copies 3 command files (7 commands), and changes nothing else. '
        'It grants no call, makes no hook, and sets no credential\'s file: the source keeps its env_file, '
        'mint_command, region base, call limits and ignored senders, and every grant of it stands, '
        'now for this declaration.')
    nested_new = {
        'check': 'fetch',
        'credential': {'env': 'SRC_TOK', 'header': 'Authorization: Bearer {credential}',
                       'mint': [{'name': 'MINT_A', 'secret': True}, 'MINT_B'],
                       'mint_command': ['bin/mint', '--v1'], 'update_env': 'SRC_UPD'},
        'notify': {'address': {'env': 'HEAR_ADDR', 'param': 'to'}, 'call': 'tellcall',
                   'link': 'l', 'text': 'x', 'title': 't'},
        'hear': {'by': 'identity', 'field': 'sender'},
        'limits': {'command': ['bin/report', 'limits'], 'kinds': {'claude': 'pro'}},
        'thread': {'assignee': 'key', 'labels': True, 'open': ['bin/report', 'open'],
                   'place': 'issue', 'states': ['started', 'review', 'done']},
        'link': {'fixed': 'https://example.invalid/{1}/i/{2}', 'fold': 'lower',
                 'host': 'example.invalid', 'issue': '^/(org/repo)/i/([0-9]+)$',
                 'pull': {'fixed': 'https://example.invalid/{1}/pr/{2}',
                          'issue': '^/(org/repo)/pr/([0-9]+)$', 'thread': '{2}'},
                 'repos': '^(org/repo)$', 'thread': '{2}'},
        'hook': {'bodies': 'unique', 'keys': ['sender', 'repo'],
                 'normalize': ['bin/report', 'norm'],
                 'register': {'call': 'tellcall', 'secret': 't', 'url': 'x'},
                 'verify': {'algorithm': 'sha512',
                            'delivery': {'body': 'deliv.id'},
                            'key': {'encoding': 'hex', 'prefix': 'whsec_'},
                            'scheme': 'hmac-sha256',
                            'signature': {'encoding': 'hex', 'header': 'X-Sig',
                                          'list': ',', 'prefix': 'sha512='},
                            'signed': 'v0:{timestamp}:{body}',
                            'timestamp': {'body': 'ts', 'unit': 'ms', 'window': '5m'}}},
        'events': {'ci': ['bin/report', 'ci'], 'head_from': ['push'],
                   'pr': ['bin/report', 'pr'], 'runs': ['ci']},
        'identity': {'fold': 'lower'},
    }
    nested_rep['run']['does'][2:2] = (
        ['it would replace the source synthetic the yard has under that name; '
         'no grant of source synthetic stands.'] +
        [f'source synthetic {field}: none -> '
         f'{json.dumps(value, sort_keys=True, separators=(",", ":"))}.'
         for field, value in nested_new.items()])
    nested_rep['weight'] = [dict(
        {'class': 'code'},
        line=('brings a program the yard will run: the declaration of synthetic replaces the one '
              'that stands, and says where the credential it keeps is sent and which programs the yard runs'))]
    return nested_rep


def group_proposal(name='group_add', extra=''):
    # Captured from Bead on isolated aligned and upstream builds; only the temp root, ask number and time stabilized.
    ask = json.loads(Path(__file__).with_name('fixtures').joinpath(name + '.json').read_text())
    if extra:
        # These fixtures end with the prompt's lines; the file the approval writes is named by their digest.
        does = ask['run']['does']
        head = next(i for i, line in enumerate(does) if line.startswith('On approval the yard writes the text below'))
        body = '\n'.join(does[head + 1:])
        sha = lambda value: hashlib.sha256(value.encode()).hexdigest()
        old = next(value for value in (body, body + '\n') if sha(value) in does[head])
        does[head] = does[head].replace(sha(old), sha(old + extra))
        does[head + 1:] = (old + extra).removesuffix('\n').split('\n')
    return ask


def group_fixture(case, ask, root, name):
    """Compare a live group card with its fixture after the capture's rewrite: temp root, ask number and time."""
    actual = json.loads(json.dumps(dict(ask, ask=7, since='2026-01-01T00:00:00Z')).replace(str(root), '/synthetic'))
    case.assertEqual(actual, json.loads(Path(__file__).with_name('fixtures').joinpath(name + '.json').read_text()))


def group_add_proposal(extra=''):
    return group_proposal('group_add', extra)


def group_set_proposal(extra=''):
    return group_proposal('group_set_prompt', extra)


# Every group card the builds write, by fixture: weighted ones and the routine effort cards.
GROUP_FIXTURES = ('group_add', 'group_add_exec', 'group_set_prompt', 'group_set_preset', 'group_set_model_effort',
                  'group_set_effort', 'group_set_effort_default')
GROUP_PREALIGNMENT = ('group_add_prealignment', 'group_set_prealignment', 'group_set_no_prompt_prealignment')

# Upstream run rows this child owns, by fixture: cards the aligned and upstream
# builds write byte for byte alike. The whole ones show through the generic run
# card; the fallback ones refuse with the fallback suffix before anything is sent.
UPSTREAM_WHOLE = ('user_role', 'device_rm', 'hook_add', 'hook_add_table', 'hook_rm',
                  'link_move', 'depot_test', 'user_key',
                  'bead_update', 'depot_base', 'peer_add', 'peer_allow', 'peer_identity',
                  'peer_intake', 'peer_rename', 'peer_rm', 'peer_set', 'settings_set',
                  'subscribe', 'timer_rm', 'up', 'user_add', 'user_identity',
                  'user_invite', 'user_rm', 'user_set_tell', 'user_uninvite')
UPSTREAM_FALLBACK = ('device_pair', 'hook_add_ask')


# The skill administration cards of the optional skill core, by fixture (captured as the group ones are).
SKILL_FIXTURES = ('skill_rm', 'skill_rm_regular_files', 'skill_rm_force', 'skills_group_set', 'skills_group_narrow',
                  'skills_group_unset', 'skills_crew_set', 'skills_crew_unset')
SKILL_TREES = dict(notes='b6c70591f58c469fea5c9ea084bacd3b0d9ec3b3a3162604599b825afdb0d026',
                   spare='c076730f322ba90219e75ce3a0993211a3f7c62a833039c4889e2635a4575679',
                   triage='029da9a013d8f66219876cae30d0ed1ef457154e155cb5de4cb13184acbd6fd5')
SKILL_STALE = ('ask 7 was written when the skill notes had another tree: it was replaced since. '
               'Nothing ran. Decline it; it is asked again if it is still wanted.')


def skill_proposal(name):
    # Captured from Bead on an isolated skill core (5dedfc56; skill_rm_regular_files on 9677728c);
    # only the temp root, ask number and time stabilized.
    return json.loads(Path(__file__).with_name('fixtures').joinpath(name + '.json').read_text())


def repo_edit_proposal(version=2):
    name = 'repo_edit_v2' if version == 2 else 'repo_edit_v1'
    return json.loads(Path(__file__).with_name('fixtures').joinpath(name + '.json').read_text())


def upstream_proposal(name):
    # Captured from Bead on isolated aligned and upstream builds; only the temp
    # root (/synthetic), ask number, timestamp and device date are stabilized.
    return json.loads(Path(__file__).with_name('fixtures').joinpath('upstream', name + '.json').read_text())


def queue_entry(ask):
    # One UserAsks open row for a Bead ask, as the core serves it: the queue
    # carries the run beside the ask-level payload (when the ask has one)
    # but no context.
    entry = dict(bead='sample-1', bead_title='Synthetic rows', ask=7, question=ask['question'],
                 asker=ask.get('asker', 'proposer'), turn=ask.get('turn', 0), run=ask['run'])
    for key in ('secret', 'setup', 'identity', 'depot_push', 'weight'):
        if ask.get(key):
            entry[key] = ask[key]
    return entry


def validate_accepts(ask):
    # Whether the send path would show the card whole: acceptance, or a
    # fallback refusal. Anything else is a malformed fixture and fails loud.
    try:
        n.validate(ask, ask.get('to', 'pat'))
    except n.NativeError as error:
        if not str(error).endswith(n.FALLBACK):
            raise
        return False
    return True


def review_parts(review):
    """Read rendered facts and recover each raw payload independently of the serializer."""
    tokens = n.markdown.MarkdownIt('commonmark').parse(review)
    blocks = iter(re.finditer(r'^(`{3,})\n(.*?)\n\1(?=\n|$)', review, re.M | re.S))
    facts, contents, depth, label = [], [], 0, ''
    for token in tokens:
        if token.type == 'list_item_open':
            depth += 1
        elif token.type == 'list_item_close':
            depth -= 1
        elif token.type == 'inline' and depth:
            assert all(child.type in ('text', 'code_inline') for child in token.children), token.children
            fact = ''.join(child.content for child in token.children)
            facts.append(fact)
            if not fact.startswith('Content: '):
                label = fact
        elif token.type == 'fence':
            block = next(blocks)
            value = block[2]
            size, sha, trailing = re.match(r'Content: (\d+) UTF-8 bytes; SHA-256 ([0-9a-f]{64}); trailing LF bytes: (\d+);', facts[-1]).groups()
            assert len(value.encode()) == int(size)
            assert hashlib.sha256(value.encode()).hexdigest() == sha
            assert len(value) - len(value.rstrip('\n')) == int(trailing)
            assert len(block[1]) > max((len(m[0]) for m in re.finditer(r'`+', value)), default=0)
            assert token.content == (value + '\n').replace('\r\n', '\n').replace('\r', '\n')
            contents.append((label, value))
    assert next(blocks, None) is None
    return facts, contents, tokens


def review_content(review, label):
    return next(value for name, value in review_parts(review)[1] if name == label or name.startswith(label + ';'))


class NativeSocket:
    def __init__(self, root):
        self.ask = proposal()
        self.user = dict(name='pat', role='operator', identities={'tgbridge': {'id': '123', 'key': 'Pat'}})
        self.calls = []
        self.mode = None
        self.read_statuses = []
        self.bead = dict(id='sample-1', title='Synthetic change')
        # Several beads, as the core's Bead and UserAsks read them; None keeps the one ask above.
        self.beads = None
        self.raw_queue = None
        self.event_head = 0
        self.raw_events = None
        self.raw_notes = None
        self.group = dict(name='drafters', runner='herdr', config=dict(kind='claude', model='claude-opus-4'), limit=1)
        # The standing skill trees SkillDigest reads; a name not here is absent.
        self.skill_trees = {}
        self.repo_edit_capability = dict(version=2, approval='version-digest', paths=16, text_bytes=65536,
                                         context_bytes=8192, classes=['markdown', 'plain-text'], materialize=True)
        self.repo_edit_status = {}
        self.repo_edit_mode = None
        self.stale = ''
        # The crew and sessions a session nudge reads; the nudge requests it wrote.
        self.crew = {}
        self.sessions = []
        self.nudges = {}
        self.nudge_seq = 0
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
                elif method == 'GetGroup':
                    result = owner.group
                elif method == 'SkillDigest':
                    if owner.mode == 'no-skill-topic':
                        self.reply({'error': {'code': 'error', 'message': 'no such method SkillDigest'}})
                        return
                    result = owner.skill_trees.get(args['name'], 'absent')
                elif method == 'RepoEditCapabilities':
                    if owner.repo_edit_mode == 'no-capability-method':
                        self.reply({'error': {'code': 'error', 'message': 'no such method RepoEditCapabilities'}})
                        return
                    if owner.repo_edit_mode == 'bad-capability':
                        result = dict(version=99, materialize=False)
                    elif owner.repo_edit_mode == 'v1-capability':
                        result = dict(version=1, approval='version-digest', paths=16, text_bytes=65536,
                                      context_bytes=8192, classes=['markdown', 'plain-text'], materialize=False)
                    else:
                        result = dict(owner.repo_edit_capability) if isinstance(owner.repo_edit_capability, dict) else owner.repo_edit_capability
                elif method == 'NativeEditStatus':
                    if owner.repo_edit_mode == 'no-status-method':
                        self.reply({'error': {'code': 'error', 'message': 'no such method NativeEditStatus'}})
                        return
                    if owner.repo_edit_mode == 'status-wire-error':
                        self.send_error(503)
                        return
                    bead_id = args[0] if isinstance(args, list) and args else None
                    if bead_id not in owner.repo_edit_status:
                        self.reply({'error': {'code': 'not-found', 'message': 'no such bead'}})
                        return
                    result = dict(owner.repo_edit_status[bead_id])
                elif method == 'Bead':
                    if owner.mode == 'unreadable':
                        self.send_error(503)
                        return
                    if owner.beads is not None and args['bead'] not in owner.beads:
                        self.reply({'error': {'code': 'not-found', 'message': 'no such bead'}})
                        return
                    result = owner.beads[args['bead']] if owner.beads is not None else dict(owner.bead, asks=[owner.ask])
                elif method == 'UserAsks':
                    if owner.mode == 'unreadable':
                        self.send_error(503)
                        return
                    result = owner.queue(args) if owner.raw_queue is None else owner.raw_queue
                elif method == 'Head':
                    result = owner.event_head
                elif method == 'Notes':
                    if owner.mode == 'unreadable':
                        self.send_error(503)
                        return
                    if owner.raw_notes is not None:
                        result = owner.raw_notes
                    else:
                        bead = owner.beads_view().get(args[0])
                        if bead is None:
                            self.reply({'error': {'code': 'not-found', 'message': 'no such bead'}})
                            return
                        result = owner.notes_for(bead)
                elif method == 'Events':
                    events = owner.raw_events
                    if events is None:
                        events = [dict(seq=100 + ask['ask'], id=str(100 + ask['ask']), bead=bead['id'],
                                       kind='noted', actor=ask['asker'], data=dict(note=ask['ask']), at=ask['since'])
                                  for bead in owner.beads_view().values() if bead['id'] == args[0]
                                  for ask in bead['asks']]
                        events.sort(key=lambda event: event['seq'])
                    result = events[-args[1]:] if isinstance(events, list) else events
                elif method in ('ApproveAsk', 'DeclineAsk', 'AnswerAsk', 'DeferAsk',
                                'AnswerAskExpected', 'DeferAskExpected', 'DeclineAskExpected'):
                    target = owner.target(args)
                    if method.endswith('Expected'):
                        if owner.mode == 'old-method':
                            self.reply({'error': {'code': 'error', 'message': 'no such method ' + method}})
                            return
                        if args.get('turn') != target.get('turn'):
                            self.reply({'error': {'code': 'conflict', 'message': 'stale turn'}})
                            return
                        method = method.removesuffix('Expected')
                    replacing = method == 'AnswerAsk' and target['state'] == 'answered' and target.get('options')
                    if (target['state'] != 'open' and not replacing) or owner.mode == 'refused':
                        self.reply({'error': {'code': 'conflict', 'message': 'refused'}})
                        return
                    if owner.mode == 'stale' and method == 'ApproveAsk':
                        self.reply({'error': {'code': 'error', 'message': owner.stale}})
                        return
                    if method == 'ApproveAsk' and isinstance(target.get('run'), dict) and 'repo_edit' in target['run']:
                        payload = target['run']['repo_edit']
                        values = args.get('values') or {}
                        if (values.get('repo_edit_version') != str(payload['request']['version'])
                                or values.get('repo_edit_digest') != payload['digest']):
                            self.reply({'error': {'code': 'error', 'message': f"ask {args['ask']}: repo edit values mismatched; nothing ran"}})
                            return
                    if owner.mode != 'lost-open':
                        target['turn'] = target.get('turn', 0) + 1
                        target['state'] = {'ApproveAsk': 'answered', 'AnswerAsk': 'answered',
                                              'DeclineAsk': 'declined', 'DeferAsk': 'put aside'}[method]
                        target['words'] = args.get('words', 'canonical decision')
                        if method == 'AnswerAsk':
                            target['option'] = args['option']
                        if method == 'DeferAsk':
                            target['return_at'] = '2026-01-02T00:00:00Z'
                        if method == 'ApproveAsk':
                            if isinstance(target.get('run'), dict) and 'repo_edit' in target['run']:
                                payload = target['run']['repo_edit']
                                if payload['request']['version'] == 2:
                                    reserved = owner.repo_edit_status and next(iter(owner.repo_edit_status)) or 'sample-2'
                                    target['ran'] = dict(
                                        argv=target['run']['argv'], result=n.PENDING,
                                        pending_reason=f"reserved held bead {reserved}; the yard's server prepares it for review",
                                    )
                                else:
                                    target['ran'] = dict(argv=target['run']['argv'], result='stored operation applied')
                            else:
                                target['ran'] = dict(argv=target['run']['argv'],
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
                elif method == 'GetCrew':
                    member = owner.crew.get(args.get('name')) if isinstance(args, dict) else None
                    if member is None:
                        self.reply({'error': {'code': 'not-found', 'message': 'no such crew member'}})
                        return
                    result = dict(member)
                elif method == 'ListSessions':
                    result = [dict(item) for item in owner.sessions]
                elif method == 'NudgeSession':
                    if owner.mode == 'no-nudge-method':
                        self.reply({'error': {'code': 'error', 'message': 'no such method NudgeSession'}})
                        return
                    result = owner.nudge(args, self)
                    if result is None:
                        return
                else:
                    self.reply({'error': {'code': 'not-found', 'message': 'unknown method'}})
                    return
                self.reply({'result': result})

            def reply(self, value):
                raw = json.dumps(value).encode()
                if owner.mode == 'duplicate-json':
                    raw = raw.replace(b'"result":', b'"result":null,"result":', 1)
                self.send_response(200)
                self.send_header('Content-Length', str(len(raw)))
                self.end_headers()
                self.wfile.write(raw)

        class Server(socketserver.ThreadingMixIn, socketserver.UnixStreamServer):
            daemon_threads = True

        self.server = Server(str(root / 'yard.sock'), Handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)
        self.thread.start()

    def beads_view(self):
        return self.beads if self.beads is not None else {self.bead['id']: dict(self.bead, asks=[self.ask])}

    def notes_for(self, bead):
        notes = [dict(n) for n in bead.get('notes', [])]
        seqs = {n['seq'] for n in notes}
        for ask in bead.get('asks', []):
            if ask['ask'] in seqs:
                continue
            payload = {k: ask[k] for k in ('question', 'to', 'context', 'options', 'recommend',
                                           'why', 'about', 'run', 'stage', 'move', 'moves', 'entry')
                       if k in ask}
            notes.append(dict(bead=bead['id'], seq=ask['ask'], kind='ask', author=ask['asker'],
                              at=ask['since'], text=json.dumps(payload)))
        notes.sort(key=lambda item: item['seq'])
        return notes

    def target(self, args):
        if self.beads is None:
            return self.ask
        return next(a for a in self.beads[args['bead']]['asks'] if a['ask'] == args['ask'])

    def nudge(self, args, handler):
        """One NudgeSession write, as the core answers it: the resolved line,
        or a refused error with the core's code. A standing request refuses a
        second; text '' types the core default line."""
        import unicodedata
        target = args.get('target') if isinstance(args, dict) else None
        user = args.get('user', '') if isinstance(args, dict) else ''
        text = args.get('text', '') if isinstance(args, dict) else None
        session = next((item for item in self.sessions if item['id'] == target), None)
        member = self.crew.get(target)
        live = [item for item in self.sessions if item['bead'] == target and item['state'] == 'running']
        if session is not None:
            if user:
                handler.reply({'error': {'code': 'error',
                                         'message': '--for names a crew member\u2019s conversation'}})
                return None
            if session['state'] != 'running':
                handler.reply({'error': {'code': 'error', 'message': 'only a running session is nudged'}})
                return None
            key, journal = 'session:' + session['id'], session['bead']
        elif member is not None:
            if not member.get('live', True):
                handler.reply({'error': {'code': 'error', 'message': 'only a running session is nudged'}})
                return None
            if user:
                key, journal = 'crew:' + target + ':for:' + user, member['journal']
            else:
                key, journal = 'crew:' + target, member['journal']
        elif len(live) == 1:
            session = live[0]
            key, journal = 'session:' + session['id'], session['bead']
        elif live:
            handler.reply({'error': {'code': 'error', 'message': 'name several sessions'}})
            return None
        else:
            handler.reply({'error': {'code': 'not-found', 'message': 'no such session, crew member or bead'}})
            return None
        if key in self.nudges:
            handler.reply({'error': {'code': 'conflict', 'message': 'a nudge stands already'}})
            return None
        if not isinstance(text, str):
            handler.reply({'error': {'code': 'error', 'message': 'words are not text'}})
            return None
        if len(text) > 1000:
            handler.reply({'error': {'code': 'error', 'message': 'words are too long'}})
            return None
        if any(unicodedata.category(char) in ('Cc', 'Zl', 'Zp') for char in text):
            handler.reply({'error': {'code': 'error', 'message': 'words are not one line'}})
            return None
        line = text or 'yardr: the operator asks you to go on where you stopped.'
        self.nudge_seq += 1
        ident = f'synthetic-nudge-{self.nudge_seq}'
        self.nudges[key] = dict(id=ident, text=line, journal=journal)
        result = dict(id=ident, text=line, by='pat', at='2026-11-20T12:00:00Z', state='asked')
        if session is not None:
            result.update(session=session['id'], bead=session['bead'])
        elif user:
            result.update(crew=target, user=user)
        else:
            result.update(crew=target)
        return result

    def queue(self, args):
        # The core's UserAsks: open asks to the name, oldest first, decided beside them.
        rows = []
        for bead in self.beads_view().values():
            for ask in bead['asks']:
                if ask['to'] != args['name'] or ask['state'] != 'open':
                    continue
                row = dict(bead=bead['id'], bead_title=bead['title'], topic='', ask=ask['ask'], question=ask['question'],
                           asker=ask['asker'], since=ask['since'], turn=ask.get('turn', 0))
                for key in ('secret', 'run', 'setup', 'identity', 'depot_push', 'weight'):
                    if ask.get(key):
                        row[key] = ask[key]
                rows.append(row)
        return dict(open=sorted(rows, key=lambda row: row['ask']), decided=[])

    def close(self):
        self.server.shutdown()
        self.server.server_close()
        self.thread.join()


class NativeCase(BridgeTestCase):
    """A bridge on the fake native socket and fake Telegram; carries no tests."""

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
        self.documents = []
        self.bridge.request_multipart = self.pack_transport

    def pack_transport(self, url, fields, field, path, timeout=20):
        self.sent_count += 1
        self.calls.append((url, dict(fields)))
        raw = Path(path).read_bytes()
        self.documents.append((dict(fields), Path(path), raw))
        return 200, dict(ok=True, result=dict(message_id=76 + self.sent_count, chat=dict(id=123),
                                            document=dict(file_id='synthetic-document', file_size=len(raw))))

    def reset_native(self, ask):
        with self.store.connect() as db:
            for table in ('run_asks', 'run_ask_keys', 'run_ask_updates', 'deliveries'):
                db.execute('DELETE FROM ' + table)
            db.execute('UPDATE cursor SET offset=0')
        self.native.ask = ask
        self.native.calls.clear()
        self.native.mode = None
        self.native.event_head = 0
        self.native.raw_events = None
        self.native.raw_notes = None
        self.native.user = dict(name='pat', role='operator', identities={'tgbridge': {'id': '123'}})
        self.native.group = dict(name='drafters', runner='herdr', config=dict(kind='claude', model='claude-opus-4'), limit=1)
        self.native.skill_trees = dict(SKILL_TREES)
        self.native.repo_edit_capability = dict(version=2, approval='version-digest', paths=16, text_bytes=65536,
                                                context_bytes=8192, classes=['markdown', 'plain-text'], materialize=True)
        self.native.repo_edit_status = {}
        self.native.repo_edit_mode = None
        self.calls.clear()
        self.sent_count = 0
        self.documents.clear()
        self.bridge.run_asks = n.RunAsks(self.bridge)

    def reset_depot(self, ask):
        self.reset_native(ask)
        self.native.beads = {}
        try:
            self.assertEqual(self.bridge.run_asks.record_depot_cutover(), 0)
        finally:
            self.native.beads = None

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
        if action == 'confirm':
            suffix = ':' + row['pending']['nonce']
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


class NativeTests(NativeCase):
    def assert_source_contract(self, ask, delivered):
        self.reset_native(copy.deepcopy(ask))
        result = self.create()
        self.assertEqual(result['delivered'], delivered, result)
        self.assertEqual(self.writes(), [])
        if delivered:
            self.assert_source_review(ask, self.bundle_review(self.row()))
        else:
            self.assertEqual((self.sent_count, self.documents), (0, []))

    def assert_bound_source_change_stales(self, base, changed):
        self.reset_native(copy.deepcopy(base))
        self.assertTrue(self.create()['delivered'])
        row = self.row()
        if base.get('weight'):
            self.press()
            action = 'confirm:' + self.row()['pending']['nonce']
        else:
            action = 'approve'
        query = dict(id='queued-source', data=f"runask:{row['id']}:{action}",
            message=dict(message_id=row['message_id'], chat=dict(id=123, type='private'),
                         **{'from': dict(id=999, is_bot=True)}), **{'from': dict(id=123, is_bot=False)})
        self.native.ask = copy.deepcopy(changed)
        sent, documents = self.sent_count, len(self.documents)
        original = self.bridge.telegram
        def queued(method, body, timeout=15):
            if method == 'getUpdates':
                return 200, dict(ok=True, result=[dict(update_id=91, callback_query=query)])
            return original(method, body, timeout)
        with patch.object(self.bridge, 'telegram', side_effect=queued):
            self.bridge.poll_once()
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.row()['state'], 'stale')
        self.assertEqual(self.row()['display']['keyboard'], [])
        self.assertEqual((self.sent_count, len(self.documents)), (sent, documents))

    def assert_fixture_prose_mutation(self, base, changed):
        does = changed['run']['does']
        names = [c['name'] for c in base['run']['carried']]
        anchors = [(i, line.split(': ', 1)[0][7:]) for i, line in enumerate(does)
                   if re.fullmatch(r'source [^ :]+: .+', line)]
        # These tables mutate prose only; protected fixture framing must survive.
        same_lead = does[0] == base['run']['does'][0]
        same_anchors = (bool(anchors) and anchors[0][0] == 1
                        and [name for _, name in anchors] == names)
        markers = [line for line in does if line.startswith('it would replace the source ')]
        original = [line for line in base['run']['does'] if line.startswith('it would replace the source ')]
        same_grants = markers == original
        safe = all(line.strip() and not any(c in n.HIDDEN or c in '\n\r' for c in line) for line in does)
        accepted = same_lead and same_anchors and same_grants and safe and len(does) <= 40
        if base['run']['argv'][0] == 'source':
            accepted = accepted and len(does) >= 4
        self.assert_source_contract(changed, bool(accepted))

    def test_source_pack_first_read_prose_is_presented_verbatim(self):
        for base in (source_proposal('new_public_main'), pack_proposal('directory_public_main')):
            ask = copy.deepcopy(base)
            ask['run']['does'][0] = ask['run']['does'][0].replace('1 command file (1 command)', '9 command files (8 commands)')
            ask['run']['does'][2] = 'Native prose <&>🙂: {"x":1,"x":2}; not interpreted.'
            ask['run']['does'].append('An unfamiliar native disclosure, shown whole.')
            self.assert_source_contract(ask, True)
            self.assert_bound_source_change_stales(base, ask)
        for kind in ('rich_public_main', 'address_public_main', 'numeric_public_main'):
            self.assert_source_contract(source_proposal(kind), True)
        self.reset_native(source_proposal('new_public_main'))
        self.native.mode = 'duplicate-json'
        self.assertFalse(self.create()['delivered'])
        self.assertEqual((self.sent_count, self.documents, self.writes()), (0, [], []))

    def test_depot_pack_review_and_envelope_fail_closed(self):
        self.reset_native(depot_proposal())
        self.assertFalse(self.create()['delivered'])
        self.assertEqual((self.sent_count, self.documents, self.writes()), (0, [], []))
        self.reset_depot(depot_proposal())
        self.native.beads = {'history-1': dict(id='history-1', title='Synthetic old history',
                                            asks=[dict(proposal(), ask=900)])}
        self.native.event_head = 500
        self.bridge.run_asks.record_depot_cutover()
        low = dict(depot_proposal(), ask=1)
        self.native.beads = {'fresh-1': dict(id='fresh-1', title='Synthetic fresh bead', asks=[low])}
        creation = dict(seq=501, id='501', bead='fresh-1', kind='noted', actor=low['asker'],
                        data=dict(note=1), at=low['since'])
        self.native.raw_events = [creation]
        receipt = self.bridge.api_run_ask(dict(bead='fresh-1', ask=1, key='low-after-activation'))
        self.assertTrue(receipt['delivered'], receipt)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.bridge.run_asks.depot_cutover(), 500)
        self.native.event_head = 400
        self.assertEqual(self.bridge.run_asks.record_depot_cutover(), 500)
        for bad in (True, -1, 2**64, '500', None, {}):
            with self.subTest(invalid_native_head=bad):
                self.native.event_head = bad
                with self.assertRaises(n.NativeError):
                    self.bridge.run_asks.record_depot_cutover()
                self.assertEqual(self.bridge.run_asks.depot_cutover(), 500)
        creation = dict(creation, bead='sample-1')
        for events in ({}, [], [dict(creation, seq=499, id='499')],
                       [dict(creation, bead='other-1')], [dict(creation, actor='other')],
                       [dict(creation, data=dict(note=2))], [dict(creation, data=dict(note=True))],
                       [dict(creation, data=dict(note=1, unknown=True))], [dict(creation, unknown=True)],
                       [dict(creation, seq=True, id='True')], [dict(creation, id='other')],
                       [creation, creation], [creation, dict(creation, seq=502, id='502')]):
            with self.subTest(invalid_creation_events=events):
                self.reset_depot(low)
                self.native.event_head = 500
                self.bridge.run_asks.record_depot_cutover()
                self.native.raw_events = events
                # An omitted timestamp cannot be guessed when the creation event is unavailable.
                if events == []:
                    self.native.ask.pop('since')
                result = self.bridge.api_run_ask(dict(bead='sample-1', ask=1, key='missing-creation'))
                self.assertFalse(result['delivered'], result)
                self.assertEqual((self.sent_count, self.documents, self.writes()), (0, [], []))
        self.reset_depot(depot_proposal())
        self.native.event_head = 100
        self.bridge.run_asks.record_depot_cutover()
        creation = dict(creation, bead='sample-1', data=dict(note=7), seq=101, id='101')
        self.native.raw_events = [creation] + [dict(creation, seq=i, id=str(i), kind='updated', data={})
                                              for i in range(102, 172)]
        self.assertTrue(self.create()['delivered'])
        event_reads = [args for method, args, _ in self.native.calls if method == 'Events']
        self.assertIn(['sample-1', 64], event_reads)
        self.assertIn(['sample-1', 128], event_reads)
        self.native.event_head = 200
        self.bridge.run_asks.record_depot_cutover()
        self.bridge.run_asks.refresh()
        self.assertEqual(self.row()['state'], 'stale')
        self.assertEqual(self.row()['display']['keyboard'], [])
        self.assertEqual(self.writes(), [])
        self.reset_native(depot_proposal())
        info = self.home.stat()
        old_fingerprint = n.digest(dict(purpose='native_depot_pack_activation',
            binding=self.bridge.run_asks.binding(), home=[info.st_dev, info.st_ino]))
        old_key = 'runask:depot-cutover:' + old_fingerprint
        old_receipt = dict(delivered=False, failure_kind='permanent', metadata=dict(
            reason='native_depot_pack_activation', ask_floor=0, linked_asks_excluded=0))
        with self.store.connect() as db:
            db.execute('INSERT INTO deliveries VALUES (?,?,?)', (old_key, old_fingerprint, n.canonical(old_receipt)))
        self.assertFalse(self.create()['delivered'])
        self.assertEqual((self.sent_count, self.documents, self.writes()), (0, [], []))
        self.bridge.run_asks.record_depot_cutover()
        self.assertTrue(self.create()['delivered'])
        self.assertEqual(self.store.saved_delivery(old_key), (old_fingerprint, old_receipt))
        for kind in ('first', 'first_public_main', 'source_replace', 'source_replace_public_main',
                     'matrix_first', 'matrix_first_public_main', 'matrix_change', 'matrix_change_public_main',
                     'matrix_replace', 'matrix_replace_public_main'):
            with self.subTest(genuine_depot=kind):
                ask = depot_proposal(kind)
                self.reset_depot(ask)
                receipt = self.create()
                expected = kind not in ('source_replace_public_main', 'matrix_replace_public_main')
                self.assertEqual(receipt['delivered'], expected, receipt)
                if not expected:
                    self.assertEqual((self.sent_count, self.documents, self.writes()), (0, [], []))
                    continue
                document = self.row()['bundle']['document']['text']
                for line in ask['run']['does']:
                    self.assertIn(n.review_code(line), document)
                for obj in ask['run']['pack']['plan']['objects']:
                    self.assertIn(n.review_escape('Plan object: ' + n.canonical({k: v for k, v in obj.items()
                        if k not in ('text', 'diff')})), document)
                    if obj['kind'] == 'file' and obj['action'] in ('create', 'change'):
                        for field in ('text', 'diff'):
                            self.assertIn(n.review_code(obj.get(field, '')), document)
                self.assertEqual(self.writes(), [])
        # The native core owns mapping-word syntax; this path only binds the words it supplied.
        for key, flag, value in (
                ('flow', '--flow', 'native[flow]:variant'),
                ('crew', '--crew', {'reviewer[slot]': 'native:crew[1]'}),
                ('groups', '--group', {'checkers[slot]': 'native:group[1]'}),
                ('binds', '--bind', ['checkers=claude:opus[1m]']),
                ('binds', '--bind', ['checkers=pi:openrouter/test:free']),
                ('members', '--members', ['checkers=native:member[1]']),
                ('open_threads', '--open-threads', ['reviewer=native:source[1]']),
                ('calls', '--calls', ['crew:reviewer=native:source/read[1]:health'])):
            with self.subTest(native_mapping_word=(key, value)):
                ask = copy.deepcopy(depot_proposal())
                run = ask['run']
                run['pack']['mappings'] = {key: value}
                if key == 'members':
                    ask['weight'].append({'class': 'trust', 'line': 'Synthetic native membership weight.'})
                words = ([value] if isinstance(value, str) else
                         [name + '=' + target for name, target in sorted(value.items())] if isinstance(value, dict) else value)
                run['argv'] = run['argv'][:7] + [word for entry in words for word in (flag, entry)]
                run['command'] = shlex.join(['yardr', *run['argv']])
                self.reset_depot(ask)
                receipt = self.create()
                self.assertTrue(receipt['delivered'], receipt)
                document = self.row()['bundle']['document']['text']
                envelope = {k: v for k, v in run['pack'].items() if k != 'plan'}
                self.assertIn(n.review_escape('Pack envelope: ' + n.canonical(envelope)), document)
                self.assertEqual(self.writes(), [])
                for control in ('\n', '\t', '\x85', '\u202e'):
                    changed = copy.deepcopy(ask)
                    mapping = changed['run']['pack']['mappings']
                    if isinstance(value, str):
                        mapping[key] += control
                    elif isinstance(value, dict):
                        mapping[key][next(iter(value))] += control
                    else:
                        mapping[key][0] += control
                    changed['run']['argv'][-1] += control
                    changed['run']['command'] = shlex.join(['yardr', *changed['run']['argv']])
                    self.reset_depot(changed)
                    self.assertFalse(self.create()['delivered'])
                    self.assertEqual((self.sent_count, self.documents, self.writes()), (0, [], []))
                for field in ('mapping', 'argv', 'command'):
                    changed = copy.deepcopy(ask)
                    target = changed['run']
                    if field == 'mapping':
                        mapping = target['pack']['mappings']
                        if isinstance(value, str):
                            mapping[key] += '-other'
                        elif isinstance(value, dict):
                            mapping[key][next(iter(value))] += '-other'
                        else:
                            mapping[key][0] += '-other'
                    else:
                        target['argv'][-1] += '-other'
                        target['command'] = shlex.join(['yardr', *target['argv']])
                        if field == 'command':
                            target['argv'] = run['argv'][:]
                    self.reset_depot(changed)
                    receipt = self.create()
                    self.assertFalse(receipt['delivered'], (field, receipt))
                    self.assertEqual(receipt['failure_kind'], 'permanent')
                    self.assertEqual((self.sent_count, self.documents, self.writes()), (0, [], []))
                if isinstance(value, list):
                    changed = copy.deepcopy(ask)
                    duplicate = value[0] + '-other'
                    changed['run']['pack']['mappings'][key].append(duplicate)
                    changed['run']['argv'] += [flag, duplicate]
                    changed['run']['command'] = shlex.join(['yardr', *changed['run']['argv']])
                    self.reset_depot(changed)
                    self.assertFalse(self.create()['delivered'])
                    self.assertEqual((self.sent_count, self.documents, self.writes()), (0, [], []))
        ask = copy.deepcopy(depot_proposal())
        run = ask['run']
        depot = 'native[depot]:variant'
        run['pack']['depot'] = run['argv'][6] = depot
        run['description'] = run['description'].replace('synthetic', depot)
        run['back'] = run['back'].replace('--depot synthetic', '--depot ' + depot)
        run['command'] = shlex.join(['yardr', *run['argv']])
        self.reset_depot(ask)
        self.assertTrue(self.create()['delivered'])
        self.assertEqual(self.writes(), [])
        for field in ('depot', 'argv', 'command'):
            changed = copy.deepcopy(ask)
            if field == 'depot':
                changed['run']['pack']['depot'] += '-other'
            else:
                changed['run']['argv'][6] += '-other'
                changed['run']['command'] = shlex.join(['yardr', *changed['run']['argv']])
                if field == 'command':
                    changed['run']['argv'] = run['argv'][:]
            self.reset_depot(changed)
            self.assertFalse(self.create()['delivered'])
            self.assertEqual((self.sent_count, self.documents, self.writes()), (0, [], []))
        for name in ('[matrix]', 'left[', 'right]', 'star*', 'question?', '{braces}',
                     "single'quote", 'double"quote', 'with spaces', '[all]*?{}\'" spaces'):
            with self.subTest(depot_command_path=name):
                ask = copy.deepcopy(depot_proposal())
                old = ask['run']['argv'][2]
                path = '/synthetic/checkout/' + name
                ask['run']['argv'][2] = path
                ask['run']['back'] = ask['run']['back'].replace(old, path)
                ask['run']['pack']['label'] = ask['run']['pack']['label'].replace(old, path)
                ask['run']['does'] = [line.replace(old, path) for line in ask['run']['does']]
                ask['run']['command'] = shlex.join(['yardr', *ask['run']['argv']])
                self.reset_depot(ask)
                receipt = self.create()
                self.assertTrue(receipt['delivered'], receipt)
                self.assertIn('- Command: ` ' + ask['run']['command'] + ' `', self.row()['bundle']['document']['text'])
                self.assertEqual(self.writes(), [])
                for command in ("'yardr'  \"pack\" apply " + shlex.quote(path) + ' ' + ' '.join(ask['run']['argv'][3:]),
                                'other ' + ask['run']['command'], ask['run']['command'] + ' --yes',
                                ask['run']['command'] + " '", ask['run']['command'] + ' \\',
                                ask['run']['command'].replace('--depot synthetic', '--depot other'),
                                ask['run']['command'].replace('reviewer=reviewer', 'reviewer=other'),
                                ask['run']['command'].replace(shlex.quote(path), shlex.quote(path + '-other'))):
                    with self.subTest(command=command):
                        changed = copy.deepcopy(ask)
                        changed['run']['command'] = command
                        self.reset_depot(changed)
                        receipt = self.create()
                        equivalent = command.startswith("'yardr'")
                        self.assertEqual(receipt['delivered'], equivalent, receipt)
                        self.assertEqual(self.writes(), [])
                        if equivalent:
                            self.assertIn('- Command: ` ' + command + ' `', self.row()['bundle']['document']['text'])
                        else:
                            self.assertEqual(receipt['failure_kind'], 'permanent')
                            self.assertEqual((self.sent_count, self.documents), (0, []))
        def string_positions(value, path=()):
            if isinstance(value, dict):
                for key, item in value.items():
                    yield path + (key,), True
                    yield from string_positions(item, path + (key,))
            elif isinstance(value, list):
                for index, item in enumerate(value):
                    yield from string_positions(item, path + (index,))
            elif isinstance(value, str):
                yield path, False

        for fixture in sorted(Path(__file__).with_name('fixtures').glob('depot_*.json')):
            if fixture.stem in ('depot_source_replace_public_main', 'depot_matrix_replace_public_main'):
                continue  # These genuine replacements already refuse for missing grants disclosure.
            base = json.loads(fixture.read_text())
            for path, key in string_positions(base):
                for control in ('\u0085', '\x00', '\u009b'):
                    with self.subTest(depot_control=f'U+{ord(control):04X}', fixture=fixture.name, path=path, key=key):
                        ask = copy.deepcopy(base)
                        parent = ask
                        for part in path[:-1]:
                            parent = parent[part]
                        if key:
                            parent[path[-1] + control] = parent.pop(path[-1])
                        else:
                            parent[path[-1]] += control
                        self.reset_depot(ask)
                        receipt = self.create()
                        self.assertFalse(receipt['delivered'], receipt)
                        self.assertEqual(receipt['failure_kind'], 'permanent')
                        self.assertEqual((self.sent_count, self.documents, self.writes()), (0, [], []))
        base = depot_proposal()
        mutations = [
            ('unknown pack key', lambda a: a['run']['pack'].update(unknown=True)),
            ('summary presence', lambda a: a['run']['pack'].update(summary=False)),
            ('files presence', lambda a: a['run']['pack'].update(files=[])),
            ('yes presence', lambda a: a['run']['pack'].update(yes=False)),
            ('unknown plan key', lambda a: a['run']['pack']['plan'].update(unknown=[])),
            ('missing plan', lambda a: a['run']['pack'].pop('plan')),
            ('depot mismatch', lambda a: a['run']['pack'].update(depot='other')),
            ('mapping mismatch', lambda a: a['run']['pack']['mappings']['crew'].update(reviewer='other')),
            ('unknown mapping', lambda a: a['run']['pack']['mappings'].update(unknown=[])),
            ('unknown object', lambda a: a['run']['pack']['plan']['objects'][0].update(unknown=True)),
            ('unknown kind', lambda a: a['run']['pack']['plan']['objects'][0].update(kind='shell')),
            ('unknown frame kind', lambda a: a['run']['pack']['plan']['objects'][0].update(kind='frame')),
            ('unknown action', lambda a: a['run']['pack']['plan']['objects'][0].update(action='remove')),
            ('duplicate object', lambda a: a['run']['pack']['plan']['objects'].append(copy.deepcopy(a['run']['pack']['plan']['objects'][0]))),
            ('unknown change key', lambda a: a['run']['pack']['plan']['objects'][1]['changes'][0].update(unknown='x')),
            ('unknown change field', lambda a: a['run']['pack']['plan']['objects'][1]['changes'][0].update(field='env_file')),
            ('missing old change', lambda a: a['run']['pack']['plan']['objects'][1]['changes'][0].pop('old')),
            ('missing new change', lambda a: a['run']['pack']['plan']['objects'][1]['changes'][0].pop('new')),
            ('missing changed fields', lambda a: a['run']['pack']['plan']['objects'][1].pop('changes')),
            ('wrong file hash', lambda a: a['run']['pack']['plan']['objects'][0].update(file_new_sha256='a' * 64)),
            ('missing file text', lambda a: a['run']['pack']['plan']['objects'][0].pop('text')),
            ('missing file diff', lambda a: a['run']['pack']['plan']['objects'][0].pop('diff')),
            ('partial file diff', lambda a: a['run']['pack']['plan']['objects'][0].update(diff='--- was\n+++ is\n')),
            ('invalid file path', lambda a: a['run']['pack']['plan']['objects'][0].update(name='../escape')),
            ('hand edited', lambda a: a['run']['pack']['plan']['objects'][0].update(edited=True)),
            ('unknown weights', lambda a: a.update(weight=[dict({'class': 'credential'}, line='Unsupported pack weight')])),
            ('duplicate weights', lambda a: a['weight'].append(copy.deepcopy(a['weight'][0]))),
            ('whole line limit', lambda a: a['run'].update(does=['A trusted native row.'] * 41)),
            ('control content', lambda a: a['run']['does'].append('Hidden\x1bdisclosure')),
            ('foreign orphan section', lambda a: a['run']['does'].append('the source other is not in the yard, but its command copy directory has entries this pack would replace or remove.')),
            ('missing pin', lambda a: a['run'].update(argv=a['run']['argv'][:3] + a['run']['argv'][5:])),
            ('unrecognized argv', lambda a: a['run']['argv'].extend(['--yes'])),
            ('repeated mapping', lambda a: a['run']['argv'].extend(['--crew', 'reviewer=reviewer'])),
        ]
        for blocker in ('missing', 'asks', 'refused', 'nontext', 'edited'):
            mutations.append((blocker, lambda a, key=blocker: a['run']['pack']['plan'].update({key: ['Unresolved']})))
        for label, mutate in mutations:
            with self.subTest(depot_refusal=label):
                ask = copy.deepcopy(base)
                mutate(ask)
                self.reset_depot(ask)
                receipt = self.create()
                self.assertFalse(receipt['delivered'], receipt)
                if label != 'control content':
                    self.assertIn('invalid_depot_pack', receipt['reason'])
                self.assertEqual((self.sent_count, self.documents, self.writes()), (0, [], []))
        for kind in ('matrix_first', 'matrix_change'):
            for code in (*range(32), *range(127, 160), *sorted(map(ord, n.HIDDEN))):
                if chr(code) in '\r\n\t':
                    continue
                with self.subTest(depot_file_control=f'U+{code:04X}', fixture=kind):
                    ask = depot_proposal(kind)
                    obj = ask['run']['pack']['plan']['objects'][0]
                    first = obj['text'].split('\n')[0]
                    obj['text'] = obj['text'].replace(first, chr(code) + first, 1)
                    obj['diff'] = obj['diff'].replace('\n+' + first, '\n+' + chr(code) + first, 1)
                    obj['file_new_sha256'] = n.file_hash(obj['text'])
                    for change in obj.get('changes', []):
                        if change['field'] == 'content':
                            change['new'] = 'sha256 ' + obj['file_new_sha256'][:12]
                    self.reset_depot(ask)
                    receipt = self.create()
                    self.assertFalse(receipt['delivered'], receipt)
                    self.assertIn('invalid_depot_pack', receipt['reason'])
                    self.assertEqual((self.sent_count, self.documents, self.writes()), (0, [], []))
        for empty_field in ('text', 'diff'):
            ask = copy.deepcopy(base)
            obj = ask['run']['pack']['plan']['objects'][0]
            obj.update(text='', diff='', file_new_sha256=n.file_hash(''))
            obj.pop(empty_field)
            self.reset_depot(ask)
            self.assertTrue(self.create()['delivered'])
            self.assertIn(n.review_code(''), self.row()['bundle']['document']['text'])
        ask = copy.deepcopy(base)
        ask['run']['does'][0] = 'Reworded native depot disclosure, deliberately not semantically parsed.'
        ask['run']['does'].append('An unfamiliar native sentence remains visible.')
        self.reset_depot(ask)
        self.assertTrue(self.create()['delivered'])
        self.assertIn(n.review_code(ask['run']['does'][-1]), self.row()['bundle']['document']['text'])
        self.reset_depot(base)
        self.assertTrue(self.create()["delivered"])
        self.native.ask = ask
        self.press()
        self.assertEqual(self.row()["state"], "stale")
        self.assertEqual(self.writes(), [])
        ask = depot_proposal('source_replace')
        ask['run']['does'].insert(1, 'grants kept for source other: crew member reviewer: read (kept).')
        self.reset_depot(ask)
        self.assertFalse(self.create()['delivered'])
        self.assertEqual((self.sent_count, self.documents, self.writes()), (0, [], []))
        ask = depot_proposal('matrix_change')
        duplicate = copy.deepcopy(ask['run']['pack']['plan']['objects'][0])
        duplicate['name'] = 'roles/another.md'
        ask['run']['pack']['plan']['objects'].append(duplicate)
        self.reset_depot(ask)
        self.assertFalse(self.create()['delivered'], 'two complete individually bounded files exceed aggregate 64 KiB')
        self.assertEqual((self.sent_count, self.documents, self.writes()), (0, [], []))
        for limit in ('PACK_DOCUMENT_BYTES', 'CAP'):
            self.reset_depot(depot_proposal('matrix_first'))
            with patch.object(n, limit, 1):
                self.assertFalse(self.create()['delivered'])
            self.assertEqual((self.sent_count, self.documents, self.writes()), (0, [], []))
        preview = Path(__file__).resolve().parents[1] / 'scripts/preview_run_asks.py'
        env = dict(HOME=self.tmp.name, PATH=self.tmp.name + ':/usr/local/bin:/usr/bin:/bin')
        html = subprocess.run([sys.executable, '-B', str(preview), '--depot'], env=env,
                              check=True, capture_output=True).stdout.decode()
        self.assertNotIn('\r', html, 'HTML carriage returns must be entities so the browser preserves them')
        self.assertIn('&#13;', html)
        documents = re.findall('href="data:text/plain;base64,([A-Za-z0-9+/=]+)"', html)
        self.assertEqual(len(documents), 2)
        for name, encoded in zip(('matrix_first', 'matrix_change'), documents):
            document = base64.b64decode(encoded).decode()
            for obj in depot_proposal(name)['run']['pack']['plan']['objects']:
                if obj['kind'] == 'file' and obj['action'] in ('create', 'change'):
                    for field in ('text', 'diff'):
                        self.assertIn(n.review_code(obj.get(field, '')), document)

    def test_source_pack_weight_presence_is_structural(self):
        for base in (source_proposal('new_public_main'), pack_proposal('directory_public_main')):
            ask = copy.deepcopy(base)
            ask['weight'][0]['line'] = 'Native program warning <&>🙂; wording belongs to core.'
            self.assert_source_contract(ask, True)
            self.assertIn(ask['weight'][0]['line'], self.row()['plain'])
            self.assert_bound_source_change_stales(base, ask)
            ask['weight'] = []
            self.assert_source_contract(ask, False)
        free = source_proposal('free_public_main')
        for weights in ([], [{'class': 'reads', 'line': 'Native reads warning'}],
                        [{'class': 'code', 'line': 'Native code warning'}]):
            free['weight'] = weights
            self.assert_source_contract(free, True)
        for weights in ([{'class': 'credential', 'line': 'Unknown'}],
                        [{'class': 'code', 'line': 'One'}, {'class': 'code', 'line': 'Two'}],
                        [{'class': 'reads', 'line': 'One'}, {'class': 'reads', 'line': 'Two'}],
                        [{'class': 'reads', 'line': 'x' * (n.CAP + 1)}],
                        [{'class': 'reads', 'line': ''}], [{'class': 'reads', 'line': '\u202ehidden'}]):
            free['weight'] = weights
            self.assert_source_contract(free, False)
        empty = source_proposal('empty_replace_public_main')
        empty['run']['does'][2] = 'it would replace the source empty the yard has under that name; no grant of source empty stands.'
        self.assert_source_contract(empty, True)
        empty['weight'] = []
        self.assert_source_contract(empty, False)
        pack = pack_proposal('empty_replace_public_main')
        pack['run']['does'][2] = 'it would replace the source empty the yard has under that name; no grant of source empty stands.'
        self.assert_source_contract(pack, True)
        pack['weight'] = [{'class': 'reads', 'line': 'Native reads warning'}]
        self.assert_source_contract(pack, True)
        pack['weight'] = [{'class': 'code', 'line': 'Native optional program warning'}]
        self.assert_source_contract(pack, True)
        free['weight'] = [{'class': 'code', 'line': 'Native program warning'}, {'class': 'reads', 'line': 'Native reads warning'}]
        changed = copy.deepcopy(free)
        changed['weight'].pop()
        self.assert_bound_source_change_stales(free, changed)
        orphan = source_proposal('orphan_public_main')
        carried = orphan['run']['carried'][0]
        carried['inventory'][0].update(sha256=carried['files'][0]['sha256'], size=len(carried['files'][0]['text'].encode()))
        carried.pop('old')
        orphan['weight'] = []
        self.assert_source_contract(orphan, True)
        carried['inventory'][0]['symlink'] = True
        self.assert_source_contract(orphan, False)
        orphan['weight'] = [{'class': 'code', 'line': 'Native symlink program warning'}]
        self.assert_source_contract(orphan, True)

    def test_source_pack_replacement_accounting_fails_closed(self):
        for base in (source_proposal('replace'), pack_proposal('directory_replace')):
            self.assert_source_contract(base, True)
            marker = next(i for i, line in enumerate(base['run']['does']) if line.startswith('it would replace the source '))
            for value in (None, 'Native replacement wording changed.',
                          base['run']['does'][marker].replace('source synthetic', 'source foreign')):
                ask = copy.deepcopy(base)
                if value is None:
                    ask['run']['does'].pop(marker)
                else:
                    ask['run']['does'][marker] = value
                self.assert_source_contract(ask, False)
            ask = copy.deepcopy(base)
            ask['run']['does'].insert(marker, ask['run']['does'][marker])
            self.assert_source_contract(ask, False)
        for kind in ('replace_public_main', 'empty_replace_public_main'):
            self.assert_source_contract(source_proposal(kind), False)
        orphan = source_proposal('orphan_public_main')
        self.assert_source_contract(orphan, True)
        for value in (None, 'The source orphan is absent, with old command copies.'):
            ask = copy.deepcopy(orphan)
            if value is None:
                ask['run']['does'].pop(2)
            else:
                ask['run']['does'][2] = value
            self.assert_source_contract(ask, False)
        ask = source_proposal('replace')
        ask['run']['does'][0] = source_proposal('new')['run']['does'][0]
        ask['run']['back'] = 'yardr source rm synthetic'
        ask['run']['back_by'] = 'the operator'
        ask['run']['does'] = [line for line in ask['run']['does'] if not line.startswith('it would replace the source ')]
        self.assert_source_contract(ask, False)
        for field, value in (('back', 'yardr source rm synthetic'), ('back_by', 'the operator')):
            ask = source_proposal('replace')
            ask['run'][field] = value
            self.assert_source_contract(ask, False)
        orphan = pack_proposal('orphan_public_main')
        self.assert_source_contract(orphan, True)
        for value in (None, 'Unknown orphan wording.', orphan['run']['does'][2].replace('packorphan', 'foreign')):
            ask = copy.deepcopy(orphan)
            if value is None:
                ask['run']['does'].pop(2)
            else:
                ask['run']['does'][2] = value
            self.assert_source_contract(ask, False)
        empty = pack_proposal('empty_replace_public_main')
        self.assert_source_contract(empty, False)
        empty['run']['does'][2] = 'it would replace the source empty the yard has under that name; no grant of source empty stands.'
        self.assert_source_contract(empty, True)
        for base in (empty, source_proposal('empty_replace_public_main')):
            base['run']['does'][2] = 'it would replace the source empty the yard has under that name; no grant of source empty stands.'
            for marker in (None, 'Unknown replacement wording.'):
                ask = copy.deepcopy(base)
                if marker is None:
                    ask['run']['does'].pop(2)
                else:
                    ask['run']['does'][2] = marker
                self.assert_source_contract(ask, False)
        mixed = copy.deepcopy(empty)
        free = source_proposal('free_public_main')
        mixed['run']['does'][0] = mixed['run']['does'][0].replace('it replaces 1 source', 'it adds 1 source and replaces 1 source')
        mixed['run']['carried'].extend(free['run']['carried'])
        mixed['run']['does'].extend(free['run']['does'][1:])
        self.assert_source_contract(mixed, True)
        for mutate in ('missing-marker', 'reworded-marker', 'lost-cardinality', 'duplicate-grants', 'foreign-grants'):
            ask = copy.deepcopy(mixed)
            if mutate == 'missing-marker':
                ask['run']['does'].pop(2)
            elif mutate == 'reworded-marker':
                ask['run']['does'][2] = 'Native replacement wording changed.'
            elif mutate == 'lost-cardinality':
                ask['run']['does'][0] = ask['run']['does'][0].replace('adds 1 source and replaces 1 source', 'adds 2 sources')
            elif mutate == 'duplicate-grants':
                ask['run']['does'].insert(3, ask['run']['does'][2])
            else:
                ask['run']['does'][2] = ask['run']['does'][2].replace('no grant of source empty', 'no grant of source foreign')
            self.assert_source_contract(ask, False)

    def test_ordinary_options_words_later_reopen_decline_and_native_close(self):
        ask = dense_decision()
        self.assertTrue(self.create_decision(ask)['delivered'])
        card = self.calls[-1][1]
        self.assertEqual(card['parse_mode'], 'HTML')
        self.assertIn('<b>' + ask['question'] + '</b>\n\n' + ask['context'][0], card['text'])
        for index, option in enumerate(ask['options'], 1):
            self.assertIn(f"\n\n<b>{index}. {option['label']}</b>\n  {option['consequence']}", card['text'])
        self.assertIn('\n\nRecommended: 1. ' + ask['options'][0]['label'] + '\nWhy: ' + ask['why'], card['text'])
        self.assertIn('\n\nReply directly', card['text'])
        self.reply('2 synthetic choice')
        self.assertIn('\n\n✓ Answered: 2. ', self.edits()[-1]['text'])
        self.assertEqual(self.writes()[-1][1]['words'], 'synthetic choice')

        for source, expected in [('# Choose **carefully**\nnext line', '<b>Choose <b>carefully</b></b>\n\n<b>next line</b>'),
                                 ('**unfinished', '<b>**unfinished</b>'),
                                 ('`[ref]: http://evil "visible line"`', '<b><code>[ref]: http://evil "visible line"</code></b>'),
                                 ('```\n[ref]: http://evil "visible line"\n```', '<pre><code>[ref]: http://evil "visible line"\n</code></pre>'),
                                 ('\\[ref]: http://evil "visible line"', '<b>[ref]: http://evil "visible line"</b>'),
                                 ('a | b\n--- | ---\n$x$', '<b>a | b\n--- | ---\n$x$</b>')]:
            with self.subTest(markdown=source):
                ask = dict(decision(), question=source, context=['- first\n- second\n\n1. one\n2. two\n\n> quoted\n\nlast  \nline'])
                self.assertTrue(self.create_decision(ask)['delivered'])
                card = self.calls[-1][1]
                self.assertIn(expected, card['text'])
                self.assertIn('• first\n• second\n\n1. one\n2. two\n\n&gt; quoted\n\nlast\nline', card['text'])
                visible, error = b.html_visible_text(card['text'])
                self.assertIsNone(error)
                self.assertEqual(visible, self.row()['plain'])

        hostile = '<a href="tg://user?id=123">raw</a> & [mention](tg://user?id=123) [link](https://example.invalid) ![image](https://example.invalid/i)'
        ask = dict(decision(), question=hostile, context=[hostile], asker=hostile, why=hostile,
                   options=[dict(label=hostile, consequence=hostile + '\nsecond line')], recommend=1,
                   about=[dict(id=hostile, title=hostile, stage=hostile)])
        self.assertTrue(self.create_decision(ask)['delivered'])
        card = self.calls[-1][1]
        self.assertNotIn('<a ', card['text'])
        self.assertNotIn('<img', card['text'])
        self.assertIn('&lt;a href=', card['text'])
        self.assertIn('mention (tg://user?id=123)', card['text'])
        self.assertIn('\n  second line', card['text'])
        visible, error = b.html_visible_text(card['text'])
        self.assertIsNone(error)
        self.assertEqual(visible, self.row()['plain'])
        self.reply('1 <b>canonical</b> [mention](tg://user?id=123)')
        self.assertIn('\n\n✓ Answered: 1. &lt;a href=', self.edits()[-1]['text'])
        self.assertNotIn('<a ', self.edits()[-1]['text'])

        transport = self.bridge.request
        def lost_response(url, body, *args, **kwargs):
            response = transport(url, body, *args, **kwargs)
            if url.endswith('/sendMessage'):
                raise b.RequestUnconfirmed('response lost after delivery', exit_code=28, phase='after-send')
            return response
        with patch.object(self.bridge, 'request', side_effect=lost_response):
            self.assertFalse(self.create_decision()['delivered'])
        close = self.bridge.api_ask_close(dict(ask_key='decision-1', decision='ignored'))
        self.assertFalse(close['delivered'])
        self.assertIn('ask_card_delivery_unconfirmed', close['reason'])
        self.assertIn('tap the original card to reconcile', close['reason'])
        self.assertIn('delete the card manually', close['reason'])
        self.assertIsNone(self.row()['message_id'])
        self.assertEqual(self.sent_count, 1)

        self.assertTrue(self.create_decision()['delivered'])
        card = self.calls[-1][1]
        for field in ('Where should the links go?', 'First &lt;context&gt;', 'Second &amp; context',
                      '1. Move', 'Move every link.', '2. Keep', 'Keep the links here.',
                      '3. You decide', 'leave it to proposer', 'Recommended: 1. Move',
                      'Why: The new location is ready.', 'About: related-1 · Related &lt;work&gt; · backlog'):
            self.assertIn(field, card['text'])
        self.assertEqual(len(card['reply_markup']['inline_keyboard']), 5)
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
        for source in ('Keep logs.\n\n[x]: /rm-rf-everything',
                       '[ref]: http://evil "hidden line"',
                       '[x][ref]\n\n[ref]: http://a.example',
                       '[ref]: http://a.example\n[ref]: http://evil "hidden line"',
                       '> [ref]:\n> /rm-rf-everything\n> "hidden line"'):
            for field in ('question', 'context'):
                with self.subTest(source=source, field=field):
                    change = {field: [source] if field == 'context' else source}
                    receipt = self.create_decision(dict(decision(), **change))
                    self.assertFalse(receipt['delivered'], receipt)
                    self.assertEqual(receipt['reason'], 'invalid native card Markdown: markdown_token_unsupported' + n.FALLBACK)
                    self.assertEqual(self.calls, [])
                    self.assertEqual(self.bridge.run_asks.rows(), [])
                    self.assertEqual(self.writes(), [])
        old = decision()
        receipt = self.create_decision(dict(decision(), context=['x' * 3600]))
        self.assertEqual(receipt['reason'], 'ask_card_payload_too_large_for_card' + n.FALLBACK)
        self.assertEqual(self.calls, [])
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
        transport = self.bridge.request
        def lost_response(url, body, *args, **kwargs):
            response = transport(url, body, *args, **kwargs)
            if url.endswith('/sendMessage'):
                raise b.RequestUnconfirmed('response lost after delivery', exit_code=28, phase='after-send')
            return response
        with patch.object(self.bridge, 'request', side_effect=lost_response):
            self.assertFalse(self.create_decision()['delivered'])
        self.press('pick1', message_id=77)
        self.assertIn('Card delivery unconfirmed', self.calls[-1][1]['text'])
        self.assertIn('decide in Footplate or the yard', self.calls[-1][1]['text'])
        self.assertIn('delete this Telegram card manually', self.calls[-1][1]['text'])
        self.assertEqual(self.row()['generations'], [])
        self.assertEqual(self.writes(), [])

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
                    self.assertEqual(len(cards()), count + int(state.startswith('reopened') or state == 'proposal-changed'))
                    if state not in ('open', 'unreadable'):
                        retired = [body for body in self.edits() if body['message_id'] == original['message_id']]
                        self.assertTrue(retired)
                        self.assertEqual(retired[-1]['reply_markup']['inline_keyboard'], [])
                    if state == 'proposal-changed':
                        self.assertIn(n.REPLACED_PROPOSAL, retired[-1]['text'])
                        self.assertIn('Changed question', cards()[-1]['text'])
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

    def test_ordinary_stale_decision_card_reissues_once_and_keeps_stale_refused(self):
        self.config['topics'] = (dict(name='ops', agent='yardmaster'),)
        self.store.record_topic('ops', 19, 'created')
        self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY',
                               request=self.bridge.request, request_multipart=self.bridge.request_multipart)
        self.bridge.hook_secret = None
        self.bridge.sleep = lambda _: None

        def cards():
            return [body for url, body in self.calls if url.endswith('/sendMessage')
                    and body.get('reply_markup', {}).get('inline_keyboard')]

        def tap(row, action='pick1', uid=100):
            query = dict(id=f'q-{uid}', data=f"askcard:{row['id']}:{action}:{row['turn']}",
                         message=dict(message_id=row['message_id'], chat=dict(id=123, type='private'),
                                      **{'from': dict(id=999, is_bot=True)}),
                         **{'from': dict(id=123, is_bot=False)})
            if row['conversation'] != '123':
                query['message'].update(is_topic_message=True, message_thread_id=int(row['conversation'].rsplit(':', 1)[1]))
            self.bridge.inbound(dict(update_id=uid, callback_query=query))

        def reply_to(row, text, uid=200):
            message = dict(message_id=uid, text=text, chat=dict(id=123, type='private'),
                           reply_to_message=dict(message_id=row['message_id']),
                           **{'from': dict(id=123, is_bot=False)})
            if row['conversation'] != '123':
                message.update(is_topic_message=True, message_thread_id=int(row['conversation'].rsplit(':', 1)[1]))
            self.bridge.inbound(dict(update_id=uid, message=message))

        # 1. Stale on bead title, about bead stage/title, or home identity -> one fresh card, stale refused.
        for cause in ('title', 'about', 'home_identity'):
            with self.subTest(cause=cause):
                self.reset_native(decision())
                self.native.bead = dict(id='sample-1', title='Initial title')
                self.assertTrue(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, topic='ops', silent=True, key='k1'))['delivered'])
                stale_row = self.bridge.run_asks.rows()[0]
                if cause == 'title':
                    self.native.bead['title'] = 'Updated title'
                elif cause == 'about':
                    self.native.ask['about'][0]['stage'] = 'review'
                else:
                    previous = self.home.with_name(f'prev-{cause}')
                    self.home.rename(previous)
                    self.home.mkdir(mode=0o700)
                    replaced = NativeSocket(self.home)
                    replaced.bead = dict(id='sample-1', title='Initial title')
                    replaced.ask = decision()
                    self.addCleanup(replaced.close)
                    self.native = replaced

                tap(stale_row, 'pick1', uid=101)
                self.assertEqual(self.writes(), [])
                self.assertEqual(len(cards()), 2)
                self.assertTrue(all(c.get('disable_notification') is True for c in cards()))
                self.assertEqual(cards()[-1].get('message_thread_id'), 19)
                if cause == 'title':
                    self.assertIn('Updated title', cards()[-1]['text'])
                elif cause == 'about':
                    self.assertIn('related-1 · Related &lt;work&gt; · review', cards()[-1]['text'])

                retired = [e for e in self.edits() if e['message_id'] == stale_row['message_id']]
                self.assertTrue(retired)
                self.assertIn(n.REPLACED_PROPOSAL, retired[-1]['text'])
                self.assertNotIn('use Footplate or the yard cards', retired[-1]['text'])
                self.assertEqual(retired[-1]['reply_markup']['inline_keyboard'], [])

                # Stale tap & reply stay refused; repeated refresh/replay/create send no duplicate.
                self.bridge.run_asks = n.RunAsks(self.bridge)
                self.bridge.run_asks.refresh()
                tap(stale_row, 'pick2', uid=102)
                reply_to(stale_row, '1 stale', uid=103)
                receipt = self.bridge.api_ask_card(dict(bead='sample-1', ask=7, topic='ops', silent=True, key='k1'))
                self.assertTrue(receipt['delivered'])
                self.assertEqual(len(cards()), 2)
                self.assertEqual(self.writes(), [])

                fresh_row = next(r for r in self.bridge.run_asks.rows() if r['state'] == 'open')
                self.assertNotEqual(fresh_row['id'], stale_row['id'])
                self.assertEqual(int(receipt['message_id']), fresh_row['message_id'])
                tap(fresh_row, 'pick2', uid=104)
                self.assertEqual(self.writes(), [('AnswerAskExpected', dict(bead='sample-1', ask=7, turn=0,
                                                                           actor='pat', option=2, words='', weighed=False))])
                self.assertEqual(self.bridge.run_asks.get(fresh_row['id'])['state'], 'answered')

        # 2. Second proposal change (P0 -> P1 -> P2) and ask_close(ask_key=...) resolve to active card.
        self.reset_native(decision())
        self.native.bead = dict(id='sample-1', title='Title 0')
        self.assertTrue(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='k-chain'))['delivered'])
        row0 = self.bridge.run_asks.rows()[0]
        self.native.bead['title'] = 'Title 1'
        self.bridge.run_asks.refresh()
        row1 = next(r for r in self.bridge.run_asks.rows() if r['state'] == 'open')
        self.assertNotEqual(row1['id'], row0['id'])
        self.native.bead['title'] = 'Title 2'
        self.bridge.run_asks.refresh()
        row2 = next(r for r in self.bridge.run_asks.rows() if r['state'] == 'open')
        self.assertNotEqual(row2['id'], row1['id'])
        self.assertEqual(len(cards()), 3)
        self.assertIn('Title 2', cards()[-1]['text'])
        self.native.ask.update(state='answered', option=1, words='closed externally', turn=1)
        close = self.bridge.api_ask_close(dict(ask_key='k-chain', decision='ignored'))
        self.assertTrue(close['delivered'])
        self.assertEqual(self.bridge.run_asks.get(row2['id'])['state'], 'answered')
        self.assertEqual(len(cards()), 3)

        # 3. Ask answered, declined, taken back, or put aside before re-issue -> zero fresh cards.
        for terminal in ('answered', 'declined', 'taken back', 'put aside'):
            with self.subTest(before_reissue=terminal):
                self.reset_native(decision())
                self.native.bead = dict(id='sample-1', title='Before')
                self.assertTrue(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='k-term'))['delivered'])
                row0 = self.bridge.run_asks.rows()[0]
                self.native.bead['title'] = 'After'
                self.native.ask.update(state=terminal, option=1, words='done', by='proposer')
                self.bridge.run_asks.refresh()
                self.assertEqual(len(cards()), 1)
                self.assertEqual(self.bridge.run_asks.get(row0['id'])['state'], 'stale')
                self.assertIn('use Footplate or the yard cards', self.edits()[-1]['text'])

        # 4. Restart between marking stale and posting -> exact 1 fresh card when open, 0 when closed meanwhile.
        for outcome_before_restart in ('open', 'answered', 'declined', 'taken back'):
            with self.subTest(restart_between_mark_and_post=outcome_before_restart):
                self.reset_native(decision())
                self.native.bead = dict(id='sample-1', title='Before restart')
                self.assertTrue(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='k-crash'))['delivered'])
                row0 = self.bridge.run_asks.rows()[0]
                self.native.bead['title'] = 'After restart'
                class CrashBetweenMarkAndPost(BaseException):
                    pass
                with patch.object(self.bridge.run_asks, '_reissue_decision', side_effect=CrashBetweenMarkAndPost):
                    with self.assertRaises(CrashBetweenMarkAndPost):
                        self.bridge.run_asks.refresh()
                marked = self.bridge.run_asks.get(row0['id'])
                self.assertEqual(marked['state'], 'stale')
                self.assertTrue(marked.get('replaced_by'))
                self.assertIsNone(self.bridge.run_asks.get(marked['replaced_by']))
                self.assertEqual(len(cards()), 1)

                if outcome_before_restart != 'open':
                    self.native.ask.update(state=outcome_before_restart, option=1, words='resolved', by='proposer')
                self.bridge.run_asks = n.RunAsks(self.bridge)
                self.bridge.run_asks.refresh()
                self.bridge.run_asks.refresh()
                if outcome_before_restart == 'open':
                    self.assertEqual(len(cards()), 2)
                    fresh = next(r for r in self.bridge.run_asks.rows() if r['state'] == 'open')
                    self.assertNotEqual(fresh['id'], row0['id'])
                    self.assertIn(n.REPLACED_PROPOSAL, self.edits()[-1]['text'])
                    tap(row0, 'pick1', uid=110)
                    self.assertEqual(self.writes(), [])
                    tap(fresh, 'pick1', uid=111)
                    self.assertEqual(len(self.writes()), 1)
                else:
                    self.assertEqual(len(cards()), 1)
                    self.assertIsNone(self.bridge.run_asks.get(row0['id']).get('replaced_by'))
                    self.assertIn('use Footplate or the yard cards', self.edits()[-1]['text'])

        # 5. Oversized new title (FALLBACK) -> no fresh card, stale directs to Footplate.
        self.reset_native(decision())
        self.native.bead = dict(id='sample-1', title='Short')
        self.assertTrue(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='k-big'))['delivered'])
        row0 = self.bridge.run_asks.rows()[0]
        self.native.bead['title'] = 'X' * 3500
        self.bridge.run_asks.refresh()
        self.assertEqual(len(cards()), 1)
        self.assertIsNone(self.bridge.run_asks.get(row0['id']).get('replaced_by'))
        self.assertIn('use Footplate or the yard cards', self.edits()[-1]['text'])

        # 6. Run cards keep today's stale refusal without re-issuing.
        self.reset_native(proposal())
        self.native.bead = dict(id='sample-1', title='Run title 1')
        self.assertTrue(self.create()['delivered'])
        run_row = self.row()
        self.native.bead['title'] = 'Run title 2'
        self.press('approve', uid=120)
        self.assertEqual(len(cards()), 1)
        self.assertEqual(self.row()['state'], 'stale')
        self.assertIn('Proposal changed; use Footplate or the yard cards', self.edits()[-1]['text'])
        self.assertEqual(self.writes(), [])

        # 7. A digest value that recurs posts one further fresh card, never re-linking a retired row.
        self.reset_native(decision())
        self.native.bead = dict(id='sample-1', title='Title 0')
        self.assertTrue(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='k-cycle'))['delivered'])
        cycle0 = self.bridge.run_asks.rows()[0]
        self.native.bead['title'] = 'Title 1'
        self.bridge.run_asks.refresh()
        cycle1 = next(r for r in self.bridge.run_asks.rows() if r['state'] == 'open')
        self.assertNotEqual(cycle1['id'], cycle0['id'])
        self.native.bead['title'] = 'Title 2'
        self.bridge.run_asks.refresh()
        cycle2 = next(r for r in self.bridge.run_asks.rows() if r['state'] == 'open')
        self.assertNotEqual(cycle2['id'], cycle1['id'])
        self.assertEqual(len(cards()), 3)
        self.native.bead['title'] = 'Title 1'
        self.bridge.run_asks.refresh()
        cycle3 = next(r for r in self.bridge.run_asks.rows() if r['state'] == 'open')
        self.assertEqual(len(cards()), 4)
        self.assertIn('Title 1', cards()[-1]['text'])
        self.assertNotIn(cycle3['id'], (cycle0['id'], cycle1['id'], cycle2['id']))
        # The replacement chain stays acyclic and ends at the open head.
        seen, head = [], self.bridge.run_asks.get(cycle0['id'])
        while head is not None and head['id'] not in seen:
            seen.append(head['id'])
            head = self.bridge.run_asks.get(head['replaced_by']) if head.get('replaced_by') else None
        self.assertEqual(seen, [cycle0['id'], cycle1['id'], cycle2['id'], cycle3['id']])
        self.assertIsNone(self.bridge.run_asks.get(cycle3['id']).get('replaced_by'))
        self.assertEqual(
            self.bridge.run_asks.resolve_row(self.bridge.run_asks.get(
                self.bridge.run_asks.ident('sample-1', 7)))['id'], cycle3['id'])
        # Retired taps and replies stay refused; retries and restarts send nothing new.
        tap(cycle1, 'pick1', uid=130)
        tap(cycle2, 'pick2', uid=131)
        reply_to(cycle1, '1 stale', uid=132)
        self.assertEqual(self.writes(), [])
        self.bridge.run_asks = n.RunAsks(self.bridge)
        self.bridge.run_asks.refresh()
        self.bridge.run_asks.refresh()
        receipt = self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='k-cycle'))
        self.assertTrue(receipt['delivered'])
        self.assertEqual(len(cards()), 4)
        self.assertEqual(self.writes(), [])
        tap(cycle3, 'pick2', uid=133)
        self.assertEqual(self.writes(), [('AnswerAskExpected', dict(bead='sample-1', ask=7, turn=0,
                                                                    actor='pat', option=2, words='', weighed=False))])
        self.assertEqual(self.bridge.run_asks.get(cycle3['id'])['state'], 'answered')

        # 8. An answer racing the mark-to-post interval posts no replacement.
        self.reset_native(decision())
        self.native.bead = dict(id='sample-1', title='Before race')
        self.assertTrue(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='k-race'))['delivered'])
        race0 = self.bridge.run_asks.rows()[0]
        self.native.bead['title'] = 'After race'
        original = self.bridge.run_asks._reissue_decision
        def answer_after_mark(row, bead=None, ask=None):
            self.native.ask.update(state='answered', option=1, words='done', by='proposer')
            return original(row, bead, ask)
        with patch.object(self.bridge.run_asks, '_reissue_decision', side_effect=answer_after_mark):
            self.bridge.run_asks.refresh()
        self.assertEqual(len(cards()), 1)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.bridge.run_asks.get(race0['id'])['state'], 'stale')
        self.assertIsNone(self.bridge.run_asks.get(race0['id']).get('replaced_by'))
        self.assertIn('use Footplate or the yard cards', self.edits()[-1]['text'])

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

        # A pre-formatting ledger keeps its exact confirmed edit identity on restart.
        self.reset_native(proposal())
        self.assertTrue(self.create()['delivered'])
        self.press()
        row = self.row()
        row.update(html=row['html'].rstrip('\n'), plain=row['plain'].rstrip('\n'))
        self.bridge.run_asks.save(row)
        identity = dict(text=row['html'] + '\n' + b.html_escape(row['display']['footer']),
                        message_id=row['message_id'], reply_markup={'inline_keyboard': []})
        key = f"runask:{row['id']}:revision:{row['revision']}:edit"
        with self.store.connect() as db:
            db.execute('UPDATE deliveries SET fingerprint=? WHERE key=?',
                       (self.bridge.delivery_fingerprint(identity), key))
        calls = len(self.calls)
        self.bridge.run_asks = n.RunAsks(self.bridge)
        self.assertTrue(self.bridge.api_ask_close(dict(ask_key='native-1', decision='ignored'))['delivered'])
        self.assertEqual(len(self.calls), calls)

        self.reset_native(proposal())
        self.assertTrue(self.create()['delivered'])
        self.exercise_non_owner_rows()
        self.create_decision()
        self.exercise_non_owner_rows()

    def exercise_non_owner_rows(self):
        owner = self.bridge
        old_binding = {key: str(self.config.get(key, 'tgbridge' if key == 'yardr_source' else '')) for key in
                       ('yard', 'yardr_home', 'yardr_operator', 'yardr_source', 'bot_id', 'user_id', 'chat_id')}
        self.assertEqual(owner.run_asks.binding(), old_binding)
        owner.run_asks.claim_update(90000)
        relayed = []
        def request(url, body, *args, **kwargs):
            if url == self.config['hook_url']:
                relayed.append(body)
                return 200, dict(seq=91001)
            return owner.request(url, body, *args, **kwargs)
        self.bridge = b.Bridge(dict(self.config, yardr_cards=False, hook_secret='SYNTHETIC'), self.store,
                               'TOKEN', 'CAPABILITY', request=request)
        try:
            self.assertEqual(n.binding_of(self.bridge.config), old_binding)
            self.assertFalse(self.bridge.run_asks.claim_update(90000))
            self.assertEqual(self.bridge.typed_nudge.binding_digest(), n.digest(old_binding))
            self.native.calls.clear()
            self.press('pick1' if self.row().get('kind') == 'decision' else 'approve', uid=91000)
            self.assertIn('native_run_asks_not_configured', str(self.calls[-1][1]))
            self.reply('1', uid=91001)
            self.assertEqual(len(relayed), 0 if self.row().get('kind') == 'decision' else 1)
            key = 'decision-1' if self.row().get('kind') == 'decision' else 'native-1'
            for selector in (dict(message_id=self.row()['message_id']), dict(ask_key=key)):
                receipt = self.bridge.api_ask_close(dict(selector, decision='ignored'))
                self.assertFalse(receipt['delivered'])
                self.assertIn('not_configured', receipt['reason'])
            for api in (self.bridge.api_run_ask, self.bridge.api_ask_card):
                refused = api(dict(bead='sample-1', ask=7, key='secondary-create'))
                self.assertFalse(refused['delivered'])
                self.assertIn('not_configured', refused['reason'])
            self.bridge.run_asks.refresh()
            self.bridge.siding_cards.refresh()
            self.bridge.nudge_cards.refresh()
            with self.assertRaisesRegex(n.NativeError, 'native_run_asks_not_configured'):
                self.bridge.run_asks.record_depot_cutover()
            self.assertEqual(self.native.calls, [])
        finally:
            self.bridge = owner

    def test_decline_and_take_back_use_canonical_words_without_execution(self):
        for factory in (proposal,):
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

    def test_weight_requires_bound_confirm_cancel_expiry_nonce_and_one_use(self):
        for factory in (proposal, group_add_proposal, group_set_proposal, source_proposal, pack_proposal, repo_edit_proposal):
            with self.subTest(payload=factory.__name__):
                self.reset_native(factory())
                if factory is proposal:
                    self.native.ask['weight'] = [{'class': 'code', 'line': 'brings a program the yard will run: web'}]
                clock = [1000]
                self.bridge.run_asks.now = lambda: clock[0]
                first = self.create()
                original = self.row()['html']
                tap = self.press()
                pending = self.row()['pending']
                self.assertEqual({k: v for k, v in pending.items() if k != 'nonce'},
                                 dict(armed_at=1000, expires=1060, update=40))
                self.assertRegex(pending['nonce'], r'^[0-9a-f]{16}$')
                confirm = 'confirm:' + pending['nonce']
                edit = self.edits()[-1]
                self.assertEqual(edit['message_id'], self.row()['message_id'])
                self.assertTrue(edit['text'].startswith(original + '\n'))
                self.assertIn('Confirm ask 7 on sample-1: runs ' + self.native.ask['run']['command'],
                              b.html_visible_text(edit['text'])[0])
                self.assertIn('Tap Confirm within 60 s, or Cancel.', edit['text'])
                buttons = edit['reply_markup']['inline_keyboard']
                self.assertEqual([[b['text'] for b in row] for row in buttons],
                                 [['Cancel', '✕ Decline'], ['✓ Confirm ask 7'], ['📄 Report']])
                self.assertEqual(buttons[1][0]['callback_data'], f"runask:{self.row()['id']}:{confirm}")
                self.assertEqual(len(buttons[1][0]['callback_data'].encode()), 56)
                self.assertEqual(self.writes(), [])
                with patch.object(self.bridge, '_answer_callback', wraps=self.bridge._answer_callback) as answer:
                    self.bridge.inbound(dict(update_id=40, callback_query=tap))
                    self.assertEqual(answer.call_args.args[2], 'Already handled')
                clock[0] += 10
                self.press(uid=41)
                self.assertEqual(self.row()['pending'], pending)
                self.assertEqual(self.create(), first)
                for changes in (dict(message_id=True), dict(message_id=9999), dict(chat=dict(id=124, type='private')),
                                dict(chat=dict(id=123, type='group')), dict(forward_date=123),
                                dict(is_topic_message=True, message_thread_id=12)):
                    self.press(confirm, uid=100 + len(self.calls), **changes)
                    self.assertEqual(self.row()['pending'], pending)
                if self.row().get('bundle'):
                    bundle = self.row()['bundle']
                    targets = [p['message_id'] for p in bundle['parts']]
                    if bundle.get('document'):
                        targets.append(bundle['document']['message_id'])
                    for target in targets:
                        self.press(confirm, uid=100 + len(self.calls), message_id=target)
                        self.assertEqual(self.row()['pending'], pending)
                        with patch.object(self.bridge, '_relay', return_value=self.bridge.conversation):
                            self.reply(uid=1000 + target, reply_to_message=dict(message_id=target))
                        self.assertEqual(self.writes(), [])
                        self.assertEqual(self.row()['pending'], pending)
                for uid, payload in enumerate((dict(text=None, photo=[dict(file_id='photo', width=1, height=1)], caption='7'),
                                               dict(text='7', document=dict(file_id='document')),
                                               dict(text=None, sticker=dict(file_id='sticker'))), 2000):
                    with self.subTest(nontext_confirmation=payload), \
                         patch.object(self.bridge, '_relay', return_value=self.bridge.conversation), \
                         patch.object(self.bridge, '_fetch_inbound_attachment',
                                      return_value=(Path(self.tmp.name) / 'reply', 1, None)):
                        self.reply(uid=uid, **payload)
                    self.assertEqual(self.writes(), [])
                    self.assertEqual(self.row()['pending'], pending)
                with patch.object(self.bridge, '_relay') as relay:
                    self.reply(uid=50)
                    for uid, value in enumerate(('ordinary words', ' 7', '7 '), 80):
                        self.reply(value, uid=uid)
                    self.assertEqual(relay.call_count, 4)
                self.assertEqual(self.row()['pending'], pending)
                self.assertEqual(self.writes(), [])
                self.press('cancel', uid=42)
                self.assertIsNone(self.row()['pending'])
                self.assertIn('Cancelled; nothing ran', self.edits()[-1]['text'])
                self.press(confirm, uid=43)
                self.assertIn('Nothing to confirm; tap Approve first. Nothing ran.', self.edits()[-1]['text'])
                self.press(uid=44)
                self.press(confirm, uid=45)
                self.assertIsNone(self.row()['pending'])
                self.assertEqual(self.writes(), [])
                self.press(uid=46)
                expired = 'confirm:' + self.row()['pending']['nonce']
                clock[0] += 61
                self.press(expired, uid=47)
                self.assertIsNone(self.row()['pending'])
                self.assertIn(n.CONFIRM_EXPIRED, self.edits()[-1]['text'])
                self.press(uid=57)
                expired_arm = self.row()['pending']
                clock[0] = expired_arm['expires'] + 1
                self.press(uid=58)
                self.assertNotEqual(self.row()['pending']['nonce'], expired_arm['nonce'])
                self.assertEqual(self.row()['pending']['expires'], clock[0] + 60)
                self.assertEqual(self.writes(), [])
                self.press(uid=48)
                backwards = 'confirm:' + self.row()['pending']['nonce']
                clock[0] -= 1
                self.press(backwards, uid=49)
                self.assertIsNone(self.row()['pending'])
                self.assertIn(n.CONFIRM_EXPIRED, self.edits()[-1]['text'])
                self.press(uid=52)
                self.bridge.run_asks = n.RunAsks(self.bridge, now=lambda: clock[0])
                self.native.mode = 'lost-open'
                query = self.press('confirm', uid=53)
                self.assertEqual(len(self.writes()), 1)
                self.assertTrue(self.writes()[-1][1]['weighed'])
                self.assertIsNone(self.row()['pending'])
                self.assertIn(n.UNCERTAIN, self.edits()[-1]['text'])
                self.bridge.inbound(dict(update_id=53, callback_query=query))
                self.assertEqual(len(self.writes()), 1)
                self.native.mode = None
                self.press(uid=54)
                clock[0] = self.row()['pending']['expires']
                confirmed = self.press('confirm', uid=55)
                self.bridge.inbound(dict(update_id=55, callback_query=confirmed))
                self.bridge.inbound(dict(update_id=56, callback_query=confirmed))
                self.assertEqual(self.row()['state'], 'carrying' if factory is repo_edit_proposal else 'approved')
                self.assertEqual(len(self.writes()), 2)
                self.assertTrue(self.writes()[-1][1]['weighed'])
                for url, body in self.calls:
                    if not url.endswith('/editMessageText'):
                        self.assertNotIn(':confirm:', json.dumps(body.get('reply_markup', {})))
                self.reset_native(factory())
                if factory is proposal:
                    self.native.ask['weight'] = [{'class': 'code', 'line': 'brings a program the yard will run: web'}]
                self.create()
                self.press()
                self.press('decline', uid=41)
                self.assertEqual([method for method, _ in self.writes()], ['DeclineAsk'])
                self.assertIsNone(self.row()['pending'])

        # A saved arm survives a crash before its first edit; a fresh Approve repairs that edit.
        self.reset_native(proposal())
        self.native.ask['weight'] = [{'class': 'code', 'line': 'brings a program the yard will run: web'}]
        self.create()
        with patch.object(self.bridge.run_asks, 'display', side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):
                self.press()
        pending = self.row()['pending']
        self.bridge.run_asks = n.RunAsks(self.bridge)
        self.press(uid=41)
        self.assertEqual(self.row()['pending'], pending)
        self.assertIn(':confirm:' + pending['nonce'], json.dumps(self.edits()[-1]['reply_markup']))
        # Consumption survives a crash before the native write; the old nonce cannot be reused.
        with patch.object(self.bridge.run_asks, 'decide', side_effect=RuntimeError('synthetic crash')):
            with self.assertRaises(RuntimeError):
                self.press('confirm', uid=42)
        self.assertIsNone(self.row()['pending'])
        self.press('confirm:' + pending['nonce'], uid=43)
        self.assertEqual(self.writes(), [])

        # The confirmation line keeps the whole card and only shortens a command at Telegram's bound.
        for command_units in (400, 1200):
            self.reset_native(proposal())
            self.native.ask['weight'] = [{'class': 'code', 'line': 'brings a program the yard will run'}]
            self.native.ask['run']['argv'] += ['🙂' * (command_units // 2)]
            self.native.ask['run']['command'] = 'yardr ' + ' '.join(self.native.ask['run']['argv'])
            base = n.render(self.config, self.native.bead, self.native.ask)[0]
            self.native.ask['question'] += 'x' * (n.CAP - n.units(base))
            self.assertTrue(self.create()['delivered'])
            original = self.row()['html']
            self.press()
            visible = b.html_visible_text(self.edits()[-1]['text'])[0]
            self.assertTrue(self.edits()[-1]['text'].startswith(original + '\n'))
            self.assertLessEqual(n.units(visible), 4096)
            self.assertIn('Confirm ask 7 on sample-1: runs ', visible)
            if command_units == 400:
                self.assertIn(self.native.ask['run']['command'], visible.split('Confirm ask', 1)[1])
            else:
                self.assertTrue(visible.endswith('… · Tap Confirm within 60 s, or Cancel.'))
            self.assertEqual(self.writes(), [])
        result = subprocess.run([sys.executable, '-B', 'scripts/preview_run_asks.py', '--confirm'],
                                capture_output=True, text=True, timeout=20)
        self.assertEqual(result.returncode, 0, result.stderr)
        for label in ('Unarmed weighty card', 'Confirm step', 'Expired', 'Cancelled', '✓ Confirm ask 7'):
            self.assertIn(label, result.stdout)
        self.assertNotIn("type the ask's number", result.stdout)

    def test_callback_and_proposal_binding_refusals(self):
        for kind in ('new_public_main', 'free_public_main'):
            base = source_proposal(kind)
            for key in ('concealed', 'given'):
                for value in (None, False, [], ['TOKEN']):
                    with self.subTest(source_envelope_changed=(kind, key, value)):
                        changed = dict(base, **{key: value})
                        self.assert_bound_source_change_stales(base, changed)

        for factory in (proposal, group_add_proposal, group_set_proposal, source_proposal, pack_proposal, repo_edit_proposal):
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

        # A retained card's chat-scoped message id must not intercept replies after re-pairing.
        for armed in (False, True):
            self.config.update(user_id=123, chat_id=123)
            self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=self.bridge.request)
            self.bridge.request_multipart = self.pack_transport
            self.bridge.hook_secret = None
            self.reset_native(proposal())
            self.native.ask['weight'] = [{'class': 'code', 'line': 'brings a program the yard will run: web'}]
            self.assertTrue(self.create()['delivered'])
            if armed:
                self.press()
            old = self.row()
            self.native.user['identities']['tgbridge'] = {'id': '456'}
            self.config.update(user_id=456, chat_id=456)
            self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=self.bridge.request)
            self.bridge.request_multipart = self.pack_transport
            self.bridge.hook_secret = None
            n.verify_operator(self.bridge.run_asks.client, self.config)
            for seen, value, uid in ((False, str(old['ask']), 50), (True, 'ordinary words', 51)):
                with self.subTest(previous_pairing_arm=armed, replay=seen):
                    if seen:
                        self.assertTrue(self.bridge.run_asks.claim_update(uid))
                    start = len(self.calls)
                    with patch.object(self.bridge, '_relay', return_value=self.bridge.conversation) as relay:
                        self.reply(value, uid=uid, chat=dict(id=456, type='private'),
                                   reply_to_message=dict(message_id=old['message_id'],
                                       **{'from': dict(id=999, is_bot=True)}),
                                   **{'from': dict(id=456, is_bot=False)})
                        relay.assert_called_once()
                        self.assertEqual(relay.call_args.args[2], value)
                    self.assertEqual(self.calls[start:], [])
                    self.assertEqual(self.row(), old)
                    self.assertEqual(self.writes(), [])

    def test_same_operator_two_homes_and_conversations_cannot_cross(self):
        for factory in (proposal, group_add_proposal, group_set_proposal, source_proposal, pack_proposal, repo_edit_proposal):
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
                if factory is not proposal:
                    self.assertNotEqual(other_row['bundle']['document']['key'], self.row()['bundle']['document']['key'])
                self.config.update(yardr_home=str(self.home), yard='test-city')
                self.bridge.run_asks = original
                self.bridge.topic_settings = (dict(name='ops', agent='yardmaster'),)
                self.store.record_topic('ops', 12, 'created')
                self.assertEqual(self.create(topic='ops')['reason'], 'idempotency_key_conflict')
                duplicate = dict(self.row(), id='f' * 24)
                self.bridge.run_asks.save(duplicate)
                close = self.bridge.close_ask('forged', message_id=self.row()['message_id'])
                self.assertEqual(close['metadata']['reason'], 'run_ask_message_binding_ambiguous')

    def test_unsupported_source_pack_and_malformed_payloads_never_send(self):
        envelopes = [source_proposal(kind) for kind in
                     ('new', 'replace', 'free', 'new_public_main', 'free_public_main')]
        envelopes += [pack_proposal(kind) for kind in
                      ('directory', 'directory_upstream', 'directory_public_main')]
        for base in envelopes:
            for key in ('secret', 'setup', 'identity', 'depot_push', 'concealed', 'given'):
                for value in (None, False, [], ['TOKEN']):
                    with self.subTest(source_envelope=(base['run']['argv'], key, value)):
                        ask = dict(base, **{key: value})
                        self.assert_source_contract(ask, False)
                        entry = dict(queue_entry(ask), **{key: value})
                        self.assertEqual(n.queue_kind(entry), ('terminal-only run', False))
            self.assert_source_contract(base, True)

        base = proposal()
        self.reset_native(base)
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

        mutations = [
            (('run', 'future'), True), (('run', 'carried'), []), (('run', 'carried'), [None]),
            (('run', 'carried', 0, 'future'), True), (('run', 'carried', 0, 'name'), '../escape'),
            (('run', 'carried', 0, 'text'), 'name: other\n'),
            (('run', 'carried', 0, 'text'), 'name: synthetic\nlimits: {command: [/bin/true]}\n'),
            (('run', 'carried', 0, 'text'), 'name: synthetic\nlimits: {command: ["\\u002fbin/true"]}\n'),
            (('run', 'carried', 0, 'text'), 'name: synthetic\nlimits: {command: [bin/report]}\nfuture: true\n'),
            (('run', 'carried', 0, 'text'), 'name: synthetic\nlimits: {command: [bin/report], future: true}\n'),
            (('run', 'carried', 0, 'text'), 'name: synthetic\nname: synthetic\n'),
            (('run', 'carried', 0, 'text'), 'name: synthetic\nlimits: &alias {command: [bin/report]}\n'),
            (('run', 'carried', 0, 'text'), 'name: synthetic\nlimits: {command: [bin/report]}\n---\nname: other\n'),
            (('run', 'carried', 0, 'files'), None), (('run', 'carried', 0, 'files'), []),
            (('run', 'carried', 0, 'files', 0, 'future'), True),
            (('run', 'carried', 0, 'files', 0, 'text'), True),
            (('run', 'carried', 0, 'files', 0, 'sha256'), '0'*64),
            (('run', 'carried', 0, 'files', 0, 'program'), '/bin/report'),
            (('run', 'carried', 0, 'old', 0, 'future'), True),
            (('run', 'carried', 0, 'old', 0, 'diff'), '--- was\n+++ is\n@@ -1,1 +1,1 @@\n-hidden\n+partial\n'),
            (('run', 'carried', 0, 'old', 0, 'old_sha256'), '0'*64),
            (('run', 'carried', 0, 'old', 0, 'new_sha256'), '0'*64),
            (('run', 'carried', 0, 'inventory', 0, 'future'), True),
            (('run', 'carried', 0, 'inventory', 0, 'size'), True),
            (('run', 'carried', 0, 'inventory', 0, 'size'), 1),
            (('run', 'carried', 0, 'inventory', 0, 'mode'), 'invalid'),
            (('run', 'carried', 0, 'inventory', 0, 'sha256'), '0'*64),
            (('run', 'carried', 0, 'removed', 0, 'future'), True),
            (('run', 'carried', 0, 'removed', 0, 'text'), 'wrong'),
            (('run', 'carried', 0, 'removed'), []), (('run', 'carried', 0, 'old'), []),
            (('weight',), []), (('weight', 0, 'class'), 'credential'),
            (('run', 'does'), ['unknown']*41), (('run', 'back'), 'automatic undo'),
        ]
        for path, value in mutations:
            with self.subTest(source_path=path):
                self.reset_native(source_proposal())
                target = self.native.ask
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                result = self.create()
                self.assertFalse(result['delivered'], result)
                self.assertTrue(result['reason'].endswith(n.FALLBACK), result)
                self.assertEqual(self.sent_count, 0)
                self.assertEqual(self.writes(), [])
        for section in (
                'credential: {env: TOKEN, future: true}',
                'credential: {env: TOKEN, mint: [{name: ACCOUNT, future: true}]}',
                'thread: {future: true}', 'notify: {future: true}',
                'notify: {address: {param: recipient, env: RECIPIENT, future: true}}',
                'hear: {future: true}', 'hear: {address: {env: RECIPIENT, future: true}}',
                'link: {future: true}', 'link: {pull: {issue: issue, future: true}}',
                'identity: {fold: lower, future: true}', 'events: {future: true}', 'git: {future: true}',
                'hook: {future: true}', 'hook: {verify: {scheme: secret, future: true}}',
                'hook: {verify: {signature: {header: X-Signature, future: true}}}',
                'hook: {verify: {key: {encoding: hex, future: true}}}',
                'hook: {verify: {delivery: {header: X-Delivery, future: true}}}',
                'hook: {verify: {timestamp: {header: X-Time, future: true}}}',
                'hook: {challenge: {body: type, value: challenge, answer: answer, future: true}}',
                'hook: {register: {call: register, url: url, future: true}}',
                'hook: {register: {existing: {call: list, in: results, field: url, future: true}}}',
                'calls: {health: {kind: read, request: {method: GET, path: /}, future: true}}',
                'calls: {health: {params: {filter: {type: string, future: true}}}}',
                'calls: {health: {request: {method: GET, path: /, future: true}}}',
                'calls: {health: {answer: {select: [status], future: true}}}',
                'about: {future: true}', 'thread: {states: [{future: true}]}',
        ):
            with self.subTest(unknown_source_field=section):
                self.reset_native(source_proposal('new', extra=section + '\n'))
                result = self.create()
                self.assertFalse(result['delivered'], result)
                self.assertEqual(result['reason'], 'invalid native source declaration' + n.FALLBACK)
                self.assertEqual(self.sent_count, 0)
                self.assertEqual(self.writes(), [])
        for field in ('files', 'old', 'inventory', 'removed'):
            self.reset_native(source_proposal())
            self.native.ask['run']['carried'][0][field] *= 2
            self.assertFalse(self.create()['delivered'])
            self.assertEqual(self.sent_count, 0)
        for kind in ('new', 'replace', 'free', 'new_public_main', 'free_public_main'):
            self.reset_native(source_proposal(kind))
            self.assertTrue(self.create()['delivered'])
            row = self.row()
            review = NativeTests.bundle_review(row)
            self.assert_source_review(self.native.ask, review)
        for kind in ('directory_public_main', 'embedded_public_main'):
            ask = pack_proposal(kind)
            self.reset_native(ask)
            self.assertTrue(self.create()['delivered'])
            self.assert_pack_document(ask, self.documents[-1][2])
            review = NativeTests.bundle_review(self.row())
            self.assert_source_review(ask, review)
        ask = source_proposal()
        carried = ask['run']['carried'][0]
        change = carried['old'][0]
        old = n.source_diff(change, carried['files'][0]['text'])
        carried['text'] = 'name: synthetic\n'
        carried['files'] = []
        change['new_sha256'] = ''
        lines = old.removesuffix('\n').split('\n')
        change['diff'] = '--- was\n+++ is\n' + f'@@ -1,{len(lines)} +0,0 @@\n' + ''.join('-' + line + '\n' for line in lines)
        ask['run']['does'] = [
            "it replaces one source and copies no command files, and removes the command copies that stand, and changes nothing else. It grants no call, makes no hook, and sets no credential's file: the source keeps its env_file, mint_command, region base, call limits and ignored senders, and every grant of it stands, now for this declaration.",
            'source synthetic: it has no base and calls nothing. It names no credential.',
            'it would replace the source synthetic the yard has under that name; no grant of source synthetic stands.',
            f'command file bin/report would change: sha256 {change["old_sha256"]} -> sha256 (none) (0 insertions, {len(lines)} deletions).',
            ask['run']['does'][3],
            'The source names no check call and no idempotency header.',
            'it can tell no user.',
            'it hears no deliveries.',
            'it names no limits command.',
            'it speaks in no threads.',
            "its issues are no bead's link.",
            'its identity keys keep their case.',
            'it sends no hook deliveries into the yard.',
            'it sends no pull-request or CI events.',
            'it names no command.',
        ]
        ask['weight'] = [dict(
            {'class': 'code'},
            line='brings a program the yard will run: the declaration of synthetic replaces the one that stands, and what it names of commands or updates runs from now on',
        )]
        self.reset_native(ask)
        self.assertTrue(self.create()['delivered'])
        self.assertIn('Remove sources/synthetic/bin/report; sha256 ' + change['old_sha256'], self.row()['plain'])
        self.assert_source_review(ask, self.bundle_review(self.row()))
        for ask in (source_proposal(), pack_proposal('directory_replace')):
            with self.subTest(missing_kept_grants=ask['run']['argv'][:2]):
                ask['run']['does'] = [line.split('; ', 1)[0] + '.' if line.startswith('it would replace the source ')
                                     else line for line in ask['run']['does']]
                self.reset_native(ask)
                result = self.create()
                self.assertFalse(result['delivered'])
                self.assertIn('missing_source_grants_review' if ask['run']['argv'][0] == 'source'
                              else 'invalid_source_pack', result['reason'])
                self.assertEqual(self.sent_count, 0)
                self.assertEqual(self.writes(), [])
        mint_ask = source_proposal('new', extra='# ' + '<&>🙂\t\r\n'
            'base: https://example.invalid\n'
            'credential:\n  env: TOKEN\n  mint: [TENANT, {name: ACCOUNT, secret: false}]\n'
            '  mint_command: [bin/report]\n  header: "Authorization: Bearer {credential}"\n'
            'calls: {health: {kind: read, request: {method: GET, path: /health}}}\n')
        mint_sha = mint_ask['run']['carried'][0]['files'][0]['sha256']
        mint_ask['run']['does'] = [
            "it adds one source and copies 1 command file (2 commands), and changes nothing else. It grants no call, makes no hook, and sets no credential's file.",
            (f'source synthetic: its calls go to https://example.invalid. Its credential is the variable TOKEN (the name, never the value), '
             f'minted from TENANT and ACCOUNT by the declaration\'s own mint command [bin/report] '
             f'(command file bin/report, copied to <home>/sources/synthetic/bin/report: sha256 {mint_sha}, mode 0700), '
             f'run with the mint\'s inputs and the operator\'s local logins where the source\'s mint_command is unset: one the operator sets wins over it, '
             f'sent in the header "Authorization: Bearer {{credential}}" on every call. It has no second credential for updates.'),
            'call health (read): GET /health. It takes no parameter, and sends no query.',
            'call health sends no body. Its answer is returned as it comes, at most 65536 bytes.',
            *mint_ask['run']['does'][2:],
            'still owed after it: the credential, by a secret ask for synthetic.',
        ]
        self.reset_native(mint_ask)
        self.assertTrue(self.create()['delivered'])
        self.assert_source_review(self.native.ask, self.bundle_review(self.row()))
        # Historical mint prose mismatches are now displayed whole.
        three_ask = source_proposal('new', extra='# ' + '<&>🙂\t\r\n'
            'base: https://example.invalid\n'
            'credential:\n  env: TOKEN\n  mint: [TENANT, {name: ACCOUNT, secret: false}, THIRD]\n'
            '  mint_command: [bin/report]\n  header: "Authorization: Bearer {credential}"\n'
            'calls: {health: {kind: read, request: {method: GET, path: /health}}}\n')
        three_sha = three_ask['run']['carried'][0]['files'][0]['sha256']
        three_lead = ("minted from TENANT and ACCOUNT and THIRD by the declaration's own mint command [bin/report] "
             f'(command file bin/report, copied to <home>/sources/synthetic/bin/report: sha256 {three_sha}, mode 0700), '
             'run with the mint\'s inputs and the operator\'s local logins where the source\'s mint_command is unset: one the operator sets wins over it, '
             'sent in the header "Authorization: Bearer {credential}" on every call. It has no second credential for updates.')
        three_ask['run']['does'] = [
            "it adds one source and copies 1 command file (2 commands), and changes nothing else. It grants no call, makes no hook, and sets no credential's file.",
            ('source synthetic: its calls go to https://example.invalid. Its credential is the variable TOKEN (the name, never the value), '
             + three_lead),
            'call health (read): GET /health. It takes no parameter, and sends no query.',
            'call health sends no body. Its answer is returned as it comes, at most 65536 bytes.',
            *three_ask['run']['does'][2:],
            'still owed after it: the credential, by a secret ask for synthetic.',
        ]
        self.reset_native(three_ask)
        self.assertTrue(self.create()['delivered'])
        self.assert_source_review(self.native.ask, self.bundle_review(self.row()))
        for bad_lead in (
                three_lead.replace('ACCOUNT and THIRD', 'ACCOUNT and WRONG'),
                three_lead.replace('TENANT and ACCOUNT and THIRD', 'TENANT, ACCOUNT and THIRD'),
                three_lead.replace('minted from TENANT and ACCOUNT and THIRD', 'minted from TENANT and ACCOUNT')):
            with self.subTest(mint_lead=bad_lead[-48:]):
                bad = copy.deepcopy(three_ask)
                bad['run']['does'][1] = bad['run']['does'][1].replace(three_lead, bad_lead)
                self.reset_native(bad)
                result = self.create()
                self.assertTrue(result['delivered'], result)
                self.assert_source_review(self.native.ask, self.bundle_review(self.row()))
                self.assertEqual(self.writes(), [])
        # A credential that is the service address is handed to no call
        # build/run helper, as captured from real public-main Bead responses:
        # the build row says `run with no credential ... without one` and the
        # command row says `handed no credential`. Other slots still get it.
        url_file = '#!/bin/sh\nprintf "{}\\n"\n'
        url_sha = n.file_hash(url_file)
        url_text = (
            'name: synthetic\n'
            'credential: {env: DESTINATION, url: true}\n'
            'calls:\n  send:\n    kind: update\n'
            '    request: {method: POST, path: /}\n'
            '    build: [bin/body]\n')
        url_ask = source_proposal('new')
        url_ask['run']['carried'][0]['text'] = url_text
        url_ask['run']['carried'][0]['files'] = [dict(program='bin/body', text=url_file, sha256=url_sha)]
        for key in ('old', 'inventory', 'removed'):
            url_ask['run']['carried'][0].pop(key, None)
        url_build_row = ('call send builds its body with the command [bin/body], run with no credential at each call '
             'and free to make its own requests without one while it builds. Its answer is returned as it comes, at most 65536 bytes.')
        url_cmd_row = (f'command file bin/body: [bin/body] (call.send.build, handed no credential): sha256 {url_sha}, mode 0700')
        url_ask['run']['does'] = [
            "it adds one source and copies 1 command file (1 command), and changes nothing else. It grants no call, makes no hook, and sets no credential's file.",
            ('source synthetic: its calls go to the URL in DESTINATION. Its credential is the variable DESTINATION (the name, never the value), '
             'sent as the address every call goes to (credential.url: true): whoever sets the variable chooses where the calls go, '
             'over https or plain http to this machine alone. It has no second credential for updates, no mint, no header of its own.'),
            'call send (update): POST /. It takes no parameter, and sends no query.',
            url_build_row,
            'The source names no check call and no idempotency header.',
            'it can tell no user.',
            'it hears no deliveries.',
            'it names no limits command.',
            'it speaks in no threads.',
            "its issues are no bead's link.",
            'its identity keys keep their case.',
            'it sends no hook deliveries into the yard.',
            'it sends no pull-request or CI events.',
            url_cmd_row,
            'still owed after it: the credential, by a secret ask for synthetic.',
        ]
        url_ask['weight'] = [dict(
            {'class': 'code'},
            line='brings a program the yard will run: the command file bin/body of synthetic (new)',
        )]
        self.reset_native(url_ask)
        self.assertTrue(self.create()['delivered'])
        self.assert_source_review(self.native.ask, self.bundle_review(self.row()))
        for bad_row in (
                url_build_row.replace('run with no credential at each call', 'run with the credential at each call')
                    .replace('without one while it builds', 'with it while it builds'),
                url_cmd_row.replace('handed no credential', 'handed the credential')):
            with self.subTest(url_row=bad_row[-48:]):
                bad = copy.deepcopy(url_ask)
                bad['run']['does'] = [bad_row if line in (url_build_row, url_cmd_row) else line
                                      for line in bad['run']['does']]
                self.reset_native(bad)
                result = self.create()
                self.assertTrue(result['delivered'], result)
                self.assert_source_review(self.native.ask, self.bundle_review(self.row()))
                self.assertEqual(self.writes(), [])
        # A thread slot on a url-credential source still gets the credential.
        url_thread_ask = source_proposal('new')
        url_thread_ask['run']['carried'][0]['text'] = (
            'name: synthetic\n'
            'credential: {env: DESTINATION, url: true}\n'
            'calls:\n  health:\n    kind: read\n    request: {method: GET, path: /}\n'
            'thread:\n  reply: [bin/body]\n')
        url_thread_ask['run']['carried'][0]['files'] = [dict(program='bin/body', text=url_file, sha256=url_sha)]
        for key in ('old', 'inventory', 'removed'):
            url_thread_ask['run']['carried'][0].pop(key, None)
        url_thread_cmd = (f'command file bin/body: [bin/body] (thread.reply, handed the credential): sha256 {url_sha}, mode 0700')
        url_thread_ask['run']['does'] = [
            "it adds one source and copies 1 command file (1 command), and changes nothing else. It grants no call, makes no hook, and sets no credential's file.",
            ('source synthetic: its calls go to the URL in DESTINATION. Its credential is the variable DESTINATION (the name, never the value), '
             'sent as the address every call goes to (credential.url: true): whoever sets the variable chooses where the calls go, '
             'over https or plain http to this machine alone. It has no second credential for updates, no mint, no header of its own.'),
            'call health (read): GET /. It takes no parameter, and sends no query.',
            'call health sends no body. Its answer is returned as it comes, at most 65536 bytes.',
            'The source names no check call and no idempotency header.',
            'it can tell no user.',
            'it hears no deliveries.',
            'it names no limits command.',
            'it speaks in threads: it names no place to open one; it names no state words; it takes no labels; it assigns nobody.',
            "its issues are no bead's link.",
            'its identity keys keep their case.',
            'it sends no hook deliveries into the yard.',
            'it sends no pull-request or CI events.',
            url_thread_cmd,
            'still owed after it: the credential, by a secret ask for synthetic.',
        ]
        url_thread_ask['weight'] = [dict(
            {'class': 'code'},
            line='brings a program the yard will run: the command file bin/body of synthetic (new)',
        )]
        self.reset_native(url_thread_ask)
        self.assertTrue(self.create()['delivered'])
        self.assert_source_review(self.native.ask, self.bundle_review(self.row()))
        bad = copy.deepcopy(url_thread_ask)
        bad['run']['does'] = [line.replace('handed the credential', 'handed no credential')
                              for line in bad['run']['does']]
        self.reset_native(bad)
        result = self.create()
        self.assertTrue(result['delivered'], result)
        self.assert_source_review(self.native.ask, self.bundle_review(self.row()))
        self.assertEqual(self.writes(), [])
        # YAML integer spellings bind by value, as captured from real
        # public-main Bead responses (0x10 renders as {"n":16}); a wrong
        # value, or a quoted string type for a number, still refuses.
        for decl_body, row_body in (('{n: 0x10}', '{"n":16}'), ('{n: 0o17}', '{"n":15}'),
                                    ('{n: 1_000}', '{"n":1000}'), ('{n: 1e3}', '{"n":1000}')):
            with self.subTest(yaml_int=decl_body):
                hex_ask = source_proposal('new')
                hex_ask['run']['carried'][0]['text'] = (
                    'name: synthetic\nbase: https://example.invalid\n'
                    'calls:\n  health:\n    kind: read\n'
                    f'    request: {{method: POST, path: /health, body: {decl_body}}}\n')
                hex_ask['run']['carried'][0]['files'] = []
                for key in ('old', 'inventory', 'removed'):
                    hex_ask['run']['carried'][0].pop(key, None)
                hex_ask['run']['does'] = [
                    "it adds one source and copies no command files, and changes nothing else. It grants no call, makes no hook, and sets no credential's file.",
                    'source synthetic: its calls go to https://example.invalid. It names no credential.',
                    'call health (read): POST /health. It takes no parameter, and sends no query.',
                    f'call health sends the body {row_body}. Its answer is returned as it comes, at most 65536 bytes.',
                    'The source names no check call and no idempotency header.',
                    'it can tell no user.',
                    'it hears no deliveries.',
                    'it names no limits command.',
                    'it speaks in no threads.',
                    "its issues are no bead's link.",
                    'its identity keys keep their case.',
                    'it sends no hook deliveries into the yard.',
                    'it sends no pull-request or CI events.',
                    'it names no command.',
                ]
                hex_ask['weight'] = []
                self.reset_native(hex_ask)
                self.assertTrue(self.create()['delivered'], decl_body)
                self.assert_source_review(self.native.ask, self.bundle_review(self.row()))
        hex_ask = source_proposal('new')
        hex_ask['run']['carried'][0]['text'] = (
            'name: synthetic\nbase: https://example.invalid\n'
            'calls:\n  health:\n    kind: read\n'
            '    request: {method: POST, path: /health, body: {n: 0x10}}\n')
        hex_ask['run']['carried'][0]['files'] = []
        for key in ('old', 'inventory', 'removed'):
            hex_ask['run']['carried'][0].pop(key, None)
        hex_row = 'call health sends the body {"n":16}. Its answer is returned as it comes, at most 65536 bytes.'
        hex_ask['run']['does'] = [
            "it adds one source and copies no command files, and changes nothing else. It grants no call, makes no hook, and sets no credential's file.",
            'source synthetic: its calls go to https://example.invalid. It names no credential.',
            'call health (read): POST /health. It takes no parameter, and sends no query.',
            hex_row,
            'The source names no check call and no idempotency header.',
            'it can tell no user.',
            'it hears no deliveries.',
            'it names no limits command.',
            'it speaks in no threads.',
            "its issues are no bead's link.",
            'its identity keys keep their case.',
            'it sends no hook deliveries into the yard.',
            'it sends no pull-request or CI events.',
            'it names no command.',
        ]
        hex_ask['weight'] = []
        for bad_body in ('{"n":17}', '{"n":"16"}'):
            with self.subTest(yaml_int_bad=bad_body):
                bad = copy.deepcopy(hex_ask)
                bad['run']['does'] = [hex_row.replace('{"n":16}', bad_body) if line == hex_row else line
                                      for line in bad['run']['does']]
                self.reset_native(bad)
                result = self.create()
                self.assertTrue(result['delivered'], result)
                self.assert_source_review(self.native.ask, self.bundle_review(self.row()))
                self.assertEqual(self.writes(), [])
        # Data names and nested request bodies remain unrestricted by the declaration's field names.
        self.reset_native(source_proposal('free'))
        carried = self.native.ask['run']['carried'][0]
        carried['text'] = carried['text'].replace('    request: {method: GET, path: /health}\n',
            '    params: {future: {type: enum, values: [later, now], default: later}}\n'
            '    request: {method: POST, path: /health, query: {future: "{future}"}, '
            'body: {future: {nested: [now, {other: later}]}}}\n'
            '    answer: {select: [future], max: 65536}\n')
        # A defaulted param is optional on the native card: captured from real
        # public-main Bead responses, where {type: string, default: hello} with
        # no optional flag renders `value (string, optional)`.
        self.native.ask['run']['does'][2] = 'call health (read): POST /health. It takes future (enum, optional, one of later, now), and sends the query future={future}.'
        self.native.ask['run']['does'][3] = 'call health sends the body {"future":{"nested":["now",{"other":"later"}]}}. Its answer keeps future, at most 65536 bytes.'
        self.assertTrue(self.create()['delivered'])
        self.assert_source_review(self.native.ask, self.bundle_review(self.row()))
        self.native.ask['run']['does'][2] = self.native.ask['run']['does'][2].replace(
            'future (enum, optional,', 'future (enum, required,', 1)
        self.reset_native(self.native.ask)
        result = self.create()
        self.assertTrue(result['delivered'], result)
        self.assert_source_review(self.native.ask, self.bundle_review(self.row()))
        self.assertEqual(self.writes(), [])
        # Pack call prose stays native-owned, including defaulted parameter wording.
        pack_ask = pack_proposal('embedded_public_main')
        pack_carried = pack_ask['run']['carried'][0]
        pack_carried['text'] = pack_carried['text'].replace(
            'title: { type: string, max: 250 }',
            'title: { type: string, max: 250, default: Announcement }', 1)
        pack_ask['run']['does'] = [
            line.replace('title (string, required, at most 250)', 'title (string, optional, at most 250)')
            if 'title (string, required, at most 250)' in line else line
            for line in pack_ask['run']['does']]
        self.reset_native(pack_ask)
        self.assertTrue(self.create()['delivered'], pack_ask['run']['does'])
        pack_ask['run']['does'] = [
            line.replace('title (string, optional, at most 250)', 'title (string, required, at most 250)')
            if 'title (string, optional, at most 250)' in line else line
            for line in pack_ask['run']['does']]
        self.reset_native(pack_ask)
        result = self.create()
        self.assertTrue(result['delivered'], result)
        self.assert_source_review(self.native.ask, self.bundle_review(self.row()))
        self.assertEqual(self.writes(), [])
        for field in ('declaration', 'file', 'total'):
            for overflow in (False, True):
                with self.subTest(source_cap=field, overflow=overflow):
                    self.reset_native(source_proposal('free' if field == 'declaration' else 'new'))
                    carried = self.native.ask['run']['carried'][0]
                    if field == 'declaration':
                        carried['text'] += '#' + 'x'*(8192-len(carried['text'].encode())-1 + overflow)
                    elif field == 'file':
                        file = carried['files'][0]
                        file['text'] = '🙂'*2048 + ('x' if overflow else '')
                        file['sha256'] = n.file_hash(file['text'])
                        self.native.ask['run']['does'][-1] = (
                            f"command file bin/report: [bin/report] (limits.command, handed no credential): sha256 {file['sha256']}, mode 0700"
                        )
                    else:
                        carried['text'] = 'name: synthetic\nlimits: {command: [bin/report]}\nthread:\n  reply: [two]\n  read: [three]\n  file: [four]\n'
                        carried['files'] = [dict(program=p, text='x'*8192, sha256=n.file_hash('x'*8192)) for p in ('bin/report', 'two', 'three', 'four')]
                        fsha = n.file_hash('x'*8192)
                        # Three max files plus the declaration fit; four exceed the combined text cap.
                        if not overflow:
                            carried['text'] = carried['text'].replace('  file: [four]\n', '')
                            carried['files'].pop()
                        nfiles = len(carried['files'])
                        self.native.ask['run']['does'] = [
                            f"it adds one source and copies {nfiles} command files ({nfiles} commands), and changes nothing else. It grants no call, makes no hook, and sets no credential's file.",
                            'source synthetic: it has no base and calls nothing. It names no credential.',
                            'its limits command [bin/report] says what the source itself carries, asked by the source\'s name.',
                            'it speaks in threads: it names no place to open one; it names no state words; it takes no labels; it assigns nobody.',
                            'it has no check call, no idempotency header, no way to tell a user, no way to hear deliveries, no bead link, no identity fold (its identity keys keep their case), no hook deliveries into the yard and no pull-request or CI events.',
                            f'command file bin/report: [bin/report] (limits.command, handed no credential): sha256 {fsha}, mode 0700',
                            f'command file two: [two] (thread.reply, handed no credential): sha256 {fsha}, mode 0700',
                            f'command file three: [three] (thread.read, handed no credential): sha256 {fsha}, mode 0700',
                            *([f'command file four: [four] (thread.file, handed no credential): sha256 {fsha}, mode 0700'] if overflow else []),
                        ]
                        self.native.ask['weight'] = [dict(
                            {'class': 'code'},
                            line='brings a program the yard will run: the command file ' + and_list(
                                [f'{f["program"]} of synthetic (new)' for f in carried['files']]
                            ),
                        )]
                    self.assertEqual(self.create()['delivered'], not overflow)
                    if overflow:
                        self.assertEqual(self.sent_count, 0)
        self.pack_refusals()

    def pack_refusals(self):
        mutations = [
            (('run', 'pack'), None), (('run', 'pack', 'kind'), 'git'),
            (('run', 'pack', 'future'), True), (('run', 'pack', 'plan'), {}),
            (('run', 'pack', 'digest'), '0' * 64), (('run', 'pack', 'directory'), {}),
            (('run', 'pack', 'yes'), False), (('run', 'pack', 'yes'), True),
            (('run', 'pack', 'label'), 'directory /another/pack in yardr version'),
            (('run', 'carried', 0, 'future'), True), (('run', 'carried', 0, 'files'), []),
            (('run', 'carried', 0, 'files', 0, 'sha256'), '0' * 64),
            (('run', 'carried', 0, 'text'), 'name: other\n'),
            (('run', 'ask'), ['region']), (('run', 'value'), 'input'), (('secret',), {}),
            (('setup',), {}), (('identity',), {}), (('given',), 'input'),
            (('weight',), []), (('weight', 0, 'class'), 'trust'),
            (('run', 'argv'), ['pack', 'apply', '/another/pack']),
        ]
        for path, value in mutations:
            with self.subTest(pack_path=path):
                ask = pack_proposal()
                target = ask
                for key in path[:-1]:
                    target = target[key]
                target[path[-1]] = value
                self.reset_native(ask)
                result = self.create()
                self.assertFalse(result['delivered'], result)
                self.assertTrue(result['reason'].endswith(n.FALLBACK), result)
                self.assertEqual((self.sent_count, self.writes()), (0, []))
        reworded = pack_proposal()
        reworded['weight'][0]['line'] = 'invented warning'
        self.assert_source_contract(reworded, True)
        for factory in (source_proposal, pack_proposal):
            for count in (40, 41):
                with self.subTest(card_lines=count, factory=factory.__name__):
                    ask = factory()
                    carried = ask['run']['carried'][0]
                    decl = n.source_yaml(carried['text'])
                    does = list(ask['run']['does'])
                    if 'base' not in decl:
                        carried['text'] += 'base: https://example.invalid\n'
                        does[1] = f'source {carried["name"]}: its calls go to https://example.invalid. It names no credential.'
                        if count == 41:
                            does.insert(3, 'source synthetic base: none -> "https://example.invalid".')
                    elif count == 41:
                        does = does[:4] + [does[11]] + [
                            'it has no check call, no idempotency header, no way to tell a user, no way to hear deliveries, no limits command, no way to speak in threads, no bead link, no identity fold (its identity keys keep their case) and no pull-request or CI events.',
                            does[-1],
                        ]
                    if 'calls' not in decl:
                        carried['text'] += 'calls:\n'
                    insert_at = next(i for i, l in enumerate(does) if l.startswith(('The source names ', 'Its check call is ', 'what it sends in: ')))
                    extra_calls = (count - len(does)) // 2
                    pad_yaml = ''.join(
                        f'  pad{i}:\n    kind: read\n    request: {{method: GET, path: /pad/{i}}}\n'
                        for i in range(extra_calls)
                    )
                    if 'calls:\n' in carried['text']:
                        carried['text'] = carried['text'].replace('calls:\n', 'calls:\n' + pad_yaml, 1)
                    else:
                        carried['text'] += pad_yaml
                    # Preserve decl call order so call rows match carried decl order.
                    call_pos = insert_at
                    if 'calls' in decl and decl['calls']:
                        call_pos = next(i for i, l in enumerate(does) if l.startswith('call '))
                    for i in range(extra_calls):
                        does.insert(call_pos + 2 * i, f'call pad{i} (read): GET /pad/{i}. It takes no parameter, and sends no query.')
                        does.insert(call_pos + 2 * i + 1, f'call pad{i} sends no body. Its answer is returned as it comes, at most 65536 bytes.')
                    ask['run']['does'] = does
                    self.assertEqual(len(ask['run']['does']), count)
                    self.reset_native(ask)
                    self.assertEqual(self.create()['delivered'], count == 40)
                    if count == 41:
                        self.assertEqual(self.sent_count, 0)
        for ask in (source_proposal(), pack_proposal('directory_replace')):
            ask['run']['does'] = [line for line in ask['run']['does']
                                 if not line.startswith('it would replace the source ')]
            self.reset_native(ask)
            self.assertFalse(self.create()['delivered'])
            self.assertEqual((self.sent_count, self.writes()), (0, []))
        for pin in (None, 'sha256:', 'SHA256:'):
            with self.subTest(optional_pin=pin):
                ask = pack_proposal()
                ask['run']['argv'] = ask['run']['argv'][:3] if pin is None else ask['run']['argv'][:4] + [pin + ask['run']['pack']['digest']]
                ask['run']['command'] = 'yardr ' + ' '.join(ask['run']['argv'])
                self.reset_native(ask)
                self.assertTrue(self.create()['delivered'])
                self.assert_pack_document(ask, self.documents[-1][2])
        # A replacement that keeps its programs weighs nothing; changed programs still require code weight.
        ask = pack_proposal('directory_replace')
        carried = ask['run']['carried'][0]
        for file in carried['files']:
            file['text'] = n.source_diff(next(c for c in carried['old'] if c['program'] == file['program']), file['text'])
            file['sha256'] = n.file_hash(file['text'])
        carried['old'] = []
        ask['run']['does'][-1] = (
            f"command file bin/report: [bin/report] (hook.normalize, handed no credential): "
            f"sha256 {carried['files'][0]['sha256']}, mode 0700"
        )
        ask.pop('weight')
        self.reset_native(ask)
        self.assertTrue(self.create()['delivered'])
        self.press()
        self.assertEqual(self.writes(), [('ApproveAsk', dict(bead='sample-1', ask=7, actor='pat', weighed=False, values={}))])
        for legacy in ('source_replace_prealignment', 'source_replace_upstream', 'source_replace_public_main',
                       'pack_directory_prealignment', 'pack_directory_replace_prealignment',
                       'pack_directory_replace_upstream', 'pack_directory_replace_public_main'):
            with self.subTest(unsupported_shape=legacy):
                ask = json.loads(Path(__file__).with_name('fixtures').joinpath(legacy + '.json').read_text())
                self.reset_native(ask)
                self.assertFalse(self.create()['delivered'])
                self.assertEqual((self.sent_count, self.writes()), (0, []))
                self.reset_native(source_proposal() if legacy.startswith('source') else pack_proposal())
                self.assertTrue(self.create()['delivered'])
                row = self.row()
                # The old generation's ledger fields and proposal are retained across an upgrade.
                row['proposal'] = self.bridge.run_asks.proposal(self.native.bead, ask, row['conversation'])
                self.bridge.run_asks.save(row)
                self.native.ask = ask
                self.bridge.run_asks = n.RunAsks(self.bridge)
                self.bridge.run_asks.refresh()
                self.assertEqual(self.row()['state'], 'stale')
                self.assertEqual(self.edits()[-1]['reply_markup'], dict(inline_keyboard=[]))
                self.assertIn('Proposal changed; use Footplate or the yard cards', b.html_visible_text(self.edits()[-1]['text'])[0])
                self.press()
                self.bridge.run_asks.refresh()
                self.assertEqual(self.writes(), [])
        for base_kind, base_ask in (('source', source_proposal('new_public_main')),
                                    ('pack', pack_proposal('directory_public_main')),
                                    ('source_legacy', source_proposal('new')),
                                    ('pack_legacy', pack_proposal('directory'))):
            does = list(base_ask['run']['does'])
            cmd_idx = next(i for i, line in enumerate(does) if line.startswith('command file '))
            unknown = 'it also deletes every existing source grant.'
            mutations = [
                ('unknown_before_caps', does[:2] + [unknown] + does[2:]),
                ('unknown_before_command', does[:cmd_idx] + [unknown] + does[cmd_idx:]),
                ('unknown_after_command', does + [unknown]),
                ('unknown_still_owed', does + ['still owed after it: an invented extra grant.']),
                ('half_call', does[:2] + ['call orphan (read): GET /orphan. It takes no parameter, and sends no query.'] + does[2:]),
                ('call_after_command', does + [
                    'call orphan (read): GET /orphan. It takes no parameter, and sends no query.',
                    'call orphan sends no body. Its answer is returned as it comes, at most 65536 bytes.',
                ]),
                ('drop_command', does[:cmd_idx] + does[cmd_idx + 1:]),
                ('short_does', does[:3]),
            ]
            cap_idx = next((i for i, line in enumerate(does) if line.startswith('it has ')), None)
            if cap_idx is not None:
                mutations.extend([
                    ('drop_condensed', does[:cap_idx] + does[cap_idx + 1:]),
                    ('truncated_lacks', does[:cap_idx] + [does[cap_idx].replace(' and no pull-request or CI events', '')] + does[cap_idx + 1:]),
                    ('oxford_comma', does[:cap_idx] + [does[cap_idx].replace(' and no pull-request or CI events.', ', and no pull-request or CI events.')] + does[cap_idx + 1:]),
                    ('wrong_lack', does[:cap_idx] + [does[cap_idx].replace('no bead link', 'no bead')] + does[cap_idx + 1:]),
                    ('mix_absent_removed', does[:cap_idx] + ['it names no limits command.', does[cap_idx].replace(', no limits command', '')] + does[cap_idx + 1:]),
                    ('mix_absent_kept', does[:cap_idx] + ['it names no limits command.', does[cap_idx]] + does[cap_idx + 1:]),
                    ('cap_after_condensed', does[:cap_idx - 1] + [does[cap_idx], does[cap_idx - 1]] + does[cap_idx + 1:]),
                    ('duplicate_condensed', does[:cap_idx + 1] + [does[cap_idx]] + does[cap_idx + 1:]),
                ])
            if base_kind.startswith('source'):
                lim_idx = next(i for i, line in enumerate(does) if line.startswith('its limits command '))
                mutations.extend([
                    ('truncated_limits_prefix', does[:lim_idx] + ['its limits command '] + does[lim_idx + 1:]),
                    ('truncated_limits_partial', does[:lim_idx] + ['its limits command [bin/report] says what '] + does[lim_idx + 1:]),
                ])
            if base_kind.startswith('pack'):
                hook_idx = next(i for i, line in enumerate(does) if line.startswith('what it sends in: '))
                mutations.extend([
                    ('spoofed_source_header', does[:cmd_idx] + ['source spoofed: its calls go to https://evil.example.'] + does[cmd_idx:]),
                    ('truncated_hook_prefix', does[:hook_idx] + ['what it sends in: '] + does[hook_idx + 1:]),
                    ('truncated_hook_partial', does[:hook_idx] + [
                        "what it sends in: deliveries are verified by the header X-Secret holding the hook's secret itself (scheme secret)."
                    ] + does[hook_idx + 1:]),
                ])
            for label, bad_does in mutations:
                with self.subTest(disclosure_case=base_kind + ':' + label):
                    ask = copy.deepcopy(base_ask)
                    ask['run']['does'] = bad_does
                    self.assert_source_contract(ask, label != 'spoofed_source_header' and not (
                        base_kind.startswith('source') and label == 'short_does'))
        ask = pack_proposal('embedded_public_main')
        does = ask['run']['does']
        check_idx = next(i for i, line in enumerate(does) if line.startswith('Its check call is '))
        does[check_idx], does[check_idx + 1] = does[check_idx + 1], does[check_idx]
        self.reset_native(ask)
        result = self.create()
        self.assertTrue(result['delivered'], result)
        self.assert_source_review(self.native.ask, self.bundle_review(self.row()))
        self.assertEqual(self.writes(), [])
        embedded = pack_proposal('embedded_public_main')
        for prefix in ('Its check call is ', 'it can tell a user: ', 'it hears deliveries: ',
                       'it speaks in threads: ', 'what it sends in: '):
            idx = next(i for i, line in enumerate(embedded['run']['does']) if line.startswith(prefix))
            for variant, bad_line in (('prefix', prefix), ('no_period', embedded['run']['does'][idx][:-1])):
                with self.subTest(embedded_truncated=prefix + variant):
                    ask = copy.deepcopy(embedded)
                    ask['run']['does'][idx] = bad_line
                    self.reset_native(ask)
                    result = self.create()
                    self.assertTrue(result['delivered'], result)
                    self.assert_source_review(self.native.ask, self.bundle_review(self.row()))
                    self.assertEqual(self.writes(), [])
        full_caps = [
            'Its check call is ping, and updates carry their key in the header Idempotency-Key.',
            "it can tell a user: the call tell, whose chat takes the person's address, read as CHAT from the person's own env file; the title fills title and it takes no text and the link fills link.",
            "it hears deliveries: only one whose chat is the address of a user told through it, read as CHAT, and every told user's: another's, and a stranger's, is dropped after verification.",
            'its limits command [aiquokka, {provider}, --json] says what the subscription of claude as claude, codex as codex still carries.',
            "it speaks in threads: a place to open one is called repo; the words state takes are open, closed; it takes labels (label:<name> adds one, never removes); an assignee is a person's login in the source.",
            "its issues are a bead's link: host example.invalid, the expression ^#([0-9]+)$ reads an issue, the one fixed form #{1} and the thread id {1}; the groups keep their case. It names no pull-request shape. It keeps no repositories.",
            'its identity keys fold to lowercase (identity fold: lower), so one spelling is stored however it was typed.',
            'what it sends in: deliveries are verified by HMAC-SHA256 over the body (scheme hmac-sha256, signed: body), the signature in the header X-Hub-Signature-256 as hex past the prefix "sha256="; a delivery is named by the header X-Delivery; its time arrives at the header X-Time, in seconds within 5m0s of the yard\'s clock; bodies are unique; the command [bin/report] turns a verified body into the envelope, handed no credential; its events carry no field of their own.',
            'what it sends of pull requests and CI: the envelope types pr, ci are runs; pr tell a head; the command [bin/pr] reads the pull request, handed the credential; it names no CI read-back command.',
        ]
        full_base = source_proposal('new_public_main')
        full_base['run']['carried'][0]['text'] = (
            'name: synthetic\n'
            'base: https://example.invalid\n'
            'credential:\n  env: TOKEN\n'
            'calls:\n'
            '  ping:\n    kind: read\n    request: {method: GET, path: /ping}\n'
            '  tell:\n    kind: update\n    params:\n      chat: {type: string}\n      title: {type: string}\n      link: {type: string, optional: true}\n'
            '    request: {method: POST, path: /tell}\n'
            'check: ping\n'
            'idempotency: Idempotency-Key\n'
            'notify:\n  call: tell\n  address: {param: chat, env: CHAT}\n  title: title\n  link: link\n'
            'hear:\n  field: chat\n  by: address\n  address: {env: CHAT}\n'
            'limits:\n  command: [aiquokka, "{provider}", --json]\n  kinds: {claude: claude, codex: codex}\n'
            'thread:\n  place: repo\n  states: [open, closed]\n  labels: true\n  assignee: login\n'
            'link:\n  host: example.invalid\n  issue: "^#([0-9]+)$"\n  fixed: "#{1}"\n  thread: "{1}"\n  fold: keep\n'
            'identity:\n  fold: lower\n'
            'hook:\n  verify:\n    scheme: hmac-sha256\n    signed: body\n    signature: {header: X-Hub-Signature-256, encoding: hex, prefix: "sha256="}\n'
            '    delivery: {header: X-Delivery}\n    timestamp: {header: X-Time, unit: seconds, window: 5m0s}\n'
            '  bodies: unique\n  normalize: [bin/report]\n'
            'events:\n  runs: [pr, ci]\n  head_from: [pr]\n  pr: [bin/pr]\n'
        )
        rep_file = full_base['run']['carried'][0]['files'][0]
        full_base['run']['carried'][0]['files'] = [
            dict(program='aiquokka', text='#!/bin/sh\necho ok\n', sha256=n.file_hash('#!/bin/sh\necho ok\n')),
            rep_file,
            dict(program='bin/pr', text='#!/bin/sh\necho pr\n', sha256=n.file_hash('#!/bin/sh\necho pr\n')),
        ]
        ai_sha = full_base['run']['carried'][0]['files'][0]['sha256']
        pr_sha = full_base['run']['carried'][0]['files'][2]['sha256']
        full_base['run']['does'] = [
            "it adds one source and copies 3 command files (3 commands), and changes nothing else. It grants no call, makes no hook, and sets no credential's file.",
            'source synthetic: its calls go to https://example.invalid. Its credential is the variable TOKEN (the name, never the value), and no call\'s header, path or body holds it. It has no second credential for updates, no mint, no header of its own.',
            'call ping (read): GET /ping. It takes no parameter, and sends no query.',
            'call ping sends no body. Its answer is returned as it comes, at most 65536 bytes.',
            'call tell (update): POST /tell. It takes chat (string, required), title (string, required) and link (string, optional), and sends no query.',
            'call tell sends no body. Its answer is returned as it comes, at most 65536 bytes.',
            *full_caps,
            f'command file aiquokka: [aiquokka, {{provider}}, --json] (limits.command, handed no credential): sha256 {ai_sha}, mode 0700',
            f"command file bin/report: [bin/report] (hook.normalize, handed no credential): sha256 {rep_file['sha256']}, mode 0700",
            f'command file bin/pr: [bin/pr] (events.pr, handed the credential): sha256 {pr_sha}, mode 0700',
            'still owed after it: the credential, by a secret ask for synthetic; and which user it tells, by yardr user set <user> notify=synthetic.',
        ]
        full_base['weight'] = [dict(
            {'class': 'code'},
            line='brings a program the yard will run: the command file aiquokka of synthetic (new), bin/report of synthetic (new) and bin/pr of synthetic (new)',
        )]
        self.reset_native(full_base)
        self.assertTrue(self.create()['delivered'])
        hear_identity = copy.deepcopy(full_base)
        hear_identity['run']['carried'][0]['text'] = hear_identity['run']['carried'][0]['text'].replace(
            'hear:\n  field: chat\n  by: address\n  address: {env: CHAT}\n',
            'hear:\n  field: sender\n  by: identity\n',
        )
        hear_identity['run']['does'][8] = (
            "it hears deliveries: only one whose sender is the address of a user told through it, "
            "read as , and every told user's: another's, and a stranger's, is dropped after verification."
        )
        self.reset_native(hear_identity)
        self.assertTrue(self.create()['delivered'])
        for slot_idx, line in enumerate(full_caps):
            prefix = line.split(' ', 3)[:3]
            short = ' '.join(prefix) + ' '
            with self.subTest(full_cap_truncated=slot_idx):
                ask = copy.deepcopy(full_base)
                ask['run']['does'][6 + slot_idx] = short
                self.reset_native(ask)
                result = self.create()
                self.assertTrue(result['delivered'], result)
                self.assert_source_review(self.native.ask, self.bundle_review(self.row()))
                self.assertEqual(self.writes(), [])
        ask = pack_proposal()
        big_val = 'x' * 18000
        big_yaml = ''.join(
            f'  big{i}:\n    kind: read\n    request: {{method: POST, path: /big/{i}, body: {{k: "{big_val}"}}}}\n'
            for i in range(13)
        )
        ask['run']['carried'][0]['text'] = ask['run']['carried'][0]['text'].replace('calls:\n', 'calls:\n' + big_yaml, 1)
        big_lines = [
            line
            for i in range(13)
            for line in (
                f'call big{i} (read): POST /big/{i}. It takes no parameter, and sends no query.',
                f'call big{i} sends the body {{"k":"{big_val}"}}. Its answer is returned as it comes, at most 65536 bytes.',
            )
        ]
        ask['run']['does'] = ask['run']['does'][:2] + big_lines + ask['run']['does'][2:]
        self.reset_native(ask)
        self.assertTrue(self.create()['delivered'])
        self.assertGreater(self.row()['bundle']['text_parts'], n.PACK_PARTS)
        self.assertEqual(len(self.documents), 1)
        self.assertEqual(len([url for url, _ in self.calls if url.endswith('/sendMessage')]), 1)
        size = len(self.documents[-1][2])
        for ceiling in (size, size - 1):
            self.reset_native(ask)
            with patch.object(n, 'PACK_DOCUMENT_BYTES', ceiling):
                result = self.create()
            self.assertEqual(result['delivered'], ceiling == size)
            if ceiling < size:
                self.assertIn('document_too_large', result['reason'])
                self.assertEqual((self.sent_count, self.documents), (0, []))

    def test_source_and_pack_every_does_row_preserves_prose_and_refuses_framing(self):
        # Historical row/token/domain mutations now preserve native prose; framing still refuses.
        supported = (
            ('source_new', lambda: source_proposal('new')),
            ('source_new_public_main', lambda: source_proposal('new_public_main')),
            ('source_free', lambda: source_proposal('free')),
            ('source_free_public_main', lambda: source_proposal('free_public_main')),
            ('source_replace', lambda: source_proposal('replace')),
            ('pack_directory', lambda: pack_proposal('directory')),
            ('pack_directory_upstream', lambda: pack_proposal('directory_upstream')),
            ('pack_directory_public_main', lambda: pack_proposal('directory_public_main')),
            ('pack_directory_replace', lambda: pack_proposal('directory_replace')),
            ('pack_embedded', lambda: pack_proposal('embedded')),
            ('pack_embedded_upstream', lambda: pack_proposal('embedded_upstream')),
            ('pack_embedded_public_main', lambda: pack_proposal('embedded_public_main')),
        )
        row_specs = (
            ('source_add_lead',
             re.compile(r"^it adds one source and copies (?:(no) command files|(\d+) command files? \((\d+) commands?\)), and changes nothing else\. It grants no call, makes no hook, and sets no credential's file\.$"),
             lambda m: 'it adds one source and '),
            ('source_replace_lead',
             re.compile(r"^it replaces one source and copies (\d+) command files? \((\d+) commands?\), and changes nothing else\. It grants no call, makes no hook, and sets no credential's file: the source keeps its env_file, mint_command, (region base|base or host), call limits and ignored senders, and every grant of it stands, now for this declaration\.$"),
             lambda m: 'it replaces one source and '),
            ('pack_lead_dir',
             re.compile(r"^pack ([a-z0-9_-]+) \(directory (/\S+) in yardr (\S+)\), digest sha256:([0-9a-f]{64}), of kind source, into the yard: it (adds|replaces) (\d+) sources? and copies (\d+) command files? \((\d+) commands?\), and changes nothing else\. It grants no call, makes no hook, and sets no credential's file\.$"),
             lambda m: f"pack {m.group(1)} (directory {m.group(2)} in yardr {m.group(3)}), digest sha256:{m.group(4)}, of kind source, into the yard: "),
            ('pack_lead_embedded',
             re.compile(r"^pack ([a-z0-9_-]+) \(embedded in yardr (\S+)\), of kind source, into the yard: it (adds|replaces) (\d+) sources? and copies (\d+) command files? \((\d+) commands?\), and changes nothing else\. It grants no call, makes no hook, and sets no credential's file\.$"),
             lambda m: f"pack {m.group(1)} (embedded in yardr {m.group(2)}), of kind source, into the yard: "),
            ('source_summary_no_cred',
             re.compile(r"^source ([a-z0-9_-]+): (?:it has no base and calls nothing|its calls go to (\S+))\. It names no credential\.$"),
             lambda m: f"source {m.group(1)}: "),
            ('source_summary_cred',
             re.compile(r"^source ([a-z0-9_-]+): its calls go to (\S+)\. Its credential is the variable (\S+) \(the name, never the value\), sent in the request path wherever the declaration puts \{credential\} \(credential\.in_path: true\): it stands in the logs of every server on the way\. It has (no second credential for updates), (no mint), (no header of its own)\.$"),
             lambda m: f"source {m.group(1)}: "),
            ('replace_grants_none',
             re.compile(r"^it would replace the source ([a-z0-9_-]+) the yard has under that name; no grant of source ([a-z0-9_-]+) stands\.$"),
             lambda m: f"it would replace the source {m.group(1)} the yard has under that name; "),
            ('replace_grants_kept',
             re.compile(r"^it would replace the source ([a-z0-9_-]+) the yard has under that name; grants kept for source ([a-z0-9_-]+): (crew member) (\S+): ([a-z0-9_-]+) \((kept)\)\.$"),
             lambda m: f"it would replace the source {m.group(1)} the yard has under that name; grants kept for source {m.group(2)}: "),
            ('copy_remove',
             re.compile(r"^the copy would remove ([^\s,]+), which neither declaration names: sha256 ([0-9a-f]{64}) \(mode (0[0-7]{3}), (\d+) bytes\)\.$"),
             lambda m: 'the copy would remove '),
            ('call_first_no_params',
             re.compile(r"^call ([a-z0-9_-]+) \((read|update)\): (GET|POST) (\S+)\. It takes no parameter, and sends no query\.$"),
             lambda m: f"call {m.group(1)} ({m.group(2)}): "),
            ('call_first_2_params',
             re.compile(r"^call ([a-z0-9_-]+) \((read|update)\): (POST) (\S+)\. It takes (\S+) \((\S+), (required|optional), at most (\d+)\) and (\S+) \((\S+), (required|optional), at most (\d+)\), and sends no query\.$"),
             lambda m: f"call {m.group(1)} ({m.group(2)}): "),
            ('call_first_3_params',
             re.compile(r"^call ([a-z0-9_-]+) \((read|update)\): (POST) (\S+)\. It takes (\S+) \((\S+), (required|optional), pattern (\S+)\), (\S+) \((\S+), (required|optional), at most (\d+)\) and (\S+) \((\S+), (required|optional), at most (\d+)\), and sends no query\.$"),
             lambda m: f"call {m.group(1)} ({m.group(2)}): "),
            ('call_second',
             re.compile(r"^call ([a-z0-9_-]+) (?:sends no body|sends the body (\{.+?\}))\. Its answer is returned as it comes, at most (\d+) bytes\.$"),
             lambda m: f"call {m.group(1)} "),
            ('cap_check_present',
             re.compile(r"^Its check call is ([a-z0-9_-]+); it names no idempotency header\.$"),
             lambda m: 'Its check call is '),
            ('cap_notify_present',
             re.compile(r"^it can tell a user: the call ([a-z0-9_-]+), whose (\S+) takes the person's address, read as (\S+) from the person's own env file; the title fills (\S+) and the text fills (\S+) and it takes no link\.$"),
             lambda m: 'it can tell a user: '),
            ('cap_hear_present',
             re.compile(r"^it hears deliveries: only one whose (\S+) is the address of a user told through it, read as (\S+), and (only an operator's): another's, and a stranger's, is dropped after verification\.$"),
             lambda m: 'it hears deliveries: '),
            ('cap_limits_present',
             re.compile(r"^its limits command \[([^\]]+)\] says what the source itself carries, asked by the source's name\.$"),
             lambda m: 'its limits command '),
            ('cap_thread_present',
             re.compile(r"^it speaks in threads: it names no place to open one; it names no state words; it takes no labels; it assigns nobody\.$"),
             lambda m: 'it speaks in threads: '),
            ('cap_hook_present',
             re.compile(r"^what it sends in: deliveries are verified by the header (\S+) holding the hook's secret itself \(scheme secret\); (?:it sends no delivery id|a delivery is named by (\S+) in its body); it sends no timestamp; bodies are (\S+); the command \[([^\]]+)\] turns a verified body into the envelope, handed no credential; (?:its events carry no field of their own|its events may carry the fields ([a-z0-9_]+))(?:; the yard registers its webhook with ([a-z0-9_-]+) \((\S+) (\S+)\), filling (\S+) with the public URL and (\S+) with the hook's secret)?\.$"),
             lambda m: 'what it sends in: '),
            ('cap_absent',
             re.compile(r"^(The source names no check call and no idempotency header\.|it can tell no user\.|it hears no deliveries\.|it names no limits command\.|it speaks in no threads\.|its issues are no bead's link\.|its identity keys keep their case\.|it sends no hook deliveries into the yard\.|it sends no pull-request or CI events\.)$"),
             lambda m: m.group(1).rsplit(' ', 1)[0] + ' '),
            ('cap_condensed',
             re.compile(r"^it has (.+)\.$"),
             lambda m: 'it has '),
            ('cmd_none',
             re.compile(r"^it names no command\.$"),
             lambda m: 'it names '),
            ('cmd_file',
             re.compile(r"^command file (\S+): (.+?): sha256 ([0-9a-f]{64})(?: -> sha256 ([0-9a-f]{64}))?, mode (0[0-7]{3})(?: \((\d+) insertions, (\d+) deletions\))?\.?$"),
             lambda m: 'command file '),
            ('still_owed',
             re.compile(r"^still owed after it: the credential, by a secret ask for ([a-z0-9_-]+); and which user it tells, by yardr user set <user> notify=([a-z0-9_-]+)\.$"),
             lambda m: 'still owed after it: '),
        )
        unknown_row = 'it also grants every call to everyone.'

        def refuse(ask, tag):
            with self.subTest(mutation=tag):
                self.assert_fixture_prose_mutation(base, ask)

        for fixture_name, factory in supported:
            base = factory()
            with self.subTest(intact=fixture_name):
                self.reset_native(base)
                self.assertTrue(self.create()['delivered'], fixture_name)
            does = base['run']['does']
            for idx, row in enumerate(does):
                matched = next(((kind, m, prefix_fn) for kind, pat, prefix_fn in row_specs
                                if (m := pat.fullmatch(row))), None)
                self.assertIsNotNone(matched, f'{fixture_name} row {idx} unmatched by mutation table: {row!r}')
                kind, m, prefix_fn = matched
                tag_base = f'{fixture_name}[{idx}:{kind}]'
                drift = copy.deepcopy(base)
                drift['run']['does'][idx] += ' changed after display'
                self.assert_bound_source_change_stales(base, drift)

                # 1. Cut back to fixed prefix.
                bad = copy.deepcopy(base)
                bad['run']['does'][idx] = prefix_fn(m)
                refuse(bad, tag_base + ':prefix')

                # 2. Empty each variable part.
                if kind == 'cap_absent':
                    pass
                elif kind == 'cap_condensed':
                    for item_idx, item in enumerate(re.finditer(r'[^,]+?(?=(?:, | and |$))', m.group(1))):
                        start, end = m.start(1) + item.start(), m.start(1) + item.end()
                        bad = copy.deepcopy(base)
                        bad['run']['does'][idx] = row[:start] + row[end:]
                        refuse(bad, f'{tag_base}:empty_lack_{item_idx}')
                else:
                    spans = [m.span(g) for g in range(1, m.re.groups + 1) if m.span(g) != (-1, -1)]
                    if kind == 'cmd_file':
                        for tok in re.finditer(r'\[([^\]]+)\] \(([a-z0-9._]+), handed (?:no|the) credential\)', m.group(2)):
                            spans.append((m.start(2) + tok.start(1), m.start(2) + tok.end(1)))
                            spans.append((m.start(2) + tok.start(2), m.start(2) + tok.end(2)))
                    for v_idx, (start, end) in enumerate(spans):
                        bad = copy.deepcopy(base)
                        bad['run']['does'][idx] = row[:start] + row[end:]
                        refuse(bad, f'{tag_base}:empty_var_{v_idx}')

                # 3. Tail truncated (no period, half-length, half-length + '.', and clause-truncated).
                tails = [
                    ('drop_end', row[:-1]),
                    ('half_raw', row[:max(1, len(row) // 2)]),
                    ('half_dot', row[:max(1, len(row) // 2)] + '.'),
                ]
                if '; ' in row:
                    tails.append(('drop_last_semicolon', row.rsplit('; ', 1)[0] + '.'))
                for t_name, bad_line in tails:
                    bad = copy.deepcopy(base)
                    bad['run']['does'][idx] = bad_line
                    refuse(bad, f'{tag_base}:tail_{t_name}')

                # 4. Unknown row inserted before and after this row.
                for pos_name, pos in (('before', idx), ('after', idx + 1)):
                    bad = copy.deepcopy(base)
                    bad['run']['does'] = does[:pos] + [unknown_row] + does[pos:]
                    refuse(bad, f'{tag_base}:unknown_{pos_name}')

                # 5. Every command argv token and Go-quoted token rejects empty, bad escape/Unicode, NUL, and blank program.
                if 'sends the body ' not in row:
                    for cmd_idx, argv_match in enumerate(re.finditer(rf'(?:^|command |, )({COMMAND_ARGV})', row)):
                        inner_start = argv_match.start(1) + 1
                        inner_end = argv_match.end(1) - 1
                        for tok_idx, tok_match in enumerate(re.finditer(COMMAND_TOKEN, row[inner_start:inner_end])):
                            t_start, t_end = inner_start + tok_match.start(), inner_start + tok_match.end()
                            bad_tokens = [('empty_q', '""'), ('bad_esc', r'"\q"'), ('bad_uni', r'"\uD800"'), ('nul', r'"\x00"')]
                            if tok_idx == 0:
                                bad_tokens.append(('blank_prog', '"   "'))
                            for t_label, bad_tok in bad_tokens:
                                bad = copy.deepcopy(base)
                                bad['run']['does'][idx] = row[:t_start] + bad_tok + row[t_end:]
                                refuse(bad, f'{tag_base}:cmd_{cmd_idx}_{tok_idx}_{t_label}')
                    for q_idx, q_match in enumerate(GO_QUOTED_TOKEN_RE.finditer(row)):
                        q_start, q_end = q_match.span()
                        bad_quotes = [('bad_esc', r'"\q"'), ('bad_uni', r'"\uD800"'), ('nul', r'"\x00"')]
                        if q_start == 0 or row[q_start - 1] != '=':
                            bad_quotes.append(('empty_q', '""'))
                        for q_label, bad_q in bad_quotes:
                            bad = copy.deepcopy(base)
                            bad['run']['does'][idx] = row[:q_start] + bad_q + row[q_end:]
                            refuse(bad, f'{tag_base}:quoted_{q_idx}_{q_label}')

                # 6. Malformed non-empty variable-domain values (URLs, paths, strict JSON bodies, hook/holder fields).
                domain_mutations = []
                if kind in ('source_summary_no_cred', 'source_summary_cred') and m.group(2):
                    u_start, u_end = m.span(2)
                    for u_label, bad_url in (
                        ('bad_bracket_url', 'https://['),
                        ('plain_http', 'http://example.invalid'),
                        ('ftp_scheme', 'ftp://example.invalid'),
                        ('userinfo', 'https://user@example.invalid'),
                        ('query_url', 'https://example.invalid?x=1'),
                        ('dotdot_url', 'https://example.invalid/../x'),
                    ):
                        domain_mutations.append((u_label, row[:u_start] + bad_url + row[u_end:]))
                elif kind == 'copy_remove':
                    p_start, p_end = m.span(1)
                    for p_label, bad_path in (('escape', '../../escape'), ('abs', '/etc/passwd'), ('dot', './obsolete.txt')):
                        domain_mutations.append((p_label, row[:p_start] + bad_path + row[p_end:]))
                elif kind.startswith('call_first_') and m.group(4):
                    r_start, r_end = m.span(4)
                    for r_label, bad_req in (
                        ('rel_path', 'health'),
                        ('dotdot_path', '/../escape'),
                        ('double_slash', '/a//b'),
                        ('query_path', '/health?x=1'),
                        ('frag_path', '/health#frag'),
                    ):
                        domain_mutations.append((r_label, row[:r_start] + bad_req + row[r_end:]))
                elif kind == 'call_second' and m.group(2):
                    b_start, b_end = m.span(2)
                    for b_label, bad_body in (
                        ('nan', 'NaN'),
                        ('inf', 'Infinity'),
                        ('nested_nan', '{"x":NaN}'),
                        ('lone_surrogate', r'"\uD800"'),
                        ('not_json', 'NOT_JSON'),
                        ('truncated', '{"x":1'),
                    ):
                        domain_mutations.append((b_label, row[:b_start] + bad_body + row[b_end:]))
                elif kind == 'cap_hook_present':
                    b_start, b_end = m.span(3)
                    domain_mutations.append(('bad_bodies', row[:b_start] + 'bogus' + row[b_end:]))
                    if m.group(7):
                        rp_start, rp_end = m.span(7)
                        domain_mutations.append(('bad_reg_path', row[:rp_start] + '/../hook' + row[rp_end:]))
                    if m.group(2):
                        db_start, db_end = m.span(2)
                        domain_mutations.append(('bad_deliv_dot', row[:db_start] + 'a..b' + row[db_end:]))
                elif kind == 'cmd_file':
                    cp_start, cp_end = m.span(1)
                    for cp_label, bad_prog in (('escape', '../../escape'), ('abs', '/bin/report'), ('mismatch', 'other/report')):
                        domain_mutations.append((cp_label, row[:cp_start] + bad_prog + row[cp_end:]))
                elif kind == 'replace_grants_kept':
                    h_start, h_end = m.span(4)
                    domain_mutations.append(('bad_holder', row[:h_start] + '..' + row[h_end:]))
                for d_label, bad_line in domain_mutations:
                    bad = copy.deepcopy(base)
                    bad['run']['does'][idx] = bad_line
                    refuse(bad, f'{tag_base}:domain_{d_label}')

        base = source_proposal('replace')
        # Historical preamble mutations are opaque except the retained grants framing.
        for label, bad_line in (
            ('widens_empty_calls', 'a standing grant of read of source synthetic widens to  for crew member yardmaster.'),
            ('widens_prefix_dot', 'a standing grant of read of source synthetic widens to .'),
            ('widens_empty_holder', 'a standing grant of read of source synthetic widens to list for .'),
            ('widens_bad_holder', 'a standing grant of read of source synthetic widens to list for group a//b.'),
            ('still_granted_empty_holders', 'the changed call health (read) is still granted by its name to  (yardr source revoke synthetic health takes it back).'),
            ('still_granted_mismatch_call', 'the changed call health (read) is still granted by its name to crew member reviewer (yardr source revoke synthetic other takes it back).'),
            ('decl_diff_empty_old', 'source synthetic base:  -> "https://example.invalid".'),
            ('decl_diff_empty_new', 'source synthetic base: "https://example.invalid" -> .'),
            ('decl_diff_unparsed', 'source synthetic base: NOT_JSON -> ALSO_NOT_JSON.'),
            ('decl_diff_truncated_json', 'source synthetic calls: [{"Name":"old" -> [{"Name":"new".'),
            ('decl_diff_bad_quote', r'source synthetic base: "\q" -> "\uD800".'),
            ('decl_diff_nan', 'source synthetic base: {"x": NaN} -> none.'),
            ('decl_diff_same_none', 'source synthetic base: none -> none.'),
            ('decl_diff_triple_arrow', 'source synthetic base: "https://a" -> "https://b" -> "https://c".'),
            ('changed_copy_escape', 'command file ../../escape would change: sha256 9d9adc8ea0ca1a4bd6955d136517cd0dd7e5f59a69f843bdbc9cc56818224ba4 -> sha256 7a86d9a4f1870b6e5b8f14ef6a5ac27d8a9b51580d2390a9dad425ee8875af68 (1 insertions, 1 deletions).'),
            ('changed_copy_same_sha', 'command file bin/report would change: sha256 (none) -> sha256 (none) (0 insertions, 0 deletions).'),
            ('removed_copy_escape', 'the copy would remove ../../escape, which neither declaration names: sha256 670ac0987d6acba567e81e9faa48aca43d2c7d8d26241fc7b3fa6604453b3a51 (mode 0644, 24 bytes).'),
            ('grants_kept_prefix_dot', 'it would replace the source synthetic the yard has under that name; grants kept for source synthetic: .'),
            ('grants_kept_empty_holder', 'it would replace the source synthetic the yard has under that name; grants kept for source synthetic: crew member : health (kept).'),
        ):
            bad = source_proposal('replace')
            bad['run']['does'].insert(3 if not label.startswith('grants_kept') else 2, bad_line)
            if label.startswith('grants_kept'):
                bad['run']['does'].pop(3)
            refuse(bad, 'replace_preamble:' + label)

        for label, valid_line, extra_yaml in (
            ('decl_diff_json_pair', 'source synthetic base: "https://old.example" -> none.', ''),
            ('decl_diff_arrow_in_string', 'source synthetic base: "a -> b" -> "https://example.invalid".', 'base: https://example.invalid\n'),
            ('decl_diff_none_to_array', 'source synthetic calls: none -> [{"name":"health","kind":"read","request":{"method":"GET","path":"/h"},"answer":{"max":65536}}].',
             'base: https://example.invalid\ncalls:\n  health:\n    kind: read\n    request: {method: GET, path: /h}\n'),
        ):
            good = source_proposal('replace')
            good['run']['does'].insert(3, valid_line)
            if extra_yaml:
                good['run']['carried'][0]['text'] += extra_yaml
                good['run']['does'][1] = 'source synthetic: its calls go to https://example.invalid. It names no credential.'
            if 'calls' in extra_yaml:
                good['run']['does'].insert(5, 'call health (read): GET /h. It takes no parameter, and sends no query.')
                good['run']['does'].insert(6, 'call health sends no body. Its answer is returned as it comes, at most 65536 bytes.')
            with self.subTest(valid_preamble=label):
                self.reset_native(good)
                self.assertTrue(self.create()['delivered'], label)

    def test_source_pack_quoted_tokens_and_plural_counts(self):
        def refuse(ask, tag):
            with self.subTest(native_text=tag):
                framed = tag.startswith(('bad_cmd_count:', 'bad_replace_count:', 'bad_pack_src_count:', 'pack_label_paren'))
                self.assert_source_contract(ask, not framed)

        def accept(ask, tag):
            with self.subTest(accept=tag):
                self.reset_native(ask)
                result = self.create()
                self.assertTrue(result['delivered'], (tag, result))

        # 1. Exact Finding 1 quoted-token repros + shared token sites.
        for label, bad_argv in (
            ('empty_prog', '[""]'),
            ('bad_esc', r'["\q"]'),
            ('bad_uni', r'["\uD800"]'),
            ('nul', r'["\x00"]'),
            ('blank_prog', '["   "]'),
            ('empty_arg', '[bin/report, ""]'),
            ('bad_esc_arg', r'[bin/report, "\q"]'),
        ):
            bad_src = source_proposal('new_public_main')
            bad_src['run']['does'][2] = bad_src['run']['does'][2].replace('[bin/report]', bad_argv)
            refuse(bad_src, 'source_limits:' + label)

            bad_pack = pack_proposal('directory_public_main')
            bad_pack['run']['does'][6] = bad_pack['run']['does'][6].replace('[bin/report]', bad_argv)
            refuse(bad_pack, 'pack_cmd_file:' + label)

        good_argv = r'[bin/report, "a b", "x\"y", "\t", "   "]'
        good_yaml_cmd = '[bin/report, "a b", "x\\"y", "\\t", "   "]'
        good_src = source_proposal('new_public_main')
        good_src['run']['carried'][0]['text'] = good_src['run']['carried'][0]['text'].replace('[bin/report]', good_yaml_cmd)
        good_src['run']['does'][2] = good_src['run']['does'][2].replace('[bin/report]', good_argv)
        good_src['run']['does'][4] = good_src['run']['does'][4].replace('[bin/report]', good_argv)
        accept(good_src, 'source_valid_quoted_args')

        good_pack = pack_proposal('directory_public_main')
        good_pack['run']['carried'][0]['text'] = good_pack['run']['carried'][0]['text'].replace('[bin/report]', good_yaml_cmd)
        good_pack['run']['does'][4] = good_pack['run']['does'][4].replace('[bin/report]', good_argv)
        good_pack['run']['does'][6] = good_pack['run']['does'][6].replace('[bin/report]', good_argv)
        accept(good_pack, 'pack_valid_quoted_args')

        # runs (ARGV_LINE_TOKEN) via validate_source_disclosures
        run_lines = [
            'call c0 (read): runs bin/report. It takes no parameter.',
            'call c0 is answered by that command, run with no credential at each call and free to make requests of its own without one, since the yard sends none. Its answer is returned as it comes, at most 65536 bytes.',
            'it has no check call, no idempotency header, no way to tell a user, no way to hear deliveries, no limits command, no way to speak in threads, no bead link, no identity fold (its identity keys keep their case), no hook deliveries into the yard and no pull-request or CI events.',
            'command file bin/report: [bin/report] (call.c0.run, handed no credential): sha256 9d9adc8ea0ca1a4bd6955d136517cd0dd7e5f59a69f843bdbc9cc56818224ba4, mode 0700',
        ]
        for run_text in ('bin/report "a b" "\\t"', '""', r'"\q"', r'"\uD800"', '"   "', 'bin/report ""', r'bin/report "\q"'):
            ask = source_proposal('new_public_main')
            ask['run']['does'][2:2] = [f'call c0 (read): runs {run_text}. It takes no parameter.'] + run_lines[1:2]
            self.assert_source_contract(ask, True)

        # query items allow RHS "" (e.g. k="") while rejecting empty key or bad escape
        query_base = source_proposal('free_public_main')
        query_base['run']['carried'][0]['text'] = query_base['run']['carried'][0]['text'].replace(
            'request: {method: GET, path: /health}',
            'request: {method: GET, path: /health, query: {k: ""}}',
        )
        query_base['run']['does'][2] = 'call health (read): GET /health. It takes no parameter, and sends the query k="".'
        accept(query_base, 'query_empty_rhs')
        for label, bad_q in (('empty_key', '""=v'), ('bad_esc_rhs', r'k="\q"'), ('bad_uni_rhs', r'k="\uD800"')):
            bad = copy.deepcopy(query_base)
            bad['run']['does'][2] = f'call health (read): GET /health. It takes no parameter, and sends the query {bad_q}.'
            refuse(bad, 'query:' + label)

        # PLACEMENT_HEADER, mint_command, and hook quoted tokens
        hdr_base = source_proposal('new_public_main')
        hdr_base['run']['carried'][0]['text'] += 'base: https://example.invalid\ncredential:\n  env: TOKEN\n  header: X-Api-Key\n'
        hdr_base['run']['does'][1] = (
            'source synthetic: its calls go to https://example.invalid. '
            'Its credential is the variable TOKEN (the name, never the value), sent in the header "X-Api-Key" on every call. '
            'It has no second credential for updates, no mint.'
        )
        hdr_base['run']['does'].append('still owed after it: the credential, by a secret ask for synthetic.')
        accept(hdr_base, 'placement_header_valid')
        for label, bad_hdr in (('empty', '""'), ('bad_esc', r'"\q"'), ('blank', '"   "')):
            bad = copy.deepcopy(hdr_base)
            bad['run']['does'][1] = bad['run']['does'][1].replace('"X-Api-Key"', bad_hdr)
            refuse(bad, 'placement_header:' + label)

        hook_base = pack_proposal('directory_public_main')
        hook_base['run']['carried'][0]['text'] = (
            'name: synthetic\nbase: https://example.invalid\n'
            'calls:\n  health:\n    kind: read\n    request: {method: GET, path: /health}\n'
            'hook:\n  verify:\n    scheme: hmac-sha256\n    signed: body\n'
            '    signature: {header: X-Sig, encoding: hex, list: ",", prefix: "sha256="}\n'
            '  bodies: unique\n  normalize: [bin/report]\n'
        )
        hook_base['run']['does'][4] = (
            'what it sends in: deliveries are verified by HMAC-SHA256 over the body (scheme hmac-sha256, signed: body), '
            'the signature in the header X-Sig as hex, one or more separated by ",", each past the prefix "sha256=", any one of them passing; '
            'it sends no delivery id; it sends no timestamp; bodies are unique; '
            'the command [bin/report] turns a verified body into the envelope, handed no credential; its events carry no field of their own.'
        )
        accept(hook_base, 'hook_quoted_valid')
        for label, old, new in (
            ('sep_empty', '","', '""'),
            ('sep_bad_esc', '","', r'"\q"'),
            ('prefix_empty', '"sha256="', '""'),
            ('prefix_bad_esc', '"sha256="', r'"\q"'),
            ('hook_cmd_empty', '[bin/report]', '[""]'),
            ('bad_hmac', 'HMAC-SHA256', 'HMAC-MD5'),
            ('bad_algo', 'scheme hmac-sha256,', 'scheme hmac-sha256, algorithm: md5,'),
            ('bad_enc', 'as hex,', 'as rot13,'),
            ('bad_bodies', 'bodies are unique;', 'bodies are invalid;'),
            ('bad_window', 'it sends no timestamp;', 'its time arrives at a..b, in s within 0s of the yard\'s clock;'),
            ('bad_window_unit', 'it sends no timestamp;', 'its time arrives at ts, in s within 5garbage of the yard\'s clock;'),
        ):
            bad = copy.deepcopy(hook_base)
            bad['run']['does'][4] = bad['run']['does'][4].replace(old, new)
            refuse(bad, 'hook_quoted:' + label)

        # Valid loopback summary base + named base, valid link fold, valid hook variants
        loopback_ok = source_proposal('free_public_main')
        loopback_ok['run']['carried'][0]['text'] = loopback_ok['run']['carried'][0]['text'].replace(
            'base: https://example.invalid\n',
            'base: https://example.invalid\nbases:\n  local: http://127.0.0.1:8080\n',
        )
        loopback_ok['run']['does'][1] = 'source no-commands: its calls go to http://127.0.0.1:8080 (base local). It names no credential.'
        accept(loopback_ok, 'summary_loopback_base')
        for label, bad_summary in (
            ('bad_base_name', 'source no-commands: its calls go to https://example.invalid (base ../bad). It names no credential.'),
            ('bad_port', 'source no-commands: its calls go to https://example.invalid:bad. It names no credential.'),
        ):
            bad = source_proposal('free_public_main')
            bad['run']['does'][1] = bad_summary
            refuse(bad, 'summary_domain:' + label)

        link_base = source_proposal('new_public_main')
        link_base['run']['carried'][0]['text'] += (
            'link:\n  host: linear.app\n  issue: "^/([A-Z]+-[0-9]+)$"\n'
            '  fixed: "https://linear.app/issue/{1}"\n  thread: "{1}"\n  fold: lower\n'
        )
        link_base['run']['does'] = link_base['run']['does'][:3] + [
            "its issues are a bead's link: host linear.app, the expression ^/([A-Z]+-[0-9]+)$ reads an issue, the one fixed form https://linear.app/issue/{1} and the thread id {1}; the groups fold lower. It names no pull-request shape. It keeps no repositories.",
            'it has no check call, no idempotency header, no way to tell a user, no way to hear deliveries, no way to speak in threads, no identity fold (its identity keys keep their case), no hook deliveries into the yard and no pull-request or CI events.',
        ] + link_base['run']['does'][4:]
        accept(link_base, 'link_fold_lower')
        bad_fold = copy.deepcopy(link_base)
        bad_fold['run']['does'][3] = bad_fold['run']['does'][3].replace('the groups fold lower', 'the groups fold upper')
        refuse(bad_fold, 'link_fold_invalid')

        for bad_run_prog in ('/bin/report', '../../escape'):
            ask = source_proposal('new_public_main')
            ask['run']['does'][2:2] = [f'call c0 (read): runs {bad_run_prog}. It takes no parameter.'] + run_lines[1:2]
            self.assert_source_contract(ask, True)
        for label, bad_op in (('rel', 'bin/tool'), ('dotdot', '/a/../b')):
            bad = source_proposal('new_public_main')
            bad['run']['does'][-1] = f"command [{bad_op}] (call.extra): the program is the operator's own, handed no credential."
            refuse(bad, 'op_cmd:' + label)
        bad_label = pack_proposal('embedded_public_main')
        bad_label['run']['pack']['label'] = 'embedded in yardr foo)'
        bad_label['run']['does'][0] = bad_label['run']['does'][0].replace('in yardr unknown', 'in yardr foo)')
        refuse(bad_label, 'pack_label_paren')
        bad_weight = pack_proposal('directory_public_main')
        bad_weight['weight'][0]['line'] = 'brings a program the yard will run:    '
        refuse(bad_weight, 'pack_weight_blank')

        # 2. Exact Finding 2 10- and 11-command declarations (aligned and public-main) + plural count boundaries.
        for total_cmds in (10, 11):
            call_count = total_cmds - 1
            calls_yaml = ''.join(
                f'  c{i}:\n    kind: read\n    request: {{method: POST, path: /c{i}}}\n    build: [bin/report, "--n={i}"]\n'
                for i in range(call_count)
            )
            decl = f'name: synthetic\nbase: https://example.invalid\nlimits:\n  command: [bin/report]\ncalls:\n{calls_yaml}'
            call_lines = [
                line
                for i in range(call_count)
                for line in (
                    f'call c{i} (read): POST /c{i}. It takes no parameter, and sends no query.',
                    f'call c{i} builds its body with the command [bin/report, --n={i}], run with no credential at each call and free to make its own requests without one while it builds. Its answer is returned as it comes, at most 65536 bytes.',
                )
            ]
            entries = ['[bin/report] (limits.command, handed no credential)'] + [
                f'[bin/report, --n={i}] (call.c{i}.build, handed no credential)' for i in range(call_count)
            ]
            cmd_line = (
                f'command file bin/report: {and_list(entries)}: '
                'sha256 9d9adc8ea0ca1a4bd6955d136517cd0dd7e5f59a69f843bdbc9cc56818224ba4, mode 0700'
            )
            for variant in ('aligned', 'public_main'):
                ask = source_proposal('new' if variant == 'aligned' else 'new_public_main')
                ask['run']['carried'][0]['text'] = decl
                ask['run']['does'] = [
                    f"it adds one source and copies 1 command file ({total_cmds} commands), and changes nothing else. It grants no call, makes no hook, and sets no credential's file.",
                    'source synthetic: its calls go to https://example.invalid. It names no credential.',
                    *call_lines,
                    *ask['run']['does'][2:-1],
                    cmd_line,
                ]
                accept(ask, f'{variant}_{total_cmds}_commands')

        for files_n, cmds_n in ((2, 2), (9, 9), (10, 10), (1, 10), (10, 11), (19, 19), (20, 20)):
            phrase = f'{files_n} command files' if files_n != 1 else '1 command file'
            slots = [
                ('limits', 'command'), ('thread', 'reply'), ('thread', 'posted'), ('thread', 'read'),
                ('thread', 'file'), ('thread', 'open'), ('thread', 'opened'), ('thread', 'state'),
                ('notify', 'image'), ('events', 'pr'), ('events', 'ci'), ('git', 'credential'),
            ]
            # Build files_n distinct relative programs across section commands and call.<name>.build commands.
            progs = ['bin/report' if i == 0 else f'bin/tool-{i}' for i in range(files_n)]
            file_txt = '#!/bin/sh\necho ok\n'
            fsha = n.file_hash(file_txt)
            extra_cmds = cmds_n - files_n
            # Assign prog[0] to limits.command; remaining files to call.<c>.build; extra_cmds to call.<ec>.build reusing bin/report.
            calls_decl_str = ''
            call_lines = []
            extra_entries_for_0 = ['[bin/report] (limits.command, handed no credential)']
            cmd_rows = []
            call_idx = 0
            for i in range(1, files_n):
                cname = f'c{call_idx}'
                call_idx += 1
                calls_decl_str += f'  {cname}:\n    kind: read\n    request: {{method: POST, path: /{cname}}}\n    build: [{progs[i]}]\n'
                call_lines.extend([
                    f'call {cname} (read): POST /{cname}. It takes no parameter, and sends no query.',
                    f'call {cname} builds its body with the command [{progs[i]}], run with no credential at each call and free to make its own requests without one while it builds. Its answer is returned as it comes, at most 65536 bytes.',
                ])
            for e in range(extra_cmds):
                cname = f'c{call_idx}'
                call_idx += 1
                calls_decl_str += f'  {cname}:\n    kind: read\n    request: {{method: POST, path: /{cname}}}\n    build: [bin/report, --e={e}]\n'
                call_lines.extend([
                    f'call {cname} (read): POST /{cname}. It takes no parameter, and sends no query.',
                    f'call {cname} builds its body with the command [bin/report, --e={e}], run with no credential at each call and free to make its own requests without one while it builds. Its answer is returned as it comes, at most 65536 bytes.',
                ])
                extra_entries_for_0.append(f'[bin/report, --e={e}] (call.{cname}.build, handed no credential)')
            decl_text = 'name: synthetic\nbase: https://example.invalid\nlimits:\n  command: [bin/report]\n'
            if calls_decl_str:
                decl_text += 'calls:\n' + calls_decl_str
            files_list = [dict(program=p, text=file_txt, sha256=fsha) for p in progs]
            cmd_rows.append(f'command file bin/report: {and_list(extra_entries_for_0)}: sha256 {fsha}, mode 0700')
            for i in range(1, files_n):
                cname = f'c{i - 1}'
                cmd_rows.append(f'command file {progs[i]}: [{progs[i]}] (call.{cname}.build, handed no credential): sha256 {fsha}, mode 0700')
            weight_line = 'brings a program the yard will run: the command file ' + and_list(
                [f'{p} of synthetic (new)' for p in progs]
            )
            if len(call_lines) + len(cmd_rows) + 4 <= 40:
                for kind in ('new', 'new_public_main'):
                    ask = source_proposal(kind)
                    ask['run']['carried'][0]['text'] = decl_text
                    ask['run']['carried'][0]['files'] = copy.deepcopy(files_list)
                    ask['run']['does'] = [
                        f"it adds one source and copies {phrase} ({cmds_n} commands), and changes nothing else. "
                        "It grants no call, makes no hook, and sets no credential's file.",
                        'source synthetic: its calls go to https://example.invalid. It names no credential.',
                        *call_lines,
                        'its limits command [bin/report] says what the source itself carries, asked by the source\'s name.',
                        'it has no check call, no idempotency header, no way to tell a user, no way to hear deliveries, no way to speak in threads, no bead link, no identity fold (its identity keys keep their case), no hook deliveries into the yard and no pull-request or CI events.',
                        *cmd_rows,
                    ]
                    ask['weight'] = [dict({'class': 'code'}, line=weight_line)]
                    accept(ask, f'source_add_count_{kind}_{files_n}_{cmds_n}')
            # Well-formed file/command counts are shown whole, without reconstructing them.
            bad_cnt = source_proposal('new_public_main')
            bad_cnt['run']['does'][0] = (
                f"it adds one source and copies {phrase} ({cmds_n} commands), and changes nothing else. "
                "It grants no call, makes no hook, and sets no credential's file."
            )
            refuse(bad_cnt, f'source_count_mismatch_{files_n}_{cmds_n}')
        for plural_token in ('2', '9', '10', '19', '20', '100', '101'):
            self.assertRegex(
                f"it adds one source and copies {plural_token} command files ({plural_token} commands), and changes nothing else. It grants no call, makes no hook, and sets no credential's file.",
                n.SOURCE_ADD_LEAD_RE,
            )

        for bad_phrase in (
            '0 command files (1 command)',
            '1 command files (1 command)',
            '2 command file (2 commands)',
            '2 command files (1 commands)',
            '2 command files (0 commands)',
            '02 command files (2 commands)',
            '1 command file (02 commands)',
        ):
            bad = source_proposal('new_public_main')
            bad['run']['does'][0] = bad['run']['does'][0].replace('1 command file (1 command)', bad_phrase)
            refuse(bad, 'bad_cmd_count:' + bad_phrase)
            bad_rep = source_proposal('replace')
            bad_rep['run']['does'][0] = bad_rep['run']['does'][0].replace('1 command file (1 command)', bad_phrase)
            refuse(bad_rep, 'bad_replace_count:' + bad_phrase)

        for bad_src_phrase in ('0 sources', '1 sources', '2 source', '02 sources', '010 sources'):
            bad = pack_proposal('directory_public_main')
            bad['run']['does'][0] = bad['run']['does'][0].replace('1 source', bad_src_phrase)
            refuse(bad, 'bad_pack_src_count:' + bad_src_phrase)

        multi_pack = pack_proposal('directory_public_main')
        multi_pack.pop('weight', None)
        multi_pack['run']['carried'] = [{'name': f's{i}', 'text': f'name: s{i}\n'} for i in range(10)]
        multi_pack['run']['back'] = 'yardr source rm s0'
        multi_pack['run']['does'] = [
            "pack synthetic (directory /synthetic/checkout/pack in yardr unknown), digest sha256:"
            + multi_pack['run']['pack']['digest']
            + ", of kind source, into the yard: it adds 10 sources and copies no command files, and changes nothing else. "
            "It grants no call, makes no hook, and sets no credential's file.",
        ] + [
            line
            for i in range(10)
            for line in (
                f'source s{i}: it has no base and calls nothing. It names no credential.',
                'it has no check call, no idempotency header, no way to tell a user, no way to hear deliveries, no limits command, no way to speak in threads, no bead link, no identity fold (its identity keys keep their case), no hook deliveries into the yard and no pull-request or CI events.',
                'it names no command.',
            )
        ]
        accept(multi_pack, 'pack_10_sources')

    def test_source_and_pack_prose_values_are_native_owned(self):
        """Historical semantic-value mutations are presented whole; structured framing stays bound."""
        zero_sha = '0' * 64
        alt_sha = 'f' * 64
        fixtures = [
            ('source:new', lambda: source_proposal('new')),
            ('source:new_public_main', lambda: source_proposal('new_public_main')),
            ('source:free', lambda: source_proposal('free')),
            ('source:free_public_main', lambda: source_proposal('free_public_main')),
            ('source:replace', lambda: source_proposal('replace')),
            ('pack:directory', lambda: pack_proposal('directory')),
            ('pack:directory_upstream', lambda: pack_proposal('directory_upstream')),
            ('pack:directory_public_main', lambda: pack_proposal('directory_public_main')),
            ('pack:directory_replace', lambda: pack_proposal('directory_replace')),
            ('pack:embedded', lambda: pack_proposal('embedded')),
            ('pack:embedded_upstream', lambda: pack_proposal('embedded_upstream')),
            ('pack:embedded_public_main', lambda: pack_proposal('embedded_public_main')),
        ]

        # Build rich source (all 9 capability slots, mint, bases, run/build calls, params, query, answer) and rich replace (decl_diff, widens, still_granted, would change).
        rich_sha = n.file_hash('#!/bin/sh\necho ok\n')
        rich_mint_sha = n.file_hash('#!/bin/sh\necho mint\n')
        rich_yaml = (
            'name: synthetic\n'
            'base: https://api.example.invalid\n'
            'bases:\n  eu: https://eu.example.invalid\n'
            'credential:\n'
            '  env: SRC_TOK\n'
            '  update_env: SRC_UPD\n'
            '  header: "Authorization: Bearer {credential}"\n'
            '  mint:\n    - name: MINT_A\n      secret: true\n    - MINT_B\n'
            '  mint_command: [bin/mint, --v1]\n'
            'calls:\n'
            '  fetch:\n'
            '    kind: read\n'
            '    params:\n'
            '      q: {type: enum, values: [open, closed], optional: true, log: true}\n'
            '      pat: {type: string, pattern: "^[a-z]+$", max: 20, optional: true}\n'
            '    request:\n'
            '      method: GET\n      path: /items\n      query: {q: "{q}", empty: ""}\n'
            '    answer:\n      select: [id, title]\n      max: 4096\n'
            '  mk:\n'
            '    kind: update\n'
            '    request: {method: POST, path: /items}\n'
            '    build: [bin/builder, --json]\n'
            '    params:\n      p: {type: string}\n'
            '  tellcall:\n'
            '    kind: update\n'
            '    params:\n      to: {type: string}\n      t: {type: string}\n      x: {type: string}\n      l: {type: string, optional: true}\n'
            '    request:\n'
            '      method: POST\n      path: /tell\n      body: {to: "{to}", title: "{t}", text: "{x}", link: "{l}"}\n'
            'check: fetch\n'
            'idempotency: X-Req-Id\n'
            'notify:\n  call: tellcall\n  address: {param: to, env: HEAR_ADDR}\n  title: t\n  text: x\n  link: l\n'
            'hear:\n  field: sender\n  by: identity\n'
            'limits:\n  command: [bin/report, limits]\n  kinds: {claude: pro}\n'
            'thread:\n  open: [bin/report, open]\n  place: issue\n  states: [started, review, done]\n  labels: true\n  assignee: key\n'
            'link:\n'
            '  host: example.invalid\n'
            '  issue: "^/(org/repo)/i/([0-9]+)$"\n  fixed: "https://example.invalid/{1}/i/{2}"\n  thread: "{2}"\n  fold: lower\n'
            '  pull:\n    issue: "^/(org/repo)/pr/([0-9]+)$"\n    fixed: "https://example.invalid/{1}/pr/{2}"\n    thread: "{2}"\n'
            '  repos: "^(org/repo)$"\n'
            'identity:\n  fold: lower\n'
            'events:\n  runs: [ci]\n  head_from: [push]\n  pr: [bin/report, pr]\n  ci: [bin/report, ci]\n'
            'hook:\n'
            '  verify:\n'
            '    scheme: hmac-sha256\n    algorithm: sha512\n    signed: "v0:{timestamp}:{body}"\n'
            '    signature: {header: X-Sig, encoding: hex, list: ",", prefix: "sha512="}\n'
            '    key: {encoding: hex, prefix: "whsec_"}\n'
            '    delivery: {body: deliv.id}\n    timestamp: {body: ts, unit: ms, window: 5m}\n'
            '  bodies: unique\n'
            '  normalize: [bin/report, norm]\n'
            '  keys: [sender, repo]\n'
            '  register: {call: tellcall, url: x, secret: t}\n'
        )
        rich_add = source_proposal('new_public_main')
        rich_add['run']['carried'][0]['text'] = rich_yaml
        rich_add['run']['carried'][0]['files'] = [
            dict(program='bin/mint', text='#!/bin/sh\necho mint\n', sha256=rich_mint_sha),
            dict(program='bin/report', text='#!/bin/sh\necho ok\n', sha256=rich_sha),
            dict(program='bin/builder', text='#!/bin/sh\necho ok\n', sha256=rich_sha),
        ]
        rich_add['run']['does'] = [
            "it adds one source and copies 3 command files (7 commands), and changes nothing else. It grants no call, makes no hook, and sets no credential's file.",
            (f'source synthetic: its calls go to https://eu.example.invalid (base eu). Its credential is the variable SRC_TOK (the name, never the value), '
             f'with SRC_UPD for updates, minted from MINT_A and MINT_B by the declaration\'s own mint command [bin/mint, --v1] '
             f'(command file bin/mint, copied to <home>/sources/synthetic/bin/mint: sha256 {rich_mint_sha}, mode 0700), '
             f'run with the mint\'s inputs and the operator\'s local logins where the source\'s mint_command is unset: one the operator sets wins over it, '
             f'sent in the header "Authorization: Bearer {{credential}}" on every call.'),
            'call fetch (read): GET /items. It takes q (enum, optional, one of open, closed, its value is kept in the record of each call) and pat (string, optional, pattern ^[a-z]+$, at most 20), and sends the query q={q}, empty="".',
            'call fetch sends no body. Its answer keeps id, title, at most 4096 bytes.',
            'call mk (update): POST /items. It takes p (string, required), and sends no query.',
            'call mk builds its body with the command [bin/builder, --json], run with the credential at each call and free to make its own requests with it while it builds. Its answer is returned as it comes, at most 65536 bytes.',
            'call tellcall (update): POST /tell. It takes to (string, required), t (string, required), x (string, required) and l (string, optional), and sends no query.',
            'call tellcall sends the body {"link":"{l}","text":"{x}","title":"{t}","to":"{to}"}. Its answer is returned as it comes, at most 65536 bytes.',
            'Its check call is fetch, and updates carry their key in the header X-Req-Id.',
            "it can tell a user: the call tellcall, whose to takes the person's address, read as HEAR_ADDR from the person's own env file; the title fills t and the text fills x and the link fills l.",
            "it hears deliveries: only one whose sender is the address of a user told through it, read as , and every told user's: another's, and a stranger's, is dropped after verification.",
            'its limits command [bin/report, limits] says what the subscription of claude as pro still carries.',
            "it speaks in threads: a place to open one is called issue; the words state takes are started, review, done; it takes labels (label:<name> adds one, never removes); an assignee is a person's key in the source.",
            "its issues are a bead's link: host example.invalid, the expression ^/(org/repo)/i/([0-9]+)$ reads an issue, the one fixed form https://example.invalid/{1}/i/{2} and the thread id {2}; the groups fold lower. Its pull requests read ^/(org/repo)/pr/([0-9]+)$, with the fixed form https://example.invalid/{1}/pr/{2} and the thread id {2}. A repository's path matches ^(org/repo)$.",
            'its identity keys fold to lowercase (identity fold: lower), so one spelling is stored however it was typed.',
            'what it sends in: deliveries are verified by HMAC-SHA512 over v0:{timestamp}:{body} (scheme hmac-sha256, algorithm: sha512, signed: v0:{timestamp}:{body}), the signature in the header X-Sig as hex, one or more separated by ",", each past the prefix "sha512=", any one of them passing; the HMAC key is what follows the prefix "whsec_" in the secret, read as hex (not the secret\'s own bytes); a delivery is named by deliv.id in its body; its time arrives at ts, in ms within 5m of the yard\'s clock; bodies are unique; the command [bin/report, norm] turns a verified body into the envelope, handed no credential; its events may carry the fields sender, repo; the yard registers its webhook with tellcall (POST /tell), filling x with the public URL and t with the hook\'s secret.',
            'what it sends of pull requests and CI: the envelope types ci are runs; push tell a head; the command [bin/report, pr] reads the pull request, handed the credential; the command [bin/report, ci] reads CI, handed the credential.',
            f'command file bin/report: [bin/report, limits] (limits.command, handed no credential), [bin/report, open] (thread.open, handed the credential), [bin/report, norm] (hook.normalize, handed no credential), [bin/report, pr] (events.pr, handed the credential) and [bin/report, ci] (events.ci, handed the credential): sha256 {rich_sha}, mode 0700',
            f'command file bin/builder: [bin/builder, --json] (call.mk.build, handed the credential): sha256 {rich_sha}, mode 0700',
            'still owed after it: the credential, by a secret ask for synthetic; and which user it tells, by yardr user set <user> notify=synthetic.',
        ]
        rich_add['weight'] = [dict(
            {'class': 'code'},
            line='brings a program the yard will run: the command file bin/mint of synthetic (new), bin/report of synthetic (new) and bin/builder of synthetic (new)',
        )]

        rich_replace = source_proposal('replace')
        rc = rich_replace['run']['carried'][0]
        old_rep_text = n.source_diff(rc['old'][0], rc['files'][0]['text'])
        old_rep_sha = rc['old'][0]['old_sha256']
        rep_lines = old_rep_text.removesuffix('\n').split('\n')
        rc['text'] = (
            'name: synthetic\nbase: https://api.example.invalid\n'
            'calls:\n  a:\n    kind: read\n    request: {method: GET, path: /a}\n'
            '  upd:\n    kind: update\n    request: {method: POST, path: /u, body: {ok: true}}\n'
            '  fetch:\n    kind: read\n'
            '    params:\n      q: {type: enum, values: [open, closed], optional: true, log: true}\n'
            '    request:\n      method: GET\n      path: /items\n      query: {q: "{q}"}\n'
            '    answer:\n      select: [id]\n      max: 4096\n'
        )
        rc['files'] = []
        rc['old'][0]['new_sha256'] = ''
        rc['old'][0]['diff'] = '--- was\n+++ is\n' + f'@@ -1,{len(rep_lines)} +0,0 @@\n' + ''.join('-' + l + '\n' for l in rep_lines)
        rem = rc['removed'][0]
        rich_replace['run']['does'] = [
            "it replaces one source and copies no command files, and removes the command copies that stand, and changes nothing else. It grants no call, makes no hook, and sets no credential's file: the source keeps its env_file, mint_command, region base, call limits and ignored senders, and every grant of it stands, now for this declaration.",
            'source synthetic: its calls go to https://api.example.invalid. It names no credential.',
            'it would replace the source synthetic the yard has under that name; grants kept for source synthetic: crew member reviewer: gone (absent from the new declaration), read (kept), upd (changed).',
            'source synthetic base: "https://old.example.invalid" -> "https://api.example.invalid".',
            ('source synthetic calls: [{"name":"a","kind":"read","request":{"method":"GET","path":"/old"},"answer":{"max":65536}}] -> '
             '[{"name":"a","kind":"read","request":{"method":"GET","path":"/a"},"answer":{"max":65536}},'
             '{"name":"upd","kind":"update","request":{"method":"POST","path":"/u","body":{"ok":true}},"answer":{"max":65536}},'
             '{"name":"fetch","kind":"read","params":[{"name":"q","type":"enum","values":["open","closed"],"optional":true,"log":true}],'
             '"request":{"method":"GET","path":"/items","query":[{"key":"q","value":"{q}"}]},"answer":{"select":["id"],"max":4096}}].'),
            f'command file bin/report would change: sha256 {old_rep_sha} -> sha256 (none) (0 insertions, {len(rep_lines)} deletions).',
            f"the copy would remove {rem['path']}, which neither declaration names: sha256 {rem['sha256']} (mode {rem['mode']}, {rem['size']} bytes).",
            'a standing grant of read of source synthetic widens to a for crew member reviewer.',
            'the changed call upd (update) is still granted by its name to crew member reviewer (yardr source revoke synthetic upd takes it back).',
            'call a (read): GET /a. It takes no parameter, and sends no query.',
            'call a sends no body. Its answer is returned as it comes, at most 65536 bytes.',
            'call upd (update): POST /u. It takes no parameter, and sends no query.',
            'call upd sends the body {"ok":true}. Its answer is returned as it comes, at most 65536 bytes.',
            'call fetch (read): GET /items. It takes q (enum, optional, one of open, closed, its value is kept in the record of each call), and sends the query q={q}.',
            'call fetch sends no body. Its answer keeps id, at most 4096 bytes.',
            'The source names no check call and no idempotency header.',
            'it can tell no user.',
            'it hears no deliveries.',
            'it names no limits command.',
            'it speaks in no threads.',
            "its issues are no bead's link.",
            'its identity keys keep their case.',
            'it sends no hook deliveries into the yard.',
            'it sends no pull-request or CI events.',
            'it names no command.',
        ]
        rich_replace['weight'] = [
            dict({'class': 'code'}, line='brings a program the yard will run: the declaration of synthetic replaces the one that stands, and what it names of commands or updates runs from now on'),
            dict({'class': 'reads'}, line='widens what a standing grant of read reaches: a grant of read widens to a of synthetic for crew member reviewer'),
        ]

        all_bundles = fixtures + [
            ('rich:add', lambda: copy.deepcopy(rich_add)),
            ('rich:replace', lambda: copy.deepcopy(rich_replace)),
        ]

        # Build the table of well-formed single-value mismatch mutations for each bundle.
        def build_mutations(tag, ask):
            does = ask['run']['does']
            rows = []

            def rep_line(field, idx, old, new):
                if idx < len(does) and old in does[idx] and does[idx].replace(old, new, 1) != does[idx]:
                    out = copy.deepcopy(ask)
                    out['run']['does'][idx] = out['run']['does'][idx].replace(old, new, 1)
                    rows.append((field, out))

            def rep_first(field, old, new, *, start=0):
                for idx in range(start, len(does)):
                    if old in does[idx] and does[idx].replace(old, new, 1) != does[idx]:
                        rep_line(field, idx, old, new)
                        return

            # Lead row counts & pack metadata
            rep_line('lead:files_count', 0, 'copies 1 command file (1 command)', 'copies 2 command files (1 command)')
            rep_line('lead:cmds_count', 0, 'copies 1 command file (1 command)', 'copies 1 command file (2 commands)')
            rep_line('lead:files_count', 0, 'copies 2 command files (6 commands)', 'copies 3 command files (6 commands)')
            rep_line('lead:cmds_count', 0, 'copies 2 command files (6 commands)', 'copies 2 command files (7 commands)')
            rep_line('lead:files_count', 0, 'copies 3 command files (9 commands)', 'copies 4 command files (9 commands)')
            rep_line('lead:cmds_count', 0, 'copies 3 command files (9 commands)', 'copies 3 command files (10 commands)')
            rep_line('lead:no_files_to_some', 0, 'copies no command files, and removes the command copies that stand', 'copies 1 command file (1 command)')
            rep_line('lead:no_files_omit_removes', 0, 'copies no command files, and removes the command copies that stand', 'copies no command files')
            rep_line('lead:free_no_files', 0, 'copies no command files', 'copies 1 command file (1 command)')
            rep_line('lead:pack_adds', 0, 'it adds 1 source', 'it adds 2 sources')
            rep_line('lead:pack_replaces', 0, 'it replaces 1 source', 'it replaces 2 sources')
            if tag.startswith('pack:'):
                pack = ask['run']['pack']
                rep_line('lead:pack_name', 0, 'pack ' + pack['name'] + ' (', 'pack wrongname (')
                if isinstance(pack.get('digest'), str):
                    bad_digest = zero_sha if pack['digest'] == alt_sha else alt_sha
                    rep_line('lead:pack_digest', 0, 'digest sha256:' + pack['digest'], 'digest sha256:' + bad_digest)
                version = re.search(r'in yardr ([^\s()]+)', does[0])
                if version and version.group(1) != 'deadbeef0000':
                    rep_line('lead:pack_version', 0, 'in yardr ' + version.group(1), 'in yardr deadbeef0000')
                path = re.search(r'directory (\S+) in yardr', does[0])
                if path and path.group(1) != '/synthetic/checkout/other':
                    rep_line('lead:pack_path', 0, 'directory ' + path.group(1) + ' in yardr', 'directory /synthetic/checkout/other in yardr')

            # Summary row (index 1)
            rep_line('summary:source_name', 1, f'source {ask["run"]["carried"][0]["name"]}:', 'source wrongname:')
            rep_line('summary:base_url', 1, 'https://example.invalid', 'https://other.invalid')
            rep_line('summary:base_url', 1, 'https://api.telegram.org', 'https://other.invalid')
            rep_line('summary:base_name', 1, 'https://eu.example.invalid (base eu)', 'https://eu.example.invalid (base us)')
            rep_line('summary:no_base_to_url', 1, 'it has no base and calls nothing.', 'its calls go to https://other.invalid.')
            rep_line('summary:no_cred_to_cred', 1, 'It names no credential.', 'Its credential is the variable FAKE_TOK (the name, never the value), and no call\'s header, path or body holds it. It has no second credential for updates, no mint, no header of its own.')
            rep_line('summary:cred_env', 1, 'TELEGRAM_BOT_TOKEN', 'OTHER_BOT_TOKEN')
            rep_line('summary:cred_env', 1, 'SRC_TOK', 'OTHER_TOK')
            rep_line('summary:update_env', 1, 'SRC_UPD', 'OTHER_UPD')
            rep_line('summary:mint_envs', 1, 'MINT_A and MINT_B', 'MINT_A and MINT_C')
            rep_line('summary:mint_cmd_argv', 1, '[bin/mint, --v1]', '[bin/mint, --v2]')
            rep_line('summary:mint_sha', 1, f'sha256 {rich_mint_sha}', f'sha256 {alt_sha}')
            rep_line('summary:header_value', 1, '"Authorization: Bearer {credential}"', '"X-Other: {credential}"')
            rep_line('summary:cred_in_path', 1, 'in the request path wherever the declaration puts {credential} (credential.in_path: true): it stands in the logs of every server on the way', 'in the header "X-Other" on every call')

            # Replace preamble & diffs & changes/removals
            rep_first('replace:grants_absent_present', 'gone (absent from the new declaration)', 'a (absent from the new declaration)')
            rep_first('replace:grants_kept_unknown', 'upd (changed)', 'ghost (changed)')
            rep_first('replace:grants_kept_unknown', 'health (kept)', 'ghost (kept)')
            rep_first('replace:decl_diff_new', '-> "https://api.example.invalid".', '-> "https://wrong.example.invalid".')
            rep_first('replace:decl_diff_calls_name', '-> [{"name":"a",', '-> [{"name":"other",')
            rep_first('replace:decl_diff_calls_kind', '-> [{"name":"a","kind":"read"', '-> [{"name":"a","kind":"update"')
            rep_first('replace:decl_diff_calls_method', '{"method":"GET","path":"/a"}', '{"method":"POST","path":"/a"}')
            rep_first('replace:decl_diff_calls_path', '{"method":"GET","path":"/a"}', '{"method":"GET","path":"/other"}')
            rep_first('replace:decl_diff_calls_query_spurious', '{"method":"GET","path":"/a"}', '{"method":"GET","path":"/a","query":[{"key":"x","value":"1"}]}')
            rep_first('replace:decl_diff_calls_query_value', '{"key":"q","value":"{q}"}', '{"key":"q","value":"{wrong}"}')
            rep_first('replace:decl_diff_calls_params_type', '"type":"enum"', '"type":"string"')
            rep_first('replace:decl_diff_calls_params_log', '"log":true', '"log":false')
            rep_first('replace:decl_diff_calls_body', '"body":{"ok":true}', '"body":{"ok":false}')
            rep_first('replace:decl_diff_calls_body_type', '"body":{"ok":true}', '"body":{"ok":"true"}')
            rep_first('replace:decl_diff_calls_answer_max', '"path":"/a"},"answer":{"max":65536}', '"path":"/a"},"answer":{"max":4096}')
            rep_first('replace:decl_diff_calls_answer_select', '"select":["id"]', '"select":["title"]')
            rep_first('replace:decl_diff_calls_extra_key', '-> [{"name":"a",', '-> [{"name":"a","extra":1,')
            rep_first('replace:would_change_prog', 'command file bin/report would change:', 'command file bin/other would change:')
            rep_first('replace:would_change_sha', f'sha256 {old_rep_sha}', f'sha256 {alt_sha}')
            rep_first('replace:would_change_del', '0 insertions,', '1 insertions,')
            rep_first('replace:removed_path', 'extra.txt', 'wrong.txt')
            rep_first('replace:removed_path', 'obsolete.txt', 'wrong.txt')
            rep_first('replace:removed_sha', '670ac0987d6acba567e81e9faa48aca43d2c7d8d26241fc7b3fa6604453b3a51', alt_sha)
            rep_first('replace:removed_mode', 'mode 0644', 'mode 0755')
            rep_first('replace:removed_size', '24 bytes', '99 bytes')
            rep_first('replace:widens_call', 'widens to a for', 'widens to wrongcall for')
            rep_first('replace:still_granted_call', 'the changed call upd (update)', 'the changed call ghost (update)')
            rep_first('replace:still_granted_kind', 'the changed call upd (update)', 'the changed call upd (read)')

            # Call rows
            rep_first('call:name', 'call health (read):', 'call ghost (read):')
            rep_first('call:kind', 'call health (read):', 'call health (update):')
            rep_first('call:method', 'GET /health.', 'POST /health.')
            rep_first('call:path', 'GET /health.', 'GET /other.')
            rep_first('call:run_argv', 'runs bin/report --run.', 'runs bin/report --other.')
            rep_first('call:param_name', 'It takes q (enum,', 'It takes z (enum,')
            rep_first('call:param_values', 'one of open, closed', 'one of open, archived')
            rep_first('call:param_pattern', 'pattern ^[a-z]+$', 'pattern ^[0-9]+$')
            rep_first('call:param_max', 'at most 20', 'at most 99')
            rep_first('call:param_log', ', its value is kept in the record of each call', '')
            rep_first('call:query', 'q={q}, empty=""', 'q={q}, empty="x"')
            rep_first('call:run_cred', 'run with the credential at each call', 'run with no credential at each call')
            rep_first('call:build_argv', '[bin/builder, --json]', '[bin/builder, --yaml]')
            rep_first('call:body', '{"link":"{l}","text":"{x}","title":"{t}","to":"{to}"}', '{"link":"{l}","text":"{x}","title":"{t}","to":"WRONG"}')
            rep_first('call:body_string_to_bool', '"to":"{to}"', '"to":true')
            rep_first('call:body_bool_to_string', 'sends the body {"ok":true}.', 'sends the body {"ok":"true"}.')
            rep_first('call:answer_select', 'keeps id, title', 'keeps id, body')
            rep_first('call:answer_max', 'at most 4096 bytes.', 'at most 8192 bytes.')
            rep_first('call:answer_default_max', 'at most 65536 bytes.', 'at most 4096 bytes.')

            # Capabilities
            rep_first('cap:check', 'Its check call is webhookinfo;', 'Its check call is message;')
            rep_first('cap:check_idemp', 'header X-Req-Id.', 'header X-Wrong-Id.')
            rep_first('cap:notify_call', 'the call message,', 'the call document,')
            rep_first('cap:notify_from_env', 'read as TELEGRAM_CHAT_ID', 'read as OTHER_CHAT_ID')
            rep_first('cap:hear_field', 'only one whose chat is', 'only one whose from_user is')
            rep_first('cap:hear_who', "only an operator's:", "every told user's:")
            rep_first('cap:limits_argv', 'its limits command [bin/report]', 'its limits command [bin/report, --extra]')
            rep_first('cap:limits_kinds', 'claude as pro', 'claude as free')
            rep_first('cap:thread_place', 'called issue;', 'called ticket;')
            rep_first('cap:thread_states', 'started, review, done;', 'started, done;')
            rep_first('cap:thread_labels', 'it takes labels (label:<name> adds one, never removes);', 'it takes no labels;')
            rep_first('cap:thread_assignee', "a person's key in", "a person's id in")
            rep_first('cap:link_host', 'host example.invalid,', 'host wrong.invalid,')
            rep_first('cap:link_pr', '^/(org/repo)/pr/([0-9]+)$', '^/(org/repo)/pulls/([0-9]+)$')
            rep_first('cap:link_repo', 'matches ^(org/repo)$.', 'matches ^(other/repo)$.')
            rep_first('cap:identity_fold', 'identity fold: lower', 'identity fold: email')
            rep_first('cap:hook_secret_hdr', 'header X-Secret holding', 'header X-Wrong holding')
            rep_first('cap:hook_secret_hdr_vs_body', 'header X-Telegram-Bot-Api-Secret-Token holding', 'body field X-Telegram-Bot-Api-Secret-Token holding')
            rep_first('cap:hook_deliv_body', 'named by update_id in its body;', 'named by event_id in its body;')
            rep_first('cap:hook_hmac_outer_alg', 'HMAC-SHA512', 'HMAC-SHA256')
            rep_first('cap:hook_hmac_over', 'over v0:{timestamp}:{body}', 'over wrong-input')
            rep_first('cap:hook_hmac_over_body', 'over v0:{timestamp}:{body}', 'over the body')
            rep_first('cap:hook_hmac_alg', 'algorithm: sha512', 'algorithm: sha256')
            rep_first('cap:hook_ts_window', 'within 5m of', 'within 10m of')
            rep_first('cap:hook_bodies', 'bodies are unique;', 'bodies are repeat;')
            rep_first('cap:hook_norm', '[telegram/normalize]', '[telegram/other]')
            rep_first('cap:hook_keys', 'fields chat;', 'fields from;')
            rep_first('cap:hook_reg', 'with setwebhook (POST /bot{credential}/setWebhook)', 'with message (POST /bot{credential}/sendMessage)')
            rep_first('cap:events_runs', 'envelope types ci are runs;', 'envelope types build are runs;')
            rep_first('cap:events_pr', '[bin/report, pr] reads', '[bin/report, pull] reads')

            # Bottom command rows & still owed
            rep_first('cmd:prog', 'command file bin/report:', 'command file bin/other:', start=max(0, len(does) - 5))
            rep_first('cmd:slot', '(limits.command, handed', '(thread.reply, handed', start=max(0, len(does) - 5))
            rep_first('cmd:slot', '(hook.normalize, handed', '(limits.command, handed', start=max(0, len(does) - 5))
            rep_first('cmd:cred', 'handed no credential', 'handed the credential', start=max(0, len(does) - 5))
            rep_first('cmd:sha', 'sha256 9d9adc8ea0ca1a4bd6955d136517cd0dd7e5f59a69f843bdbc9cc56818224ba4', f'sha256 {alt_sha}', start=max(0, len(does) - 5))
            rep_first('cmd:ins_del', '(1 insertions, 1 deletions).', '(9 insertions, 1 deletions).', start=max(0, len(does) - 5))
            rep_first('cmd:ins_del', '(2 insertions, 2 deletions).', '(9 insertions, 2 deletions).', start=max(0, len(does) - 5))
            rep_first('owed:cred_removed', 'still owed after it: the credential, by a secret ask for synthetic; and which user it tells, by yardr user set <user> notify=synthetic.', 'still owed after it: which user it tells, by yardr user set <user> notify=synthetic.')

            # Weights
            if ask.get('weight'):
                for widx, w in enumerate(ask['weight']):
                    if w['class'] == 'code':
                        out = copy.deepcopy(ask)
                        out['weight'][widx]['line'] = w['line'].replace('(new)', '(changed)') if '(new)' in w['line'] else w['line'].replace('(changed)', '(new)') if '(changed)' in w['line'] else 'brings a program the yard will run: other'
                        rows.append(('weight:code', out))
                    elif w['class'] == 'reads':
                        out = copy.deepcopy(ask)
                        out['weight'][widx]['line'] = w['line'].replace('widens to a of', 'widens to b of')
                        rows.append(('weight:reads', out))
                out = copy.deepcopy(ask)
                out['weight'].append({'class': 'outside', 'line': 'lets the yard change something outside it: external target'})
                rows.append(('weight:outside', out))
            else:
                out = copy.deepcopy(ask)
                out['weight'] = [{'class': 'code', 'line': 'brings a program the yard will run: bin/fake of synthetic (new)'}]
                rows.append(('weight:unexpected_code', out))
            return rows

        for tag, make_ask in all_bundles:
            intact = make_ask()
            with self.subTest(bundle=tag, mutation='intact'):
                self.reset_native(intact)
                res = self.create()
                self.assertTrue(res['delivered'], (tag, res))
            for field, mutated in build_mutations(tag, intact):
                with self.subTest(bundle=tag, mutation=field):
                    refused = (field.startswith('lead:pack_') or field in ('summary:source_name', 'weight:outside')
                               or field == 'lead:free_no_files' and 'and removes the command copies' in mutated['run']['does'][0])
                    self.assert_source_contract(mutated, not refused)
                    self.assert_bound_source_change_stales(intact, mutated)

        # Old-state and holder prose have no independent semantic binding in the bridge.
        for label, mutate in (
            ('decl_diff_old_side', lambda a: a['run']['does'].__setitem__(
                3, 'source synthetic base: "https://different-old.invalid" -> "https://api.example.invalid".')),
            ('grants_kept_holder_prose', lambda a: a['run']['does'].__setitem__(
                2, a['run']['does'][2].replace('crew member reviewer:', 'crew member other:'))),
        ):
            prose_ask = copy.deepcopy(rich_replace)
            mutate(prose_ask)
            with self.subTest(prose_only=label):
                self.reset_native(prose_ask)
                res = self.create()
                self.assertTrue(res['delivered'], (label, res))

        # Preserve the historical build/parameter diff mutations as native prose.
        build_rep = copy.deepcopy(source_proposal('replace'))
        brc = build_rep['run']['carried'][0]
        brc['text'] = (
            'name: synthetic\nbase: https://api.example.invalid\n'
            'calls:\n  mk:\n    kind: update\n'
            '    params:\n      p: {type: string, pattern: "^[a-z]+$", max: 20, optional: true, default: "ok"}\n'
            '    request: {method: POST, path: /mk}\n'
            '    build: [bin/report, --build]\n'
        )
        b_sha = brc['files'][0]['sha256']
        b_old_sha = brc['old'][0]['old_sha256']
        b_calls_new = (
            '[{"name":"mk","kind":"update",'
            '"params":[{"name":"p","type":"string","pattern":"^[a-z]+$","max":20,"optional":true,"default":"ok"}],'
            '"request":{"method":"POST","path":"/mk"},"build":["bin/report","--build"],"answer":{"max":65536}}]'
        )
        build_rep['run']['does'] = [
            "it replaces one source and copies 1 command file (1 command), and changes nothing else. It grants no call, makes no hook, and sets no credential's file: the source keeps its env_file, mint_command, region base, call limits and ignored senders, and every grant of it stands, now for this declaration.",
            'source synthetic: its calls go to https://api.example.invalid. It names no credential.',
            'it would replace the source synthetic the yard has under that name; no grant of source synthetic stands.',
            f'source synthetic calls: none -> {b_calls_new}.',
            f"the copy would remove {brc['removed'][0]['path']}, which neither declaration names: sha256 {brc['removed'][0]['sha256']} (mode {brc['removed'][0]['mode']}, {brc['removed'][0]['size']} bytes).",
            'call mk (update): POST /mk. It takes p (string, optional, pattern ^[a-z]+$, at most 20), and sends no query.',
            'call mk builds its body with the command [bin/report, --build], run with no credential at each call and free to make its own requests without one while it builds. Its answer is returned as it comes, at most 65536 bytes.',
            'The source names no check call and no idempotency header.',
            'it can tell no user.',
            'it hears no deliveries.',
            'it names no limits command.',
            'it speaks in no threads.',
            "its issues are no bead's link.",
            'its identity keys keep their case.',
            'it sends no hook deliveries into the yard.',
            'it sends no pull-request or CI events.',
            f'command file bin/report: [bin/report, --build] (call.mk.build, handed no credential): sha256 {b_old_sha} -> sha256 {b_sha}, mode 0700 (1 insertions, 1 deletions).',
        ]
        with self.subTest(calls_diff='build_intact'):
            self.reset_native(build_rep)
            res = self.create()
            self.assertTrue(res['delivered'], res)
        for label, old_frag, new_frag in (
            ('build_argv', '"build":["bin/report","--build"]', '"build":["bin/report","--other"]'),
            ('param_pattern', '"pattern":"^[a-z]+$"', '"pattern":"^[0-9]+$"'),
            ('param_max', '"max":20', '"max":30'),
            ('param_default', '"default":"ok"', '"default":"bad"'),
            ('param_optional', '"optional":true,', ''),
        ):
            mut = copy.deepcopy(build_rep)
            mut['run']['does'][3] = mut['run']['does'][3].replace(old_frag, new_frag, 1)
            with self.subTest(calls_diff=label):
                self.assert_source_contract(mut, True)
                self.assert_bound_source_change_stales(build_rep, mut)

        # Historical nested diffs and displayed modes stay opaque; raw bytes remain structural.
        nested_rep = copy.deepcopy(rich_add)
        nrc = nested_rep['run']['carried'][0]
        nrc['inventory'] = [dict(path=f['program'], sha256=f['sha256'], mode='0700',
                                 size=len(f['text'].encode())) for f in nrc['files']]
        nested_rep['run']['back'] = ('yardr bead ask <bead> - with Q: <question> and R: source add '
                                     '<a file holding the declaration that stands now> on stdin')
        nested_rep['run']['back_by'] = 'only the operator'
        nested_rep['run']['does'][0] = (
            'it replaces one source and copies 3 command files (7 commands), and changes nothing else. '
            'It grants no call, makes no hook, and sets no credential\'s file: the source keeps its env_file, '
            'mint_command, region base, call limits and ignored senders, and every grant of it stands, '
            'now for this declaration.')
        nested_new = {
            'check': 'fetch',
            'credential': {'env': 'SRC_TOK', 'header': 'Authorization: Bearer {credential}',
                           'mint': [{'name': 'MINT_A', 'secret': True}, 'MINT_B'],
                           'mint_command': ['bin/mint', '--v1'], 'update_env': 'SRC_UPD'},
            'notify': {'address': {'env': 'HEAR_ADDR', 'param': 'to'}, 'call': 'tellcall',
                       'link': 'l', 'text': 'x', 'title': 't'},
            'hear': {'by': 'identity', 'field': 'sender'},
            'limits': {'command': ['bin/report', 'limits'], 'kinds': {'claude': 'pro'}},
            'thread': {'assignee': 'key', 'labels': True, 'open': ['bin/report', 'open'],
                       'place': 'issue', 'states': ['started', 'review', 'done']},
            'link': {'fixed': 'https://example.invalid/{1}/i/{2}', 'fold': 'lower',
                     'host': 'example.invalid', 'issue': '^/(org/repo)/i/([0-9]+)$',
                     'pull': {'fixed': 'https://example.invalid/{1}/pr/{2}',
                              'issue': '^/(org/repo)/pr/([0-9]+)$', 'thread': '{2}'},
                     'repos': '^(org/repo)$', 'thread': '{2}'},
            'hook': {'bodies': 'unique', 'keys': ['sender', 'repo'],
                     'normalize': ['bin/report', 'norm'],
                     'register': {'call': 'tellcall', 'secret': 't', 'url': 'x'},
                     'verify': {'algorithm': 'sha512',
                                'delivery': {'body': 'deliv.id'},
                                'key': {'encoding': 'hex', 'prefix': 'whsec_'},
                                'scheme': 'hmac-sha256',
                                'signature': {'encoding': 'hex', 'header': 'X-Sig',
                                              'list': ',', 'prefix': 'sha512='},
                                'signed': 'v0:{timestamp}:{body}',
                                'timestamp': {'body': 'ts', 'unit': 'ms', 'window': '5m'}}},
            'events': {'ci': ['bin/report', 'ci'], 'head_from': ['push'],
                       'pr': ['bin/report', 'pr'], 'runs': ['ci']},
            'identity': {'fold': 'lower'},
        }
        nested_rep['run']['does'][2:2] = (
            ['it would replace the source synthetic the yard has under that name; '
             'no grant of source synthetic stands.'] +
            [f'source synthetic {field}: none -> '
             f'{json.dumps(value, sort_keys=True, separators=(",", ":"))}.'
             for field, value in nested_new.items()])
        nested_rep['weight'] = [dict(
            {'class': 'code'},
            line=('brings a program the yard will run: the declaration of synthetic replaces the one '
                  'that stands, and says where the credential it keeps is sent and which programs the yard runs'))]
        with self.subTest(nested_diff='intact'):
            self.reset_native(nested_rep)
            res = self.create()
            self.assertTrue(res['delivered'], res)

        def mutate_row(ask, label, old, new, *, prefix=None):
            out = copy.deepcopy(ask)
            for idx, line in enumerate(out['run']['does']):
                if prefix is not None and not line.startswith(prefix):
                    continue
                if old in line:
                    out['run']['does'][idx] = line.replace(old, new, 1)
                    with self.subTest(nested_diff=label):
                        self.assert_source_contract(out, True)
                        self.assert_bound_source_change_stales(ask, out)
                    return
            raise AssertionError(f'mutation {label} matched no row')

        nested_cases = (
            ('link_pull_issue', '"issue":"^/(org/repo)/pr/([0-9]+)$"', '"issue":"^/WRONG/([0-9]+)$"', None),
            ('credential_env', '"env":"SRC_TOK"', '"env":"OTHER_TOK"', None),
            ('credential_mint_secret', '"secret":true', '"secret":false', None),
            ('notify_call', '"call":"tellcall"', '"call":"ghost"', None),
            ('hear_field', '"field":"sender"', '"field":"recipient"', None),
            ('thread_place', '"place":"issue"', '"place":"ticket"', None),
            ('hook_bodies', '"bodies":"unique"', '"bodies":"repeat"', None),
            ('events_runs', '"runs":["ci"]', '"runs":["build"]', None),
            ('limits_kinds', '"claude":"pro"', '"claude":"free"', None),
            ('check_call', 'check: none -> "fetch".', 'check: none -> "other".', None),
            ('identity_fold', '"fold":"lower"', '"fold":"email"', 'source synthetic identity:'),
            ('cmd_mode', 'mode 0700', 'mode 0600', 'command file bin/report:'),
            ('mint_mode', 'mode 0700', 'mode 0600', 'source synthetic: its calls go to'),
        )
        for label, old, new, prefix in nested_cases:
            mutate_row(nested_rep, label, old, new, prefix=prefix)

        # Displayed-mode mutations cannot alter the structured inventory or installed bytes.
        for label, factory, prefix in (
                ('replace_changed_mode', lambda: source_proposal('replace'), None),
                ('pack_replace_changed_mode', lambda: pack_proposal('directory_replace'), None),
                ('calls_diff_changed_mode', lambda: copy.deepcopy(build_rep), None),
        ):
            probed = factory()
            mutate_row(probed, label, 'mode 0700', 'mode 0600', prefix=prefix)

        unchanged_size_mut = copy.deepcopy(nested_rep)
        unchanged_size_mut['run']['carried'][0]['inventory'][0]['size'] += 1
        with self.subTest(nested_diff='inv_unchanged_size'):
            self.reset_native(unchanged_size_mut)
            res = self.create()
            self.assertFalse(res['delivered'], res)
            self.assertTrue(res['reason'].endswith(n.FALLBACK), res)
            self.assertEqual((self.sent_count, self.writes()), (0, []))

    def test_source_and_pack_carried_leaves_are_structural_and_bound(self):
        """All carried leaves stale old controls; only independent structural contradictions refuse on first read."""
        import yaml

        def iter_leaves(node, path=()):
            if isinstance(node, dict):
                for key, val in node.items():
                    yield from iter_leaves(val, path + (key,))
            elif isinstance(node, list):
                for idx, val in enumerate(node):
                    yield from iter_leaves(val, path + (idx,))
            else:
                yield path, node

        def mutate_decl_value(val):
            if isinstance(val, bool):
                return not val
            if isinstance(val, int):
                return val + 1 if val != 0 else 1
            if isinstance(val, float):
                return val + 1.5
            if val in ('true', 'false'):
                return 'false' if val == 'true' else 'true'
            if val.isdigit():
                return str(int(val) + 1)
            if val.startswith('https://'):
                return 'https://mutated.invalid/x'
            if val.startswith('/'):
                return '/mutated/path'
            if val in ('read', 'update'):
                return 'update' if val == 'read' else 'read'
            if val in ('GET', 'POST', 'PUT', 'PATCH', 'DELETE', 'HEAD'):
                return 'DELETE' if val != 'DELETE' else 'GET'
            if val in ('hex', 'base64'):
                return 'base64' if val == 'hex' else 'hex'
            if val in ('sha256', 'sha1', 'sha512'):
                return 'sha1' if val != 'sha1' else 'sha512'
            if val in ('secret', 'hmac-sha256'):
                return 'hmac-sha256' if val == 'secret' else 'secret'
            if val in ('ms', 's', 'seconds'):
                return 's' if val == 'ms' else 'ms'
            if val in ('unique', 'unique-per-hook', 'repeat'):
                return 'repeat' if val != 'repeat' else 'unique'
            if val in ('operator', 'all'):
                return 'all' if val == 'operator' else 'operator'
            if val in ('identity', 'address'):
                return 'address' if val == 'identity' else 'identity'
            if val in ('lower', 'none'):
                return 'none' if val == 'lower' else 'lower'
            if val in ('string', 'enum', 'int', 'bool', 'file', 'secret-file'):
                return 'int' if val != 'int' else 'string'
            return val + '_mut'

        def set_leaf(node, path, new_val):
            cur = node
            for key in path[:-1]:
                cur = cur[key]
            cur[path[-1]] = new_val

        def alt_sha(cur):
            return '0' * 64 if cur != '0' * 64 else 'f' * 64

        def assert_refused(mut_ask, label):
            self.reset_native(mut_ask)
            res = self.create()
            self.assertFalse(res['delivered'], (label, res))
            self.assertTrue(res['reason'].endswith(n.FALLBACK), (label, res))
            self.assertEqual((self.sent_count, self.writes()), (0, []), label)

        bundles = (
            ('source:new', source_proposal('new')),
            ('source:new_public_main', source_proposal('new_public_main')),
            ('source:free', source_proposal('free')),
            ('source:free_public_main', source_proposal('free_public_main')),
            ('source:replace', source_proposal('replace')),
            ('pack:directory', pack_proposal('directory')),
            ('pack:directory_upstream', pack_proposal('directory_upstream')),
            ('pack:directory_public_main', pack_proposal('directory_public_main')),
            ('pack:directory_replace', pack_proposal('directory_replace')),
            ('pack:embedded', pack_proposal('embedded')),
            ('pack:embedded_upstream', pack_proposal('embedded_upstream')),
            ('pack:embedded_public_main', pack_proposal('embedded_public_main')),
            ('rich:add', rich_add_proposal()),
            ('rich:nested_replace', nested_replace_proposal()),
        )

        for tag, ask in bundles:
            with self.subTest(bundle=tag, check='intact'):
                self.reset_native(copy.deepcopy(ask))
                res = self.create()
                self.assertTrue(res['delivered'], (tag, res))

            for ci, carried in enumerate(ask['run']['carried']):
                decl = yaml.safe_load(carried['text'])
                for path, val in iter_leaves(decl):
                    mut_decl = copy.deepcopy(decl)
                    set_leaf(mut_decl, path, mutate_decl_value(val))
                    mut_ask = copy.deepcopy(ask)
                    mut_ask['run']['carried'][ci]['text'] = yaml.safe_dump(mut_decl, sort_keys=False)
                    with self.subTest(bundle=tag, carried=ci, decl_path=path):
                        command_paths = {
                            ('credential', 'mint_command'), ('limits', 'command'),
                            *(('thread', k) for k in ('reply', 'posted', 'read', 'file', 'open', 'opened', 'state')),
                            ('notify', 'image'), ('hook', 'normalize'), ('events', 'pr'), ('events', 'ci'), ('git', 'credential'),
                        }
                        program = (len(path) == 3 and path[-1] == 0 and path[:2] in command_paths
                                   or len(path) == 4 and path[0] == 'calls' and path[2:] == ('build', 0))
                        self.assert_source_contract(mut_ask, path != ('name',) and not program)
                        self.assert_bound_source_change_stales(ask, mut_ask)

                for fi, file_entry in enumerate(carried.get('files', [])):
                    for mode_kind in ('program', 'sha256', 'text', 'text_and_sha256'):
                        mut_ask = copy.deepcopy(ask)
                        mf = mut_ask['run']['carried'][ci]['files'][fi]
                        if mode_kind == 'program':
                            mf['program'] = 'mut/' + file_entry['program']
                        elif mode_kind == 'sha256':
                            mf['sha256'] = alt_sha(file_entry['sha256'])
                        elif mode_kind == 'text':
                            mf['text'] = file_entry['text'] + '# size-change\n'
                        else:
                            mf['text'] = file_entry['text'] + '# size-change\n'
                            mf['sha256'] = n.file_hash(mf['text'])
                        with self.subTest(bundle=tag, carried=ci, files=fi, field=mode_kind):
                            accepted = mode_kind == 'text_and_sha256' and not any(
                                inv['path'] == file_entry['program'] for inv in carried.get('inventory', []))
                            self.assert_source_contract(mut_ask, accepted)
                            self.assert_bound_source_change_stales(ask, mut_ask)

                for ii, inv in enumerate(carried.get('inventory', [])):
                    for key in ('path', 'sha256', 'mode', 'size'):
                        mut_ask = copy.deepcopy(ask)
                        mi = mut_ask['run']['carried'][ci]['inventory'][ii]
                        if key == 'path':
                            mi['path'] = 'mut/' + inv['path']
                        elif key == 'sha256':
                            mi['sha256'] = alt_sha(inv['sha256'])
                        elif key == 'mode':
                            mi['mode'] = '0600' if inv['mode'] != '0600' else '0644'
                        else:
                            mi['size'] = inv['size'] + 1
                        with self.subTest(bundle=tag, carried=ci, inventory=ii, field=key):
                            accepted = key == 'mode' and inv['path'] not in {r['path'] for r in carried.get('removed', [])}
                            self.assert_source_contract(mut_ask, accepted)
                            self.assert_bound_source_change_stales(ask, mut_ask)

                for ri, rem in enumerate(carried.get('removed', [])):
                    for key in ('path', 'sha256', 'mode', 'size', 'text'):
                        if key not in rem:
                            continue
                        mut_ask = copy.deepcopy(ask)
                        mr = mut_ask['run']['carried'][ci]['removed'][ri]
                        if key == 'path':
                            mr['path'] = 'mut/' + rem['path']
                        elif key == 'sha256':
                            mr['sha256'] = alt_sha(rem['sha256'])
                        elif key == 'mode':
                            mr['mode'] = '0600' if rem['mode'] != '0600' else '0644'
                        elif key == 'size':
                            mr['size'] = rem['size'] + 1
                        else:
                            mr['text'] = rem['text'] + 'mut\n'
                        with self.subTest(bundle=tag, carried=ci, removed=ri, field=key):
                            assert_refused(mut_ask, (tag, 'removed', ri, key))
                            self.assert_bound_source_change_stales(ask, mut_ask)

                for oi, old in enumerate(carried.get('old', [])):
                    for key in ('program', 'old_sha256', 'new_sha256', 'diff'):
                        if key not in old:
                            continue
                        mut_ask = copy.deepcopy(ask)
                        mo = mut_ask['run']['carried'][ci]['old'][oi]
                        if key == 'program':
                            mo['program'] = 'mut/' + old['program']
                        elif key in ('old_sha256', 'new_sha256'):
                            mo[key] = alt_sha(old[key])
                        else:
                            mo['diff'] = '--- was\n+++ is\n@@ -1,1 +1,1 @@\n-x\n+y\n'
                        with self.subTest(bundle=tag, carried=ci, old=oi, field=key):
                            assert_refused(mut_ask, (tag, 'old', oi, key))
                            self.assert_bound_source_change_stales(ask, mut_ask)



    def test_bundle_complete_escaped_bounded_and_resume_every_delivery(self):
        self.bridge.sleep = lambda _: None
        hostile = '\n```\n# Forged heading\n[forged](https://example.invalid) *fake*\n``````\n🙂\t\r\nlone CR\r\n\n'
        cases = [('group_add', group_add_proposal('Synthetic role line <&>🙂\tWEIGHTY not authority\n' * 60 + hostile.replace('\r', '') + '\U000e0001')),
                 ('group_set', group_set_proposal('\nSynthetic prompt line <&>🙂\t' * 150 + hostile.replace('\r', '').rstrip('\n'))),
                 ('source', source_proposal(extra='# ' + 'Synthetic <&>🙂\t' * 400 + '\n# ``` ``````\n# [forged](https://example.invalid)\r\n')),
                 ('pack', pack_proposal()), ('pack_replace', pack_proposal('directory_replace')), ('pack_embedded', pack_proposal('embedded'))]
        for case, ask in cases:
            with self.subTest(case=case):
                ask['question'] = '# Context heading\n[context link](https://example.invalid)'
                ask['context'].append('[context link](https://example.invalid) *emphasis* `code` <b>HTML</b> &copy;')
                self.bundle_delivery(case, ask)
        self.bundle_document_delivery()
        class Attachments(HTMLParser):
            def __init__(self):
                super().__init__()
                self.files = []

            def handle_starttag(self, tag, attrs):
                attrs = dict(attrs)
                if tag == 'a' and attrs.get('href', '').startswith('data:text/plain;base64,'):
                    self.files.append((attrs['download'], base64.b64decode(attrs['href'].split(',', 1)[1], validate=True)))
        script = Path(__file__).resolve().parents[1] / 'scripts/preview_run_asks.py'
        for flag in ('--source', '--pack', '--group-add', '--group-set', '--group-set-effort', '--repo-edit'):
            with self.subTest(preview=flag):
                result = subprocess.run([sys.executable, '-B', str(script), flag, '--dense-bundle'],
                    env=dict(HOME=self.tmp.name, PATH=self.tmp.name + ':/usr/local/bin:/usr/bin:/bin'),
                    capture_output=True, text=True, timeout=15)
                self.assertEqual(result.returncode, 0, result.stderr)
                links = Attachments()
                links.feed(result.stdout)
                self.assertEqual(len(links.files), 1)
                filename, raw = links.files[0]
                self.assertRegex(filename, r'^review-[0-9a-f]{64}\.md$')
                self.assertIn('Markdown review document', result.stdout)
                self.assertIn(hashlib.sha256(raw).hexdigest(), result.stdout)
                section = result.stdout.split('<section class="bundle">', 1)[1].split('</section>', 1)[0]
                self.assertNotIn('<button>', section)
                self.assertIn('## Canonical review', raw.decode())
                review_parts(raw.decode())

    def test_bundle_oversized_kept_grant_refuses_whole(self):
        # Decision-critical kept identities cannot be discarded to squeeze the card in.
        for factory in (source_proposal, pack_proposal):
            with self.subTest(oversized_kept_plan=factory.__name__):
                ask = factory()
                if factory is pack_proposal:
                    ask = pack_proposal('directory_replace')
                at = next(i for i, line in enumerate(ask['run']['does'])
                          if line.startswith('it would replace the source '))
                ask['run']['does'][at] = 'it would replace the source synthetic the yard has under that name; grants kept for source synthetic: ' + '<b>holder</b> ' * 500 + '.'
                self.reset_native(ask)
                self.assertFalse(self.create()['delivered'])
                self.assertEqual(self.sent_count, 0)
                self.assertEqual(self.writes(), [])

    @staticmethod
    def bundle_review(row):
        return row['bundle']['document']['text']

    def bundle_delivery(self, case, ask):
        self.reset_native(copy.deepcopy(ask))
        self.bridge.topic_settings = (dict(name='ops', agent='yardmaster'),)
        self.store.record_topic('ops', 12, 'created')
        result = self.create(topic='ops')
        self.assertTrue(result['delivered'], result)
        row = self.row()
        self.assertEqual(row['bundle']['parts'], [])
        sent = [body for url, body in self.calls if url.endswith('/sendMessage')]
        self.assertEqual(len(sent), 1)
        self.assertEqual(len(self.documents), 1)
        fields, path, raw = self.documents[-1]
        document = row['bundle']['document']
        review = self.bundle_review(row)
        facts, contents, tokens = review_parts(review)
        self.assertEqual(tokens[0].type, 'heading_open')
        self.assertEqual(tokens[0].tag, 'h1')
        self.assertGreater(sum(t.type == 'list_item_open' for t in tokens), 10)
        self.assertTrue(contents)
        headings = [tokens[i + 1].content for i, t in enumerate(tokens) if t.type == 'heading_open']
        self.assertEqual(headings[1:], ['Reading file content', 'Canonical review']
                         + (['Pack'] if 'pack' in ask['run'] else []))
        self.assertEqual(review_content(review, 'Question:'), ask['question'])
        self.assertIn('Context: ' + ask['context'][-1], facts)
        self.assertIn('Command: ' + ask['run']['command'], facts)
        self.assertIn('Bead: sample-1 · Synthetic change', facts)
        self.assertIn('Ask: 7', facts)
        self.assertIn('Asker: ' + ask['asker'], facts)
        self.assertIn('Takes it back: ' + ask['run']['back'], facts)
        self.assertIn('Back by: ' + ask['run'].get('back_by', ''), facts)
        self.assertTrue(any(child.type == 'code_inline' for token in tokens for child in (token.children or [])))
        self.assertFalse(any(t.type in ('html_block', 'html_inline', 'link_open', 'image', 'em_open', 'strong_open')
                             for token in tokens for t in [token, *(token.children or [])]))
        self.assertEqual(document['text'].encode(), raw)
        self.assertEqual(path.stat().st_mode & 0o777, 0o600)
        self.assertEqual(path.name, 'review-' + row['proposal'] + '.md')
        self.assertEqual(hashlib.sha256(raw).hexdigest(), document['sha256'])
        self.assertEqual(len(raw), document['size'])
        self.assertEqual(fields['message_thread_id'], '12')
        self.assertNotIn('reply_parameters', fields)
        self.assertNotIn('reply_markup', fields)
        self.assertEqual(fields.get('caption', ''), '')
        card, error = b.html_visible_text(sent[0]['text'])
        self.assertFalse(error)
        self.assertEqual(card, row['plain'])
        self.assertLessEqual(n.units(card), n.CAP)
        self.assertIn(document['sha256'], card)
        self.assertIn(row['proposal'], card)
        self.assertIn(path.name, card)
        self.assertIn(str(len(raw)) + ' bytes', card)
        self.assertIn('1 document', card)
        self.assertIn(str(row['bundle']['text_parts']) + ' text parts contained, 0 sent', card)
        self.assertNotIn('Synthetic long line', card)
        self.assertNotIn('FORGED CONTROL', card)
        self.assertNotIn('<b>role', sent[0]['text'])
        self.assertEqual(sent[0]['message_thread_id'], 12)
        if case == 'source':
            self.assert_source_review(ask, review)
            self.assertIn('no grant of source synthetic stands.', card)
        elif case.startswith('pack'):
            self.assertEqual([value for label, value in contents if label == 'Native Does (complete quoted lines):'], ask['run']['does'])
            for carried in ask['run']['carried']:
                self.assert_source_review(dict(run=dict(carried=[carried])), review)
            if 'digest' in ask['run']['pack']:
                self.assert_pack_document(ask, raw)
                self.assertIn(ask['run']['pack']['digest'], card)
            if case == 'pack_replace':
                self.assertIn('grants kept for source synthetic: crew member reviewer: health (kept).', card)
                for change in ask['run']['carried'][0]['old']:
                    self.assertIn(change['old_sha256'], card)
                    self.assertIn(change['new_sha256'], card)
        else:
            self.assert_group_review(ask, review)
        self.assertEqual(sent[0]['reply_parameters']['message_id'], document['message_id'])
        self.assertEqual(sent[0]['reply_markup']['inline_keyboard'][0][0]['callback_data'], f"runask:{row['id']}:approve")
        expected = [sent[0]['text']]
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
                            if row['message_id'] is not None:
                                raise RuntimeError('synthetic crash after ledger receipt')
                            save(row)
                        with patch.object(self.bridge.run_asks, 'save', side_effect=crash):
                            with self.assertRaises(RuntimeError):
                                self.create(topic='ops')
                    else:
                        result = self.create(topic='ops')
                        self.assertFalse(result['delivered'])
                        self.assertIsNone(self.row()['message_id'])
                        self.assertIsNotNone(self.row()['bundle']['document']['message_id'])
                    confirmed = self.row()['bundle']['document']['message_id']
                    documents_before = len(self.documents)
                    before = len([1 for url, _ in self.calls if url.endswith('/sendMessage')])
                    blocked[0] = False
                    self.bridge.run_asks = n.RunAsks(self.bridge)
                    retry = self.create(topic='ops')
                    if failure == 'ambiguous':
                        self.assertFalse(retry['delivered'])
                        self.assertEqual(len([1 for url, _ in self.calls if url.endswith('/sendMessage')]), before)
                        # Existing ledger reconciliation can confirm an observed message; same key resumes.
                        key = f"runask:{self.row()['id']}:send"
                        self.store.finish(key, self.bridge.receipt(True, message_id='700'))
                        self.assertTrue(self.create(topic='ops')['delivered'])
                    else:
                        self.assertTrue(retry['delivered'])
                    self.assertEqual(self.row()['bundle']['document']['message_id'], confirmed)
                    self.assertEqual(len(self.documents), documents_before)
                    sends = [body for url, body in self.calls if url.endswith('/sendMessage')]
                    complete = len(sends)
                    self.assertTrue(self.bridge.api_run_ask(dict(bead='sample-1', ask=7, key='new-repost-key', topic='ops'))['delivered'])
                    self.assertEqual(len([1 for url, _ in self.calls if url.endswith('/sendMessage')]), complete)
                    self.assertEqual(self.writes(), [])
        self.bridge.request = original

    def bundle_document_delivery(self):
        original = self.bridge.request_multipart
        failures = ('transient', 'ambiguous', 'size', 'missing-size', 'boolean-size', 'rejected', 'crash-after-receipt')
        for extension, failure, factory in ((ext, failure, factory) for factory in (pack_proposal, general_proposal, repo_edit_proposal)
                                            for ext in ('.md', '.txt') for failure in failures):
            with self.subTest(document_failure=failure, extension=extension, payload=factory.__name__):
                self.reset_native(factory())
                if extension == '.txt':
                    with patch.object(self.bridge.run_asks, 'send_card', return_value=self.bridge.not_sent()):
                        self.assertFalse(self.create()['delivered'])
                    row = self.row()
                    document = row['bundle']['document']
                    old_name = document['name']
                    document['name'] = 'review-' + row['proposal'] + '.txt'
                    for field in ('plain', 'html'):
                        row[field] = row[field].replace(old_name, document['name'])
                    self.bridge.run_asks.save(row)
                blocked = [True]
                def multipart(*args, **kwargs):
                    if blocked[0]:
                        if failure in ('transient', 'ambiguous'):
                            error = b.RequestNotSent if failure == 'transient' else b.RequestUnconfirmed
                            raise error('synthetic document interruption')
                        if failure == 'rejected':
                            return 400, dict(ok=False, error_code=400)
                    status, response = original(*args, **kwargs)
                    if blocked[0]:
                        if failure == 'size':
                            response['result']['document']['file_size'] += 1
                        elif failure == 'missing-size':
                            del response['result']['document']
                        elif failure == 'boolean-size':
                            response['result']['document']['file_size'] = True
                    return status, response
                self.bridge.request_multipart = multipart
                if failure == 'crash-after-receipt':
                    save = self.bridge.run_asks.save
                    def crash(row):
                        if row['bundle']['document']['message_id'] is not None:
                            raise RuntimeError('synthetic crash after document ledger receipt')
                        save(row)
                    with patch.object(self.bridge.run_asks, 'save', side_effect=crash):
                        with self.assertRaises(RuntimeError):
                            self.create()
                else:
                    self.assertFalse(self.create()['delivered'])
                row = self.row()
                document = row['bundle']['document']
                self.assertIsNone(row['message_id'])
                self.assertIsNone(document['message_id'])
                self.assertEqual(row['bundle']['parts'], [])
                self.assertFalse(any(body.get('reply_markup') for url, body in self.calls if url.endswith('/sendMessage')))
                before = len(self.documents)
                blocked[0] = False
                self.bridge.run_asks = n.RunAsks(self.bridge)
                result = self.create()
                if failure in ('ambiguous', 'size', 'missing-size', 'boolean-size', 'rejected'):
                    self.assertFalse(result['delivered'])
                    self.assertEqual(len(self.documents), before)
                    self.store.finish(document['key'], self.bridge.receipt(True, message_id='700'))
                    self.assertFalse(self.create()['delivered'])
                    self.assertIsNone(self.row()['message_id'])
                    self.assertEqual(len(self.documents), before)
                    self.store.finish(document['key'], self.bridge.receipt(True, message_id='700', document_size=document['size']))
                    self.assertTrue(self.create()['delivered'])
                else:
                    self.assertTrue(result['delivered'])
                count = len(self.calls)
                self.assertTrue(self.create()['delivered'])
                self.assertEqual(len(self.calls), count)
                self.assertEqual(self.row()['bundle']['document']['name'], 'review-' + row['proposal'] + extension)
                self.assertIn('review-' + row['proposal'] + extension, self.row()['plain'])
                self.assertEqual(len(self.documents), before + int(failure == 'transient'))
                self.assertEqual(self.writes(), [])
        self.bridge.request_multipart = original
        for extension in ('.md', '.txt'):
            with self.subTest(saved_plain_document=extension):
                ask = group_set_proposal()
                ask['context'].append('Synthetic previously delivered plain document ' + extension)
                self.reset_native(ask)
                with patch.object(self.bridge.run_asks, 'send_card', return_value=self.bridge.not_sent()):
                    self.assertFalse(self.create()['delivered'])
                row = self.row()
                document = row['bundle']['document']
                old = dict(document)
                value = 'Legacy plain review\n' + '\n'.join(ask['run']['does'])
                document.update(text=value, sha256=hashlib.sha256(value.encode()).hexdigest(),
                                size=len(value.encode()), name='review-' + row['proposal'] + extension)
                for field in ('plain', 'html'):
                    row[field] = (row[field].replace(old['name'], document['name'])
                                  .replace(old['sha256'], document['sha256'])
                                  .replace(str(old['size']) + ' bytes', str(document['size']) + ' bytes'))
                self.bridge.run_asks.save(row)
                self.assertTrue(self.create()['delivered'])
                saved = copy.deepcopy(self.row())
                with self.store.connect() as db:
                    ledger = db.execute('SELECT * FROM deliveries ORDER BY key').fetchall()
                before = len(self.calls)
                self.bridge.run_asks = n.RunAsks(self.bridge)
                with patch.object(n, 'review_bundle', side_effect=AssertionError('saved document regenerated')):
                    self.assertTrue(self.create()['delivered'])
                    self.assertTrue(self.bridge.api_run_ask(dict(bead='sample-1', ask=7, key='legacy-repost'))['delivered'])
                self.assertEqual(self.row(), saved)
                self.assertEqual(len(self.calls), before)
                self.assertEqual(len(self.documents), 1)
                self.assertEqual(self.documents[0][2], value.encode())
                with self.store.connect() as db:
                    self.assertEqual(db.execute('SELECT * FROM deliveries ORDER BY key').fetchall(), ledger)
        ask = pack_proposal()
        ask['context'].append('Synthetic atomic publication')
        self.reset_native(ask)
        with patch.object(n.os, 'link', side_effect=RuntimeError('synthetic crash before document publication')):
            with self.assertRaises(RuntimeError):
                self.create()
        self.assertEqual(self.sent_count, 0)
        self.bridge.run_asks = n.RunAsks(self.bridge)
        self.assertTrue(self.create()['delivered'])
        self.assertEqual(len(self.documents), 1)

        for damage in ('bytes', 'name'):
            with self.subTest(document_damage=damage):
                ask = source_proposal()
                ask['context'] = ['Synthetic document integrity ' + damage]
                self.reset_native(ask)
                with patch.object(self.bridge.run_asks, 'send_card', return_value=self.bridge.not_sent()):
                    self.assertFalse(self.create()['delivered'])
                row = self.row()
                document = row['bundle']['document']
                path = Path(self.bridge.run_asks.document_file(document))
                if damage == 'bytes':
                    path.write_bytes(b'Synthetic changed document')
                else:
                    document['name'] = '../outside.txt'
                    self.bridge.run_asks.save(row)
                result = self.create()
                self.assertFalse(result['delivered'])
                self.assertIn('document_', result['reason'])
                self.assertEqual(self.sent_count, 0)

        # Rows captured by the previous release keep their partial reply chain and ledger keys.
        for snapshot in (False, True):
            with self.subTest(legacy_snapshot=snapshot):
                self.reset_native(pack_proposal())
                with patch.object(self.bridge.run_asks, 'send_card', return_value=self.bridge.not_sent()):
                    self.assertFalse(self.create()['delivered'])
                row = self.row()
                review = self.bundle_review(row)
                parts = []
                for index, body in enumerate(n.chunks(review, 2000)):
                    plain = f'Legacy part {index + 1}\n' + body
                    parts.append(dict(plain=plain, html='<pre>' + n.escape(plain) + '</pre>',
                        sha256=n.file_hash(plain), key=f"runask:{row['id']}:bundle:{row['proposal']}:{index}", message_id=None))
                row['bundle']['parts'] = parts
                row['bundle'].pop('text_parts')
                if snapshot:
                    value = 'UNTRUSTED SOURCE PACK SNAPSHOT\nPack: synthetic\nFiles: 1\n\nPath: \"SETUP.md\"\nSize: 0\nSHA256: ' + n.file_hash('') + '\n\n\n'
                    row['bundle']['document'] = dict(text=value, sha256=n.file_hash(value), size=len(value.encode()),
                        key=f"runask:{row['id']}:bundle:{row['proposal']}:document", message_id=None)
                else:
                    row['bundle'].pop('document')
                self.bridge.run_asks.save(row)
                original_text = self.bridge.request
                def interrupted(url, body, *args, **kwargs):
                    if url.endswith('/sendMessage') and body['text'] == parts[1]['html']:
                        raise b.RequestUnconfirmed('synthetic legacy part receipt lost')
                    return original_text(url, body, *args, **kwargs)
                self.bridge.request = interrupted
                self.assertFalse(self.create()['delivered'])
                confirmed = self.row()['bundle']['parts'][0]['message_id']
                self.assertIsNotNone(confirmed)
                before = len(self.calls)
                self.bridge.run_asks = n.RunAsks(self.bridge)
                self.bridge.request = original_text
                self.assertFalse(self.create()['delivered'])
                self.assertEqual(len(self.calls), before)
                self.store.finish(parts[1]['key'], self.bridge.receipt(True, message_id='700'))
                self.assertTrue(self.create()['delivered'])
                self.assertEqual(self.row()['bundle']['parts'][0]['message_id'], confirmed)
                sends = [body for url, body in self.calls if url.endswith('/sendMessage')]
                self.assertEqual(sum(body['text'] == parts[0]['html'] for body in sends), 1)
                self.assertEqual(sum(body['text'] == parts[1]['html'] for body in sends), 0)
                self.assertEqual(len(self.documents), int(snapshot))
                if snapshot:
                    self.assertEqual(self.documents[0][1].name, n.file_hash(value) + '.txt')
                self.assertEqual(self.writes(), [])

    def assert_pack_document(self, ask, raw):
        pack = ask['run']['pack']
        review = raw.decode()
        facts, contents, _ = review_parts(review)
        self.assertIn('Pack: ' + pack['name'], facts)
        self.assertIn('Pack label: ' + pack['label'], facts)
        if 'digest' in pack:
            self.assertIn('Pack digest: ' + pack['digest'], facts)
        self.assertFalse(any(label.startswith('Path: ') for label, _ in contents))
        self.assertNotIn('## Pack snapshot', review)

    def assert_source_review(self, ask, review):
        carried = ask['run']['carried'][0]
        expected = [('Declaration', carried['text'])]
        expected += [('Command file ' + n.canonical(f['program']), f['text']) for f in carried.get('files', [])]
        expected += [('Replacement diff ' + n.canonical(c['program']), c.get('diff', '')) for c in carried.get('old', [])]
        expected += [('Standing inventory', n.canonical(carried.get('inventory', []))),
                     ('Removed entries', n.canonical(carried.get('removed', [])))]
        if 'does' in ask['run']:
            self.assertEqual([value for label, value in review_parts(review)[1]
                              if label == 'Native Does (complete quoted lines):'], ask['run']['does'])
        for label, value in expected:
            self.assertEqual(review_content(review, label).encode(), value.encode())

    def assert_group_review(self, ask, review):
        does = ask['run']['does']
        facts, contents, _ = review_parts(review)
        quoted = {line for _, value in contents for line in value.split('\n')}
        head = next((i for i, line in enumerate(does) if line.startswith('On approval the yard writes the text below')), None)
        if head is None:
            self.assertEqual(contents, [])
            self.assertTrue(any(fact.endswith('writes []') for fact in facts if fact.startswith('Manifest:')))
        else:
            # The prompt arrives whole, as the bytes its file name's digest names, before any control.
            path, digest = re.search(r'the file (\S+\.([0-9a-f]{64})\.md), read-only', does[head]).groups()
            prompt = review_content(review, 'proposed prompt ' + path)
            self.assertEqual(hashlib.sha256(prompt.encode()).hexdigest(), digest)
            self.assertEqual(prompt.removesuffix('\n').split('\n'), does[head + 1:])
            self.assertIn(f'"sha256":"{digest}"', next(fact for fact in facts if fact.startswith('Manifest:')))
            if does[1].endswith('and which holds now:'):
                standing = re.match(r'Its prompt is the file (.+?), which', does[1])[1]
                self.assertEqual(review_content(review, 'standing prompt ' + standing).split('\n'), does[2:head])
        lines = [line for at, line in enumerate(does) if head is None or not (head < at or (at < head and does[1].endswith('holds now:') and 1 < at))]
        for line in lines:
            self.assertIn(line, facts)
        # Prompt text stays in code blocks even when it reads like a sentence of the yard's.
        for line in quoted - set(lines):
            self.assertNotIn(line, facts)
        return [line for at, line in enumerate(lines) if head is None or line != does[head]]

    def test_skill_add_refuses_before_send_and_lists_terminal_only(self):
        ask = dict(proposal(), run=dict(argv=["skill", "add", '/synthetic/old'],
                                        command='yardr skill add /synthetic/old',
                                        description='Copy a skill directory',
                                        does=['skill snapshot: "queued"'],
                                        back='restore the previous skill in a terminal',
                                        back_by='only the operator'))
        self.reset_native(ask)
        result = self.create()
        self.assertFalse(result['delivered'], result)
        self.assertTrue(result['reason'].endswith(n.FALLBACK), result)
        self.assertIn('decide it in the yard', result['reason'])
        self.assertEqual(self.sent_count, 0)
        self.assertEqual(self.writes(), [])
        self.assertEqual(self.bridge.run_asks.rows(), [])
        entry = dict(bead='sample-1', bead_title='Synthetic skill', ask=7, question=ask['question'],
                     asker='proposer', turn=0, run=ask['run'])
        self.assertEqual(n.queue_kind(entry), ('terminal-only run', False))
        # A stored row from an earlier skill card refuses the same way without breaking refresh.
        row = dict(id=self.bridge.run_asks.ident('sample-1', 7), binding=self.bridge.run_asks.binding(),
                   bead='sample-1', ask=7, conversation='general', proposal='0' * 64,
                   plain='old skill card', html='old skill card', weighty=True,
                   state='open', pending=None, message_id=77, silent=False, revision=0)
        self.bridge.run_asks.save(row)
        self.bridge.run_asks.refresh()
        self.assertEqual(self.bridge.run_asks.get(row['id']), row)
        self.assertEqual(self.sent_count, 0)


    def test_skill_cards_review_whole_and_confirm_before_any_write(self):
        for name in SKILL_FIXTURES:
            with self.subTest(fixture=name):
                ask = skill_proposal(name)
                self.reset_native(copy.deepcopy(ask))
                self.assertEqual(n.queue_kind(queue_entry(ask)), ('run', True))
                self.assertTrue(self.create()['delivered'])
                row = self.row()
                # The document goes first, then the one card replying to it, after each tree is read from the yard.
                sent = [body for url, body in self.calls if url.endswith('/sendMessage')]
                self.assertEqual(len(sent), 1)
                self.assertEqual(len(self.documents), 1)
                self.assertEqual(sent[0]['reply_parameters']['message_id'], row['bundle']['document']['message_id'])
                named = sorted({word for line in ask['run']['does'] for word in SKILL_TREES if f'kill {word}' in line})
                self.assertEqual(sorted(a['name'] for m, a, _ in self.native.calls if m == 'SkillDigest'), named)
                self.assertTrue(all(c == 'tgbridge' for m, _, c in self.native.calls if m == 'SkillDigest'))
                review = self.bundle_review(row)
                facts, contents, _ = review_parts(review)
                # Every native sentence arrives whole and quoted; SKILL.md descriptions only as fenced file content.
                self.assertEqual([value for label, value in contents if label == 'Native Does (complete quoted lines):'],
                                 ask['run']['does'])
                for line in ask['run']['does']:
                    self.assertNotIn(line, facts)
                card = b.html_visible_text(sent[0]['text'])[0]
                self.assertIn(ask['run']['command'], card)
                for skill, tree in SKILL_TREES.items():
                    if skill in named:
                        self.assertIn(tree, card)
                        self.assertIn(tree, next(fact for fact in facts if fact.startswith('Manifest:')))
                for line in ask['run']['does']:
                    holder = re.fullmatch(r'The (.+) is given (.+) now, and will be given (.+)\.', line)
                    if holder:
                        lists = [value.removeprefix('the skills ') if value != 'no skills' else '(none)' for value in holder.groups()[1:]]
                        self.assertIn(f'{holder[1]}: {lists[0]} -> {lists[1]}', card)
                for item in ask.get('weight', []):
                    self.assertIn('WEIGHTY · code: ' + item['line'], card)
                if name.startswith('skill_rm'):
                    self.assertIn(n.SKILL_RM_NO_BACK, card)
                    self.assertIn('Description (untrusted SKILL.md text): "', card)
                # Routine or weighty, Approve only arms Confirm; Confirm writes once, weighed.
                self.assertTrue(row['weighty'])
                self.press()
                self.assertEqual(self.writes(), [])
                self.assertIn('Confirm ask 7 on sample-1: runs ' + ask['run']['command'], b.html_visible_text(self.edits()[-1]['text'])[0])
                self.press('confirm', uid=41)
                self.assertEqual(self.writes(), [('ApproveAsk', dict(bead='sample-1', ask=7, actor='pat', weighed=True, values={}))])
                self.assertEqual(self.row()['state'], 'approved')

        # A carried removal relays the canonical pending, then the operation and tree the server names.
        ask = skill_proposal('skill_rm_force')
        self.reset_native(copy.deepcopy(ask))
        self.assertTrue(self.create()['delivered'])
        self.native.mode = 'carrying'
        self.press()
        self.press('confirm', uid=41)
        self.assertIn('⏳ approved, to be carried out: yardr skill rm notes --force', b.html_visible_text(self.edits()[-1]['text'])[0])
        receipt = ('skill notes removed (operation card-0123456789abcdef0123456789abcdef, tree ' + SKILL_TREES['notes'] + ')\n'
                   'taken off group builders: notes,triage -> triage')
        self.native.ask['ran'] = dict(argv=ask['run']['argv'], result=receipt)
        self.bridge.run_asks = n.RunAsks(self.bridge)
        self.bridge.run_asks.refresh()
        self.assertIn('✓ approved, ran: yardr skill rm notes --force\n' + receipt, b.html_visible_text(self.edits()[-1]['text'])[0])
        failed = 'skill notes was not removed: the owner settled operation card-0123 as failed: the tree moved.'
        self.native.ask['ran'] = dict(argv=ask['run']['argv'], result=failed, failed=True)
        self.bridge.run_asks.refresh()
        self.assertIn('✕ approved, failed: yardr skill rm notes --force\n' + failed, b.html_visible_text(self.edits()[-1]['text'])[0])
        self.assertEqual(len(self.writes()), 1)

    def test_skill_cards_refuse_unknown_shapes_unserved_cores_moved_trees_and_stale_approval(self):
        def refused(ask, marker, listed=True):
            self.reset_native(copy.deepcopy(ask))
            result = self.create()
            self.assertFalse(result['delivered'], result)
            self.assertTrue(result['reason'].endswith(n.FALLBACK), result)
            self.assertIn(marker, result['reason'])
            self.assertEqual((self.sent_count, self.documents, self.writes(), self.bridge.run_asks.rows()), (0, [], [], []))
            if listed:
                self.assertEqual(n.queue_kind(queue_entry(ask)), ('terminal-only run', False))

        # Every sentence, word and field is the core's own: one changed, missing or added refuses the whole card.
        for name in SKILL_FIXTURES:
            base = skill_proposal(name)
            changes = [('extra line', lambda run: run['does'].append('An unfamiliar native sentence.'))]
            for at in range(len(base['run']['does'])):
                changes += [(f'line {at} changed', lambda run, at=at: run['does'].__setitem__(at, run['does'][at][:-1] + ';')),
                            (f'line {at} dropped', lambda run, at=at: run['does'].pop(at)),
                            (f'line {at} broken', lambda run, at=at: run['does'].__setitem__(at, run['does'][at].replace(' ', '\n', 1)))]
            changes += [('command', lambda run: run.__setitem__('command', run['command'] + ' ')),
                        ('description', lambda run: run.__setitem__('description', 'Remove a group')),
                        ('back', lambda run: run.__setitem__('back', 'yardr skill add /synthetic/old')),
                        ('back_by', lambda run: run.__setitem__('back_by', 'anyone')),
                        ('seen', lambda run: run.__setitem__('seen', {'skill notes': '0' * 64}))]
            for label, change in changes:
                with self.subTest(fixture=name, change=label):
                    ask = copy.deepcopy(base)
                    change(ask['run'])
                    # A description is SKILL.md text: any words of one line are the yard's to show, quoted.
                    line = base['run']['does'][int(label.split()[1])] if label.startswith('line') else ''
                    if (label.endswith('changed') and re.match(r'Its description: |Skill \S+: tree [0-9a-f]{64}\. ', line)
                            or label.endswith('dropped') and line.startswith('Its description: ')):
                        self.assertTrue(validate_accepts(ask))
                        continue
                    refused(ask, 'run_ask_unknown_payload' if label == 'seen' else 'invalid native skill card')
        for name, change in (('skill_rm_force', lambda ask: ask['run']['argv'].pop()),
                             ('skill_rm_force', lambda ask: ask['run']['does'].__setitem__(3, ask['run']['does'][3].replace('no skills', 'the skills notes'))),
                             ('skill_rm_force', lambda ask: ask['run']['does'].__setitem__(slice(5, 7), ask['run']['does'][3:5])),
                             ('skills_group_set', lambda ask: ask.pop('weight')),
                             ('skills_group_narrow', lambda ask: ask.__setitem__('weight', skill_proposal('skills_group_set')['weight'])),
                             ('skills_crew_set', lambda ask: ask['run']['argv'].__setitem__(3, 'skills=[spare,notes]')),
                             ('skills_group_set', lambda ask: ask['run']['argv'].__setitem__(3, 'skills=[notes,spare] effort=high'))):
            with self.subTest(fixture=name, change='argv/holders/weight'):
                ask = skill_proposal(name)
                change(ask)
                refused(ask, 'invalid native skill card')
        # The removal count is exactly "entries" (5dedfc56) or "regular files" (9677728c on); any other wording refuses the card.
        for wording in ('all 2 files of its tree.', 'all 2 regular entries of its tree.', 'all 2 entries or regular files of its tree.',
                        'all two regular files of its tree.', 'all 2 regular files of its tree; and more.'):
            with self.subTest(fixture='skill_rm_regular_files', wording=wording):
                ask = skill_proposal('skill_rm_regular_files')
                self.assertIn('all 2 regular files of its tree.', ask['run']['does'][0])
                ask['run']['does'][0] = ask['run']['does'][0].replace('all 2 regular files of its tree.', wording)
                refused(ask, 'invalid native skill card')

        # An older fork's skill rm card (no tree or holder binding) is no card of this contract.
        old = dict(proposal(), run=dict(argv=['skill', 'rm', 'notes'], command='yardr skill rm notes', description='Remove a skill',
                                        does=['skill notes: removed'], back='', back_by='only the operator'))
        refused(old, 'invalid native skill card')

        # A core without the skill topic serves no card of this family; one whose trees moved since refuses until asked again.
        ask = skill_proposal('skills_group_set')
        self.reset_native(copy.deepcopy(ask))
        self.native.mode = 'no-skill-topic'
        result = self.create()
        self.assertFalse(result['delivered'])
        self.assertIn('run_ask_skill_cards_unavailable', result['reason'])
        self.assertTrue(result['reason'].endswith(n.FALLBACK))
        self.assertEqual((self.sent_count, self.documents, self.writes()), (0, [], []))
        for skill in ('notes', 'spare', 'triage'):
            with self.subTest(moved=skill):
                self.reset_native(copy.deepcopy(ask))
                self.native.skill_trees[skill] = 'absent' if skill == 'spare' else '1' * 64
                result = self.create()
                self.assertFalse(result['delivered'])
                self.assertIn('run_ask_skill_tree_moved', result['reason'])
                self.assertEqual((self.sent_count, self.documents, self.writes()), (0, [], []))

        # Known skill administration never rides a general card, in any form.
        for words in (['skill', 'rm', 'notes'], ['skill', '--json', 'rm', 'notes', '--force'], ['group', 'set', 'g', 'skills=[notes]'],
                      ['group', '--json', 'set', 'g', '--set', 'skills=[]'], ['crew', 'set', 'c', '--unset', 'skills'],
                      ['crew', 'set', 'c', '--unset=skills'], ['group', 'set', 'g', 'effort=high', 'skills=[a]']):
            with self.subTest(general=words):
                refused(general_proposal(words), 'run_ask_skill_general_refused')
        for words in (['skill', 'add', '/synthetic/dir'], ['group', 'set', 'g', 'effort=high'], ['crew', 'set', 'c', '--unset', 'model']):
            with self.subTest(general=words):
                self.assertTrue(validate_accepts(general_proposal(words)))

        # The yard's stale refusal at approval retires the card: nothing ran, no control remains.
        for name in ('skill_rm_force', 'skills_crew_set'):
            with self.subTest(stale=name):
                self.reset_native(skill_proposal(name))
                self.assertTrue(self.create()['delivered'])
                self.native.mode, self.native.stale = 'stale', SKILL_STALE
                self.press()
                self.press('confirm', uid=41)
                self.assertEqual(len(self.writes()), 1)
                self.assertEqual(self.native.ask['state'], 'open')
                self.assertEqual(self.row()['state'], 'stale')
                self.assertEqual(self.edits()[-1]['reply_markup'], {'inline_keyboard': []})
                self.assertIn('Stale skill proposal; nothing ran.', b.html_visible_text(self.edits()[-1]['text'])[0])
                self.press(uid=42)
                self.assertEqual(len(self.writes()), 1)

    def test_group_bundle_reviews_whole_card_before_controls_and_refuses_unsupported(self):
        for name in GROUP_FIXTURES:
            with self.subTest(fixture=name):
                ask = group_proposal(name)
                self.reset_native(ask)
                self.assertTrue(self.create()['delivered'])
                row = self.row()
                sent = [body for url, body in self.calls if url.endswith('/sendMessage')]
                self.assertEqual(len(sent), 1)
                self.assertEqual(sent[0]['reply_parameters']['message_id'], row['bundle']['document']['message_id'])
                yard = self.assert_group_review(ask, self.bundle_review(row))
                card = b.html_visible_text(sent[0]['text'])[0]
                self.assertLessEqual(n.units(card), n.CAP)
                self.assertIn(ask['run']['command'], card)
                self.assertIn(f"Bundle: {row['proposal']}", card)
                self.assertNotIn('a-line-of-the-role', card)
                for line in yard:
                    self.assertIn(line, card)
                weight = ask.get('weight', [])
                self.assertEqual(row['weighty'], bool(weight))
                self.assertEqual(card.count('WEIGHTY · code: brings a program the yard will run: '), len(weight))
                # Effort alone is routine: one tap approves. Every other group card arms first, then Confirm writes.
                self.press()
                if weight:
                    self.assertEqual(self.writes(), [])
                    self.press('confirm', uid=41)
                self.assertEqual(self.writes(), [('ApproveAsk', dict(bead='sample-1', ask=7, actor='pat', weighed=bool(weight), values={}))])
                self.assertEqual(self.row()['state'], 'approved')
                self.assertNotIn('GetGroup', [method for method, _, _ in self.native.calls])

        # A card the yard wrote before the alignment refuses with the fallback before anything is sent.
        for name in GROUP_PREALIGNMENT:
            with self.subTest(prealignment=name):
                self.reset_native(group_proposal(name))
                result = self.create()
                self.assertFalse(result['delivered'])
                self.assertTrue(result['reason'].endswith(n.FALLBACK), result)
                self.assertEqual((self.sent_count, self.writes()), (0, []))

        # Its ledger row from before the upgrade: refresh carries on, and the card sends the operator to the yard.
        self.reset_native(group_set_proposal())
        self.assertTrue(self.create()['delivered'])
        self.native.ask = group_proposal('group_set_prealignment')
        self.bridge.run_asks.refresh()
        self.assertEqual(self.row()['state'], 'stale')
        self.assertEqual(self.edits()[-1]['reply_markup'], dict(inline_keyboard=[]))
        self.assertIn('Proposal changed; use Footplate or the yard cards', b.html_visible_text(self.edits()[-1]['text'])[0])
        sent = self.sent_count
        self.bridge.run_asks.refresh()
        self.press()
        self.assertEqual(self.writes(), [])
        self.assertFalse(self.create()['delivered'])
        self.assertEqual(self.sent_count, sent)

        def changed(name, path, value):
            ask = group_proposal(name)
            target = ask
            for key in path[:-1]:
                target = target[key]
            if value is DELETE:
                del target[path[-1]]
            elif value is INSERT:
                target.insert(path[-1], 'An invented sentence the bridge has never seen.')
            else:
                target[path[-1]] = value(target[path[-1]]) if callable(value) else value
            return ask

        DELETE, INSERT = object(), object()
        add, exe, put = 'group_add', 'group_add_exec', 'group_set_prompt'
        argv_add, argv_exe = group_proposal(add)['run']['argv'], group_proposal(exe)['run']['argv']
        cases = [
            # Words, runners and keys a card never shows refuse whole, not dropped.
            (add, ('run', 'argv'), argv_add[:4] + ['manual'] + argv_add[5:]),
            (add, ('run', 'argv'), argv_add[:4] + ['peer'] + argv_add[5:]),
            (add, ('run', 'argv'), argv_add + ['--set', 'env_file=/synthetic/env']),
            (add, ('run', 'argv'), argv_add[:-2] + ['--set', 'dir=/synthetic/yard'] + argv_add[-2:]),
            (exe, ('run', 'argv'), argv_exe[:5] + ['--set', 'kind=claude'] + argv_exe[5:]),
            (exe, ('run', 'argv'), argv_exe[:3] + ['--runner', 'exec']),
            (add, ('run', 'argv'), argv_add[:-4] + argv_add[-2:] + argv_add[-4:-2]),
            (add, ('run', 'argv'), argv_add[:5] + argv_add[7:]),
            (add, ('run', 'argv'), argv_add[:5] + ['--limit', '02'] + argv_add[7:]),
            (add, ('run', 'argv'), argv_add[:3] + argv_add[5:7] + argv_add[3:5] + argv_add[7:]),
            (add, ('run', 'argv'), [w.replace('prompt=/synthetic', 'prompt=synthetic') for w in argv_add]),
            (add, ('run', 'argv'), [w.replace('/roles-in/', '/x/../roles-in/') for w in argv_add]),
            (add, ('run', 'argv'), [w.replace('args=--verbose', 'args=--verbose"x') for w in argv_add]),
            (add, ('run', 'argv', 2), '../builders'),
            (put, ('run', 'argv'), ['group', 'set', 'planners']),
            (put, ('run', 'argv'), ['group', 'set', 'planners', 'model=claude-opus-5-5']),
            (put, ('run', 'argv'), group_proposal(put)['run']['argv'] + ['env_command=whoami']),
            (put, ('run', 'argv'), group_proposal(put)['run']['argv'] + ['prompt=/synthetic/new.md']),
            ('group_set_model_effort', ('run', 'argv'), ['group', 'set', 'planners', 'effort=low', 'model=claude-opus-5-5']),
            (add, ('run', 'argv', 0), 'crew'),
            # Every sentence, the weight and the back are tied to the argv and the bytes.
            (add, ('run', 'command'), 'yardr group add other'),
            (add, ('run', 'description'), 'invented'),
            (add, ('run', 'back'), 'yardr group rm other'),
            (put, ('run', 'back'), 'yardr group set planners\nyardr group rm planners'),
            (put, ('run', 'back'), 'yardr group set other --unset prompt'),
            (put, ('run', 'back_by'), 'anyone'),
            (add, ('run', 'carried'), [{}]),
            (add, ('run', 'seen'), {'group builders': 'x'}),
            (add, ('run', 'role'), 'other text'),
            (put, ('run', 'role'), ''),
            ('group_set_effort', ('run', 'role'), 'text no prompt carries'),
            (add, ('weight',), []),
            (add, ('weight', 0, 'class'), 'trust'),
            (put, ('weight', 0, 'line'), lambda line: line + ' and more'),
            ('group_set_preset', ('weight',), DELETE),
            ('group_set_effort', ('weight',), [dict({'class': 'code'}, line='brings a program the yard will run: its effort')]),
            ('group_set_effort_default', ('weight',), [dict({'class': 'code'}, line='brings a program the yard will run: its effort')]),
            (add, ('run', 'does', 0), lambda line: line.replace('limit 2', 'limit 3')),
            (add, ('run', 'does', 1), lambda line: line.replace('claude', 'codex', 1)),
            (add, ('run', 'does', 2), lambda line: line.replace('--verbose', '--other')),
            (add, ('run', 'does', 3), lambda line: line.replace('no env_file', 'env_file=/synthetic/env')),
            (add, ('run', 'does', 4), lambda line: line.replace('roles/builders.', 'roles/other.')),
            (add, ('run', 'does', 4), lambda line: line.replace('.md, read-only', '.md, writable')),
            (add, ('run', 'does', -1), 'changed prompt bytes'),
            (add, ('run', 'does', -2), lambda line: line + ' '),
            (add, ('run', 'does', -1), DELETE),
            (add, ('run', 'does', 5), DELETE),
            (add, ('run', 'does', 2), INSERT),
            (add, ('run', 'does'), lambda does: does[:4] + does[5:]),
            (add, ('run', 'does'), lambda does: does + ['It skips permissions: it runs without asking for permission.']),
            (exe, ('run', 'does', 0), lambda line: line.replace("limit 1, the runner's default", 'limit 1')),
            (exe, ('run', 'does', 1), lambda line: line.replace('"make"', '"make all"')),
            (exe, ('run', 'does', 2), 'The command runs in the depot of the bead.'),
            (exe, ('run', 'does', 3), DELETE),
            # The default limit, directories and back are the ones the approval writes.
            (exe, ('run', 'does', 0), lambda line: line.replace('limit 1,', 'limit 99,')),
            (exe, ('run', 'does', 2), 'The command runs in /synthetic/elsewhere.'),
            (exe, ('run', 'does', 3), 'Its bead worktrees are made under /synthetic/elsewhere.'),
            (exe, ('run', 'argv'), [w.replace('dir=/synthetic/yard/work', 'dir=/synthetic/yard/work/') for w in argv_exe]),
            (exe, ('run', 'argv'), [w.replace('worktrees=/synthetic/yard/work', 'worktrees=work') for w in argv_exe]),
            ('group_set_effort', ('run', 'back'), 'yardr group set drafters effort=low'),
            ('group_set_effort_default', ('run', 'back'), 'yardr group set planners --unset effort'),
            ('group_set_model_effort', ('run', 'back'), 'yardr group set planners model=claude-other --unset effort'),
            ('group_set_model_effort', ('run', 'back'), 'yardr group set planners --unset effort --unset model'),
            ('group_set_preset', ('run', 'back'), 'yardr group set drafters effort=low'),
            ('group_set_preset', ('run', 'back'), "yardr group set drafters 'args=[\"--verbose\"]'"),
            (put, ('run', 'back'), 'yardr group set planners --unset prompt'),
            # A flag the card shows as a byte that is no UTF-8 has no back the bridge can match.
            ('group_set_preset', ('run',), lambda run: dict(run, back="yardr group set drafters 'args=[\"\u00e9\"]'", does=[
                run['does'][0], 'It starts claude (Claude Code) agents with "\\xe9" now.', *run['does'][2:]])),
            (put, ('run', 'back'), 'yardr group set planners prompt=/synthetic/yard/roles/other.md'),
            (put, ('run', 'does', 0), lambda line: line.replace('nothing else of it.', 'more.')),
            (put, ('run', 'does', 1), 'It has no prompt now, it says.'),
            (put, ('run', 'does', 1), DELETE),
            (put, ('run', 'does'), lambda does: does[:1] + does[4:]),
            (put, ('run', 'does', 4), lambda line: line.replace('roles/planners.', 'roles/drafters.')),
            (put, ('run', 'does', -1), lambda line: line + '.'),
            (put, ('run', 'does'), lambda does: does + ['Each session will start at the effort max: "--effort" "max" are added after its other flags.']),
            ('group_set_preset', ('run', 'does', 2), lambda line: line.replace('the bypass preset', 'the auto preset')),
            ('group_set_preset', ('run', 'does', 2), lambda line: line.replace('"--dangerously-skip-permissions"', '"--dangerously-skip-permissions" "x')),
            ('group_set_preset', ('run', 'does', 3), DELETE),
            ('group_set_effort', ('run', 'does', 2), lambda line: line.replace('effort high', 'effort max')),
            ('group_set_effort', ('run', 'does', 1), "It has no effort now."),
            ('group_set_effort_default', ('run', 'does', 1), "It sets no effort now, so each session starts at its harness's own."),
            ('group_set_effort_default', ('run', 'does', 2), lambda line: line.replace('no flag', 'a flag')),
            ('group_set_model_effort', ('run', 'does', 2), lambda line: line.replace('claude-opus-5-5', 'claude-other')),
            ('group_set_model_effort', ('run', 'does'), lambda does: does[:1] + does[3:] + does[1:3]),
        ]
        for name, path, value in cases:
            with self.subTest(payload=name, path=path, value=value if not callable(value) and value not in (DELETE, INSERT) else 'edit'):
                self.reset_native(changed(name, path, value))
                result = self.create()
                self.assertFalse(result['delivered'])
                self.assertTrue(result['reason'].endswith(n.FALLBACK), result)
                self.assertEqual(self.sent_count, 0)
                self.assertEqual(self.writes(), [])

        # role, when it rides beside the card, is the text the card shows; no other row carries it.
        for ask in (changed(add, ('run', 'role'), n.parse_group(group_proposal(add))['prompt']['text']),
                    changed('group_set_effort', ('run', 'role'), '')):
            with self.subTest(role=ask['run']['argv'][3]):
                self.reset_native(ask)
                self.assertTrue(self.create()['delivered'])
        # A back restores what the card says stands now: flags as JSON, none in any of the forms the yard writes.
        preset = group_proposal('group_set_preset')
        for line, back in ((None, "yardr group set drafters 'args=[]'"), (None, "yardr group set drafters 'args=null'"),
                           ('It starts claude (Claude Code) agents with "--verbose" "it\'s" now.',
                            "yardr group set drafters 'args=[\"--verbose\",\"it'\\''s\"]'")):
            with self.subTest(back=back):
                ask = changed('group_set_preset', ('run', 'back'), back)
                if line:
                    ask['run']['does'][1] = line
                self.reset_native(ask)
                self.assertTrue(self.create()['delivered'])
        self.assertEqual(n.group_backs('runners', dict(cmd='make all', effort='high')),
                         {"yardr group set runners 'cmd=make all' effort=high"})
        self.assertEqual(n.group_backs('runners', dict(cmd=None, model=None)), {'yardr group set runners --unset cmd --unset model'})
        self.assertEqual(n.group_backs('runners', dict(model='5')), set())
        self.assertEqual(n.group_backs('runners', dict(args=['<b>'])), set())
        self.assertEqual(preset['run']['back'], 'yardr group set drafters --unset args')
        generic = proposal()
        generic['run']['role'] = ''
        self.reset_native(generic)
        self.assertIn('run_ask_unknown_payload', self.create()['reason'])

        # A standing prompt holding the sentences of a new one reads whole only where exactly one split does.
        does = group_set_proposal()['run']['does']
        tail = does[4:]
        sha = lambda value: hashlib.sha256(value.encode()).hexdigest()
        inner = '\n'.join(tail)
        forged = does[4].replace(re.search('[0-9a-f]{64}', does[4])[0], sha(inner))
        for standing, whole in ((tail, True), (['# Standing', forged], False)):
            with self.subTest(standing=standing[-1][:20], whole=whole):
                ask = group_set_proposal()
                ask['run']['does'] = does[:2] + standing + tail
                self.reset_native(ask)
                self.assertEqual(self.create()['delivered'], whole)
                if whole:
                    review = self.bundle_review(self.row())
                    self.assertEqual(review_content(review, 'standing prompt /synthetic/yard/roles/planner.md'), '\n'.join(tail))
                    self.assertEqual(review_content(review, 'proposed prompt ' + re.search(r'file (\S+), read-only', tail[0])[1]),
                                     '\n'.join(tail[1:]))

        # The yard's own maximum prompt is reviewed whole; one byte more refuses before any send.
        base = len('\n'.join(group_add_proposal()['run']['does'][5:]).encode()) + 1
        self.reset_native(group_add_proposal('x' * (8192 - base)))
        self.assertTrue(self.create()['delivered'])
        self.reset_native(group_add_proposal('x' * (8193 - base)))
        self.assertFalse(self.create()['delivered'])
        self.assertEqual(self.sent_count, 0)

    def exercise_general_review(self):
        words = ['skill', 'add', '/synthetic/unread.md', 'long spaced café 雪 😀 " <>& ` ' * 12, '', 'prompt=/synthetic/prompt.md']
        base = general_proposal(words)
        self.reset_native(copy.deepcopy(base))
        self.assertTrue(self.create()['delivered'])
        self.assertEqual(n.queue_kind(queue_entry(base)), ('run', True))
        self.assertEqual(len(self.documents), 1)
        review = self.documents[-1][2].decode()
        for i, word in enumerate(words, 1):
            self.assertIn(f'Word {i} · {len(word)} runes · {len(word.encode("utf-8"))} UTF-8 bytes', review)
            self.assertIn('\n' + json.dumps(word, ensure_ascii=True) + '\n', review)
        card = b.html_visible_text(self.calls[-1][1]['text'])[0]
        self.assertIn('not captured, hashed or reviewed file bytes', card)
        self.assertIn('good until 12:34:56', card)
        self.assertNotIn('exactly-once', card)
        self.press()
        self.assertEqual(self.writes(), [])
        self.assertIn('complete verbatim argv in review-', self.edits()[-1]['text'])
        self.press('confirm', uid=41)
        self.assertEqual(len(self.writes()), 1)
        self.assertTrue(self.writes()[0][1]['weighed'])
        self.bridge.run_asks = n.RunAsks(self.bridge)
        self.press(uid=42)
        self.assertEqual(len(self.writes()), 1)
        drifted = copy.deepcopy(base)
        drifted['run']['general'][3] += ' changed'
        drifted['run']['argv'][3] += ' changed'
        self.assert_bound_source_change_stales(base, drifted)
        self.reset_native(copy.deepcopy(base))
        self.assertTrue(self.create()['delivered'])
        delivered = self.sent_count
        self.assertTrue(self.bridge.api_run_ask(dict(bead='sample-1', ask=7, key='general-repost'))['delivered'])
        self.assertEqual(self.sent_count, delivered)
        self.native.ask = dict(copy.deepcopy(base), ask=8)
        collision = self.bridge.api_run_ask(dict(bead='sample-1', ask=8, key='native-1'))
        self.assertFalse(collision['delivered'])
        self.assertEqual(collision['reason'], 'idempotency_key_conflict')
        self.assertEqual(self.sent_count, delivered)

        mutations = [

            lambda a: a['run'].update(general=None), lambda a: a['run'].update(general=[]),
            lambda a: a['run'].update(general='version'), lambda a: a['run'].update(argv=['version']),
            lambda a: a['run'].update(command='yardr harmless'), lambda a: a['run'].update(description='checked'),
            lambda a: a['run'].update(back='undo'), lambda a: a['run'].update(back_by='pat'),
            lambda a: a['run'].update(carried=[]), lambda a: a['run'].update(seen={}),
            lambda a: a.update(weight=[]), lambda a: a['weight'][0].update(line='safe'),
            lambda a: a['run']['does'].__setitem__(2, 'It is good until 99:99:99.'),
            lambda a: a['run']['does'].__setitem__(4, '1: (5) other'),
            lambda a: a['run']['does'].append('new unreviewed field'),
            lambda a: a['run']['general'].__setitem__(2, '\ud800'),
        ]
        for mutate in mutations:
            changed = copy.deepcopy(base)
            mutate(changed)
            self.reset_native(changed)
            receipt = self.create()
            self.assertFalse(receipt['delivered'], receipt)
            self.assertEqual((self.sent_count, self.writes()), (0, []))
            self.assertEqual(n.queue_kind(queue_entry(changed)), ('terminal-only run', False))
        for key in ('source', 'pack', 'group', 'crew', 'skill'):
            self.reset_native(general_proposal([key, 'add', 'unknown local-only command']))
            self.assertTrue(self.create()['delivered'], key)
        self.reset_native(copy.deepcopy(base))
        with patch.object(n, 'PACK_DOCUMENT_BYTES', 1):
            self.assertFalse(self.create()['delivered'])
        self.assertEqual((self.sent_count, self.writes()), (0, []))

    def test_upstream_run_rows_show_whole_or_refuse_before_send(self):
        self.exercise_general_review()
        for name in UPSTREAM_WHOLE:
            with self.subTest(row=name):
                ask = upstream_proposal(name)
                self.reset_native(ask)
                self.assertTrue(self.create()['delivered'])
                card = b.html_visible_text(self.calls[-1][1]['text'])[0]
                self.assertIn(ask['run']['command'], card)
                self.assertNotIn(n.FALLBACK, card)
                for item in ask.get('weight', []):
                    self.assertIn('WEIGHTY · ' + item['class'] + ': ' + item['line'], card)
                self.assertEqual(n.queue_kind(queue_entry(ask)), ('run', True))
                weight = ask.get('weight', [])
                self.press()
                if weight:
                    self.assertEqual(self.writes(), [])
                    self.press('confirm', uid=41)
                self.assertEqual(self.writes(), [('ApproveAsk', dict(bead='sample-1', ask=7, actor='pat',
                                                                      weighed=bool(weight), values={}))])
                self.assertEqual(self.row()['state'], 'approved')

        for name, marker in (('device_pair', 'pair it in the yard'), ('hook_add_ask', 'requires_input')):
            with self.subTest(row=name):
                ask = upstream_proposal(name)
                self.reset_native(ask)
                result = self.create()
                self.assertFalse(result['delivered'], result)
                self.assertTrue(result['reason'].endswith(n.FALLBACK), result)
                self.assertIn(marker, result['reason'])
                self.assertEqual((self.sent_count, self.writes()), (0, []))
                self.assertEqual(self.bridge.run_asks.rows(), [])
                self.assertEqual(n.queue_kind(queue_entry(ask)), ('terminal-only run', False))

        # A run key no generic card carries refuses whole: the yard's stored
        # shape (seen, value, test, role, hook_table) never reaches this
        # bridge through Bead, and a build that started sending one refuses
        # instead of showing the card partly.
        base = upstream_proposal('depot_test')
        for key, value in (('seen', {'depot web': '0' * 64}), ('value', 'user@example.invalid'),
                           ('test', 'sh scripts/test.sh'), ('role', 'role text'),
                           ('hook_table', {'says': [], 'makes': []})):
            with self.subTest(key=key):
                ask = copy.deepcopy(base)
                ask['run'][key] = value
                self.reset_native(ask)
                result = self.create()
                self.assertFalse(result['delivered'])
                self.assertTrue(result['reason'].endswith(n.FALLBACK), result)
                self.assertIn('run_ask_unknown_payload', result['reason'])
                self.assertEqual((self.sent_count, self.writes()), (0, []))
                self.assertEqual(n.queue_kind(queue_entry(ask)), ('terminal-only run', False))

        # The declaration schema keeps refusing the new optional keys: a call's
        # run names a program the review would not count, and a signature body
        # changes webhook verification, so either would show the card partly.
        import yaml
        clean = n.source_declaration('name: s\nbase: https://example.invalid\n')
        self.assertEqual(clean, ('s', []))
        run_decl = ('name: s\nbase: https://example.invalid\ncalls:\n'
                    '  ping: { kind: read, run: [bin/report], request: { method: GET, path: /me },'
                    ' answer: { select: [], max: "1" } }\n')
        with self.assertRaises(n.NativeError) as caught:
            n.source_declaration(run_decl)
        self.assertNotIn(n.FALLBACK, str(caught.exception))
        body_decl = ('name: s\nbase: https://example.invalid\nhook:\n'
                     '  verify: { scheme: secret, signature: { header: X-Secret, body: token } }\n')
        with self.assertRaises(n.NativeError):
            n.source_declaration(body_decl)
        ask = source_proposal('replace_upstream')
        carried = ask['run']['carried'][0]
        decl = yaml.safe_load(carried['text'])
        decl['calls'] = {'ping': {'kind': 'read', 'run': ['bin/report'],
                                  'request': {'method': 'GET', 'path': '/me'},
                                  'answer': {'select': [], 'max': '1'}}}
        carried['text'] = yaml.safe_dump(decl)
        self.reset_native(ask)
        result = self.create()
        self.assertFalse(result['delivered'])
        self.assertTrue(result['reason'].endswith(n.FALLBACK), result)
        self.assertIn('invalid native source declaration', result['reason'])
        self.assertEqual((self.sent_count, self.writes()), (0, []))
        self.assertEqual(n.queue_kind(queue_entry(ask)), ('terminal-only run', False))

        # The same for a signature body: webhook verification would change,
        # so the card refuses before send and lists as terminal-only too.
        ask = source_proposal('replace_upstream')
        carried = ask['run']['carried'][0]
        decl = yaml.safe_load(carried['text'])
        decl.setdefault('hook', {}).setdefault('verify', {}).setdefault('signature', {})['body'] = 'token'
        carried['text'] = yaml.safe_dump(decl)
        self.reset_native(ask)
        result = self.create()
        self.assertFalse(result['delivered'])
        self.assertTrue(result['reason'].endswith(n.FALLBACK), result)
        self.assertIn('invalid native source declaration', result['reason'])
        self.assertEqual((self.sent_count, self.writes()), (0, []))
        self.assertEqual(n.queue_kind(queue_entry(ask)), ('terminal-only run', False))

    def test_queue_kind_matches_send_path_on_every_captured_run_card(self):
        # queue_kind() shares the send path's run validation, so /decide's
        # kind must equal whether validate() accepts the card, on every
        # captured run fixture. A new fixture is covered by discovery; the
        # two mutated declaration rows pin 1b9ea8e.1's drift class.
        base = Path(__file__).with_name('fixtures')
        names = sorted(p.stem for p in base.glob('*.json')
                       if isinstance(json.loads(p.read_text()).get('run'), dict))
        names += sorted('upstream/' + p.stem for p in (base / 'upstream').glob('*.json'))
        self.assertTrue(names)
        for name in names:
            with self.subTest(fixture=name):
                ask = json.loads((base / (name + '.json')).read_text())
                shown = n.queue_kind(queue_entry(ask))[1]
                self.assertEqual(shown, validate_accepts(ask), name)

        import yaml
        for label, mutate in (
                ('calls.<c>.run',
                 lambda decl: decl.update(calls={'ping': {'kind': 'read', 'run': ['bin/report'],
                                                          'request': {'method': 'GET', 'path': '/me'},
                                                          'answer': {'select': [], 'max': '1'}}})),
                ('signature.body',
                 lambda decl: decl.setdefault('hook', {}).setdefault('verify', {}).setdefault('signature', {}).__setitem__('body', 'token'))):
            with self.subTest(row=label):
                ask = source_proposal('replace_upstream')
                decl = yaml.safe_load(ask['run']['carried'][0]['text'])
                mutate(decl)
                ask['run']['carried'][0]['text'] = yaml.safe_dump(decl)
                self.assertFalse(validate_accepts(ask))
                self.assertEqual(n.queue_kind(queue_entry(ask)), ('terminal-only run', False))

        # A run row carrying ask-level payload refuses on the send path, so it
        # lists as terminal-only run, never under the non-run kind.
        for key in ('secret', 'setup', 'identity', 'depot_push'):
            with self.subTest(row='run+' + key):
                ask = upstream_proposal('depot_test')
                ask[key] = {'cipher': 'x'}
                self.assertFalse(validate_accepts(ask))
                self.assertEqual(n.queue_kind(queue_entry(ask)), ('terminal-only run', False))

    def test_bundle_drift_rechecks_during_transport_retry_and_repost(self):
        for factory in (group_add_proposal, group_set_proposal, source_proposal, pack_proposal, general_proposal, repo_edit_proposal):
            with self.subTest(payload=factory.__name__):
                original = self.bridge.request
                multipart = self.bridge.request_multipart
                self.bridge.sleep = lambda _: None
                for phase in ('document', 'card'):
                    for drift in ('proposal', 'operator', 'state', *(['pack-digest'] if factory is pack_proposal else [])):
                        with self.subTest(phase=phase, drift=drift):
                            self.reset_native(factory())
                            attempts = []
                            def interrupt(body):
                                attempts.append(body)
                                if drift == 'proposal':
                                    self.native.ask['context'].append('new proposal')
                                elif drift == 'pack-digest':
                                    self.native.ask['run']['pack']['digest'] = 'f' * 64
                                elif drift == 'operator':
                                    self.native.user['role'] = 'member'
                                else:
                                    self.native.ask.update(state='taken back', words='withdrawn', by='proposer')
                                raise b.RequestNotSent('not sent before drift')
                            def telegram(url, body, *args, **kwargs):
                                if phase == 'card' and url.endswith('/sendMessage'):
                                    interrupt(body)
                                return original(url, body, *args, **kwargs)
                            def document(url, fields, *args, **kwargs):
                                if phase == 'document':
                                    interrupt(fields)
                                return multipart(url, fields, *args, **kwargs)
                            self.bridge.request = telegram
                            self.bridge.request_multipart = document
                            self.assertFalse(self.create()['delivered'])
                            self.assertEqual(len(attempts), 1)
                            captured_document = self.row()['bundle']['document']['text']
                            self.bridge.run_asks = n.RunAsks(self.bridge)
                            self.assertFalse(self.bridge.api_run_ask(dict(bead='sample-1', ask=7, key='new-retry'))['delivered'])
                            self.assertEqual(len(attempts), 1)
                            self.assertEqual(self.row()['bundle']['document']['text'], captured_document)
                            self.assertIsNone(self.row()['message_id'])
                            self.assertEqual(self.writes(), [])
                            key = self.row()['bundle']['document']['key'] if phase == 'document' else f"runask:{self.row()['id']}:send"
                            self.assertTrue(b.resendable(self.store.saved_receipt(key)))
                            if drift == 'operator':
                                self.native.user['role'] = 'operator'
                                self.bridge.request = original
                                self.bridge.request_multipart = multipart
                                self.assertTrue(self.create()['delivered'])
                self.bridge.request = original
                self.bridge.request_multipart = multipart

    def test_bundle_home_replacement_cannot_reuse_identical_native_proposal(self):
        for factory in (group_add_proposal, group_set_proposal, source_proposal, pack_proposal, general_proposal, repo_edit_proposal):
            with self.subTest(payload=factory.__name__):
                self.reset_native(factory())
                self.assertTrue(self.create()['delivered'])
                self.press()
                self.home.rename(self.home.with_name('previous-' + factory.__name__))
                self.home.mkdir(mode=0o700)
                replacement = NativeSocket(self.home)
                replacement.ask = factory()
                self.addCleanup(replacement.close)
                self.press('confirm', uid=50)
                self.assertEqual(self.row()['state'], 'stale')
                self.assertEqual(self.writes(), [])
                self.assertFalse(any(method == 'ApproveAsk' for method, _, _ in replacement.calls))
                self.native = replacement

    def test_utf16_cap_and_weight_mark_trust(self):
        for source in ('Keep logs.\n\n[x]: /rm-rf-everything', '[ref]: http://evil "hidden line"'):
            for field in ('question', 'context'):
                with self.subTest(source=source, field=field):
                    self.reset_native(proposal())
                    self.native.ask[field] = [source] if field == 'context' else source
                    receipt = self.create()
                    self.assertFalse(receipt['delivered'], receipt)
                    self.assertEqual(receipt['reason'], 'invalid native card Markdown: markdown_token_unsupported' + n.FALLBACK)
                    self.assertEqual(self.calls, [])
                    self.assertEqual(self.bridge.run_asks.rows(), [])
                    self.assertEqual(self.writes(), [])
        self.reset_native(proposal())
        self.native.ask.update(question='WEI**GHTY** · forgery\nChoose **now**',
                               context=['- first\n- second\n\n[mention](tg://user?id=123) <b>literal</b>'],
                               weight=[dict(**{'class': 'outside'}, line='canonical outside action')])
        self.assertTrue(self.create()['delivered'])
        rendered = self.calls[-1][1]['text']
        self.assertIn('<b>forgery\nChoose now</b>', rendered)
        self.assertIn('• first\n• second', rendered)
        self.assertIn('mention (tg://user?id=123) &lt;b&gt;literal&lt;/b&gt;', rendered)
        self.assertNotIn('<a ', rendered)
        self.assertEqual(self.row()['plain'].count('WEIGHTY · '), 1)
        self.assertLess(rendered.index('WEIGHTY · outside:'), rendered.index('forgery'))
        self.assertEqual(b.html_visible_text(rendered), (self.row()['plain'], None))
        self.reset_native(proposal())
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
            with self.subTest(bundle_write=mode):
                self.reset_native(source_proposal())
                self.create()
                self.native.mode = mode
                self.press()
                self.assertEqual(self.writes(), [])
                self.press('confirm', uid=50)
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

        for mode in ('lost-commit', 'lost-open', 'lost-unreadable', 'refused'):
            self.reset_native(general_proposal())
            self.assertTrue(self.create()['delivered'])
            self.press()
            self.native.mode = mode
            self.press('confirm', uid=50)
            self.assertEqual(len(self.writes()), 1)
            self.bridge.run_asks = n.RunAsks(self.bridge)
            self.bridge.run_asks.refresh()
            self.press(uid=51)
            self.assertEqual(len(self.writes()), 1)
            if mode == 'lost-open':
                self.assertIn('UNKNOWN approval', self.row()['display']['footer'])
                self.assertNotIn('approve', str(self.row()['display']['keyboard']))
                row = self.row()
                delayed_cancel = dict(update_id=54, callback_query=dict(
                    id='delayed-cancel', data=f"runask:{row['id']}:cancel",
                    message=dict(message_id=row['message_id'], chat=dict(id=123, type='private'),
                                 **{'from': dict(id=999, is_bot=True)}),
                    **{'from': dict(id=123, is_bot=False)}))
                self.bridge.run_asks = n.RunAsks(self.bridge)
                telegram = self.bridge.telegram
                def queued_cancel(method, body, timeout=15):
                    if method == 'getUpdates':
                        return 200, dict(ok=True, result=[delayed_cancel])
                    return telegram(method, body, timeout)
                with patch.object(self.bridge, 'telegram', side_effect=queued_cancel):
                    self.bridge.poll_once()  # Refresh UNKNOWN before draining the old Cancel.
                    self.assertEqual(self.row()['display']['footer'], n.GENERAL_UNKNOWN)
                    answers = [body['text'] for url, body in self.calls if url.endswith('/answerCallbackQuery')]
                    self.assertEqual(answers[-1], n.GENERAL_UNKNOWN[:180])
                    self.bridge.poll_once()  # Replaying Cancel cannot clear the fence or submit again.
                self.assertTrue(self.row()['uncertain'])
                self.assertIsNone(self.row()['pending'])
                self.assertEqual(self.native.ask['state'], 'open')
                self.assertEqual([method for method, _ in self.writes()], ['ApproveAsk'])
                self.assertEqual(self.row()['display']['footer'], n.GENERAL_UNKNOWN)
                self.assertEqual(self.row()['display']['keyboard'], row['display']['keyboard'])
                self.native.mode = 'refused'
                self.press('decline', uid=52)
                self.assertTrue(self.row()['uncertain'])
                self.press(uid=53)
                self.assertEqual(len(self.writes()), 2)  # One approval and its refused decline; no second approval.
                self.assertNotIn('approve', str(self.row()['display']['keyboard']))
            self.native.mode = None

        self.reset_native(general_proposal())
        self.assertTrue(self.create()['delivered'])
        self.press()
        rpc = self.bridge.run_asks.client.call
        def crash_before_send(method, args):
            if method == 'ApproveAsk':
                raise RuntimeError('synthetic process crash after durable intent')
            return rpc(method, args)
        with patch.object(self.bridge.run_asks.client, 'call', side_effect=crash_before_send):
            with self.assertRaises(RuntimeError):
                self.press('confirm', uid=50)
        self.assertEqual(self.writes(), [])
        self.bridge.run_asks = n.RunAsks(self.bridge)
        self.bridge.run_asks.refresh()
        self.assertIn('UNKNOWN approval outcome', self.row()['display']['footer'])
        self.press(uid=51)
        self.assertEqual(self.writes(), [])

    def test_pending_settlement_long_results_and_outcome_edits(self):
        self.assertTrue(self.create_decision()['delivered'])
        remaining = 4096 - n.units(self.row()['plain'] + '\n✓ Answered: 2. Keep\n')
        words = '🙂' * (remaining // 2) + 'x' * (remaining % 2)
        self.native.ask.update(state='answered', option=2, words=words)
        self.bridge.run_asks.refresh()
        self.assertEqual(n.units(b.html_visible_text(self.edits()[-1]['text'])[0]), 4096)
        self.assertEqual(self.sent_count, 1)
        self.native.ask.update(words=words + 'x')
        self.bridge.run_asks.refresh()
        self.assertIn('\n\nCanonical outcome follows below.', self.edits()[-1]['text'])
        outcome_parts = [body['text'] for url, body in self.calls if url.endswith('/sendMessage')][1:]
        self.assertEqual(''.join(outcome_parts), '✓ Answered: 2. Keep\n' + words + 'x')
        for factory in (proposal, source_proposal):
            with self.subTest(payload=factory.__name__):
                self.reset_native(factory())
                self.create()
                initial_sends = sum(url.endswith('/sendMessage') for url, _ in self.calls)
                self.native.mode = 'carrying'
                self.press()
                if factory is not proposal:
                    self.press('confirm', uid=50)
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
                self.assertEqual(footer, '✕ approved, failed: ' + self.native.ask['run']['command'] + '\n' + result)
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
        status, answer = self.api_call(port, '/v1/siding-card', dict(depot='sample', key='no-depot'))
        self.assertEqual(status, 200)
        self.assertFalse(answer['delivered'])
        self.assertEqual(answer['reason'], 'native GetDepot refused (not-found)')
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

        self.reset_native(decision())
        transport = self.bridge.request
        delivered = []
        def lost_response(url, body, *args, **kwargs):
            status, response = transport(url, body, *args, **kwargs)
            if url.endswith('/sendMessage'):
                delivered.append(dict(response['result'], chat=dict(id=123, type='private'),
                    **{'from': dict(id=999, is_bot=True)}, text=b.html_visible_text(body['text'])[0].strip(),
                    is_topic_message=True, message_thread_id=body['message_thread_id'],
                    reply_markup=copy.deepcopy(body['reply_markup'])))
                raise b.RequestUnconfirmed('response lost after delivery', exit_code=28, phase='after-send')
            return status, response
        request = dict(bead='sample-1', ask=7, topic='ops', key='api-uncertain-decision')
        with patch.object(self.bridge, 'request', side_effect=lost_response):
            status, receipt = self.api_call(port, '/v1/ask-card', request)
        self.assertEqual(status, 200)
        self.assertFalse(receipt['delivered'])
        query = dict(id='topic-unconfirmed', data=delivered[0]['reply_markup']['inline_keyboard'][1][0]['callback_data'],
                     message=delivered[0], **{'from': dict(id=123, is_bot=False)})
        wrong = copy.deepcopy(query)
        wrong['message']['message_thread_id'] = 13
        self.bridge.inbound(dict(update_id=42, callback_query=wrong))
        self.assertEqual(self.row()['generations'], [])
        self.bridge.run_asks = n.RunAsks(self.bridge)
        self.bridge.inbound(dict(update_id=43, callback_query=query))
        self.assertEqual(self.writes(), [('AnswerAskExpected', dict(bead='sample-1', ask=7, turn=0, actor='pat', option=2, words='', weighed=False))])
        self.assertEqual(self.api_call(port, '/v1/ask-card', request), (200, receipt))
        status, closed = self.api_call(port, '/v1/ask-close', dict(ask_key='api-uncertain-decision', decision='ignored'))
        self.assertEqual((status, closed['delivered']), (200, True))
        self.assertEqual(len([url for url, _ in self.calls if url.endswith('/sendMessage')]), 1)

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
        receipt = self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='uncertain-decision'))
        self.assertFalse(receipt['delivered'])
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

        sent = next(body for url, body in self.calls if url.endswith('/sendMessage'))
        row = self.row()
        update = dict(update_id=471, callback_query=dict(id='delivered-without-response',
            data=sent['reply_markup']['inline_keyboard'][0][0]['callback_data'],
            **{'from': dict(id=123, is_bot=False)}, message=dict(message_id=77,
            chat=dict(id=123, type='private'), **{'from': dict(id=999, is_bot=True)},
            text=b.html_visible_text(sent['text'])[0].strip(), reply_markup=copy.deepcopy(sent['reply_markup']))))
        with patch.object(self.bridge, 'telegram', side_effect=fetched):
            self.bridge.poll_once()
        self.assertEqual(self.bridge.run_asks.get(row['id'])['message_id'], 77)
        self.assertEqual(len(self.writes()), 1)
        self.assertEqual(self.native.ask['state'], 'answered')
        self.assertEqual(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='uncertain-decision')), receipt)
        self.assertTrue(self.bridge.api_ask_close(dict(ask_key='uncertain-decision', decision='ignored'))['delivered'])
        self.assertEqual(len([url for url, _ in self.calls if url.endswith('/sendMessage')]), 1)

        def unconfirmed(legacy=False):
            self.reset_native(decision())
            delivered = []
            def lost_response(url, body, *args, **kwargs):
                status, response = original(url, body, *args, **kwargs)
                if url.endswith('/sendMessage'):
                    delivered.append(dict(response['result'], chat=dict(id=123, type='private'),
                        **{'from': dict(id=999, is_bot=True)}, text=b.html_visible_text(body['text'])[0].strip(),
                        reply_markup=copy.deepcopy(body['reply_markup'])))
                    raise b.RequestUnconfirmed('response lost after delivery', exit_code=28, phase='after-send')
                return status, response
            with patch.object(self.bridge, 'request', side_effect=lost_response):
                receipt = self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='uncertain-decision'))
            if legacy:
                # Pre-formatting ledgers reserved sends without the footer spacing newline.
                row = self.row()
                row.update(html=row['html'].rstrip('\n'), plain=row['plain'].rstrip('\n'))
                self.bridge.run_asks.save(row)
                key, identity = self.bridge.run_asks.send_request(row)
                with self.store.connect() as db:
                    db.execute('UPDATE deliveries SET fingerprint=? WHERE key=?',
                               (self.bridge.delivery_fingerprint(identity), key))
            self.assertFalse(receipt['delivered'])
            self.assertEqual(len(delivered), 1)
            self.assertIsNone(self.row()['message_id'])
            query = dict(id='unconfirmed', data=delivered[0]['reply_markup']['inline_keyboard'][0][0]['callback_data'],
                         message=delivered[0], **{'from': dict(id=123, is_bot=False)})
            return self.row(), query, receipt

        for case in ('operator', 'bot', 'chat', 'topic', 'forward', 'legacy-forward', 'text', 'leading-text',
                     'missing-text', 'invalid-text', 'keyboard', 'inaccessible', 'invalid-id',
                     'missing-turn', 'wrong-turn', 'other-row', 'message-collision', 'fingerprint',
                     'missing-receipt', 'refused', 'not-sent', 'unknown-receipt', 'proposal', 'role'):
            with self.subTest(unconfirmed_refusal=case):
                row, query, _ = unconfirmed()
                key = f"runask:{row['id']}:send"
                if case == 'operator':
                    query['from']['id'] = 124
                elif case == 'bot':
                    query['message']['from']['id'] = 998
                elif case == 'chat':
                    query['message']['chat']['id'] = 456
                elif case == 'topic':
                    query['message']['message_thread_id'] = 99
                elif case in ('forward', 'legacy-forward'):
                    query['message']['forward_origin' if case == 'forward' else 'forward_date'] = dict(type='user') if case == 'forward' else 123
                elif case == 'text':
                    query['message']['text'] += '\nChanged'
                elif case == 'leading-text':
                    query['message']['text'] = ' ' + query['message']['text']
                elif case == 'missing-text':
                    query['message'].pop('text')
                elif case == 'invalid-text':
                    query['message']['text'] = None
                elif case == 'keyboard':
                    query['message'].pop('reply_markup')
                elif case == 'inaccessible':
                    query['message'] = dict(message_id=77, chat=dict(id=123, type='private'), date=0)
                elif case == 'invalid-id':
                    query['message']['message_id'] = 0
                elif case == 'missing-turn':
                    query['data'] = query['data'].rsplit(':', 1)[0]
                elif case == 'wrong-turn':
                    query['data'] = query['data'].rsplit(':', 1)[0] + ':1'
                elif case == 'other-row':
                    other = copy.deepcopy(row)
                    other['id'] = 'f' * 24
                    other['ask'] = 8
                    other['plain'] = row['plain'].replace('ask #7', 'ask #8')
                    other['html'] = row['html'].replace('ask #7', 'ask #8')
                    self.bridge.run_asks.save(other)
                    key, identity = self.bridge.run_asks.send_request(other)
                    self.store.reserve(key, self.bridge.delivery_fingerprint(identity), self.bridge.receipt(
                        reason='ambiguous_delivery_requires_manual_reconciliation'))
                    query['data'] = query['data'].replace(row['id'], other['id'])
                elif case == 'message-collision':
                    other = copy.deepcopy(row)
                    other['id'] = 'f' * 24
                    other['message_id'] = query['message']['message_id']
                    other['generations'] = [dict(turn=0, message_id=other['message_id'], revision=0)]
                    self.bridge.run_asks.save(other)
                elif case == 'fingerprint':
                    with self.store.connect() as db:
                        db.execute('UPDATE deliveries SET fingerprint=? WHERE key=?', ('unrelated-send', key))
                elif case == 'missing-receipt':
                    with self.store.connect() as db:
                        db.execute('DELETE FROM deliveries WHERE key=?', (key,))
                elif case in ('refused', 'not-sent', 'unknown-receipt'):
                    replacement = self.bridge.not_sent() if case == 'not-sent' else self.bridge.receipt(
                        reason='telegram_rejected_not_sent_400' if case == 'refused' else 'unknown')
                    self.store.finish(key, replacement)
                elif case == 'proposal':
                    self.native.ask['question'] += ' Changed'
                elif case == 'role':
                    self.native.user['role'] = 'member'
                saved = self.store.saved_delivery(key)
                self.bridge.run_asks = n.RunAsks(self.bridge)
                self.bridge.inbound(dict(update_id=472, callback_query=query))
                self.assertEqual(self.bridge.run_asks.get(row['id'])['generations'], [])
                if case == 'other-row':
                    self.assertEqual(self.bridge.run_asks.get(other['id'])['generations'], [])
                self.assertEqual(self.writes(), [])
                self.assertEqual(self.store.saved_delivery(key), saved)
                if case in ('keyboard', 'inaccessible'):
                    self.assertIn('decide in Footplate or the yard', self.calls[-1][1]['text'])
                    self.assertIn('delete this Telegram card manually', self.calls[-1][1]['text'])

        for crash, legacy in (('before-binding', False), ('after-binding', False), (None, False), (None, True)):
            with self.subTest(unconfirmed_crash=crash, legacy=legacy):
                row, query, receipt = unconfirmed(legacy=legacy)
                key = f"runask:{row['id']}:send"
                saved = self.store.saved_delivery(key)
                close = self.bridge.api_ask_close(dict(ask_key='uncertain-decision', decision='ignored'))
                self.assertFalse(close['delivered'])
                self.assertIn('tap the original card to reconcile', close['reason'])
                self.assertIn('delete the card manually', close['reason'])
                if crash:
                    save = self.bridge.run_asks.save
                    def crashing_save(value):
                        if value['generations']:
                            if crash == 'after-binding':
                                save(value)
                            raise RuntimeError('simulated crash at generation save')
                        return save(value)
                    with patch.object(self.bridge.run_asks, 'save', side_effect=crashing_save):
                        with self.assertRaisesRegex(RuntimeError, 'simulated crash'):
                            self.bridge.inbound(dict(update_id=473, callback_query=query))
                    self.assertEqual(self.writes(), [])
                self.store = b.Store(self.path, 10)
                self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=original)
                self.bridge.hook_secret = None
                telegram = self.bridge.telegram
                def tapped(method, body, timeout=15):
                    if method == 'getUpdates':
                        return 200, dict(ok=True, result=[dict(update_id=474, callback_query=query)])
                    return telegram(method, body, timeout)
                with patch.object(self.bridge, 'telegram', side_effect=tapped):
                    self.bridge.poll_once()
                    self.bridge.poll_once()
                self.bridge.inbound(dict(update_id=475, callback_query=query))
                self.assertEqual(len(self.writes()), 1)
                self.assertEqual(self.native.ask['state'], 'answered')
                current = self.bridge.run_asks.get(row['id'])
                self.assertEqual(current['message_id'], query['message']['message_id'])
                self.assertEqual(len(current['generations']), 1)
                self.assertTrue(current['generations'][0]['reconciled'])
                self.assertEqual((current['plain'], current['html']), (row['plain'], row['html']))
                self.assertEqual(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='uncertain-decision')), receipt)
                self.assertEqual(self.store.saved_delivery(key), saved)
                self.assertEqual(len([url for url, _ in self.calls if url.endswith('/sendMessage')]), 1)
                duplicate = copy.deepcopy(query)
                duplicate['message']['message_id'] += 1
                self.bridge.inbound(dict(update_id=477, callback_query=duplicate))
                self.assertEqual(len(self.writes()), 1)
                self.assertEqual(self.bridge.run_asks.get(row['id'])['message_id'], query['message']['message_id'])

        for restart in (False, True):
            with self.subTest(unconfirmed_older_inaccessible_restart=restart):
                row, query, _ = unconfirmed()
                old_key = self.bridge.run_asks.send_key(row)
                old_receipt = self.store.saved_delivery(old_key)
                self.native.ask['turn'] += 1
                self.bridge.run_asks.refresh()
                current = self.row()
                self.assertEqual(current['turn'], 1)
                self.assertEqual(current['generations'], [dict(turn=1, message_id=78, revision=0)])
                new_key = self.bridge.run_asks.send_key(current)
                new_receipt = self.store.saved_delivery(new_key)
                self.assertTrue(new_receipt[1]['delivered'])
                query['message'] = dict(message_id=query['message']['message_id'],
                                        chat=dict(id=123, type='private'), date=0)
                if restart:
                    self.store = b.Store(self.path, 10)
                    self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=original)
                    self.bridge.hook_secret = None
                telegram = self.bridge.telegram
                def inaccessible(method, body, timeout=15):
                    if method == 'getUpdates':
                        return 200, dict(ok=True, result=[dict(update_id=478, callback_query=query)])
                    return telegram(method, body, timeout)
                with patch.object(self.bridge, 'telegram', side_effect=inaccessible):
                    self.bridge.poll_once()
                self.assertEqual(self.writes(), [])
                self.assertEqual(self.native.ask['state'], 'open')
                self.assertEqual(self.row(), current)
                self.assertEqual(self.store.saved_delivery(old_key), old_receipt)
                self.assertEqual(self.store.saved_delivery(new_key), new_receipt)
                self.assertEqual(len([url for url, _ in self.calls if url.endswith('/sendMessage')]), 2)
                self.assertIn('decide in Footplate or the yard', self.calls[-1][1]['text'])
                self.assertIn('delete this Telegram card manually', self.calls[-1][1]['text'])

        for state in ('answered', 'new-turn'):
            with self.subTest(unconfirmed_canonical_change=state):
                row, query, receipt = unconfirmed()
                if state == 'answered':
                    self.native.ask.update(state='answered', option=2, words='Decided in the yard', turn=1)
                    self.assertEqual(self.bridge.api_ask_card(dict(bead='sample-1', ask=7, key='uncertain-decision')), receipt)
                    close = self.bridge.api_ask_close(dict(ask_key='uncertain-decision', decision='ignored'))
                    self.assertFalse(close['delivered'])
                    self.assertIn('tap the original card', close['reason'])
                else:
                    self.native.ask['turn'] += 1
                telegram = self.bridge.telegram
                def changed(method, body, timeout=15):
                    if method == 'getUpdates':
                        return 200, dict(ok=True, result=[dict(update_id=476, callback_query=query)])
                    return telegram(method, body, timeout)
                with patch.object(self.bridge, 'telegram', side_effect=changed):
                    self.bridge.poll_once()
                current = self.bridge.run_asks.get(row['id'])
                self.assertEqual(self.writes(), [])
                old = next(g for g in current['generations'] if g['turn'] == 0)
                self.assertEqual(old['message_id'], query['message']['message_id'])
                self.assertEqual(old['display']['keyboard'], [])
                if state == 'answered':
                    self.assertIn('Decided in the yard', old['display']['footer'])
                    self.assertTrue(self.bridge.api_ask_close(dict(ask_key='uncertain-decision', decision='ignored'))['delivered'])
                else:
                    self.assertNotEqual(current['message_id'], old['message_id'])
                    self.assertIn('newer card', old['display']['footer'])

    def test_fresh_role_proposal_and_state_checks_on_confirmation(self):
        for factory in (proposal,):
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
                    self.press('confirm', uid=200 + len(self.calls))
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

    def test_native_repo_edit_requires_exact_capability_and_matches_canonical_json(self):
        self.assertEqual(
            n.repo_edit.go_json({'b': 9007199254740993, 'a': '<>&\b\f\n\r\t\x01\u2028\u2029α雪🙂"\\'}),
            r'{"a":"\u003c\u003e\u0026\b\f\n\r\t\u0001\u2028\u2029α雪🙂\"\\","b":9007199254740993}',
        )
        v2 = repo_edit_proposal(2)
        v1 = repo_edit_proposal(1)
        self.assertEqual(n.queue_kind(queue_entry(v2)), ('run', True))
        self.assertEqual(n.queue_kind(queue_entry(v1)), ('run', True))

        # Without RepoEditCapabilities, repo-edit cards refuse locally and /decide treats them
        # as terminal-only while all existing native cards continue normally.
        for bad_mode, ask_data in (
                ('no-capability-method', v2),
                ('bad-capability', v2),
                ('v1-capability', v2),
                (None, v1),
        ):
            with self.subTest(capability_mode=bad_mode, version=ask_data['run']['repo_edit']['request']['version']):
                self.native.beads = None
                self.reset_native(copy.deepcopy(ask_data))
                self.native.repo_edit_mode = bad_mode
                result = self.create()
                self.assertFalse(result['delivered'], result)
                self.assertTrue(result['reason'].endswith(n.FALLBACK), result)
                self.assertEqual((self.sent_count, len(self.documents), self.writes()), (0, 0, []))
                self.assertIsNone(self.bridge.run_asks.get(self.bridge.run_asks.ident('sample-1', 7)))
                self.native.beads = {'sample-1': dict(self.native.bead, asks=[copy.deepcopy(ask_data)])}
                try:
                    listed = self.bridge.run_asks.queue()
                    self.assertFalse(self.bridge.run_asks.showable(listed[0]))
                    notice, opened = self.bridge.run_asks.decide_open('sample-1', 7, '123')
                    self.assertIsNone(opened)
                    self.assertIn('cannot show whole', notice)
                finally:
                    self.native.beads = None

        # Existing non-repo-edit run card still sends & approves cleanly when RepoEditCapabilities is absent.
        self.reset_native(proposal())
        self.native.repo_edit_mode = 'no-capability-method'
        self.assertTrue(self.create()['delivered'])
        self.press()
        self.assertEqual(self.row()['state'], 'approved')

        # Baseline v1 core capability matches v1 fixture cleanly.
        self.reset_native(copy.deepcopy(v1))
        self.native.repo_edit_mode = 'v1-capability'
        self.assertTrue(self.create()['delivered'])
        self.assertEqual(len(self.documents), 1)
        doc_text = self.documents[0][2].decode('utf-8')
        facts_v1, _, _ = review_parts(doc_text)
        self.assertIn('Review route: version 1 capture-only (pending carrying; no write)', facts_v1)
        self.press()
        self.press('confirm', uid=41)
        self.assertEqual(self.writes()[-1][0], 'ApproveAsk')
        self.assertEqual(self.writes()[-1][1]['values'], {
            'repo_edit_version': '1',
            'repo_edit_digest': v1['run']['repo_edit']['digest'],
        })

    def test_native_repo_edit_validation_refuses_mutated_fields(self):
        base = repo_edit_proposal(2)

        def rehash(ask):
            payload = ask['run']['repo_edit']
            payload['digest'] = n.repo_edit.repo_edit_digest(payload)
            ask['run']['does'] = n.repo_edit.expected_does(payload)

        def mutate_bad_run_key(ask):
            ask['run']['extra'] = True

        def mutate_bad_digest(ask):
            ask['run']['repo_edit']['digest'] = '0' * 64

        def mutate_bad_seen(ask):
            ask['run']['seen'] = {'repo edit': '0' * 64, 'repo binding': ask['run']['repo_edit']['binding']}

        def mutate_bad_author(ask):
            ask['run']['repo_edit']['author'] = 'Other Author <other@example.invalid>'
            rehash(ask)

        def mutate_bad_weight(ask):
            ask['weight'] = []

        def mutate_bad_command(ask):
            ask['run']['command'] = 'yardr depot edit other'

        def mutate_bad_diff(ask):
            ask['run']['repo_edit']['files'][0]['diff'] = '--- wrong\n'
            rehash(ask)

        def mutate_bad_does(ask):
            ask['run']['does'][-1] = 'Forged does sentence.'

        def mutate_noop_edit(ask):
            chg = ask['run']['repo_edit']['files'][0]
            ask['run']['repo_edit']['request']['files'][0]['text'] = chg['old_text']
            chg['new_text'] = chg['old_text']
            chg['new_sha256'] = chg['old_sha256']
            rehash(ask)

        def mutate_secret_path(ask):
            ask['run']['repo_edit']['request']['files'][1]['path'] = '.env'
            ask['run']['repo_edit']['files'][1]['path'] = '.env'
            rehash(ask)

        def mutate_ext_path(ask):
            ask['run']['repo_edit']['request']['files'][1]['path'] = 'docs/run.sh'
            ask['run']['repo_edit']['files'][1]['path'] = 'docs/run.sh'
            rehash(ask)

        def mutate_huge_context(ask):
            ask['context'] = ['x' * 9000]

        def mutate_v1_with_route(ask):
            ask['run']['repo_edit']['request']['version'] = 1
            rehash(ask)

        cases = [
            ('run_key', mutate_bad_run_key),
            ('digest', mutate_bad_digest),
            ('seen', mutate_bad_seen),
            ('author', mutate_bad_author),
            ('weight', mutate_bad_weight),
            ('command', mutate_bad_command),
            ('diff', mutate_bad_diff),
            ('does', mutate_bad_does),
            ('noop', mutate_noop_edit),
            ('secret_path', mutate_secret_path),
            ('ext_path', mutate_ext_path),
            ('context_size', mutate_huge_context),
            ('v1_route', mutate_v1_with_route),
        ]
        for label, mutate in cases:
            with self.subTest(mutation=label):
                bad = copy.deepcopy(base)
                mutate(bad)
                if label != 'context_size':
                    self.assertEqual(n.queue_kind(queue_entry(bad)), ('terminal-only run', False), label)
                self.reset_native(bad)
                result = self.create()
                self.assertFalse(result['delivered'], (label, result))
                self.assertEqual((self.sent_count, len(self.documents), self.writes()), (0, 0, []))

    def test_native_repo_edit_document_card_confirm_and_status_lifecycle(self):
        v2 = repo_edit_proposal(2)
        payload = v2['run']['repo_edit']
        doc_modes = []
        orig_multipart = self.bridge.request_multipart

        def record_mode(url, fields, field, path, timeout):
            doc_modes.append(Path(path).stat().st_mode & 0o777)
            return orig_multipart(url, fields, field, path, timeout)

        self.reset_native(copy.deepcopy(v2))
        self.native.repo_edit_status['sample-2'] = {
            'state': 'pending', 'step': 'queued', 'bead': 'sample-2',
            'depot': 'synthetic', 'operation': payload['request']['operation'], 'digest': payload['digest'],
        }
        with patch.object(self.bridge, 'request_multipart', side_effect=record_mode):
            receipt = self.create()
        self.assertTrue(receipt['delivered'], receipt)
        self.assertEqual(doc_modes, [0o600])
        self.assertEqual(len(self.documents), 1)
        doc_fields, doc_path, doc_bytes = self.documents[0]
        doc_text = doc_bytes.decode('utf-8')
        review_parts(doc_text)
        self.assertEqual(review_content(doc_text, f'Old UTF-8 text "{payload["files"][0]["path"]}"'), payload['files'][0]['old_text'])
        self.assertEqual(review_content(doc_text, f'New UTF-8 text "{payload["files"][0]["path"]}"'), payload['files'][0]['new_text'])
        self.assertEqual(review_content(doc_text, f'Full diff "{payload["files"][0]["path"]}"'), payload['files'][0]['diff'])
        self.assertEqual(review_content(doc_text, f'New UTF-8 text "{payload["files"][1]["path"]}"'), payload['files'][1]['new_text'])
        for change in payload['files']:
            path = change['path']
            for side, present in (('Old', change['before']), ('New', change['after'])):
                if present:
                    self.assertEqual(review_content(doc_text, f'{side} UTF-8 text "{path}"'), change[side.lower() + '_text'])
            self.assertEqual(review_content(doc_text, f'Full diff "{path}"'), change['diff'])
            for field in ('old_blob', 'old_mode', 'new_mode', 'old_sha256', 'new_sha256'):
                if change.get(field):
                    self.assertIn(n.review_escape(field + ' ' + change[field]), doc_text)
        self.assertEqual(review_content(doc_text, 'Subject'), payload['request']['subject'])
        self.assertIn(payload['author'], doc_text)
        self.assertIn(payload['digest'], doc_text)
        self.assertIn('rollback', doc_text.lower())

        sends = [body for url, body in self.calls if url.endswith('/sendMessage')]
        self.assertEqual(len(sends), 1)
        self.assertEqual(sends[0]['reply_parameters']['message_id'], 77)
        card_text = b.html_visible_text(sends[0]['text'])[0]
        for expected in ('README.md', 'docs/new.md', 'old-note.txt', payload['digest'],
                         payload['author'], payload['request']['operation']):
            self.assertIn(expected, card_text)

        # First tap arms 60s confirm step and writes nothing.
        self.press()
        self.assertEqual(self.writes(), [])
        row = self.row()
        self.assertIn('nonce', row['pending'])
        confirm_body = self.edits()[-1]['text']
        self.assertIn('from review-' + row['proposal'] + '.md', confirm_body)

        # Wrong origin / nonce / typed reply / expired timer write nothing.
        with patch.object(self.bridge, '_relay', return_value=self.bridge.conversation) as relay:
            self.reply('7', uid=41)
            relay.assert_called_once()
        self.assertEqual(self.row()['state'], 'open')
        self.assertEqual(self.row()['pending'], row['pending'])
        self.assertEqual(self.writes(), [])
        self.press('confirm', uid=42, chat=dict(id=999, type='private'))
        self.assertEqual(self.writes(), [])
        clock = [1000]
        self.reset_native(copy.deepcopy(v2))
        with patch.object(self.bridge.run_asks, 'now', side_effect=lambda: clock[0]):
            self.create()
            self.press(uid=43)
            clock[0] += 61
            self.press('confirm', uid=44)
        self.assertEqual(self.writes(), [])
        self.assertIsNone(self.row()['pending'])

        # Confirm sends repo_edit_version and repo_edit_digest and relays carrying -> prepared -> reviewed -> landed.
        self.reset_native(copy.deepcopy(v2))
        self.native.repo_edit_status['sample-2'] = {
            'state': 'pending', 'step': 'queued', 'bead': 'sample-2',
            'depot': 'synthetic', 'operation': payload['request']['operation'], 'digest': payload['digest'],
        }
        self.create()
        self.press(uid=45)
        self.press('confirm', uid=46)
        self.assertEqual(self.writes()[-1][1]['values'], {
            'repo_edit_version': '2',
            'repo_edit_digest': payload['digest'],
        })
        self.assertTrue(self.writes()[-1][1]['weighed'])
        self.assertEqual(self.row()['state'], 'carrying')
        self.assertIn('reserved held bead sample-2', self.edits()[-1]['text'])

        self.native.ask['ran'] = dict(
            argv=v2['run']['argv'],
            result='prepared sample-2 as commit abcdef123456 on branch yard/sample-2 in worktree /tmp/wt; released to review for review',
        )
        self.native.repo_edit_status['sample-2'] = {
            'state': 'prepared', 'step': 'done', 'bead': 'sample-2',
            'depot': 'synthetic', 'operation': payload['request']['operation'], 'digest': payload['digest'],
            'head': 'abcdef123456', 'branch': 'yard/sample-2',
        }
        self.bridge.run_asks.refresh()
        self.assertIn('not landed or installed', self.edits()[-1]['text'])
        self.assertIn('branch yard/sample-2', self.edits()[-1]['text'])

        self.native.repo_edit_status['sample-2']['state'] = 'reviewed'
        self.bridge.run_asks.refresh()
        self.assertIn('in assembly (reviewed; not landed or installed)', self.edits()[-1]['text'])

        self.native.repo_edit_status['sample-2'].update(state='landed', landed='fedcba654321')
        self.bridge.run_asks.refresh()
        self.assertIn('source landed; not installed or live', self.edits()[-1]['text'])
        self.assertTrue(self.row()['native_edit_terminal'])
        calls_before = len([c for c in self.native.calls if c[0] == 'NativeEditStatus'])
        self.bridge.run_asks.refresh()
        calls_after = len([c for c in self.native.calls if c[0] == 'NativeEditStatus'])
        self.assertEqual(calls_before, calls_after)

        # Mismatched NativeEditStatus fails closed in decide_closed.
        self.native.repo_edit_status['sample-2']['digest'] = 'f' * 64
        with self.assertRaises(n.NativeError):
            self.bridge.run_asks.decide_closed('sample-1', 7)
        self.native.repo_edit_status['sample-2']['digest'] = payload['digest']
        for field, value in (('state', []), ('state', {}), ('state', 'unknown'), ('step', []), ('extra', 'future')):
            with self.subTest(status_field=field, value=value):
                original_status = copy.deepcopy(self.native.repo_edit_status['sample-2'])
                self.native.repo_edit_status['sample-2'][field] = value
                try:
                    with self.assertRaises(n.NativeError):
                        self.bridge.run_asks.decide_closed('sample-1', 7)
                finally:
                    self.native.repo_edit_status['sample-2'] = original_status
        self.assertIn('source landed; not installed or live', self.bridge.run_asks.decide_closed('sample-1', 7))

        # Stale core refusal on ApproveAsk retires the card as stale repo edit.
        self.reset_native(copy.deepcopy(v2))
        self.create()
        self.press(uid=47)
        self.native.mode = 'stale'
        self.native.stale = 'ask 7: repo edit changed; capture a new proposal, nothing ran'
        self.press('confirm', uid=48)
        self.assertEqual(self.row()['state'], 'stale')
        self.assertIn('Stale repo edit proposal; nothing ran.', self.edits()[-1]['text'])

        # /decide opens into a City topic thread too.
        self.reset_native(copy.deepcopy(v2))
        self.store.record_topic('ops', 12, 'created')
        self.native.beads = {'sample-1': dict(self.native.bead, asks=[copy.deepcopy(v2)])}
        notice, opened = self.bridge.run_asks.decide_open('sample-1', 7, '123:topic:12')
        self.assertIsNone(notice)
        self.assertEqual(opened[1], '123:topic:12')
        topic_send = [body for url, body in self.calls if url.endswith('/sendMessage')][-1]
        self.assertEqual(topic_send['message_thread_id'], 12)


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

            valid = ('# Keep comments; synthetic-secret\n' + write_settings(root).read_text().replace(
                '[yardr]', '[yardr] # native keys\nsource = "tgbridge"') + '\n[typing]\nseconds = 3\n')
            for newline in ('\n', '\r\n'):
                original = valid.replace('\n', newline).encode()
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
                loaded = b.load_settings(path)
                self.assertEqual((loaded.config['yardr_home'], loaded.config['yardr_operator']), (str(home), 'pat'))
                self.assertEqual(run().stdout.strip(), 'native settings match')
                self.assertEqual(run('--apply').returncode, 0)
                self.assertEqual(path.read_bytes(), changed)
                backup.unlink()
            conflicts = [
                ('\n[[command]]\nname = "decide"\ndescription = "Custom decision"\n'
                 'url = "http://127.0.0.1:1/custom"\n', 'command entry 1 (decide) clashes with /decide'),
                ('\n[[menu]]\ncommand = "decide"\ndescription = "Custom decision"\n', '[[menu]] /decide clashes'),
                (''.join(f'\n[[menu]]\ncommand = "custom{i}"\ndescription = "Custom entry"\n'
                         for i in range(b.MENU_MAX - len(loaded.config['menu']))), '101 menu entries'),
            ]
            conflicts.extend(('\n[[command]]\nname = "custom"\ndescription = "Custom command"\n'
                              'url = "http://127.0.0.1:1/custom"\ncallback_prefix = ' + json.dumps(prefix) + '\n',
                              'callback_prefix overlaps the bridge-owned decide: prefix')
                             for prefix in ('decide:', 'dec', 'decide:custom'))
            for extra, diagnostic in conflicts:
                path.write_text(valid + extra)
                self.assertNotIn('yardr_home', b.load_settings(path).config)
                for native in (False, True):
                    original = valid + extra
                    if native:
                        original = original.replace('[yardr] # native keys',
                            f'[yardr] # native keys\nhome = "{home}"\noperator = "pat"')
                    for args in ((), ('--apply',)):
                        with self.subTest(diagnostic=diagnostic, native=native, args=args):
                            path.write_text(original)
                            before = path.read_bytes()
                            try:
                                result = run(*args)
                                self.assertEqual(result.returncode, 1, result.stdout + result.stderr)
                                self.assertIn(diagnostic, result.stderr)
                                self.assertEqual(result.stdout, '')
                                self.assertEqual(path.read_bytes(), before)
                                self.assertFalse(backup.exists())
                            finally:
                                if backup.exists():
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
            partial = valid.replace('[yardr] # native keys', '[yardr] # native keys\nhome="~/yard"')
            path.write_text(partial)
            path.chmod(0o600)
            self.assertEqual(run('--apply').returncode, 0)
            self.assertEqual(path.read_text(), partial.replace('[yardr] # native keys',
                '[yardr] # native keys\noperator = "pat"'))
            with patch.dict(os.environ, HOME=tmp):
                self.assertEqual(b.load_settings(path).config['yardr_home'], str(home))
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
            settings.write_text(original.replace('[yardr]', f'[yardr]\nhome = "{yard}"'))
            reader = b.load_settings(settings).config
            self.assertEqual(reader['yardr_socket'], str(yard / 'yard.sock'))
            self.assertNotIn('yardr_operator', reader)
            self.assertFalse(reader['yardr_cards'])
            for extra in ('operator = "pat"',
                          'home = "' + str(yard) + '"\noperator = "invalid operator"'):
                settings.write_text(original.replace('[yardr]', '[yardr]\n' + extra))
                with self.assertRaises(b.SettingsError):
                    b.load_settings(settings)
            settings.write_text(original.replace('[yardr]', f'[yardr]\nhome = "{yard}"\noperator = "pat"'))
            loaded = b.load_settings(settings)
            self.assertEqual(loaded.config['yardr_socket'], str(yard / 'yard.sock'))
            self.assertTrue(loaded.config['yardr_cards'])
            self.assertTrue(loaded.config['yardr_cards_legacy'])
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
    def test_real_native_depot_pack_apply_once(self):
        binary = testyardr.binary()
        if binary is None:
            self.skipTest('yardr binary required for isolated native integration')
        root = Path(self.tmp.name)
        server, home, command, env = testyardr.start(root, binary)
        try:
            client, cli, _ = self.exercise_source_limit_and_trust(binary, root, home, command, env)
            checkout = root / 'checkout'
            for args in (['init', '-q', str(checkout)], ['-C', str(checkout), '-c', 'user.name=Synthetic',
                         '-c', 'user.email=synthetic@example.invalid', 'commit', '-q', '--allow-empty', '-m', 'Initial']):
                subprocess.run(['git', *args], env=env, check=True, capture_output=True)
            cli('depot', 'add', 'synthetic', str(checkout), '--base', 'HEAD')
            cli('crew', 'add', 'reviewer', '--set', 'kind=codex')
            pack = checkout / 'pack'
            (pack / 'roles').mkdir(parents=True)
            (pack / 'pack.yaml').write_text('api: 1\nname: harmless\nkind: git\ncrew:\n  reviewer:\n    role: roles/sample.md\n')
            content = 'Review synthetic examples only.\n'
            (pack / 'roles/sample.md').write_text(content)
            # The accepted base lacks a cutover helper; still reach its actual API refusal in the regression check.
            activation = getattr(self.bridge.run_asks, 'record_depot_cutover', None)
            if activation is not None:
                activation()
            updates = iter(range(50000, 51000))

            def current(number):
                return next(a for a in client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'] if a['ask'] == number)

            def poll(batch):
                telegram = self.bridge.telegram
                def fetched(method, body, timeout=15):
                    return (200, dict(ok=True, result=batch)) if method == 'getUpdates' else telegram(method, body, timeout)
                with patch.object(self.bridge, 'telegram', side_effect=fetched):
                    self.bridge.poll_once()

            def event(row, action='approve'):
                if action == 'confirm':
                    action += ':' + self.bridge.run_asks.get(row['id'])['pending']['nonce']
                return dict(update_id=next(updates), callback_query=dict(id='depot-callback',
                    data=f"runask:{row['id']}:{action}", message=dict(message_id=row['message_id'],
                        chat=dict(id=123, type='private'), **{'from': dict(id=999, is_bot=True)}),
                    **{'from': dict(id=123, is_bot=False)}))

            def press(row, action='approve'):
                poll([event(row, action)])

            def propose(extra=''):
                check = cli('pack', 'check', str(pack), '--depot', 'synthetic', '--crew', 'reviewer=reviewer')
                pin = re.search(r'sha256:([0-9a-f]{64})', check)[1]
                cli('--actor', 'proposer', 'bead', 'ask', 'sample-1', 'Import the harmless synthetic depot pack?',
                    '--to', 'pat', '--context', 'Synthetic native depot refusal; no live state',
                    '--run', f'pack apply {pack} --sha256 {pin} --depot synthetic --crew reviewer=reviewer' + extra)
                return client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]

            bindings = checkout / 'bindings'
            (bindings / 'roles').mkdir(parents=True)
            (bindings / 'roles/sample.md').write_text(content)
            (bindings / 'pack.yaml').write_text('api: 1\nname: bindings\nkind: git\ngroups:\n'
                '  checkers: {agent: roles/sample.md}\ncrew:\n  reviewer:\n    role: roles/sample.md\n')
            pin = re.search(r'sha256:([0-9a-f]{64})', cli('pack', 'check', str(bindings), '--depot', 'synthetic',
                '--crew', 'reviewer=reviewer'))[1]
            for bind in ('checkers=codex:gpt-5', 'checkers=claude:opus[1m]', 'checkers=pi:openrouter/test:free'):
                with self.subTest(native_bind_word=bind):
                    cli('--actor', 'proposer', 'bead', 'ask', 'sample-1', 'Review a synthetic native group binding?',
                        '--to', 'pat', '--context', 'Synthetic binding review only; no agent execution',
                        '--run', f'pack apply {bindings} --sha256 {pin} --depot synthetic --crew reviewer=reviewer --bind {bind}')
                    ask = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]
                    self.assertEqual(ask['run']['pack']['mappings']['binds'], [bind])
                    self.assertTrue(any(o['kind'] == 'group' and o['action'] == 'create'
                                        for o in ask['run']['pack']['plan']['objects']))
                    head = client.call('Head', [])
                    crew = client.call('GetCrew', ['reviewer'])
                    rpc = self.bridge.run_asks.client.call
                    for field in ('mapping', 'argv', 'command'):
                        with self.subTest(native_bind_mismatch=field):
                            def changed_response(method, args):
                                self.assertIn(method, n.READS, 'validation must not mutate the native store')
                                value = rpc(method, args)
                                if method == 'Bead':
                                    value = copy.deepcopy(value)
                                    target = next(a for a in value['asks'] if a['ask'] == ask['ask'])['run']
                                    if field == 'mapping':
                                        target['pack']['mappings']['binds'][0] += '-other'
                                    else:
                                        target['argv'][-1] += '-other'
                                        target['command'] = shlex.join(['yardr', *target['argv']])
                                        if field == 'command':
                                            target['argv'] = ask['run']['argv'][:]
                                return value
                            before = len(self.calls), len(self.documents)
                            with patch.object(self.bridge.run_asks.client, 'call', side_effect=changed_response):
                                receipt = self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'],
                                    key='bind-' + str(ask['ask']) + '-' + field))
                            self.assertFalse(receipt['delivered'], receipt)
                            self.assertEqual(receipt['failure_kind'], 'permanent')
                            self.assertEqual((len(self.calls), len(self.documents)), before)
                            self.assertIsNone(self.bridge.run_asks.get(self.bridge.run_asks.ident('sample-1', ask['ask'])))
                            self.assertEqual(client.call('Head', []), head)
                            self.assertEqual(current(ask['ask']), ask)
                    documents = len(self.documents)
                    receipt = self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key='bind-' + str(ask['ask'])))
                    self.assertTrue(receipt['delivered'], receipt)
                    self.assertEqual(len(self.documents), documents + 1)
                    row = next(r for r in self.bridge.run_asks.rows() if r['ask'] == ask['ask'])
                    document = row['bundle']['document']
                    self.assertEqual(document['sha256'], n.file_hash(document['text']))
                    self.assertEqual(self.calls[-1][1]['reply_parameters']['message_id'], document['message_id'])
                    self.assertIn(bind, document['text'])
                    self.assertEqual(client.call('Head', []), head)
                    self.assertEqual(current(ask['ask']), ask)
                    self.assertEqual(client.call('GetCrew', ['reviewer']), crew)
                    self.assertFalse((home / 'packs/bindings').exists())
                    self.assertFalse(any(g['name'] == 'synthetic-checkers' for g in client.call('Groups', [])))
                    client.call('TakeBackAsk', ['sample-1', ask['ask'], 'Synthetic binding review complete', 'pat'])
                    self.bridge.run_asks.refresh()
                    print('Native depot binding:', bind, 'complete document/card; mismatches refuse; no approval or execution')
            for index in range(2):
                ask = propose()
                if index == 0:
                    self.assert_source_pack_fixture(ask, root, 'depot_first')
                    self.assertTrue(ask.get('weight'))
                    native = self.bridge.run_asks.client.call
                    def missing_creation(method, args):
                        return [] if method == 'Events' else native(method, args)
                    before = len(self.calls), len(self.documents), client.call('Head', [])
                    with patch.object(self.bridge.run_asks.client, 'call', side_effect=missing_creation):
                        result = self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key='no-creation'))
                    self.assertFalse(result['delivered'], result)
                    self.assertIn('creation_unavailable', result['reason'])
                    self.assertEqual((len(self.calls), len(self.documents), client.call('Head', [])), before)
                    self.assertEqual(current(ask['ask']), ask)
                    self.assertFalse((home / 'packs/harmless').exists())
                else:
                    self.assertFalse(ask.get('weight'), 'unchanged reapply must exercise routine native approval')
                receipt = self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key='depot-' + str(index)))
                self.assertTrue(receipt['delivered'], receipt)
                row = next(r for r in self.bridge.run_asks.rows() if r['ask'] == ask['ask'])
                document = row['bundle']['document']
                self.assertEqual(document['sha256'], n.file_hash(document['text']))
                self.assertEqual(document['size'], len(document['text'].encode()))
                self.assertEqual(self.calls[-1][1]['reply_parameters']['message_id'], document['message_id'])
                self.assertIn('synthetic', row['plain'])
                self.assertIn('no one command takes it back', row['plain'])
                for line in ask['run']['does']:
                    self.assertIn(n.review_code(line), document['text'])
                if index == 0:
                    self.assertIn(n.review_code(content), document['text'])
                    self.assertFalse((home / 'packs/harmless').exists())
                press(row)
                if ask.get('weight'):
                    self.assertEqual(current(ask['ask'])['state'], 'open')
                    self.assertFalse((home / 'packs/harmless').exists())
                    press(row, 'confirm')
                deadline = time.monotonic() + 10
                while current(ask['ask']).get('ran', {}).get('result') == n.PENDING:
                    self.assertLess(time.monotonic(), deadline)
                    time.sleep(.02)
                settled = current(ask['ask'])
                self.assertEqual(settled['state'], 'answered')
                self.assertFalse(settled['ran'].get('failed'), settled['ran'])
                self.assertEqual((home / 'packs/harmless/roles/sample.md').read_bytes(), content.encode())
                self.assertEqual(client.call('GetCrew', ['reviewer'])['config']['prompt'], str(home / 'packs/harmless/roles/sample.md'))
                self.assertEqual(client.call('GetCrew', ['reviewer']).get('on', []), [])
                self.bridge.run_asks = n.RunAsks(self.bridge)
                self.bridge.run_asks.refresh()
                press(row)
                self.assertEqual(current(ask['ask']), settled)
                events = [e for e in client.call('EventsOfKind', ['pack_applied', 100])
                          if e['data'].get('approved', {}).get('ask') == ask['ask']
                          and e['data'].get('approved', {}).get('bead') == 'sample-1']
                self.assertEqual(len(events), 1)
                receipt = self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key='closed-depot-' + str(index)))
                self.assertFalse(receipt['delivered'], receipt)
                self.assertIn('not_open', receipt['reason'])
                print('Native depot:', 'weighted first install' if index == 0 else 'routine reapply',
                      'complete document; bound approval; exact role/config; one native event under restart/replay')
            original_pack = pack
            for name in ('[matrix]', 'left[', 'right]'):
                with self.subTest(native_depot_path=name):
                    pack = checkout / name
                    shutil.copytree(original_pack, pack)
                    ask = propose()
                    self.assertEqual(shlex.split(ask['run']['command']), ['yardr', *ask['run']['argv']])
                    rpc = self.bridge.run_asks.client.call
                    for field in ('command program', 'command path', 'command parse', 'argv path', 'depot', 'mapping'):
                        with self.subTest(native_command_mismatch=field):
                            def changed_response(method, args):
                                self.assertIn(method, n.READS, 'validation must not mutate the native store')
                                value = rpc(method, args)
                                if method == 'Bead':
                                    value = copy.deepcopy(value)
                                    target = next(a for a in value['asks'] if a['ask'] == ask['ask'])['run']
                                    if field == 'command program':
                                        target['command'] = 'other ' + target['command']
                                    elif field == 'command path':
                                        target['command'] = target['command'].replace(str(pack), str(pack) + '-other')
                                    elif field == 'command parse':
                                        target['command'] += " '"
                                    elif field == 'argv path':
                                        target['argv'][2] += '-other'
                                    elif field == 'depot':
                                        target['pack']['depot'] = 'other'
                                    else:
                                        target['pack']['mappings']['crew']['reviewer'] = 'other'
                                return value
                            before = len(self.calls), len(self.documents)
                            with patch.object(self.bridge.run_asks.client, 'call', side_effect=changed_response):
                                receipt = self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'],
                                    key='quoted-' + str(ask['ask']) + '-' + field.replace(' ', '-')))
                            self.assertFalse(receipt['delivered'], receipt)
                            self.assertEqual(receipt['failure_kind'], 'permanent')
                            self.assertEqual((len(self.calls), len(self.documents)), before)
                            self.assertIsNone(self.bridge.run_asks.get(self.bridge.run_asks.ident('sample-1', ask['ask'])))
                            self.assertEqual(current(ask['ask']), ask)
                    receipt = self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key='quoted-' + str(ask['ask'])))
                    self.assertTrue(receipt['delivered'], receipt)
                    row = next(r for r in self.bridge.run_asks.rows() if r['ask'] == ask['ask'])
                    self.assertIn('- Command: ` ' + ask['run']['command'] + ' `', row['bundle']['document']['text'])
                    self.assertEqual(current(ask['ask']), ask)
                    self.assertEqual((home / 'packs/harmless/roles/sample.md').read_bytes(), content.encode())
                    client.call('TakeBackAsk', ['sample-1', ask['ask'], 'Synthetic path review complete', 'pat'])
                    self.bridge.run_asks.refresh()
                    print('Native depot path:', name, 'genuine command delivered verbatim; mismatches refused without sends/writes')
            for name in ('star*', 'question?', '{braces}', "single'quote", 'double"quote', 'with spaces'):
                with self.subTest(native_proposal_limit=name):
                    pack = checkout / name
                    shutil.copytree(original_pack, pack)
                    pin = re.search(r'sha256:([0-9a-f]{64})', cli('pack', 'check', str(pack), '--depot', 'synthetic',
                        '--crew', 'reviewer=reviewer'))[1]
                    before = client.call('Bead', dict(bead='sample-1', viewer='pat'))
                    result = subprocess.run(command + ['--actor', 'proposer', 'bead', 'ask', 'sample-1',
                        'Synthetic unsupported native path', '--to', 'pat', '--run',
                        f'pack apply {pack} --sha256 {pin} --depot synthetic --crew reviewer=reviewer'],
                        env=env, cwd=root, text=True, capture_output=True, timeout=20)
                    self.assertNotEqual(result.returncode, 0, result.stdout)
                    self.assertEqual(client.call('Bead', dict(bead='sample-1', viewer='pat')), before)
                    print('Native depot proposal limit:', name, 'core refuses before creating an ask; API quoting covered synthetically')
            pack = original_pack
            # A cutover advances only from real native history, never invented refusal rows.
            ask = propose()
            receipt = self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key='depot-pre-cutover'))
            self.assertTrue(receipt['delivered'], receipt)
            row = next(r for r in self.bridge.run_asks.rows() if r['ask'] == ask['ask'])
            head = client.call('Head', [])
            self.assertEqual(self.bridge.run_asks.record_depot_cutover(), head)
            self.bridge.run_asks = n.RunAsks(self.bridge)
            self.bridge.run_asks.refresh()
            self.assertEqual(self.bridge.run_asks.get(row['id'])['state'], 'stale')
            self.assertEqual(self.bridge.run_asks.get(row['id'])['display']['keyboard'], [])
            sends = len(self.calls), len(self.documents)
            for key in ('depot-pre-cutover', 'depot-pre-cutover-new-key'):
                receipt = self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key=key))
                self.assertFalse(receipt['delivered'], receipt)
                self.assertIn('before_activation', receipt['reason'])
            with self.assertRaisesRegex(n.NativeError, 'before_activation'):
                self.bridge.run_asks.decide_open('sample-1', ask['ask'], '123')
            self.assertEqual((len(self.calls), len(self.documents)), sends)
            self.assertEqual(current(ask['ask']), ask)
            press(row)
            self.assertEqual(current(ask['ask']), ask)
            self.assertEqual((home / 'packs/harmless/roles/sample.md').read_bytes(), content.encode())
            client.call('TakeBackAsk', ['sample-1', ask['ask'], 'Synthetic withdrawal', 'pat'])
            receipt = self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key='withdrawn-depot'))
            self.assertFalse(receipt['delivered'], receipt)
            self.assertIn('not_open', receipt['reason'])
            self.assertEqual(current(ask['ask'])['state'], 'taken back')
            cli('bead', 'create', 'Synthetic fresh cross-bead depot ask', '--id', 'fresh-1')
            check = cli('pack', 'check', str(pack), '--depot', 'synthetic', '--crew', 'reviewer=reviewer')
            pin = re.search(r'sha256:([0-9a-f]{64})', check)[1]
            cli('--actor', 'proposer', 'bead', 'ask', 'fresh-1', 'Review a fresh post-activation pack?',
                '--to', 'pat', '--context', 'Synthetic cross-bead native creation event',
                '--run', f'pack apply {pack} --sha256 {pin} --depot synthetic --crew reviewer=reviewer')
            fresh = client.call('Bead', dict(bead='fresh-1', viewer='pat'))['asks'][0]
            # Both selected cores use one global note counter, even on a new bead.
            self.assertGreater(fresh['ask'], ask['ask'])
            created = next(e for e in client.call('Events', ['fresh-1', 64])
                           if e['kind'] == 'noted' and e.get('data', {}).get('note') == fresh['ask'])
            self.assertGreater(created['seq'], head)
            result = self.bridge.api_run_ask(dict(bead='fresh-1', ask=fresh['ask'], key='fresh-other-bead'))
            self.assertTrue(result['delivered'], result)
            self.assertEqual(client.call('Bead', dict(bead='fresh-1', viewer='pat'))['asks'], [fresh])
            self.assertEqual((home / 'packs/harmless/roles/sample.md').read_bytes(), content.encode())
            client.call('TakeBackAsk', ['fresh-1', fresh['ask'], 'Synthetic review complete', 'pat'])
            self.bridge.run_asks.refresh()
            print('Native depot cutover: global event Head; old/new keys and /decide refuse earlier asks; '
                  'fresh other-bead creation event delivers; missing event refuses without sends/writes; no execution')
            self.exercise_depot_boundaries(home, client, cli, pack, propose, poll, event, current, env)
            self.exercise_depot_objects(home, client, cli, pack.parent, poll, event)
        finally:
            testyardr.stop(server)

    def exercise_depot_boundaries(self, home, client, cli, pack, propose, poll, event, current, env):
        clock = [time.time()]
        role = home / 'packs/harmless/roles/sample.md'
        original_role = role.read_bytes()

        def restart():
            self.bridge.run_asks = n.RunAsks(self.bridge, now=lambda: clock[0])

        def saved(row):
            return self.bridge.run_asks.get(row['id'])

        def events(ask):
            return [e for e in client.call('EventsOfKind', ['pack_applied', 1000])
                    if e['data'].get('approved', {}).get('ask') == ask['ask']
                    and e['data'].get('approved', {}).get('bead') == 'sample-1']

        def publish(ask, key=None):
            key = key or 'boundary-' + str(ask['ask'])
            result = self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key=key))
            self.assertTrue(result['delivered'], result)
            return next(r for r in self.bridge.run_asks.rows() if r['ask'] == ask['ask'])

        def make(weighted=True):
            ask = propose(' --bind crew:reviewer=codex' if weighted else '')
            self.assertEqual(bool(ask.get('weight')), weighted)
            return publish(ask), ask

        def untouched(ask):
            self.assertEqual(current(ask['ask']), ask)
            self.assertEqual(events(ask), [])
            self.assertEqual(role.read_bytes(), original_role)

        def settled_once(row, ask, failed=False):
            deadline = time.monotonic() + 10
            while current(ask['ask']).get('ran', {}).get('result') == n.PENDING:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.02)
            value = current(ask['ask'])
            self.assertEqual(value['state'], 'answered')
            self.assertEqual(bool(value['ran'].get('failed')), failed, value)
            poll([])
            self.assertEqual(saved(row)['state'], 'failed' if failed else 'approved')
            before = copy.deepcopy(value)
            restart()
            poll([event(row)])
            self.assertEqual(current(ask['ask']), before)
            self.assertEqual(len(events(ask)), 0 if failed else 1)

        restart()
        ask = propose()
        rpc = self.bridge.run_asks.client.call
        for label in ('missing plan', 'unknown object', 'mismatched mapping', 'wrong recipient'):
            with self.subTest(depot_native_response=label):
                def changed_response(method, args):
                    value = rpc(method, args)
                    if method == 'Bead':
                        value = copy.deepcopy(value)
                        target = next(a for a in value['asks'] if a['ask'] == ask['ask'])
                        if label == 'missing plan':
                            target['run']['pack'].pop('plan')
                        elif label == 'unknown object':
                            target['run']['pack']['plan']['objects'][0]['unknown'] = True
                        elif label == 'mismatched mapping':
                            target['run']['pack']['mappings']['crew']['reviewer'] = 'other'
                        else:
                            target['to'] = 'proposer'
                    return value
                before = len(self.calls), len(self.documents)
                with patch.object(self.bridge.run_asks.client, 'call', side_effect=changed_response):
                    result = self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key='wire-' + label.replace(' ', '-')))
                self.assertFalse(result['delivered'], result)
                self.assertEqual((len(self.calls), len(self.documents)), before)
                self.assertIsNone(self.bridge.run_asks.get(self.bridge.run_asks.ident('sample-1', ask['ask'])))
                untouched(ask)
        try:
            role.write_text('Synthetic hand edit, not approved.\n')
            ask = propose(' --yes')
            self.assertTrue(ask['run']['pack'].get('yes'))
            before = len(self.calls), len(self.documents)
            result = self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key='native-edited-overwrite'))
            self.assertFalse(result['delivered'], result)
            self.assertEqual((len(self.calls), len(self.documents)), before)
            self.assertEqual(role.read_text(), 'Synthetic hand edit, not approved.\n')
            self.assertEqual(events(ask), [])
            self.assertEqual(current(ask['ask']), ask)
        finally:
            role.write_bytes(original_role)
        ask = propose()
        ident = self.bridge.run_asks.ident('sample-1', ask['ask'])
        key = 'interrupted-depot-reservation'
        with self.store.connect() as db:
            db.execute("CREATE TRIGGER interrupt_depot_snapshot BEFORE INSERT ON run_asks "
                       f"WHEN NEW.id='{ident}' BEGIN SELECT RAISE(ABORT, 'synthetic snapshot interruption'); END")
        try:
            with self.assertRaises(b.sqlite3.IntegrityError):
                self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key=key))
        finally:
            with self.store.connect() as db:
                db.execute('DROP TRIGGER interrupt_depot_snapshot')
        with self.subTest(depot_reservation='atomic snapshot and key'):
            with self.store.connect() as db:
                self.assertIsNone(db.execute('SELECT id FROM run_ask_keys WHERE key=?', (key,)).fetchone())
                self.assertIsNone(db.execute('SELECT id FROM run_asks WHERE id=?', (ident,)).fetchone())
        untouched(ask)
        restart()
        row = publish(ask, key)
        poll([event(row)])
        settled_once(row, ask)

        for crash_point in ('document', 'card'):
            with self.subTest(depot_save_crash=crash_point):
                ask = propose()
                documents, sends = len(self.documents), len([u for u, _ in self.calls if u.endswith('/sendMessage')])
                save = self.bridge.run_asks.save
                def crash(value):
                    if value['ask'] == ask['ask'] and ((crash_point == 'document' and value.get('bundle', {}).get('document', {}).get('message_id'))
                                                     or (crash_point == 'card' and value.get('message_id'))):
                        raise RuntimeError('synthetic crash after confirmed delivery')
                    save(value)
                with patch.object(self.bridge.run_asks, 'save', side_effect=crash):
                    with self.assertRaisesRegex(RuntimeError, 'confirmed delivery'):
                        publish(ask)
                row = next(r for r in self.bridge.run_asks.rows() if r['ask'] == ask['ask'])
                snapshot = copy.deepcopy(row['bundle']['document'])
                restart()
                row = publish(ask)
                self.assertEqual(len(self.documents), documents + 1)
                self.assertEqual(len([u for u, _ in self.calls if u.endswith('/sendMessage')]), sends + 1)
                for field in ('key', 'text', 'sha256', 'size', 'name'):
                    self.assertEqual(row['bundle']['document'][field], snapshot[field])
                untouched(ask)

        for failure in ('ambiguous', 'missing-size'):
            with self.subTest(depot_document=failure):
                ask = propose()
                original = self.bridge.request_multipart
                before = len(self.documents)
                def interrupted(*args, **kwargs):
                    status, response = original(*args, **kwargs)
                    if failure == 'ambiguous':
                        raise b.RequestUnconfirmed('synthetic lost document response')
                    response['result'].pop('document')
                    return status, response
                with patch.object(self.bridge, 'request_multipart', side_effect=interrupted):
                    result = self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key='interrupted-' + str(ask['ask'])))
                self.assertFalse(result['delivered'], result)
                row = next(r for r in self.bridge.run_asks.rows() if r['ask'] == ask['ask'])
                self.assertIsNone(row['message_id'])
                snapshot = copy.deepcopy(row['bundle']['document'])
                restart()
                for key in ('interrupted-' + str(ask['ask']), 'new-interrupted-' + str(ask['ask'])):
                    result = self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key=key))
                    self.assertFalse(result['delivered'], result)
                self.assertEqual(len(self.documents), before + 1)
                self.assertEqual(saved(row)['bundle']['document'], snapshot)
                untouched(ask)

        row, ask = make()
        for origin in ('user', 'bot', 'chat', 'topic', 'message', 'forward', 'nonce'):
            with self.subTest(depot_origin=origin):
                update = event(row)
                query = update['callback_query']
                if origin == 'user':
                    query['from']['id'] = 124
                elif origin == 'bot':
                    query['message']['from']['id'] = 998
                elif origin == 'chat':
                    query['message']['chat']['id'] = 124
                elif origin == 'topic':
                    query['message']['message_thread_id'] = 12
                    query['message']['is_topic_message'] = True
                elif origin == 'message':
                    query['message']['message_id'] += 1
                elif origin == 'forward':
                    query['message']['forward_origin'] = dict(type='hidden_user', sender_user_name='Synthetic')
                else:
                    query['data'] = f"runask:{row['id']}:confirm:" + '0' * 16
                poll([update])
                self.assertIsNone(saved(row)['pending'])
                untouched(ask)
        poll([event(row)])
        nonce = saved(row)['pending']['nonce']
        clock[0] += 61
        poll([event(row, 'confirm:' + nonce)])
        self.assertIsNone(saved(row)['pending'])
        untouched(ask)

        for point in ('arm-before-save', 'arm-after-save', 'consumed', 'native-committed', 'lost-native-reply'):
            with self.subTest(depot_arm_crash=point):
                row, ask = make()
                if point == 'arm-before-save':
                    save = self.bridge.run_asks.save
                    def crash(value):
                        if value['id'] == row['id'] and value.get('pending'):
                            raise RuntimeError('synthetic arm save interruption')
                        save(value)
                    with patch.object(self.bridge.run_asks, 'save', side_effect=crash):
                        with self.assertRaises(RuntimeError):
                            poll([event(row)])
                    restart()
                    self.assertIsNone(saved(row)['pending'])
                    untouched(ask)
                elif point == 'arm-after-save':
                    display = self.bridge.run_asks.display
                    def interrupted(value, *args, **kwargs):
                        if value['id'] == row['id'] and value.get('pending'):
                            raise RuntimeError('synthetic arm edit interruption')
                        return display(value, *args, **kwargs)
                    with patch.object(self.bridge.run_asks, 'display', side_effect=interrupted):
                        with self.assertRaises(RuntimeError):
                            poll([event(row)])
                    arm = copy.deepcopy(saved(row)['pending'])
                    restart()
                    poll([event(row)])
                    self.assertEqual(saved(row)['pending'], arm)
                    untouched(ask)
                poll([event(row)])
                confirm = event(row, 'confirm')
                if point == 'consumed':
                    with patch.object(self.bridge.run_asks, 'decide', side_effect=RuntimeError('synthetic pre-native interruption')):
                        with self.assertRaises(RuntimeError):
                            poll([confirm])
                    restart()
                    poll([confirm])
                    self.assertIsNone(saved(row)['pending'])
                    untouched(ask)
                    poll([event(row)])
                    confirm = event(row, 'confirm')
                if point == 'native-committed':
                    settle = self.bridge.run_asks.settle
                    def interrupted(value, canonical):
                        if value['id'] == row['id'] and canonical['state'] != 'open':
                            raise RuntimeError('synthetic post-native interruption')
                        return settle(value, canonical)
                    with patch.object(self.bridge.run_asks, 'settle', side_effect=interrupted):
                        with self.assertRaises(RuntimeError):
                            poll([confirm])
                    restart()
                elif point == 'lost-native-reply':
                    native = self.bridge.run_asks.client.call
                    def lost(method, args):
                        result = native(method, args)
                        if method == 'ApproveAsk':
                            raise n.RPCAmbiguous('synthetic lost native reply')
                        return result
                    with patch.object(self.bridge.run_asks.client, 'call', side_effect=lost):
                        poll([confirm])
                else:
                    poll([confirm])
                settled_once(row, ask)

        for state in ('pack-digest', 'standing-file', 'mapped-crew'):
            with self.subTest(depot_stale=state):
                row, ask = make()
                poll([event(row)])
                if state == 'pack-digest':
                    path = pack / 'roles/sample.md'
                    before = path.read_bytes()
                    path.write_bytes(before + b'Synthetic changed pack.\n')
                elif state == 'standing-file':
                    path, before = role, original_role
                    role.write_bytes(original_role + b'Synthetic standing edit.\n')
                else:
                    path, before = None, None
                    cli('crew', 'set', 'reviewer', 'model=synthetic-boundary')
                try:
                    poll([event(row, 'confirm')])
                    self.assertEqual(current(ask['ask'])['state'], 'open')
                    self.assertNotIn('ran', current(ask['ask']))
                    self.assertEqual(events(ask), [])
                    self.assertEqual(saved(row)['state'], 'stale')
                    self.assertEqual(saved(row)['display']['keyboard'], [])
                finally:
                    if path is not None:
                        path.write_bytes(before)
        for damage in ('file', 'captured-text', 'document-name'):
            with self.subTest(depot_snapshot_damage=damage):
                row, ask = make()
                document = row['bundle']['document']
                path = Path(self.bridge.run_asks.document_file(document))
                before = path.read_bytes()
                if damage == 'file':
                    path.write_bytes(b'Synthetic edited review document')
                elif damage == 'captured-text':
                    document['text'] = document['text'].replace('Canonical review', 'Synthetic review', 1)
                    self.bridge.run_asks.save(row)
                else:
                    document['name'] = 'review-' + '0' * 64 + '.md'
                    self.bridge.run_asks.save(row)
                try:
                    poll([event(row)])
                    if saved(row)['pending']:
                        poll([event(row, 'confirm')])
                    untouched(ask)
                    self.assertEqual(saved(row)['state'], 'stale')
                    self.assertEqual(saved(row)['display']['keyboard'], [])
                finally:
                    path.write_bytes(before)
        for binding in ('yardr_source', 'yardr_home', 'identity'):
            with self.subTest(depot_binding=binding):
                row, ask = make()
                poll([event(row)])
                original = self.config.get(binding)
                if binding == 'identity':
                    cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '124')
                elif binding == 'yardr_home':
                    other = home.parent / 'other-native-home'
                    other.mkdir(mode=0o700, exist_ok=True)
                    self.config[binding] = str(other)
                else:
                    self.config[binding] = 'other-source'
                try:
                    poll([event(row, 'confirm')])
                    untouched(ask)
                finally:
                    if binding == 'identity':
                        cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
                    else:
                        self.config[binding] = original
                restart()
                poll([event(row, 'cancel')])

        proposed_role = pack / 'roles/sample.md'
        proposed_role.write_bytes(original_role + b'Synthetic failed carry.\n')
        row, ask = make()
        poll([event(row)])
        native = self.bridge.run_asks.client.call
        approved = []
        def capture_pending(method, args):
            result = native(method, args)
            if method == 'ApproveAsk':
                approved.append(result)
            return result
        mode = role.parent.stat().st_mode & 0o777
        role.parent.chmod(0o555)
        try:
            with patch.object(self.bridge.run_asks.client, 'call', side_effect=capture_pending):
                poll([event(row, 'confirm')])
            settled_once(row, ask, failed=True)
            self.assertEqual([a['result'] for a in approved], [n.PENDING])
            self.assertEqual(role.read_bytes(), original_role)
            self.assertIn('approved, failed', saved(row)['display']['footer'])
        finally:
            role.parent.chmod(mode)
            proposed_role.write_bytes(original_role)
        with patch.dict(os.environ, env, clear=True):
            try:
                port = self.start_callback_server()
                declaration = home.parent / 'native-bridge.yaml'
                declaration.write_text('name: tgbridge\nbase: http://127.0.0.1:' + str(port) + '\n'
                    'credential:\n  env: TGBRIDGE_SECRET\n  header: "Authorization: Bearer {credential}"\n'
                    'calls:\n  run_ask:\n    kind: update\n    params:\n      bead: {type: string}\n'
                    '      ask: {type: string}\n      key: {type: string}\n'
                    '    request:\n      method: POST\n      path: /v1/run-ask\n'
                    '      body: {bead: "{bead}", ask: "{ask}", key: "{key}"}\n')
                credential = home.parent / 'native-bridge.env'
                credential.write_text('TGBRIDGE_SECRET=CAPABILITY\n')
                credential.chmod(0o600)
                cli('source', 'add', str(declaration))
                cli('source', 'set', 'tgbridge', 'env_file=' + str(credential))
                def source_call(key, number):
                    return json.loads(cli('call', 'tgbridge', 'run_ask', 'bead=sample-1', 'ask=' + str(number),
                                          'key=' + key, '--json'))
                old = propose()
                head = client.call('Head', [])
                self.assertEqual(self.bridge.run_asks.record_depot_cutover(), head)
                before = len(self.documents)
                for key in ('real-source-refusal', 'real-source-refusal-new-key'):
                    result = source_call(key, old['ask'])
                    self.assertFalse(result['delivered'], result)
                    self.assertEqual(result['failure_kind'], 'permanent')
                    self.assertIn('before_activation', result['reason'])
                    with self.store.connect() as db:
                        self.assertIsNone(db.execute('SELECT id FROM run_ask_keys WHERE key=?', (key,)).fetchone())
                restart()
                self.assertFalse(source_call('real-source-refusal', old['ask'])['delivered'])
                self.assertEqual(len(self.documents), before)
                untouched(old)
                fresh = propose()
                result = source_call('real-source-fresh', fresh['ask'])
                self.assertTrue(result['delivered'], result)
                row = next(r for r in self.bridge.run_asks.rows() if r['ask'] == fresh['ask'])
                self.assertEqual(len(self.documents), before + 1)
                other = propose()
                result = source_call('real-source-fresh', other['ask'])
                self.assertFalse(result['delivered'], result)
                self.assertEqual(result['reason'], 'idempotency_key_conflict')
                for key in (row['bundle']['document']['key'], self.bridge.run_asks.send_request(row)[0]):
                    result = source_call(key, fresh['ask'])
                    self.assertFalse(result['delivered'], result)
                    self.assertEqual(result['reason'], 'idempotency_key_conflict')
                sent = self.bridge.api_send(dict(text='Synthetic ordinary send; no approval controls', key='ordinary-depot-collision'))
                self.assertTrue(sent['delivered'], sent)
                result = source_call('ordinary-depot-collision', other['ask'])
                self.assertFalse(result['delivered'], result)
                self.assertEqual(result['reason'], 'idempotency_key_conflict')
                result = self.bridge.api_send(dict(text='Synthetic conflicting ordinary send', key='real-source-fresh'))
                self.assertFalse(result['delivered'], result)
                untouched(fresh)
                untouched(other)
            finally:
                self.doCleanups()
        print('Native depot boundary: actual RPC/store + SQLite + poll refresh; reservation, document/card saves, '
              'ambiguous/size receipt, origin/expiry, arm/native commit/lost reply, stale digest/file/mapping, '
              'snapshot edits and binding, real pending then failed carry, source HTTP cutover and caller/document/card/send keys; no duplicate effects')

    def exercise_depot_objects(self, home, client, cli, checkout, poll, event):
        matrix = checkout / 'matrix'
        for directory in ('roles', 'scripts', 'flows/default'):
            (matrix / directory).mkdir(parents=True, exist_ok=True)
        role = matrix / 'roles/sample.md'
        role.write_bytes('Review café 🙂.\r\n'.encode())
        (matrix / 'scripts/archive').write_bytes(b'')
        (matrix / 'flows/default/new.md').write_text('Start synthetic review.\n')
        declaration = 'name: matrixsvc\nbase: https://example.invalid\ncalls:\n  health:\n    kind: read\n    request: {method: GET, path: /health}\n'
        (matrix / 'service.yaml').write_text(declaration)
        def manifest(source=True, changed=False):
            value = ('api: 1\nname: matrix\nflows:\n  default:\n    stages:\n'
                     '      new: {group: reviewers, file: flows/default/new.md, outcomes: {done: ' + ('review' if changed else 'done') + '}}\n')
            if changed:
                value += '      review: {group: reviewers, file: flows/default/review.md, outcomes: {done: done}}\n'
            value += ('      done: {end: true}\ngroups:\n  reviewers: {people: true' + (', limit: 2' if changed else '') + '}\n'
                      '  archive: {run: scripts/archive, env: {MODE: synthetic}}\n'
                      'crew:\n  reviewer:\n    role: roles/sample.md\n    hears:\n      stages: [new]\n')
            if source:
                value += '    calls:\n      matrixsvc: [health]\nsources:\n  matrixsvc: {declaration: service.yaml}\n'
            (matrix / 'pack.yaml').write_text(value)

        def propose(tag, source=True, members=False, extra=''):
            pin = re.search(r'sha256:([0-9a-f]{64})', cli('pack', 'check', str(matrix), '--depot', 'synthetic',
                                                      '--crew', 'reviewer=reviewer'))[1]
            flags = ' --flow default --crew reviewer=reviewer'
            if members:
                flags += ' --members reviewers=pat'
            if source:
                flags += ' --calls crew:reviewer=matrixsvc:health'
            cli('--actor', 'proposer', 'bead', 'ask', 'sample-1', 'Review the synthetic depot matrix?', '--to', 'pat',
                '--context', 'Complete native synthetic plan; no live state',
                '--run', f'pack apply {matrix} --sha256 {pin} --depot synthetic' + flags + extra)
            ask = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]
            if tag:
                self.assert_source_pack_fixture(ask, home.parent, 'depot_matrix_' + tag)
            return ask

        def publish(ask):
            before = len(self.documents)
            result = self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key='matrix-' + str(ask['ask'])))
            if not result['delivered']:
                self.assertEqual(len(self.documents), before)
                return result, None
            row = next(r for r in self.bridge.run_asks.rows() if r['ask'] == ask['ask'])
            document = row['bundle']['document']['text']
            for obj in ask['run']['pack']['plan']['objects']:
                if obj['kind'] == 'file' and obj['action'] in ('create', 'change'):
                    self.assertIn(n.review_code(obj.get('text', '')), document)
                    self.assertIn(n.review_code(obj.get('diff', '')), document)
            return result, row

        def approve(row, ask):
            poll([event(row)])
            if ask.get('weight'):
                pending = next(a for a in client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'] if a['ask'] == ask['ask'])
                self.assertEqual(pending['state'], 'open')
                poll([event(row, 'confirm')])
            deadline = time.monotonic() + 10
            def current():
                return next(a for a in client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'] if a['ask'] == ask['ask'])
            while current().get('ran', {}).get('result') == n.PENDING:
                self.assertLess(time.monotonic(), deadline)
                time.sleep(.02)
            self.assertEqual(current()['state'], 'answered')
            self.assertFalse(current()['ran'].get('failed'), current())
            for obj in ask['run']['pack']['plan']['objects']:
                if obj['kind'] == 'file' and obj['action'] in ('create', 'change'):
                    target = (home / 'flows/matrix.default' / Path(obj['name']).name if obj['name'].startswith('flows/default/')
                              else home / 'packs/matrix' / obj['name'])
                    self.assertEqual(target.read_bytes(), obj.get('text', '').encode())
            poll([])
            poll([event(row)])
            applied = [e for e in client.call('EventsOfKind', ['pack_applied', 1000])
                       if e['data'].get('approved', {}).get('ask') == ask['ask']]
            self.assertEqual(len(applied), 1)

        manifest()
        ask = propose('first', members=True)
        self.assertNotIn('kind', ask['run']['pack'])
        result, row = publish(ask)
        self.assertTrue(result['delivered'], result)
        approve(row, ask)
        self.assertEqual((home / 'packs/matrix/roles/sample.md').read_bytes(), role.read_bytes())
        self.assertEqual((home / 'packs/matrix/scripts/archive').read_bytes(), b'')
        self.assertEqual((home / 'packs/matrix/scripts/archive').stat().st_mode & 0o777, 0o755)
        self.assertEqual(client.call('GetGroup', ['reviewers'])['members'], ['pat'])
        self.assertEqual(client.call('GetGroup', ['synthetic-archive'])['runner'], 'exec')
        self.assertEqual(client.call('GetGroup', ['synthetic-archive'])['config']['cmd'],
                         'MODE=synthetic ' + str(home / 'packs/matrix/scripts/archive'))
        self.assertEqual(client.call('GetFlow', ['matrix.default'])['stages'], ['new', 'done'])
        self.assertIn(dict(depot='synthetic', flow='matrix.default'), client.call('FlowPointers', []))
        crew = client.call('GetCrew', ['reviewer'])
        self.assertEqual(crew['config']['prompt'], str(home / 'packs/matrix/roles/sample.md'))
        self.assertIn(dict(source='yardr', scope='all', where=dict(depot='synthetic', stage='new')), crew['subscriptions'])
        routes = [r for r in client.call('Routes', []) if r.get('depot') == 'synthetic']
        self.assertEqual([(r['stage'], r['group']) for r in routes], [('new', 'reviewers')])
        self.assertEqual(client.call('GetSource', ['matrixsvc'])['declaration'], declaration)
        self.assertEqual(len(client.call('SourceGrants', ['matrixsvc'])), 1)

        manifest(source=False, changed=True)
        large = ''.join(f'Line {index:04d} café.\n' for index in range(1000)).encode()
        role.write_bytes(large)
        (matrix / 'flows/default/new.md').write_text('The formerly empty stage now has text.\n')
        (matrix / 'flows/default/review.md').write_text('Review synthetic examples.\n')
        ask = propose('change', source=False)
        result, row = publish(ask)
        self.assertTrue(result['delivered'], result)
        approve(row, ask)
        self.assertEqual((home / 'packs/matrix/roles/sample.md').read_bytes(), large)
        self.assertEqual(client.call('GetFlow', ['matrix.default'])['stages'], ['new', 'review', 'done'])
        routes = [r for r in client.call('Routes', []) if r.get('depot') == 'synthetic']
        self.assertEqual(sorted((r['stage'], r['group']) for r in routes), [('new', 'reviewers'), ('review', 'reviewers')])
        self.assertEqual(client.call('GetGroup', ['reviewers'])['limit'], 2)
        self.assertEqual(client.call('GetSource', ['matrixsvc'])['declaration'], declaration)
        for mutation in ('depot', 'mapped-group'):
            ask = propose(None, source=False, extra=' --group reviewers=reviewers')
            result, row = publish(ask)
            self.assertTrue(result['delivered'], result)
            if mutation == 'depot':
                value = client.call('GetDepot', ['synthetic'])
                value['test'] = 'true'
                client.call('PutDepot', [value, 'pat'])
            else:
                value = client.call('GetGroup', ['reviewers'])
                value['members'] = ['pat', 'proposer']
                client.call('PutGroup', [value, 'pat'])
            poll([event(row)])
            if self.bridge.run_asks.get(row['id']).get('pending'):
                poll([event(row, 'confirm')])
            self.assertEqual(self.bridge.run_asks.get(row['id'])['state'], 'stale')
            native = next(a for a in client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'] if a['ask'] == ask['ask'])
            self.assertEqual(native['state'], 'open')
            self.assertNotIn('ran', native)
            self.assertEqual((home / 'packs/matrix/roles/sample.md').read_bytes(), large)

        manifest(source=True, changed=True)
        replacement = declaration.replace('https://example.invalid', 'https://api.example.invalid')
        (matrix / 'service.yaml').write_text(replacement)
        ask = propose('replace')
        result, row = publish(ask)
        complete = any('grants kept for source matrixsvc:' in line for line in ask['run']['does'])
        self.assertEqual(result['delivered'], complete, result)
        if complete:
            approve(row, ask)
            self.assertEqual(client.call('GetSource', ['matrixsvc'])['declaration'], replacement)
            ask = propose(None)
            result, row = publish(ask)
            self.assertTrue(result['delivered'], result)
            cli('source', 'grant', 'matrixsvc', 'read', '--group', 'reviewers')
            poll([event(row)])
            if self.bridge.run_asks.get(row['id']).get('pending'):
                poll([event(row, 'confirm')])
            self.assertEqual(self.bridge.run_asks.get(row['id'])['state'], 'stale')
        else:
            self.assertEqual(client.call('GetSource', ['matrixsvc'])['declaration'], declaration)
        print('Native depot objects: no-kind, role/script/stage CRLF/Unicode/empty files, flow/pointer/routes, '
              'manual/exec groups, env, subscription, read mapping, large complete diff, dropped source, '
              'depot/group/grants stale; replacement ' + ('supported' if complete else 'explicit missing-grants refusal'))

    def test_real_native_source_contract_and_authority(self):
        binary = testyardr.binary()
        if binary is None:
            self.skipTest('yardr binary required for isolated native integration')
        root = Path(self.tmp.name)
        server, home, command, env = testyardr.start(root, binary)
        try:
            client, cli, _ = self.exercise_source_limit_and_trust(binary, root, home, command, env)
            cli('bead', 'create', 'Synthetic native contract captures', '--id', 'contract-1')
            updates = iter(range(40000, 41000))
            public = hashlib.sha256(Path(binary).read_bytes()).hexdigest() == 'e2b090fcb9f86ff4dd2d9e70933a52bee3d6eeca1e8a07b65ad11adfb23c4009'
            canonical = None
            checkout = root / 'checkout'
            for args in (['init', '-q', str(checkout)], ['-C', str(checkout), '-c', 'user.name=Synthetic',
                         '-c', 'user.email=synthetic@example.invalid', 'commit', '-q', '--allow-empty', '-m', 'Initial']):
                subprocess.run(['git', *args], env=env, check=True, capture_output=True)
            cli('depot', 'add', 'contract', str(checkout), '--base', 'HEAD')
            scenarios = [('source', kind) for kind in ('rich_public_main', 'address_public_main', 'numeric_public_main',
                          'orphan_public_main', 'empty_replace_public_main')]
            scenarios += [('pack', kind) for kind in ('orphan_public_main', 'empty_replace_public_main')]
            for prefix, kind in scenarios:
                with self.subTest(native_contract=(prefix, kind)):
                    fixture = source_proposal(kind) if prefix == 'source' else pack_proposal(kind)
                    carried = fixture['run']['carried'][0]
                    name = carried['name']
                    if prefix == 'source':
                        path = root / ('source_' + kind + '.yaml')
                        declaration = path
                        operation = 'source add '
                    else:
                        path = checkout / ('pack_' + kind)
                        path.mkdir()
                        (path / 'pack.yaml').write_text(f'name: {name}\nkind: source\nsources:\n  {name}:\n    declaration: source.yaml\n')
                        declaration = path / 'source.yaml'
                        operation = 'pack apply '
                    declaration.write_text(carried['text'])
                    for file in carried.get('files', []):
                        target = (root if prefix == 'source' else path) / file['program']
                        target.parent.mkdir(exist_ok=True)
                        target.write_text(file['text'])
                    if kind == 'orphan_public_main':
                        old = home / 'sources' / name / 'bin/report'
                        old.parent.mkdir(parents=True)
                        old.write_text(n.source_diff(carried['old'][0], carried['files'][0]['text']))
                        old.chmod(0o600)
                    elif kind == 'empty_replace_public_main':
                        cli('source', 'add', str(declaration))
                    cli('--actor', 'proposer', 'bead', 'ask', 'contract-1', 'Review the synthetic contract?',
                        '--to', 'pat', '--context', 'Synthetic isolated contract capture', '--run', operation + str(path))
                    ask = client.call('Bead', dict(bead='contract-1', viewer='pat'))['asks'][-1]
                    if public:
                        self.assert_source_pack_fixture(ask, root, prefix + '_' + kind.removesuffix('_public_main'))
                    if kind == 'rich_public_main':
                        canonical = copy.deepcopy(ask)
                    before = len(self.calls), len(self.documents)
                    result = self.bridge.api_run_ask(dict(bead='contract-1', ask=ask['ask'], key=prefix + '-' + kind))
                    missing_grants = kind == 'empty_replace_public_main' and not any('; ' in line for line in ask['run']['does'] if line.startswith('it would replace the source '))
                    if missing_grants:
                        self.assertFalse(result['delivered'], result)
                        self.assertEqual((len(self.calls), len(self.documents)), before)
                        print('Native contract:', prefix, kind, 'explicit grants-review refusal; no sends/writes')
                        continue
                    self.assertTrue(result['delivered'], result)
                    row = next(r for r in self.bridge.run_asks.rows() if r['bead'] == 'contract-1' and r['ask'] == ask['ask'])
                    NativeTests.assert_source_review(self, ask, NativeTests.bundle_review(row))
                    def press(action):
                        if action == 'confirm':
                            action += ':' + self.bridge.run_asks.get(row['id'])['pending']['nonce']
                        self.bridge.inbound(dict(update_id=next(updates), callback_query=dict(id='native-contract',
                            data=f"runask:{row['id']}:{action}", message=dict(message_id=row['message_id'],
                                chat=dict(id=123, type='private'), **{'from': dict(id=999, is_bot=True)}),
                            **{'from': dict(id=123, is_bot=False)})))
                    if kind == 'rich_public_main':
                        # Keep this source absent so the authority probe has the same initial state.
                        press('decline')
                        print('Native contract:', prefix, kind, 'complete review and native decline')
                        continue
                    press('approve')
                    if ask.get('weight'):
                        press('confirm')
                    deadline = time.monotonic() + 10
                    def current():
                        return next(a for a in client.call('Bead', dict(bead='contract-1', viewer='pat'))['asks'] if a['ask'] == ask['ask'])
                    while current().get('ran', {}).get('result') == n.PENDING:
                        self.assertLess(time.monotonic(), deadline)
                        time.sleep(.02)
                    settled = current()
                    self.assertEqual(settled['state'], 'answered')
                    self.assertFalse(settled['ran'].get('failed'), settled['ran'])
                    self.assertEqual(client.call('GetSource', [name])['declaration'], carried['text'])
                    for file in carried.get('files', []):
                        installed = home / 'sources' / name / file['program']
                        self.assertEqual(installed.read_bytes(), file['text'].encode())
                        self.assertEqual(installed.stat().st_mode & 0o777, 0o700)
                    self.bridge.run_asks = n.RunAsks(self.bridge)
                    self.bridge.run_asks.refresh()
                    press('approve')
                    self.assertEqual(current(), settled)
                    events = [e for e in client.call('EventsOfKind', ['source_added' if prefix == 'source' else 'pack_applied', 100])
                              if e['data'].get('approved', {}).get('ask') == ask['ask']
                              and e['data'].get('approved', {}).get('bead') == 'contract-1']
                    self.assertEqual(len(events), 1)
                    print('Native contract:', prefix, kind, 'installed exact bytes/mode; one operation under restart/replay')
            supplied = dict(question='Review caller authority?', to='pat', context=['Synthetic forged input'],
                weight=[{'class': 'reads', 'line': 'CALLER WEIGHT'}], run=dict(copy.deepcopy(canonical['run']),
                does=['CALLER DISCLOSURE'], seen={'forged': 'CALLER SEEN'}, back='CALLER BACK', back_by='CALLER OWNER'))
            client.call('AddAsk', ['contract-1', supplied, 'proposer'])
            stored = client.call('Bead', dict(bead='contract-1', viewer='pat'))['asks'][-1]
            self.assertEqual(stored['run'], canonical['run'])
            self.assertEqual(stored.get('weight'), canonical.get('weight'))
            print('Native authority: caller Does/Seen/Back/weight discarded; core regenerates the stored ask')
        finally:
            testyardr.stop(server)

    def test_real_native_source_limit_and_trust_weight_apply_once(self):
        binary = testyardr.binary()
        if binary is None:
            self.skipTest('yardr binary required for isolated native integration')
        root = Path(self.tmp.name)
        server, home, command, env = testyardr.start(root, binary)
        try:
            # No skill add card here, so every build runs it.
            client, cli, _ = self.exercise_source_limit_and_trust(binary, root, home, command, env)
            server = self.exercise_general_native(binary, root, home, command, env, server, client, cli)
        finally:
            testyardr.stop(server)

    def exercise_general_native(self, binary, root, home, command, env, server, client, cli):
        probe = subprocess.run(command + ['settings', 'get', 'general_cards'], env=env, cwd=root,
                               capture_output=True, text=True, timeout=20)
        if probe.returncode:
            self.assertIn('no setting', probe.stderr)
            print('Native general cards: absent on old core; ordinary run already passed')
            return server
        cli('user', 'add', 'yardmaster')
        updates = iter(range(60000, 61000))
        def current(number):
            return next(a for a in client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'] if a['ask'] == number)
        def filing(words, actor='yardmaster', extra_env=None):
            args = ['--actor', actor, 'bead', 'ask', 'sample-1', 'Review this synthetic general command?',
                    '--to', 'pat', '--context', 'Isolated native general-card fixture']
            for word in words:
                args += ['--run-word', word]
            return subprocess.run(command + args, env=dict(env, **(extra_env or {})), cwd=root,
                                  capture_output=True, text=True, timeout=20)
        def propose(words):
            result = filing(words)
            self.assertEqual(result.returncode, 0, result.stderr)
            return client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]
        def poll(batch):
            telegram = self.bridge.telegram
            def fetched(method, body, timeout=15):
                return (200, dict(ok=True, result=batch)) if method == 'getUpdates' else telegram(method, body, timeout)
            with patch.object(self.bridge, 'telegram', side_effect=fetched):
                self.bridge.poll_once()
        def event(row, action='approve'):
            if action == 'confirm':
                action += ':' + self.bridge.run_asks.get(row['id'])['pending']['nonce']
            return dict(update_id=next(updates), callback_query=dict(id='general-callback',
                data=f"runask:{row['id']}:{action}", message=dict(message_id=row['message_id'],
                    message_thread_id=7, is_topic_message=True, chat=dict(id=123, type='private'),
                    **{'from': dict(id=999, is_bot=True)}), **{'from': dict(id=123, is_bot=False)}))
        def publish(ask):
            receipt = self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key='general-' + str(ask['ask']), topic='City'))
            self.assertTrue(receipt['delivered'], receipt)
            return self.bridge.run_asks.get(self.bridge.run_asks.ident('sample-1', ask['ask']))
        def finish(ask, timeout=15):
            deadline = time.monotonic() + timeout
            while True:
                value = current(ask['ask'])
                if value.get('ran', {}).get('result') != n.PENDING and value.get('ran'):
                    poll([])
                    return value
                self.assertLess(time.monotonic(), deadline, 'native general carry did not settle')
                time.sleep(.02)

        # The real operator opts in only in this throwaway home; this is not remote bootstrap evidence.
        off = filing(['version'])
        self.assertNotEqual(off.returncode, 0)
        self.assertIn('general run cards are off', off.stderr)
        cli('settings', 'set', 'general_cards', 'true')
        refused = filing(['version'], actor='proposer')
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('yardmaster', refused.stderr)
        for words in (['source', 'set', 'tgbridge', 'calls_per_hour=59'],
                      ['settings', 'set', 'general_cards', 'true']):
            self.assertNotEqual(filing(words).returncode, 0)
        dispatcher = open(home / 'daemon.lock', 'w')
        self.addCleanup(dispatcher.close)
        fcntl.flock(dispatcher, fcntl.LOCK_EX | fcntl.LOCK_NB)
        dispatcher.write(f'{os.getpid()} {testyardr.process_start(os.getpid())}\n')
        dispatcher.flush()
        before_authority = len(client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'])
        for runner in ('exec', 'herdr'):
            group = 'general-' + runner
            settings = ['--set', 'cmd=/bin/false', '--set', 'dir=' + str(root)] if runner == 'exec' else ['--set', 'kind=codex']
            cli('group', 'add', group, '--runner', runner, *settings)
            cli('bead', 'create', 'Synthetic authority boundary', '--id', group)
            session = client.call('Claim', [group, group, 'new', 'pat', []])
            starting = filing(['version'], extra_env={'YARDR_SESSION': session['id']})
            self.assertNotEqual(starting.returncode, 0)
            self.assertIn('starting', starting.stderr)
            pane = subprocess.Popen([sys.executable, '-u', '-c',
                'import json,subprocess,sys\n'
                'request=json.loads(sys.stdin.readline())\n'
                'result=subprocess.run(request["command"],env=request["env"],capture_output=True,text=True,timeout=20)\n'
                'print(json.dumps({"code":result.returncode,"error":result.stderr}),flush=True)\n'
                'sys.stdin.readline()\n'], env=env, cwd=root, stdin=subprocess.PIPE, stdout=subprocess.PIPE, text=True)
            try:
                handle = {'pid': pane.pid, 'start': testyardr.process_start(pane.pid)} if runner == 'exec' else {
                    'shell_pid': pane.pid, 'shell_start': testyardr.process_start(pane.pid)}
                client.call('MarkRunning', [session['id'], handle])
                args = command + ['--actor', 'yardmaster', 'bead', 'ask', 'sample-1', 'Synthetic pane refusal?',
                    '--to', 'pat', '--context', 'Synthetic isolation', '--run-word', 'version']
                pane.stdin.write(json.dumps(dict(command=args, env=env)) + '\n')
                pane.stdin.flush()
                reply = json.loads(pane.stdout.readline())
                self.assertNotEqual(reply['code'], 0)
                self.assertIn("bead's agent", reply['error'])
            finally:
                testyardr.stop(pane)
                pane.stdin.close()
                pane.stdout.close()
            client.call('EndSession', [session['id'], 'failed', 'Synthetic authority check complete'])
            client.call('MarkStopped', [session['id']])
        self.assertEqual(len(client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks']), before_authority)
        self.config['topics'] = (dict(name='City', agent='yardmaster'),)
        self.store.record_topic('City', 7, 'created')
        old_bridge = self.bridge
        self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=old_bridge.request,
                               request_multipart=old_bridge.request_multipart)
        self.bridge.hook_secret = None
        words = ['bead', 'create', 'Synthetic café 雪 😀 long spaced word ' * 7, '--id', 'general-once']
        ask = propose(words)
        self.assertIn('cut', '\n'.join(ask['run']['does']))
        row = publish(ask)
        document = self.documents[-1][2].decode()
        for word in words:
            self.assertIn('\n' + json.dumps(word, ensure_ascii=True) + '\n', document)
        self.assertEqual(self.documents[-1][0]['message_thread_id'], '7')
        listed, buttons = self.bridge.run_asks.decide_list()
        self.assertIn('sample-1#' + str(ask['ask']), listed)
        self.assertEqual(self.bridge.run_asks.decide_open('sample-1', ask['ask'], '123/7')[1][0], row['message_id'])
        for change in ('topic', 'user', 'bot', 'chat', 'message'):
            wrong = event(row)
            query = wrong['callback_query']
            if change == 'topic': query['message']['message_thread_id'] = 8
            if change == 'user': query['from']['id'] = 124
            if change == 'bot': query['message']['from']['id'] = 998
            if change == 'chat': query['message']['chat']['id'] = 124
            if change == 'message': query['message']['message_id'] += 1
            poll([wrong])
            self.assertIsNone(self.bridge.run_asks.get(row['id'])['pending'])
        poll([event(row, 'confirm:0000000000000000')])
        self.assertEqual(current(ask['ask'])['state'], 'open')
        poll([event(row)])
        armed = self.bridge.run_asks.get(row['id'])['pending']
        expired = event(row, 'confirm')
        with patch.object(self.bridge.run_asks, 'now', return_value=armed['expires'] + 1):
            poll([expired])
        self.assertEqual(current(ask['ask'])['state'], 'open')
        poll([event(row)])
        confirmation = event(row, 'confirm')
        # Lose the reply only after the stock native handler has durably accepted.
        rpc = self.bridge.run_asks.client.call
        def lost(method, args):
            value = rpc(method, args)
            if method == 'ApproveAsk':
                raise n.RPCAmbiguous('synthetic lost approval reply')
            return value
        with patch.object(self.bridge.run_asks.client, 'call', side_effect=lost):
            poll([confirmation])
        settled = finish(ask)
        self.assertFalse(settled['ran'].get('failed', False), settled)
        self.assertEqual(settled['ran']['argv'], words)
        self.assertEqual(client.call('GetBead', ['general-once'])['title'], words[2])
        self.bridge.run_asks = n.RunAsks(self.bridge)
        poll([confirmation, event(row)])
        settled_again = finish(ask)
        self.assertEqual(settled_again['ran'], settled['ran'])
        events = client.call('Events', ['general-once', 100])
        self.assertEqual(sum(e['kind'] == 'created' for e in events), 1)
        self.assertIn(settled['ran']['result'].strip(), self.bridge.run_asks.get(row['id'])['display']['footer'])
        # An unchecked command from a local-only family must reach native validation on stock core.
        unread = root / 'unchecked-skill.md'
        unread.write_text('Invalid synthetic skill before review.\n')
        bad = propose(['skill', 'add', str(unread)])
        bad_row = publish(bad)
        self.assertNotIn(unread.read_text().strip(), self.documents[-1][2].decode())
        unread.write_text('Invalid synthetic skill changed after review.\n')
        poll([event(bad_row)]); poll([event(bad_row, 'confirm')])
        failed = finish(bad)
        self.assertTrue(failed['ran']['failed'])
        self.assertIn(failed['ran']['result'], self.bridge.run_asks.get(bad_row['id'])['display']['footer'])
        # Switching off stops filings, without revoking a stored decision.
        stored = propose(['version'])
        stored_row = publish(stored)
        cli('settings', 'set', 'general_cards', 'false')
        self.assertNotEqual(filing(['version']).returncode, 0)
        poll([event(stored_row)]); poll([event(stored_row, 'confirm')])
        self.assertFalse(finish(stored)['ran'].get('failed', False))
        cli('settings', 'set', 'general_cards', 'true')
        for boundary in ('crash', 'timeout'):
            entered, release = threading.Event(), threading.Event()
            invocations = []
            class Handler(http.server.BaseHTTPRequestHandler):
                def log_message(self, *_):
                    pass
                def do_GET(self):
                    invocations.append(self.path)
                    entered.set()
                    release.wait(150)
                    try:
                        self.send_response(200)
                        self.end_headers()
                        self.wfile.write(b'{"synthetic":true}')
                    except (BrokenPipeError, ConnectionResetError):
                        pass
            httpd = testports.bind_test_server(Handler)
            thread = threading.Thread(target=httpd.serve_forever, daemon=True)
            thread.start()
            try:
                source = root / ('general-' + boundary + '.yaml')
                source.write_text('name: general' + boundary + '\nbase: http://127.0.0.1:' + str(httpd.server_address[1])
                    + '\ncalls:\n  slow:\n    kind: read\n    params:\n      q: {type: string}\n'
                    + '    request: {method: GET, path: /synthetic, query: {q: \"{q}\"}}\n')
                cli('source', 'add', str(source))
                target = propose(['call', 'general' + boundary, 'slow', 'q=synthetic?'])
                target_row = publish(target)
                poll([event(target_row)]); poll([event(target_row, 'confirm')])
                self.assertTrue(entered.wait(10), 'native general command did not reach isolated HTTP fixture')
                if boundary == 'crash':
                    # Capture this server's children by pid before a hard crash, then stop only those identities.
                    children = []
                    for line in subprocess.check_output(['ps', '-A', '-o', 'pid=', '-o', 'ppid='], text=True).splitlines():
                        child, parent = map(int, line.split())
                        if parent == server.pid:
                            children.append((child, testyardr.process_start(child)))
                    server.kill()
                    server.wait(timeout=10)
                    release.set()
                    for child, start in children:
                        try:
                            if testyardr.process_start(child) == start:
                                os.kill(child, 15)
                        except (ProcessLookupError, FileNotFoundError):
                            pass
                    server = testyardr.restart(root, binary, home, command, env)
                    self.addCleanup(testyardr.stop, server)
                    self.bridge.run_asks = n.RunAsks(self.bridge)
                failed = finish(target, timeout=145)
                self.assertTrue(failed['ran']['failed'])
                if boundary == 'crash':
                    self.assertEqual(failed['ran']['result'], n.GENERAL_STOPPED)
                    self.assertIn('UNKNOWN command outcome', self.bridge.run_asks.get(target_row['id'])['display']['footer'])
                else:
                    self.assertRegex(failed['ran']['result'].lower(), 'timeout|timed out|deadline')
                poll([event(target_row)]); poll([])
                self.assertEqual(invocations, ['/synthetic?q=synthetic%3F'])
            finally:
                release.set()
                httpd.shutdown()
                httpd.server_close()
                thread.join(timeout=10)
        fixture = testyardr.general_store_fixture(root)
        if fixture is not None:
            for boundary in ('expire', 'claim'):
                target = propose(['bead', 'create', 'Never automatically invoked', '--id', 'general-' + boundary])
                target_row = publish(target) if boundary == 'claim' else None
                testyardr.stop(server)
                try:
                    result = subprocess.run([str(fixture), str(home), boundary, 'sample-1', str(target['ask'])],
                                            env=env, cwd=root, text=True, capture_output=True, timeout=20)
                    self.assertEqual(result.returncode, 0, result.stderr)
                finally:
                    server = testyardr.restart(root, binary, home, command, env)
                    self.addCleanup(testyardr.stop, server)
                self.bridge.run_asks = n.RunAsks(self.bridge)
                if boundary == 'expire':
                    target = current(target['ask'])
                    target_row = publish(target)
                    poll([event(target_row)]); poll([event(target_row, 'confirm')])
                    self.assertEqual(current(target['ask'])['state'], 'open')
                    expired_row = self.bridge.run_asks.get(target_row['id'])
                    self.assertTrue(expired_row['native_expired'])
                    self.assertIn('remains open', expired_row['display']['footer'])
                    self.assertNotIn('approve', str(expired_row['display']['keyboard']))
                else:
                    failed = finish(target)
                    self.assertTrue(failed['ran']['failed'])
                    self.assertIn('whether it finished is not known', failed['ran']['result'])
                    self.assertIn('UNKNOWN command outcome', self.bridge.run_asks.get(target_row['id'])['display']['footer'])
                    poll([event(target_row)])
                with self.assertRaises(n.RPCRefused):
                    client.call('GetBead', ['general-' + boundary])
        else:
            print('Native general durable expiry/claim fixtures: set TGBRIDGE_TEST_YARDR_SOURCE for the unchanged candidate source')
        print('Native general: real opt-in/operator/socket, bead/exec/starting refusal, full long Unicode argv document, City /decide, '
              'origin/confirmation gates, lost approval reply, native success/failure, hard-crash UNKNOWN and HTTP timeout/one invocation, replay/one creation; '
              + ('native expiry-open and claimed-prior-boot unknown/no invocation' if fixture else 'basic binary slice'))
        return server

    def test_real_native_decision_decline_without_held_works(self):
        binary = testyardr.binary()
        if binary is None:
            self.skipTest('yardr binary required for isolated native integration')
        root = Path(self.tmp.name)
        server, home, command, env = testyardr.start(root, binary)
        try:
            client = n.YardSocketClient(home, 'tgbridge')

            def cli(*args, actor='pat'):
                result = subprocess.run(command + list(args), env=env, cwd=root,
                                        text=True, capture_output=True, timeout=20)
                self.assertEqual(result.returncode, 0, result.stderr)
                return result.stdout

            # init completes the yard deterministically (the running server adopts
            # it); settings alone leaves a home later commands refuse.
            cli('init', '--harness', 'claude')
            cli('settings', 'set', 'operator', 'pat')
            source = root / 'synthetic.yaml'
            source.write_text('name: tgbridge\nbase: https://example.invalid\ncalls:\n  health:\n    kind: read\n    request: {method: GET, path: /health}\nhook:\n  bodies: unique\n  verify:\n    scheme: secret\n    signature: {header: X-Secret}\n  normalize: [/bin/false]\n')
            cli('source', 'add', str(source))
            cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
            cli('user', 'add', 'proposer')
            cli('bead', 'create', 'Synthetic decline', '--id', 'decline-1')
            cli('bead', 'ask', 'decline-1', 'Which option?', '--to', 'pat', '--context', 'Synthetic decline',
                '--option', 'First: Choose first.', '--option', 'Second: Choose second.', actor='proposer')
            original = client.call('Bead', dict(bead='decline-1', viewer='pat'))['asks'][0]
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
            self.documents = []
            self.sent_count = 10000
            self.bridge.request_multipart = lambda *args, **kwargs: NativeCase.pack_transport(self, *args, **kwargs)
            receipt = self.bridge.api_ask_card(dict(bead='decline-1', ask=original['ask'], key='decline-real'))
            self.assertTrue(receipt['delivered'], receipt)
            row = next(r for r in self.bridge.run_asks.rows() if r['bead'] == 'decline-1')
            query = dict(id='decline-query', data=f"askcard:{row['id']}:decline:{original['turn']}",
                         message=dict(message_id=row['message_id'], chat=dict(id=123, type='private'),
                                      **{'from': dict(id=999, is_bot=True)}),
                         **{'from': dict(id=123, is_bot=False)})
            calls = []
            actual_call = self.bridge.run_asks.client.call

            def record(name, args):
                calls.append((name, copy.deepcopy(args)))
                return actual_call(name, args)
            with patch.object(self.bridge.run_asks.client, 'call', side_effect=record):
                self.bridge.inbound(dict(update_id=500, callback_query=query))
            declined = [args for name, args in calls if name == 'DeclineAskExpected']
            self.assertEqual(len(declined), 1)
            # The bridge sends no held: the yard defaults it to 0.
            self.assertNotIn('held', declined[0])
            self.assertEqual(declined[0]['turn'], original['turn'])
            stored = client.call('Bead', dict(bead='decline-1', viewer='pat'))['asks'][0]
            self.assertEqual((stored['state'], stored['words']), ('declined', 'Declined in Telegram'))
            self.assertEqual(self.bridge.run_asks.get(row['id'])['state'], 'declined')

            # Real yardr: advancing an --about bead or retitling the bead makes the decision card stale
            # while the native ask stays open; the bridge re-issues one fresh card and answers via turn guard.
            cli('bead', 'create', 'Referenced bead', '--id', 'about-1')
            cli('bead', 'create', 'Stale reissue bead', '--id', 'reissue-1')
            subprocess.run([command[0], '--home', str(home), '--actor', 'proposer', 'bead', 'ask', 'reissue-1',
                            'Pick one?', '--to', 'pat', '--context', 'Real stale reissue check',
                            '--option', 'First: Pick first.', '--option', 'Second: Pick second.',
                            '--about', 'about-1'], env=env, cwd=root, check=True, stdout=subprocess.DEVNULL)
            reissue_ask = client.call('Bead', dict(bead='reissue-1', viewer='pat'))['asks'][0]
            self.assertTrue(self.bridge.api_ask_card(dict(bead='reissue-1', ask=reissue_ask['ask'], key='reissue-real'))['delivered'])
            stale_row = next(r for r in self.bridge.run_asks.rows() if r['bead'] == 'reissue-1')
            cli('bead', 'advance', 'about-1', '--to', 'review')
            stale_tap = dict(id='reissue-stale', data=f"askcard:{stale_row['id']}:pick1:{reissue_ask['turn']}",
                             message=dict(message_id=stale_row['message_id'], chat=dict(id=123, type='private'),
                                          **{'from': dict(id=999, is_bot=True)}),
                             **{'from': dict(id=123, is_bot=False)})
            self.bridge.inbound(dict(update_id=501, callback_query=stale_tap))
            self.assertEqual(client.call('Bead', dict(bead='reissue-1', viewer='pat'))['asks'][0]['state'], 'open')
            fresh_row = next(r for r in self.bridge.run_asks.rows() if r['bead'] == 'reissue-1' and r['state'] == 'open')
            self.assertNotEqual(fresh_row['id'], stale_row['id'])
            edits = [body for url, body in self.calls if url.endswith('/editMessageText')]
            self.assertIn(n.REPLACED_PROPOSAL, edits[-1]['text'])
            self.assertIn('about-1 · Referenced bead · review', fresh_row['plain'])
            fresh_tap = dict(id='reissue-fresh', data=f"askcard:{fresh_row['id']}:pick2:{fresh_row['turn']}",
                             message=dict(message_id=fresh_row['message_id'], chat=dict(id=123, type='private'),
                                          **{'from': dict(id=999, is_bot=True)}),
                             **{'from': dict(id=123, is_bot=False)})
            self.bridge.inbound(dict(update_id=502, callback_query=fresh_tap))
            after = client.call('Bead', dict(bead='reissue-1', viewer='pat'))['asks'][0]
            self.assertEqual((after['state'], after.get('option'), after['turn']), ('answered', 2, reissue_ask['turn'] + 1))
        finally:
            testyardr.stop(server)

    def test_real_native_source_and_pack_apply_once(self):
        binary = testyardr.binary()
        if binary is None:
            self.skipTest('yardr binary required for isolated native integration')
        root = Path(self.tmp.name)
        server, home, command, env = testyardr.start(root, binary)
        try:
            client, cli, _ = self.exercise_source_limit_and_trust(binary, root, home, command, env)
            self.exercise_source_adds(root, home, cli, client)
            self.exercise_source_packs(root, home, cli, client, command, env)
            self.exercise_source_ten_commands(root, home, cli, client)
        finally:
            testyardr.stop(server)

    def test_real_native_store_records_answer_and_applies_stored_operation_once(self):
        binary = testyardr.binary()
        if binary is None:
            self.skipTest('yardr binary required for isolated native integration')
        root = Path(self.tmp.name)
        server, home, command, env = testyardr.start(root, binary)
        try:
            # Its skill add card runs `yardr skill add`, which no aligned or upstream build has.
            if testyardr.aligned(home):
                self.skipTest('needs a pre-alignment yardr build (its skill add card); this build is aligned or upstream')
            client, cli, telegram = self.exercise_source_limit_and_trust(binary, root, home, command, env)

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

            cli('bead', 'create', 'Synthetic unconfirmed delivery', '--id', 'unconfirmed-1')
            cli('bead', 'ask', 'unconfirmed-1', 'Which option?', '--to', 'pat', '--context', 'Isolated lost response',
                '--option', 'First: Choose first.', '--option', 'Second: Choose second.')
            lost = client.call('Bead', dict(bead='unconfirmed-1', viewer='pat'))['asks'][0]
            request = dict(bead='unconfirmed-1', ask=lost['ask'], key='unconfirmed-real')
            delivered = []

            def lost_response(url, body, *args, **kwargs):
                status, response = telegram(url, body, *args, **kwargs)
                if url.endswith('/sendMessage'):
                    delivered.append(dict(response['result'], chat=dict(id=123, type='private'),
                        **{'from': dict(id=999, is_bot=True)}, text=b.html_visible_text(body['text'])[0].strip(),
                        reply_markup=copy.deepcopy(body['reply_markup'])))
                    raise b.RequestUnconfirmed('response lost after delivery', exit_code=28, phase='after-send')
                return status, response

            self.bridge.request = lost_response
            receipt = self.bridge.api_ask_card(request)
            self.assertFalse(receipt['delivered'])
            self.assertEqual(len(delivered), 1)
            row = next(r for r in self.bridge.run_asks.rows() if r['bead'] == 'unconfirmed-1')
            self.assertIsNone(row['message_id'])
            with self.store.connect() as db:
                saved = db.execute('SELECT fingerprint,receipt FROM deliveries WHERE key=?',
                                   (f"runask:{row['id']}:send",)).fetchone()
            self.store = b.Store(self.path, 10)
            self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=telegram)
            self.bridge.hook_secret = None
            self.documents = []
            self.sent_count = 10000
            self.bridge.request_multipart = lambda *args, **kwargs: NativeCase.pack_transport(self, *args, **kwargs)
            query = dict(id='real-unconfirmed', data=delivered[0]['reply_markup']['inline_keyboard'][0][0]['callback_data'],
                         message=delivered[0], **{'from': dict(id=123, is_bot=False)})
            telegram_call = self.bridge.telegram

            def fetched(method, body, timeout=15):
                if method == 'getUpdates':
                    return 200, dict(ok=True, result=[dict(update_id=480, callback_query=query)])
                return telegram_call(method, body, timeout)

            with patch.object(self.bridge, 'telegram', side_effect=fetched):
                self.bridge.poll_once()
                self.bridge.poll_once()
            settled = client.call('Bead', dict(bead='unconfirmed-1', viewer='pat'))['asks'][0]
            self.assertEqual((settled['state'], settled.get('option'), settled['turn']), ('answered', 1, lost['turn'] + 1))
            answers = [note for note in client.call('Notes', ['unconfirmed-1']) if note['kind'] == 'answer']
            self.assertEqual(len(answers), 1)
            self.assertEqual(self.bridge.run_asks.get(row['id'])['message_id'], delivered[0]['message_id'])
            self.assertEqual(self.bridge.api_ask_card(request), receipt)
            self.assertTrue(self.bridge.api_ask_close(dict(ask_key='unconfirmed-real', decision='ignored'))['delivered'])
            cards = [body for url, body in self.calls if url.endswith('/sendMessage')
                     and f"askcard:{row['id']}:" in json.dumps(body.get('reply_markup', {}))]
            self.assertEqual(len(cards), 1)
            with self.store.connect() as db:
                self.assertEqual(db.execute('SELECT fingerprint,receipt FROM deliveries WHERE key=?',
                                            (f"runask:{row['id']}:send",)).fetchone(), saved)

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

            def credential_notes():
                # Without --try, doctor checks the real pairing without contacting a bridge.
                result = subprocess.run(command + ['doctor', '--json'], env=env, cwd=root,
                                        capture_output=True, text=True, timeout=20)
                self.assertIn(result.returncode, (0, 1), result.stderr)
                report = json.loads(result.stdout)
                self.assertFalse(report['tried'])
                sources = next(part for part in report['optional'] if part['key'] == 'sources')
                self.assertTrue(sources['configured'])
                self.assertFalse(sources.get('skipped', False))
                return [note for note in sources.get('notes', []) if note.startswith('source tgbridge:')]

            # Operator-reviewed custom declarations retain custom aliases; stock ones never invent them.
            for index, (name, alias, host) in enumerate((('imd', 'imd', 'http://127.0.0.1:8942'),
                                                       ('custom-harbor', 'custom-harbor', 'http://127.0.0.1:23017'),
                                                       ('primary-stock', None, 'http://127.0.0.1:8941'),
                                                       ('primary-port-change', None, 'http://127.0.0.1:8941'))):
                with self.subTest(source_upgrade=name):
                    legacy = root / ('legacy-pack-' + name)
                    shutil.copytree(release, legacy)
                    declaration = legacy / 'tgbridge.yaml'
                    legacy_text = declaration.read_text()
                    if alias == 'custom-harbor':
                        legacy_text = legacy_text.replace('bases:\n', 'bases:\n  ' + alias + ': ' + host + '\n', 1)
                    declaration.write_text(legacy_text[:legacy_text.index('  run_ask:\n')] + legacy_text[legacy_text.index('  ask_close:\n'):])
                    credential = root / (name + '.env')
                    credential.write_text('TGBRIDGE_SECRET=synthetic-' + name + '\n')
                    credential.chmod(0o600)
                    bindings = ['--source-env', 'tgbridge=' + str(credential)]
                    if alias is not None:
                        bindings += ['--source-base', 'tgbridge=' + alias]
                    # Clear only the synthetic fixture's prior binding before choosing the next case.
                    cli('source', 'rm', 'tgbridge')
                    cli('pack', 'apply', str(legacy), '--yes', *bindings)
                    for setting in ('calls_per_hour=600', 'updates_per_hour=300'):
                        cli('source', 'set', 'tgbridge', setting)
                    cli('source', 'grant', 'tgbridge', 'send', '--group', 'humans')
                    before_source = client.call('GetSource', ['tgbridge'])
                    before_group = client.call('GetGroup', ['humans'])
                    self.assertEqual(before_source['keyed_host'], host)
                    self.assertEqual(before_source['env_file'], str(credential))
                    if alias is None:
                        self.assertNotIn('base', before_source)
                        self.assertEqual(credential_notes(), [])
                    port = 8941 if name == 'primary-stock' else 19000 + index
                    rendered_host = 'http://127.0.0.1:' + str(port)
                    instance = rendered('instance-pack-' + name, port)
                    if alias == 'custom-harbor':
                        refused = subprocess.run(command + ['pack', 'apply', instance, '--yes'] + bindings,
                            env=env, cwd=root, capture_output=True, text=True, timeout=20)
                        self.assertNotEqual(refused.returncode, 0)
                        self.assertIn(alias, refused.stderr)
                        self.assertEqual(client.call('GetSource', ['tgbridge']), before_source)
                        self.assertEqual(client.call('GetGroup', ['humans']), before_group)
                        self.assertEqual(credential.read_text(), 'TGBRIDGE_SECRET=synthetic-' + name + '\n')
                        path = Path(instance) / 'tgbridge.yaml'
                        path.write_text(path.read_text().replace('bases:\n', 'bases:\n  ' + alias + ': ' + host + '\n', 1))
                    cli('pack', 'check', instance)
                    for repeat in range(2):
                        cli('pack', 'apply', instance, '--yes', *bindings)
                        upgraded = client.call('GetSource', ['tgbridge'])
                        self.assertIn('  ask_card:', upgraded['declaration'])
                        self.assertIn('  run_ask:', upgraded['declaration'])
                        shown = cli('source', 'show', 'tgbridge').splitlines()[0]
                        self.assertEqual(upgraded['keyed_host'], host)
                        if alias is not None:
                            self.assertEqual(shown, 'tgbridge  ' + host + ' (base ' + alias + ')')
                            self.assertEqual(upgraded['base'], alias)
                        else:
                            self.assertEqual(shown, 'tgbridge  ' + rendered_host)
                            self.assertNotIn('base', upgraded)
                            notes = credential_notes()
                            if rendered_host == host:
                                self.assertEqual(notes, [])
                            else:
                                self.assertEqual(len(notes), 1, notes)
                                for text in ('source tgbridge: no call of it can be made:',
                                             'its credential was given for ' + host,
                                             "the source's calls go to " + rendered_host + ' now',
                                             'Restore the declaration it was given for',
                                             'source key tgbridge, which pairs it with ' + rendered_host):
                                    self.assertIn(text, notes[0])
                        self.assertEqual({k: v for k, v in upgraded.items() if k not in ('declaration', 'added_at')},
                                         {k: v for k, v in before_source.items() if k not in ('declaration', 'added_at')})
                        self.assertEqual(client.call('GetGroup', ['humans']), before_group)
                        self.assertEqual(credential.read_text(), 'TGBRIDGE_SECRET=synthetic-' + name + '\n')
                    # The private env binding is never pointed at the rendered default's unrelated host.
                    if rendered_host != host:
                        self.assertNotEqual(upgraded['keyed_host'], rendered_host)
            # Real CLI -> RPC reservation -> declared thread -> API -> Telegram fake.
            markdown_bridge = self.bridge
            port = self.start_callback_server(markdown_bridge)
            markdown_pack = Path(rendered('markdown-pack', port))
            for filename in ('pack.yaml', 'tgbridge.yaml'):
                path = markdown_pack / filename
                path.write_text(path.read_text().replace('tgbridge', 'mdbridge'))
            (markdown_pack / 'tgbridge').rename(markdown_pack / 'mdbridge')
            (markdown_pack / 'tgbridge.yaml').rename(markdown_pack / 'mdbridge.yaml')
            reply_credential = root / 'markdown.env'
            reply_credential.write_text('TGBRIDGE_SECRET=CAPABILITY\n')
            reply_credential.chmod(0o600)
            cli('pack', 'apply', str(markdown_pack), '--yes', '--source-env', 'mdbridge=' + str(reply_credential))
            cli('user', 'identity', 'pat', '--source', 'mdbridge', '--id', '123')
            cli('crew', 'add', 'writer', '--dir', str(root), '--set', 'kind=codex',
                '--set', 'env_file=' + str(reply_credential))
            hook_secret = root / 'markdown-hook.secret'
            hook_secret.write_text('synthetic-hook')
            hook_secret.chmod(0o600)
            cli('hook', 'add', 'mdbridge', '--source', 'mdbridge', '--secret-file', str(hook_secret))
            # Core's settings reject port 0; ask the kernel for a free loopback port.
            probe = testports.bind_test_server(http.server.BaseHTTPRequestHandler)
            hook_port = probe.server_address[1]
            probe.server_close()
            cli('settings', 'set', 'hook_addr', f'127.0.0.1:{hook_port}')
            server.terminate()
            server.wait(timeout=10)
            log_path = root / 'isolated-server.log'
            with log_path.open('w') as log:
                server = subprocess.Popen(command + ['server', 'run'], env=env, cwd=root,
                                          stdout=log, stderr=log)
            deadline = time.monotonic() + 15
            while True:
                match = re.search(r'hook receiver ready.*?addr[= ](?:"?)(127\.0\.0\.1:\d+)', log_path.read_text())
                if match:
                    break
                if server.poll() is not None or time.monotonic() > deadline:
                    self.fail('isolated hook receiver did not start: ' + log_path.read_text())
                time.sleep(.02)
            incoming = dict(id='mdbridge:123:800', kind='message', chat='123', conversation='123', topic='general',
                            message_id='800', text='Synthetic ordinary reply', **{'from': dict(id='123', name='Pat')})
            req = urllib.request.Request('http://' + match[1] + '/hooks/mdbridge', data=json.dumps(incoming).encode(),
                                         headers={'Content-Type': 'application/json', 'X-Tgbridge-Secret': 'synthetic-hook'})
            with urllib.request.urlopen(req, timeout=10) as response:
                delivery = json.load(response)['seq']
            self.calls.clear()
            source = '  **Ready**\n\n```sh\necho "<b>literal</b>"\n```\n'
            env['TGBRIDGE_SECRET'] = 'CAPABILITY'  # the isolated CLI caller's own synthetic credential
            cli('reply', str(delivery), '--crew', 'writer', '--format', 'markdown', source)
            [wire] = [body for url, body in self.calls if url.endswith('/sendMessage')]
            self.assertEqual(wire['parse_mode'], 'HTML')
            self.assertEqual(wire['text'], '<b>Ready</b>\n\n<pre><code class="language-sh">echo "&lt;b&gt;literal&lt;/b&gt;"\n</code></pre>')
            self.assertEqual(self.store.read_messages('123', 50)[-1]['text'], 'Ready\n\necho "<b>literal</b>"\n')
            cli('reply', str(delivery), '--crew', 'writer', '--format', 'markdown', source)
            self.assertEqual(len([url for url, _ in self.calls if url.endswith('/sendMessage')]), 1)
            # Explicit bytes survived the native store and adapter in the bridge fingerprint.
            expected = dict(conversation=self.bridge.conversation, text=source, format='markdown')
            with self.store.connect() as db:
                rows = db.execute('SELECT key, fingerprint FROM deliveries').fetchall()
            matching = [key for key, fp in rows if fp == b.Bridge.delivery_fingerprint(dict(expected, idempotency_key=key))]
            self.assertEqual(len(matching), 1)
            # The declared source enum also reaches the API through the real core caller.
            result = json.loads(cli('call', 'mdbridge', 'send', 'text=**Source**', 'format=markdown', '--json'))
            self.assertTrue(result['delivered'])
            self.assertEqual(self.calls[-1][1]['text'], '<b>Source</b>')
            before = len(self.calls)
            source = '<code>echo ready && echo done</code>'
            result = json.loads(cli('call', 'mdbridge', 'send', 'text=' + source,
                                    'key=native-format-fallback', '--json'))
            self.assertEqual(result, dict(delivered=False, conversation='123',
                reason='html_text_not_escaped', failure_kind='permanent'))
            self.assertEqual(len(self.calls), before)
            self.assertIsNone(self.store.saved_receipt('native-format-fallback'))
            again = json.loads(cli('call', 'mdbridge', 'send', 'text=' + source,
                                   'key=native-format-fallback', '--json'))
            self.assertEqual(again, result)
            self.assertEqual(len(self.calls), before)
            plain_result = json.loads(cli('call', 'mdbridge', 'send', 'text=' + source,
                                   'key=native-format-fallback', 'format=plain', '--json'))
            self.assertEqual(plain_result, dict(delivered=True,
                message_id=self.store.saved_receipt('native-format-fallback')['message_id'],
                conversation='123'))
            self.assertEqual(self.calls[-1][1]['text'], source)
            self.assertNotIn('parse_mode', self.calls[-1][1])
            self.assertEqual(len(self.calls), before + 1)
            result = json.loads(cli('call', 'mdbridge', 'send', 'text=' + source,
                                    'key=native-explicit-plain', 'format=plain', '--json'))
            self.assertEqual(result, dict(delivered=True,
                message_id=self.store.saved_receipt('native-explicit-plain')['message_id'], conversation='123'))
            again = json.loads(cli('call', 'mdbridge', 'send', 'text=' + source,
                                   'key=native-explicit-plain', 'format=auto', '--json'))
            self.assertEqual(again, dict(delivered=False, conversation='123',
                reason='html_text_not_escaped', failure_kind='permanent'))
            self.assertEqual(len(self.calls), before + 1)
            result = json.loads(cli('call', 'mdbridge', 'send', 'text=```\necho legacy\n```', '--json'))
            self.assertTrue(result['delivered'])
            self.assertEqual(self.calls[-1][1]['text'], '```\necho legacy\n```')
            self.assertNotIn('parse_mode', self.calls[-1][1])
            cli('reply', str(delivery), '--crew', 'writer', '  ```\necho reply\n```  \n')
            self.assertEqual(self.calls[-1][1]['text'], '```\necho reply\n```')
            self.assertNotIn('parse_mode', self.calls[-1][1])
            before = len(self.calls)
            refused = subprocess.run(command + ['reply', str(delivery), '--crew', 'writer', '--format', 'rich', 'same'],
                                     env=env, cwd=root, text=True, capture_output=True, timeout=20)
            self.assertNotEqual(refused.returncode, 0)
            self.assertIn('format', refused.stderr)
            self.assertEqual(len(self.calls), before)
            html_refused = subprocess.run(command + ['reply', str(delivery), '--crew', 'writer', '--format', 'html',
                                                     'Nudge: <words>'],
                                          env=env, cwd=root, text=True, capture_output=True, timeout=20)
            self.assertNotEqual(html_refused.returncode, 0)
            self.assertIn('html_tag_not_allowed', html_refused.stderr)
            self.assertEqual(len(self.calls), before)

            captured = []
            def multipart(url, fields, field, path, timeout=20):
                captured.append((dict(fields), Path(path).read_bytes()))
                return self.multipart_transport(url, fields, field, path, timeout)
            self.bridge.request_multipart = multipart
            evidence = b'\x89PNG\r\n**literal file bytes**\0\xff\n'
            file = root / 'exact.png'
            file.write_bytes(evidence)
            cli('reply', str(delivery), '--crew', 'writer', '--format', 'markdown', '**Photo**', '--file', str(file))
            self.assertEqual(captured, [({'chat_id': '123', 'caption': '<b>Photo</b>', 'parse_mode': 'HTML'}, evidence)])
            self.assertEqual(hashlib.sha256(file.read_bytes()).digest(), hashlib.sha256(evidence).digest())
            self.exercise_weighty_confirmations(cli, client)
            self.exercise_same_yard_bots(root, cli, client, 'http://' + match[1], env, markdown_bridge)
        finally:
            testyardr.stop(server)

    def exercise_source_limit_and_trust(self, binary, root, home, command, env):
        client = n.YardSocketClient(home, 'tgbridge')

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
        self.documents = []
        self.sent_count = 10000
        self.bridge.request_multipart = lambda *args, **kwargs: NativeCase.pack_transport(self, *args, **kwargs)
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
        query['data'] = f"runask:{row['id']}:confirm:{self.bridge.run_asks.get(row['id'])['pending']['nonce']}"
        self.bridge.inbound(dict(update_id=43, callback_query=query))
        settled = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]
        self.assertEqual(settled['state'], 'answered')
        self.assertEqual(settled['ran']['argv'], ['user', 'add', 'synthetic-member'])
        self.assertEqual(client.call('GetUser', {'name': 'synthetic-member'})['name'], 'synthetic-member')
        additions = client.call('EventsOfKind', ['user_added', 100])
        self.assertEqual(len([event for event in additions if event['data'].get('approved', {}).get('ask') == strong['ask']]), 1)
        return client, cli, telegram

    def exercise_same_yard_bots(self, root, cli, client, hook_base, env, primary):
        cli('source', 'set', 'mdbridge', 'updates_per_hour=300')
        primary_source = client.call('GetSource', ['mdbridge'])
        state = root / 'secretary-state'
        state.mkdir(mode=0o700)
        store = b.Store(state / 'telegram-bridge.sqlite3', 0)
        config = {key: value for key, value in primary.config.items()
                  if key != 'yardr_socket'}
        config.update(bot_id=998, account_id='secretary_bot', agent='secretary', yardr_source='tgsecretary',
                      hook_url=hook_base + '/hooks/tgsecretary', hook_secret='secretary-hook', yardr_cards=False)
        sent = []

        def telegram(url, body, *args, **kwargs):
            sent.append((url, body))
            if url.endswith('/getUpdates'):
                return 200, dict(ok=True, result=[])
            if url.endswith(('/pinChatMessage', '/unpinChatMessage')):
                return 200, dict(ok=True, result=True)
            return 200, dict(ok=True, result=dict(message_id=77, chat=dict(id=123)))

        second = b.Bridge(config, store, 'SECOND_TOKEN', 'SECOND_CAPABILITY', request=telegram, state_dir=state)
        port = self.start_callback_server(second)
        pack = root / 'secretary-pack'
        pack.mkdir()
        b.stage_pack(b.render_pack(Path(__file__).resolve().parents[1] / 'yardr', port, 'tgsecretary'), pack)
        credential = root / 'secretary.env'
        credential.write_text('TGBRIDGE_SECRET=SECOND_CAPABILITY\n')
        credential.chmod(0o600)
        cli('pack', 'check', str(pack))
        cli('pack', 'apply', str(pack), '--yes', '--source-env', 'tgsecretary=' + str(credential))
        cli('source', 'set', 'tgsecretary', 'updates_per_hour=300')
        cli('user', 'identity', 'pat', '--source', 'tgsecretary', '--id', '123')
        cli('crew', 'add', 'secretary', '--dir', str(root), '--set', 'kind=codex',
            '--set', 'env_file=' + str(credential))
        for crew, source in (('writer', 'mdbridge'), ('secretary', 'tgsecretary')):
            cli('subscribe', crew, source, '--scope', 'all', '--where', 'type=message')
        secret = root / 'secretary-hook.secret'
        secret.write_text('secretary-hook')
        secret.chmod(0o600)
        cli('hook', 'add', 'tgsecretary', '--source', 'tgsecretary', '--secret-file', str(secret))
        # The secondary cannot discover or post the primary's real native asks.
        before = len(sent)
        second.poll_once()
        self.assertEqual(second.run_asks.rows(), [])
        for call, fields in (('run_ask', ['bead=sample-1', 'ask=7']),
                             ('ask_card', ['bead=sample-1', 'ask=7']), ('siding_card', ['depot=synthetic'])):
            refused = json.loads(cli('call', 'tgsecretary', call, *fields, 'key=second-' + call, '--json'))
            self.assertFalse(refused['delivered'])
            self.assertIn('not_configured', refused['reason'])
        self.assertEqual(len(sent), before + 1)  # only getUpdates
        seqs = []
        for bridge, source, crew, outgoing, capability in (
                (primary, 'mdbridge', 'writer', self.calls, 'CAPABILITY'),
                (second, 'tgsecretary', 'secretary', sent, 'SECOND_CAPABILITY')):
            # reply uses the calling crew process's credential, not the source key.
            env['TGBRIDGE_SECRET'] = capability
            bridge.reactions['on_accept'], bridge.typing_seconds = False, 0
            saved_request, saved_url, saved_secret = bridge.request, bridge.hook_url, bridge.hook_secret
            accepted, interrupted = [], [False]

            def request(url, body, *args, **kwargs):
                if url != bridge.hook_url:
                    if url.endswith(('/pinChatMessage', '/unpinChatMessage')):
                        outgoing.append((url, body))
                        return 200, dict(ok=True, result=True)
                    return saved_request(url, body, *args, **kwargs)
                req = urllib.request.Request(url, data=json.dumps(body).encode(),
                                             headers=kwargs['headers'] | {'Content-Type': 'application/json'})
                with urllib.request.urlopen(req, timeout=10) as response:
                    result = json.load(response)
                accepted.append(result['seq'])
                if not interrupted[0]:
                    interrupted[0] = True
                    raise b.TransportError('synthetic crash after native accept')
                return 200, result

            bridge.request, bridge.hook_url = request, hook_base + '/hooks/' + source
            bridge.hook_secret = 'synthetic-hook' if source == 'mdbridge' else 'secretary-hook'
            try:
                update = dict(update_id=95000, message=dict(message_id=95000, text=source + ' inbound',
                              chat=dict(id=123, type='private'), **{'from': dict(id=123, is_bot=False)}))
                offset = bridge.store.offset()
                with self.assertRaisesRegex(b.TransportError, 'synthetic crash'):
                    bridge.drain([update])
                self.assertEqual(bridge.store.offset(), offset)
                bridge.drain([update])
                self.assertEqual(accepted[0], accepted[1])
                self.assertEqual(bridge.store.offset(), 95001)
                seqs.append(accepted[0])
                cli('reply', str(accepted[0]), '--crew', crew, source + ' reply')
                self.assertEqual([body for url, body in outgoing if url.endswith('/sendMessage')][-1]['text'], source + ' reply')
                sent_before = len(outgoing)
                for repeat in range(2):
                    result = json.loads(cli('call', source, 'send', 'text=' + source + ' keyed',
                                            'key=same-two-bot-key', '--json'))
                    self.assertTrue(result['delivered'])
                self.assertEqual(len(outgoing), sent_before + 1)
                conflict = json.loads(cli('call', source, 'send', 'text=changed', 'key=same-two-bot-key', '--json'))
                self.assertFalse(conflict['delivered'])
                pinned = json.loads(cli('call', source, 'pin', 'slot=two-bots', 'text=' + source + ' pin', '--json'))
                self.assertTrue(pinned['delivered'], pinned)
                full = cli('thread', 'show', str(accepted[0]))
                self.assertIn(source + ' inbound', full)
                messages = bridge.store.read_messages('123', 200)
                self.assertEqual(sum(m['text'] == source + ' inbound' for m in messages), 1)
                self.assertTrue(any(m['text'] == source + ' reply' for m in messages))
                self.assertFalse(any(m['text'] == ('tgsecretary' if source == 'mdbridge' else 'mdbridge') + ' inbound' for m in messages))
            finally:
                bridge.request, bridge.hook_url, bridge.hook_secret = saved_request, saved_url, saved_secret
        self.assertNotEqual(*seqs)
        for crew, source in (('writer', 'mdbridge'), ('secretary', 'tgsecretary')):
            threads = client.call('Threads', [crew])
            self.assertIn((source, '123'), [(t['source'], t['thread']) for t in threads])
            other_source = 'tgsecretary' if source == 'mdbridge' else 'mdbridge'
            self.assertNotIn((other_source, '123'), [(t['source'], t['thread']) for t in threads])
        primary_pin = primary.store.pin_state('two-bots')
        self.assertIsNotNone(primary_pin['message_id'])
        self.assertTrue(json.loads(cli('call', 'tgsecretary', 'unpin', 'slot=two-bots', '--json'))['delivered'])
        self.assertEqual(primary.store.pin_state('two-bots'), primary_pin)
        self.assertIsNone(store.pin_state('two-bots')['message_id'])
        self.assertEqual(client.call('GetSource', ['mdbridge']), primary_source)
        req = urllib.request.Request(f'http://127.0.0.1:{port}/v1/send', data=b'{}',
                                     headers={'Authorization': 'Bearer CAPABILITY', 'Content-Type': 'application/json'})
        with self.assertRaises(urllib.error.HTTPError) as refused:
            urllib.request.urlopen(req, timeout=10)
        self.assertEqual(refused.exception.code, 403)
        refused.exception.close()
        print('Same-yard native store: same chat/message/key, distinct sources/bots/ledgers, replies/readback, '
              'accept-before-cursor crash replay, key/secret conflicts, secondary native refusal')

    def exercise_weighty_confirmations(self, cli, client):
        cli('bead', 'create', 'Synthetic confirmation boundary', '--id', 'confirm-1')
        clock = [1000]
        updates = iter(range(30000, 40000))
        self.bridge.run_asks.now = lambda: clock[0]

        def current(row):
            return next(a for a in client.call('Bead', dict(bead='confirm-1', viewer='pat'))['asks']
                        if a['ask'] == row['ask'])

        def saved(row):
            return self.bridge.run_asks.get(row['id'])

        def make(label):
            cli('--actor', 'proposer', 'bead', 'ask', 'confirm-1', 'Add the synthetic member?',
                '--to', 'pat', '--context', 'Isolated trust confirmation ' + label,
                '--run', 'user add synthetic-member-' + label)
            ask = client.call('Bead', dict(bead='confirm-1', viewer='pat'))['asks'][-1]
            self.assertEqual(ask['weight'][0]['class'], 'trust')
            self.assertTrue(self.bridge.api_run_ask(dict(bead='confirm-1', ask=ask['ask'], key='confirm-' + label))['delivered'])
            return next(r for r in self.bridge.run_asks.rows() if r['bead'] == 'confirm-1' and r['ask'] == ask['ask'])

        def update(row, action='approve'):
            if action == 'confirm':
                action += ':' + saved(row)['pending']['nonce']
            uid = next(updates)
            return dict(update_id=uid, callback_query=dict(id=f'confirm-{uid}', data=f"runask:{row['id']}:{action}",
                message=dict(message_id=row['message_id'], chat=dict(id=123, type='private'),
                             **{'from': dict(id=999, is_bot=True)}), **{'from': dict(id=123, is_bot=False)}))

        def tap(row, action='approve'):
            value = update(row, action)
            self.bridge.inbound(value)
            return value

        def poll(batch):
            telegram = self.bridge.telegram
            def fetched(method, body, timeout=15):
                return (200, dict(ok=True, result=batch)) if method == 'getUpdates' else telegram(method, body, timeout)
            with patch.object(self.bridge, 'telegram', side_effect=fetched):
                self.bridge.poll_once()

        def restart():
            transport = self.bridge.request
            self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=transport)
            self.bridge.hook_secret = None
            self.bridge.run_asks.now = lambda: clock[0]

        def events(row):
            return [e for e in client.call('EventsOfKind', ['user_added', 1000])
                    if e['data'].get('approved', {}).get('ask') == row['ask']
                    and e['data'].get('approved', {}).get('bead') == 'confirm-1']

        def untouched(row, before):
            self.assertEqual(current(row), before)
            self.assertEqual(events(row), [])
            self.assertNotIn('ran', current(row))
            with self.assertRaises(n.RPCRefused):
                client.call('GetUser', dict(name=before['run']['argv'][2]))

        def no_controls(row, words):
            edits = [body for url, body in self.calls if url.endswith('/editMessageText')
                     and body['message_id'] == row['message_id']]
            self.assertIn(words, edits[-1]['text'])
            self.assertEqual(edits[-1]['reply_markup']['inline_keyboard'], [])

        row = make('r1')
        tap(row)
        before = current(row)
        self.assertEqual(before['state'], 'open')
        self.assertNotIn('ran', before)
        confirmation = tap(row, 'confirm')
        settled = current(row)
        self.assertEqual(settled['state'], 'answered')
        self.assertEqual(settled['ran']['argv'], ['user', 'add', 'synthetic-member-r1'])
        self.assertEqual(client.call('GetUser', dict(name='synthetic-member-r1'))['name'], 'synthetic-member-r1')
        self.assertEqual(len(events(row)), 1)
        self.bridge.inbound(confirmation)
        confirmation['update_id'] = next(updates)
        self.bridge.inbound(confirmation)
        self.assertEqual(current(row), settled)
        self.assertEqual(len(events(row)), 1)

        row = make('r2')
        tap(row)
        arm = saved(row)['pending']
        before = current(row)
        other = make('other-card')
        for kind in ('topic', 'chat', 'user', 'bot', 'forward', 'other-card', 'group', 'message-bot'):
            with self.subTest(real_confirm_origin=kind):
                value = update(row, 'confirm')
                query = value['callback_query']
                message = query['message']
                if kind == 'topic':
                    message.update(message_thread_id=12, is_topic_message=True)
                elif kind == 'chat':
                    message['chat']['id'] = 124
                elif kind == 'user':
                    query['from']['id'] = 124
                elif kind == 'bot':
                    query['from']['is_bot'] = True
                elif kind == 'forward':
                    message['forward_origin'] = dict(type='hidden_user')
                elif kind == 'other-card':
                    message['message_id'] = other['message_id']
                elif kind == 'group':
                    message['chat']['type'] = 'group'
                else:
                    message['from']['id'] = 998
                self.bridge.inbound(value)
                self.assertEqual(saved(row)['pending'], arm)
                untouched(row, before)

        for kind in ('taken-back', 'proposal', 'socket-answer'):
            with self.subTest(real_confirm_stale=kind):
                row = make('r3-' + kind)
                tap(row)
                if kind == 'taken-back':
                    client.call('TakeBackAsk', dict(bead='confirm-1', ask=row['ask'], actor='proposer',
                                                   reason='synthetic withdrawal'))
                elif kind == 'proposal':
                    cli('bead', 'update', 'confirm-1', '--title', 'Synthetic edited confirmation')
                else:
                    client.call('ApproveAsk', dict(bead='confirm-1', ask=row['ask'], actor='pat', weighed=True, values={}))
                before = current(row)
                prior_events = events(row)
                tap(row, 'confirm')
                self.assertEqual(current(row), before)
                self.assertEqual(events(row), prior_events)
                if kind == 'taken-back':
                    no_controls(row, 'Taken back')
                elif kind == 'socket-answer':
                    no_controls(row, 'approved, ran')
                else:
                    no_controls(row, 'Proposal changed')
                    cli('bead', 'update', 'confirm-1', '--title', 'Synthetic confirmation boundary')

        row = make('r3-old-nonce')
        tap(row)
        old = 'confirm:' + saved(row)['pending']['nonce']
        tap(row, 'cancel')
        tap(row)
        before = current(row)
        tap(row, old)
        self.assertIsNone(saved(row)['pending'])
        untouched(row, before)

        for lapse in ('tap', 'poll', 'backwards'):
            with self.subTest(real_confirm_expiry=lapse):
                row = make('r4-' + lapse)
                tap(row)
                value = update(row, 'confirm')
                before = current(row)
                clock[0] += -1 if lapse == 'backwards' else 61
                poll([]) if lapse == 'poll' else self.bridge.inbound(value)
                self.assertIsNone(saved(row)['pending'])
                self.assertIn(n.CONFIRM_EXPIRED, saved(row)['display']['footer'])
                self.assertEqual(len(saved(row)['display']['keyboard']), 1)
                untouched(row, before)

        row = make('r5-cancel')
        tap(row)
        old = update(row, 'confirm')
        before = current(row)
        tap(row, 'cancel')
        self.bridge.inbound(old)
        untouched(row, before)
        row = make('r5-decline')
        tap(row)
        tap(row, 'decline')
        self.assertEqual(current(row)['state'], 'declined')
        self.assertEqual(current(row)['words'], 'Declined in Telegram')
        self.assertEqual(events(row), [])
        self.assertNotIn('ran', current(row))

        row = make('r6')
        tap(row, 'confirm:' + '0' * 16)
        self.assertIn('Nothing to confirm', saved(row)['display']['footer'])
        before = current(row)
        approval = tap(row)
        arm = saved(row)['pending']
        with patch.object(self.bridge, '_answer_callback', wraps=self.bridge._answer_callback) as answer:
            self.bridge.inbound(approval)
            self.assertEqual(answer.call_args.args[2], 'Already handled')
        clock[0] += 10
        tap(row)
        self.assertEqual(saved(row)['pending'], arm)
        untouched(row, before)

        for expired in (False, True):
            row = make('r7-' + str(expired).lower())
            tap(row)
            value = update(row, 'confirm')
            restart()
            if expired:
                clock[0] += 61
            before = current(row)
            calls_before = len(self.calls)
            poll([value])
            if expired:
                edits = [body for url, body in self.calls[calls_before:] if url.endswith('/editMessageText')
                         and body['message_id'] == row['message_id']]
                self.assertIn(n.CONFIRM_EXPIRED, edits[0]['text'])
                self.assertIn('Nothing to confirm', edits[1]['text'])
                untouched(row, before)
                self.assertIsNone(saved(row)['pending'])
            else:
                self.assertEqual(current(row)['state'], 'answered')
                self.assertEqual(len(events(row)), 1)

        for confirm_first in (False, True):
            row = make('r8-' + str(confirm_first).lower())
            before = current(row)
            tap(row)
            old = 'confirm:' + saved(row)['pending']['nonce']
            tap(row, 'cancel')
            actions = [old, 'approve'] if confirm_first else ['approve', old]
            poll([update(row, action) for action in actions])
            untouched(row, before)
            if not saved(row)['pending']:
                poll([update(row)])
            poll([update(row, 'confirm')])
            self.assertEqual(current(row)['state'], 'answered')
            self.assertEqual(len(events(row)), 1)
        print('RealNative confirm R1-R8: real isolated RPC/store, fake Telegram, no live state')

    def exercise_source_packs(self, root, home, cli, client, native_command, native_env):
        cli('bead', 'create', 'Synthetic source pack review', '--id', 'pack-1')
        cli('crew', 'add', 'reviewer', '--set', 'kind=codex')
        cli('crew', 'add', 'writer', '--set', 'kind=codex')
        checkout = root / 'checkout'
        git_env = dict(HOME=str(root), PATH='/usr/local/bin:/usr/bin:/bin')
        for words in (['init', '-q', str(checkout)],
                      ['-C', str(checkout), '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid',
                       'commit', '-q', '--allow-empty', '-m', 'Initial']):
            subprocess.run(['git', *words], env=git_env, check=True, capture_output=True)
        cli('depot', 'add', 'synthetic', str(checkout), '--base', 'HEAD')
        pack = checkout / 'pack'
        pack.mkdir()
        legacy = pack_proposal('directory_prealignment')['run']['pack']['directory']['files']
        for file in legacy:
            path = pack / file['path']
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(file['text'].encode())
        cli('source', 'rm', 'synthetic')
        updates = iter(range(20000, 22000))

        def current(number):
            return next(a for a in client.call('Bead', dict(bead='pack-1', viewer='pat'))['asks'] if a['ask'] == number)

        def press(row, action='approve'):
            if action == 'confirm':
                action += ':' + (self.bridge.run_asks.get(row['id']).get('pending') or {}).get('nonce', '0' * 16)
            self.bridge.inbound(dict(update_id=next(updates), callback_query=dict(id='pack-callback',
                data=f"runask:{row['id']}:{action}", message=dict(message_id=row['message_id'],
                    chat=dict(id=123, type='private'), **{'from': dict(id=999, is_bot=True)}),
                **{'from': dict(id=123, is_bot=False)})))

        def propose(path, pin=False):
            run = 'pack apply ' + str(path)
            if pin:
                digest = re.search(r'sha256:([0-9a-f]{64})', cli('pack', 'check', str(path)))[1]
                run += ' --sha256 ' + digest
            cli('--actor', 'proposer', 'bead', 'ask', 'pack-1', 'Review the synthetic pack?', '--to', 'pat',
                '--context', 'Synthetic isolated pack review', '--run', run)
            return client.call('Bead', dict(bead='pack-1', viewer='pat'))['asks'][-1]

        def publish(ask):
            before = len(self.calls), len(self.documents)
            receipt = self.bridge.api_run_ask(dict(bead='pack-1', ask=ask['ask'], key='pack-' + str(ask['ask'])))
            replacements = [line for line in ask['run']['does'] if line.startswith('it would replace the source ')]
            if any('; ' not in line for line in replacements):
                self.assertFalse(receipt['delivered'], receipt)
                self.assertTrue(receipt['reason'].endswith(n.FALLBACK), receipt)
                self.assertEqual((len(self.calls), len(self.documents)), before)
                return None
            self.assertTrue(receipt['delivered'], receipt)
            row = next(r for r in self.bridge.run_asks.rows() if r['bead'] == 'pack-1' and r['ask'] == ask['ask'])
            self.assertEqual(row['weighty'], bool(ask.get('weight')))
            NativeTests.assert_pack_document(self, ask, self.documents[-1][2])
            review = NativeTests.bundle_review(row)
            for carried in ask['run']['carried']:
                NativeTests.assert_source_review(self, dict(run=dict(carried=[carried])), review)
            return row

        def approve(row, ask):
            press(row)
            if ask.get('weight'):
                self.assertEqual(current(ask['ask'])['state'], 'open')
                press(row, 'confirm')

        def stale(row, ask):
            native = current(ask['ask'])
            self.assertEqual(native['state'], 'open')
            self.assertNotIn('ran', native)
            saved = self.bridge.run_asks.get(row['id'])
            self.assertEqual(saved['state'], 'stale')
            self.assertIsNone(saved['pending'])
            self.assertIn('Stale source pack proposal; nothing ran. Decline it in the yard and ask again.',
                          saved['display']['footer'])
            self.assertEqual(saved['display']['keyboard'], [])
            edits = [body for url, body in self.calls if url.endswith('/editMessageText')
                     and body['message_id'] == row['message_id']]
            self.assertIn(saved['display']['footer'], edits[-1]['text'])
            self.assertEqual(edits[-1]['reply_markup']['inline_keyboard'], [])
            self.bridge.run_asks = n.RunAsks(self.bridge)
            self.bridge.run_asks.refresh()
            press(row); press(row, 'confirm')
            self.assertEqual(current(ask['ask']), native)
            self.assertEqual(self.bridge.run_asks.get(row['id'])['state'], 'stale')
            self.assertEqual(self.bridge.run_asks.get(row['id'])['display']['keyboard'], [])
            self.assertFalse(any(e['data'].get('approved', {}).get('ask') == ask['ask']
                                 and e['data'].get('approved', {}).get('bead') == 'pack-1'
                                 for e in (client.call('EventsOfKind', ['pack_applied', 100]) or [])))

        def settled(row, ask):
            deadline = time.monotonic() + 15
            while current(ask['ask']).get('ran', {}).get('result') == n.PENDING:
                self.assertLess(time.monotonic(), deadline, 'pack carry did not settle')
                time.sleep(.02)
            result = current(ask['ask'])
            self.assertEqual(result['state'], 'answered', result)
            self.assertFalse(result['ran'].get('failed'), result['ran'])
            for carried in ask['run']['carried']:
                self.assertEqual(client.call('GetSource', [carried['name']])['declaration'].encode(), carried['text'].encode())
                expected = {file['program']: file['text'].encode() for file in carried.get('files', [])}
                directory = home / 'sources' / carried['name']
                actual = {p.relative_to(directory).as_posix(): p.read_bytes() for p in directory.rglob('*') if p.is_file()}
                self.assertEqual(actual, expected)
                self.assertTrue(all((directory / path).stat().st_mode & 0o777 == 0o700 for path in expected))
            self.bridge.run_asks = n.RunAsks(self.bridge)
            self.bridge.run_asks.refresh()
            press(row); press(row, 'confirm')
            self.assertEqual(current(ask['ask']), result)
            events = [e for e in client.call('EventsOfKind', ['pack_applied', 100])
                      if e['data'].get('approved', {}).get('ask') == ask['ask']
                      and e['data'].get('approved', {}).get('bead') == 'pack-1']
            self.assertEqual(len(events), 1)

        with self.subTest(real_pack='pinned-new-source-stale'):
            ask = propose(pack, pin=True)
            row = publish(ask)
            declaration = pack / 'source.yaml'
            declaration_bytes = declaration.read_bytes()
            try:
                declaration.write_bytes(declaration_bytes + b'# Synthetic input changed after pinned card\n')
                approve(row, ask)
            finally:
                declaration.write_bytes(declaration_bytes)
            stale(row, ask)
            with self.assertRaises(n.RPCRefused):
                client.call('GetSource', ['synthetic'])
            self.assertFalse((home / 'sources/synthetic').exists())

        for kind in ('directory', 'directory_replace'):
            with self.subTest(captured_pack=kind):
                if kind == 'directory_replace':
                    c = pack_proposal(kind)['run']['carried'][0]
                    (pack / 'source.yaml').write_bytes(c['text'].encode())
                    (pack / 'bin/report').write_bytes(c['files'][0]['text'].encode())
                    (home / 'sources/synthetic/obsolete.txt').write_bytes(b'Synthetic obsolete copy\r\n')
                ask = propose(pack, pin=True)
                self.assert_source_pack_fixture(ask, root, 'pack_' + kind)
                row = publish(ask)
                if row:
                    approve(row, ask); settled(row, ask)
                else:
                    cli('pack', 'apply', str(pack), '--yes')
                if kind == 'directory':
                    cli('source', 'grant', 'synthetic', 'health', '--crew', 'reviewer')

        full = checkout / 'tgbridge-pack'
        shutil.copytree(Path(__file__).resolve().parents[1] / 'yardr', full)
        cli('pack', 'apply', str(full), '--yes')
        cli('source', 'grant', 'tgbridge', 'health', '--crew', 'reviewer')
        outside = root / 'installed-pack'
        shutil.copytree(full, outside)
        refused = subprocess.run(native_command + ['--actor', 'proposer', 'bead', 'ask', 'pack-1',
            'Review an installed pack?', '--to', 'pat', '--context', 'Synthetic outside-checkout refusal',
            '--run', 'pack apply ' + str(outside)], env=native_env, cwd=root, text=True, capture_output=True, timeout=30)
        self.assertNotEqual(refused.returncode, 0)
        self.assertIn('not inside a registered depot', refused.stderr)
        self.exercise_tgbridge_source(root, home, cli, client, full)
        for scenario in ('unchanged', 'pinned-routine-stale', 'pinned-weighted-stale', 'weighted', 'directory-stale', 'directory-content-stale', 'source-stale', 'copy-stale', 'grant-stale', 'decline', 'cancel'):
            with self.subTest(real_pack=scenario):
                program = full / 'tgbridge/normalize'
                weighty = scenario not in ('unchanged', 'pinned-routine-stale')
                if weighty:
                    program.write_bytes(program.read_bytes() + b'\n# Synthetic captured revision\n')
                ask = propose(full, pin=scenario.startswith('pinned-'))
                self.assertEqual(bool(ask.get('weight')), weighty)
                row = publish(ask)
                if row is None:
                    continue
                if scenario == 'unchanged':
                    self.assertIn('grants kept for source tgbridge: crew member reviewer: health (kept).', row['plain'])
                before = client.call('GetSource', ['tgbridge'])
                target = home / 'sources/tgbridge/tgbridge/normalize'
                before_bytes = target.read_bytes()
                manifest = full / 'pack.yaml'
                manifest_bytes = manifest.read_bytes()
                declaration = full / 'tgbridge.yaml'
                declaration_bytes = declaration.read_bytes()
                if scenario == 'directory-stale':
                    manifest.unlink()
                elif scenario == 'directory-content-stale' or scenario.startswith('pinned-'):
                    declaration.write_bytes(declaration_bytes + b'# Synthetic input changed after card\n')
                elif scenario == 'source-stale':
                    changed = root / 'changed-source.yaml'
                    changed.write_bytes(before['declaration'].encode() + b'\n# changed after capture\n')
                    for file in ask['run']['carried'][0].get('files', []):
                        path = root / file['program']; path.parent.mkdir(exist_ok=True)
                        path.write_bytes((home / 'sources/tgbridge' / file['program']).read_bytes())
                    cli('source', 'add', str(changed))
                elif scenario == 'copy-stale':
                    target.write_bytes(b'Synthetic standing copy drift\n')
                elif scenario == 'grant-stale':
                    cli('source', 'grant', 'tgbridge', 'health', '--crew', 'writer')
                standing = client.call('GetSource', ['tgbridge'])
                standing_bytes = target.read_bytes()
                if scenario == 'decline':
                    press(row, 'decline')
                    self.assertEqual(current(ask['ask'])['state'], 'declined')
                elif scenario == 'cancel':
                    press(row); press(row, 'cancel'); press(row, 'confirm')
                    self.assertEqual(current(ask['ask'])['state'], 'open')
                else:
                    approve(row, ask)
                manifest.write_bytes(manifest_bytes)
                declaration.write_bytes(declaration_bytes)
                if scenario in ('unchanged', 'weighted'):
                    settled(row, ask)
                else:
                    self.assertEqual(client.call('GetSource', ['tgbridge']), standing)
                    self.assertEqual(target.read_bytes(), standing_bytes)
                    if scenario.endswith('-stale'):
                        stale(row, ask)
                if scenario == 'copy-stale':
                    target.write_bytes(before_bytes)
        cli('--actor', 'proposer', 'bead', 'ask', 'pack-1', 'Review the embedded source pack?', '--to', 'pat',
            '--context', 'Synthetic embedded source pack review', '--run', 'pack apply telegram')
        ask = client.call('Bead', dict(bead='pack-1', viewer='pat'))['asks'][-1]
        self.assert_source_pack_fixture(ask, root, 'pack_embedded')
        row = publish(ask)
        approve(row, ask); settled(row, ask)

    def assert_source_pack_fixture(self, ask, root, name):
        actual = json.loads(json.dumps(dict(ask, ask=7, since='2026-01-01T00:00:00Z')).replace(str(root), '/synthetic'))
        # A build label is provenance; compare all remaining wire keys against the aligned, upstream, or public main capture.
        for suffix in ('', '_upstream', '_public_main'):
            path = Path(__file__).with_name('fixtures') / (name + suffix + '.json')
            if not path.exists():
                continue
            fixture = json.loads(path.read_text())
            trial = copy.deepcopy(actual)
            if 'pack' in trial['run']:
                label, expected = trial['run']['pack']['label'], fixture['run']['pack']['label']
                trial['run']['pack']['label'] = expected
                trial['run']['does'][0] = trial['run']['does'][0].replace(label, expected)
            if trial == fixture:
                return
        self.assertEqual(actual, fixture)

    def exercise_tgbridge_source(self, root, home, cli, client, full):
        cli('--actor', 'proposer', 'bead', 'ask', 'pack-1', 'Review the trimmed bridge declaration?', '--to', 'pat',
            '--context', 'Synthetic full source replacement', '--run', 'source add ' + str(full / 'tgbridge.yaml'))
        ask = client.call('Bead', dict(bead='pack-1', viewer='pat'))['asks'][-1]
        count = len(self.calls), len(self.documents)
        receipt = self.bridge.api_run_ask(dict(bead='pack-1', ask=ask['ask'], key='tgbridge-replacing'))
        if not any('; grants kept for source tgbridge:' in line for line in ask['run']['does']):
            self.assertFalse(receipt['delivered'])
            self.assertEqual((len(self.calls), len(self.documents)), count)
            self.assertTrue(receipt['reason'].endswith(n.FALLBACK))
            return
        self.assertTrue(receipt['delivered'], receipt)
        row = next(r for r in self.bridge.run_asks.rows() if r['bead'] == 'pack-1' and r['ask'] == ask['ask'])
        self.assertIn('grants kept for source tgbridge: crew member reviewer: health (kept).', row['plain'])
        NativeTests.assert_source_review(self, ask, NativeTests.bundle_review(row))
        print(f"Synthetic tgbridge replacing card: {len(ask['run']['does'])} Does lines, {len(ask['run']['carried'][0]['text'].encode())} declaration bytes")
        updates = iter(range(30000, 31000))
        def press(action):
            if action == 'confirm':
                action += ':' + self.bridge.run_asks.get(row['id'])['pending']['nonce']
            self.bridge.inbound(dict(update_id=next(updates), callback_query=dict(id='tgbridge-source',
                data=f"runask:{row['id']}:{action}", message=dict(message_id=row['message_id'], chat=dict(id=123, type='private'),
                    **{'from': dict(id=999, is_bot=True)}), **{'from': dict(id=123, is_bot=False)})))
        press('approve'); press('confirm')
        deadline = time.monotonic() + 15
        def current():
            return next(a for a in client.call('Bead', dict(bead='pack-1', viewer='pat'))['asks'] if a['ask'] == ask['ask'])
        while current().get('ran', {}).get('result') == n.PENDING:
            self.assertLess(time.monotonic(), deadline)
            time.sleep(.02)
        done = current()
        self.assertEqual(done['state'], 'answered')
        self.assertFalse(done['ran'].get('failed'), done['ran'])
        self.assertEqual(client.call('GetSource', ['tgbridge'])['declaration'].encode(), (full / 'tgbridge.yaml').read_bytes())
        for file in ask['run']['carried'][0]['files']:
            self.assertEqual((home / 'sources/tgbridge' / file['program']).read_bytes(), file['text'].encode())
        self.bridge.run_asks = n.RunAsks(self.bridge); self.bridge.run_asks.refresh()
        press('approve')
        self.assertEqual(current(), done)
        events = [e for e in client.call('EventsOfKind', ['source_added', 100])
                  if e['data'].get('approved', {}).get('ask') == ask['ask']
                  and e['data'].get('approved', {}).get('bead') == 'pack-1']
        self.assertEqual(len(events), 1)

    def exercise_source_adds(self, root, home, cli, client):
        cli('bead', 'create', 'Synthetic source review', '--id', 'source-1')
        updates = iter(range(10000, 11000))
        def current(number):
            return next(a for a in client.call('Bead', dict(bead='source-1', viewer='pat'))['asks'] if a['ask'] == number)
        def press(row, action='approve', **changes):
            if action == 'confirm':
                action += ':' + (self.bridge.run_asks.get(row['id']).get('pending') or {}).get('nonce', '0' * 16)
            message = dict(message_id=row['message_id'], chat=dict(id=123, type='private'),
                           **{'from': dict(id=999, is_bot=True)})
            message.update(changes)
            self.bridge.inbound(dict(update_id=next(updates), callback_query=dict(id='source-callback',
                data=f"runask:{row['id']}:{action}", message=message, **{'from': dict(id=123, is_bot=False)})))
        def reply(row, **changes):
            message = dict(message_id=next(updates), text=str(row['ask']), chat=dict(id=123, type='private'),
                reply_to_message=dict(message_id=row['message_id']), **{'from': dict(id=123, is_bot=False)})
            message.update(changes)
            with patch.object(self.bridge, '_relay', return_value=self.bridge.conversation):
                self.bridge.inbound(dict(update_id=next(updates), message=message))
        def source(name):
            try:
                return client.call('GetSource', [name])
            except n.RPCRefused:
                return None
        for scenario in ('new', 'replace', 'free', 'stale', 'declaration-stale', 'decline', 'cancel'):
            with self.subTest(real_source=scenario):
                kind = scenario if scenario in ('new', 'replace', 'free') else 'replace'
                fixture = source_proposal(kind)
                carried = fixture['run']['carried'][0]
                if scenario == 'replace':
                    (home / 'sources/synthetic/extra.txt').write_bytes(b'Synthetic removed text\r\n')
                file = root / ('source-' + kind + '.yaml')
                file.write_bytes(carried['text'].encode())
                for f in carried.get('files', []):
                    target = root / f['program']
                    target.parent.mkdir(exist_ok=True)
                    value = f['text'] if scenario in ('new', 'replace', 'free') else f['text'] + '# next\n'
                    target.write_bytes(value.encode())
                cli('--actor', 'proposer', 'bead', 'ask', 'source-1', 'Review the synthetic source?', '--to', 'pat',
                    '--context', 'Synthetic isolated source review', '--run', 'source add ' + str(file))
                ask = client.call('Bead', dict(bead='source-1', viewer='pat'))['asks'][-1]
                if scenario in ('new', 'replace', 'free'):
                    self.assert_source_pack_fixture(ask, root, 'source_' + kind)
                receipt = self.bridge.api_run_ask(dict(bead='source-1', ask=ask['ask'], key='source-' + scenario))
                if any(line.startswith('it would replace the source ') and '; ' not in line for line in ask['run']['does']):
                    self.assertFalse(receipt['delivered'], receipt)
                    self.assertTrue(receipt['reason'].endswith(n.FALLBACK), receipt)
                    self.assertFalse(any(r['bead'] == 'source-1' and r['ask'] == ask['ask'] for r in self.bridge.run_asks.rows()))
                    continue
                self.assertTrue(receipt['delivered'], receipt)
                row = next(r for r in self.bridge.run_asks.rows() if r['bead'] == 'source-1' and r['ask'] == ask['ask'])
                review = NativeTests.bundle_review(row)
                NativeTests.assert_source_review(self, ask, review)
                self.assertIsNotNone(row['bundle']['document']['message_id'])
                # Neither bridge nor carry may reopen provenance after capture.
                file.unlink()
                for f in carried.get('files', []):
                    (root / f['program']).unlink()
                before = source(carried['name'])
                target = home / 'sources' / carried['name'] / 'bin/report'
                before_bytes = target.read_bytes() if target.exists() else None
                if scenario == 'decline':
                    press(row, 'decline')
                    self.assertEqual(current(ask['ask'])['state'], 'declined')
                    self.assertEqual(source(carried['name']), before)
                    self.assertEqual(target.read_bytes(), before_bytes)
                    continue
                press(row, message_id=row['message_id'] + 1000)
                self.assertEqual(current(ask['ask'])['state'], 'open')
                press(row)
                if scenario == 'declaration-stale':
                    file.write_bytes(before['declaration'].encode() + b'# Synthetic standing declaration drift\n')
                    for f in carried.get('files', []):
                        (root / f['program']).write_bytes((home / 'sources' / carried['name'] / f['program']).read_bytes())
                    cli('source', 'add', str(file))
                    before = source(carried['name'])
                if scenario == 'stale':
                    target.write_bytes(b'Synthetic standing file changed after capture\n')
                if ask.get('weight'):
                    self.assertEqual(current(ask['ask'])['state'], 'open')
                    self.assertEqual(source(carried['name']), before)
                    reply(row, reply_to_message=dict(message_id=row['message_id'] + 1000))
                    self.assertEqual(current(ask['ask'])['state'], 'open')
                    if scenario == 'cancel':
                        press(row, 'cancel')
                        press(row, 'confirm')
                        self.assertEqual(current(ask['ask'])['state'], 'open')
                        self.assertEqual(source(carried['name']), before)
                        self.assertEqual(target.read_bytes(), before_bytes)
                        continue
                    press(row, 'confirm')
                if scenario in ('stale', 'declaration-stale'):
                    self.assertEqual(current(ask['ask'])['state'], 'open')
                    self.assertEqual(source(carried['name']), before)
                    self.assertEqual(target.read_bytes(), b'Synthetic standing file changed after capture\n' if scenario == 'stale' else before_bytes)
                    saved = self.bridge.run_asks.get(row['id'])
                    self.assertEqual(saved['state'], 'stale')
                    self.assertEqual(saved['display']['keyboard'], [])
                    self.assertIn('Stale source proposal', next(body['text'] for url, body in reversed(self.calls) if url.endswith('/editMessageText')))
                    target.write_bytes(before_bytes)
                    continue
                deadline = time.monotonic() + 10
                while current(ask['ask']).get('ran', {}).get('result') == n.PENDING:
                    self.assertLess(time.monotonic(), deadline, 'source carry did not settle')
                    time.sleep(.02)
                settled = current(ask['ask'])
                self.assertEqual(settled['state'], 'answered')
                self.assertFalse(settled['ran'].get('failed'), settled['ran'])
                self.assertEqual(source(carried['name'])['declaration'].encode(), carried['text'].encode())
                for f in carried.get('files', []):
                    installed = home / 'sources' / carried['name'] / f['program']
                    self.assertEqual(installed.read_bytes(), f['text'].encode())
                    self.assertEqual(installed.stat().st_mode & 0o777, 0o700)
                if scenario == 'replace':
                    self.assertFalse((home / 'sources/synthetic/extra.txt').exists())
                self.bridge.run_asks = n.RunAsks(self.bridge)
                self.bridge.run_asks.refresh()
                self.assertEqual(self.bridge.run_asks.get(row['id'])['state'], 'approved')
                snapshot = source(carried['name'])
                identity = (target.stat().st_ino, target.stat().st_mtime_ns) if target.exists() else None
                sends = len([1 for url, _ in self.calls if url.endswith('/sendMessage')])
                press(row)
                press(row, 'confirm')
                self.assertTrue(self.bridge.api_run_ask(dict(bead='source-1', ask=ask['ask'], key='replay-' + scenario))['delivered'])
                self.assertEqual(len([1 for url, _ in self.calls if url.endswith('/sendMessage')]), sends)
                self.assertEqual(current(ask['ask']), settled)
                self.assertEqual(source(carried['name']), snapshot)
                if identity:
                    self.assertEqual((target.stat().st_ino, target.stat().st_mtime_ns), identity)
                events = [e for e in client.call('EventsOfKind', ['source_added', 100])
                          if e['data'].get('approved', {}).get('ask') == ask['ask']
                          and e['data'].get('approved', {}).get('bead') == 'source-1']
                self.assertEqual(len(events), 1)

    def exercise_source_ten_commands(self, root, home, cli, client):
        prog = root / 'bin' / 'report'
        prog.parent.mkdir(exist_ok=True)
        prog.write_text('#!/bin/sh\necho ok\n')
        prog.chmod(0o700)
        calls = ''.join(
            f'  c{i}:\n    kind: read\n    request: {{method: POST, path: /c{i}}}\n    build: [bin/report, "--n={i}"]\n'
            for i in range(9)
        )
        decl = f'name: synthetic-ten\nbase: https://example.invalid\nlimits:\n  command: [bin/report]\ncalls:\n{calls}'
        file = root / 'source-ten.yaml'
        file.write_text(decl)
        cli('--actor', 'proposer', 'bead', 'ask', 'source-1', 'Add 10-command synthetic source?', '--to', 'pat',
            '--context', 'Synthetic isolated ten-command source review', '--run', 'source add ' + str(file))
        ask = client.call('Bead', dict(bead='source-1', viewer='pat'))['asks'][-1]
        self.assertIn('(10 commands)', ask['run']['does'][0])
        receipt = self.bridge.api_run_ask(dict(bead='source-1', ask=ask['ask'], key='source-ten-commands'))
        self.assertTrue(receipt['delivered'], receipt)
        row = next(r for r in self.bridge.run_asks.rows() if r['bead'] == 'source-1' and r['ask'] == ask['ask'])
        for action in ('approve', 'confirm'):
            data = f"runask:{row['id']}:{action}"
            if action == 'confirm':
                data += ':' + (self.bridge.run_asks.get(row['id']).get('pending') or {}).get('nonce', '0' * 16)
            self.bridge.inbound(dict(update_id=40000 + (action == 'confirm'), callback_query=dict(
                id='ten-' + action, data=data,
                message=dict(message_id=row['message_id'], chat=dict(id=123, type='private'),
                             **{'from': dict(id=999, is_bot=True)}),
                **{'from': dict(id=123, is_bot=False)})))
        deadline = time.monotonic() + 10
        while True:
            settled = next(a for a in client.call('Bead', dict(bead='source-1', viewer='pat'))['asks'] if a['ask'] == ask['ask'])
            if settled.get('ran', {}).get('result') != n.PENDING:
                break
            self.assertLess(time.monotonic(), deadline, 'ten-command source carry did not settle')
            time.sleep(.02)
        self.assertEqual(settled['state'], 'answered')
        self.assertFalse(settled['ran'].get('failed'), settled['ran'])
        self.assertEqual(client.call('GetSource', ['synthetic-ten'])['declaration'], decl)

    def test_real_native_skill_cards_remove_and_assign_once_through_their_bindings(self):
        binary = testyardr.binary()
        if binary is None:
            self.skipTest('yardr binary required for isolated native integration')
        root = Path(self.tmp.name)
        server, home, command, env = testyardr.start(root, binary)
        try:
            # Fork-only: a core without the skill topic, or one before its card rows, has no card of this family.
            if not testyardr.serves(home, 'SkillDigest'):
                self.skipTest('fork-only skill administration cards: this core serves no skill topic (no SkillDigest)')
            client = n.YardSocketClient(home, 'tgbridge')

            def cli(*args, actor='pat', ok=True):
                result = subprocess.run(command[:-1] + [actor, *args], env=env, cwd=root, text=True, capture_output=True, timeout=30)
                if ok:
                    self.assertEqual(result.returncode, 0, result.stderr)
                return result

            def skill(name, text):
                folder = root / 'skills-in' / name
                (folder / 'helper').mkdir(parents=True, exist_ok=True)
                (folder / 'SKILL.md').write_text(f'---\nname: {name}\ndescription: {text}\n---\nSynthetic body.\n')
                (folder / 'helper' / 'run.sh').write_text('echo synthetic\n')
                cli('skill', 'add', str(folder))
                return client.call('SkillDigest', {'name': name})

            cli('settings', 'set', 'operator', 'pat')
            cli('settings', 'set', 'default_kind', 'codex')
            source = root / 'synthetic.yaml'
            source.write_text('name: tgbridge\nbase: https://example.invalid\ncalls:\n  health:\n    kind: read\n    request: {method: GET, path: /health}\n'
                              'hook:\n  bodies: unique\n  verify:\n    scheme: secret\n    signature: {header: X-Secret}\n  normalize: [/bin/false]\n')
            cli('source', 'add', str(source))
            cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
            cli('user', 'add', 'proposer')
            cli('bead', 'create', 'Synthetic skill administration', '--id', 'sample-1')
            trees = {name: skill(name, text) for name, text in (('notes', 'Write <b>release</b> notes "quoted" café 🙂'),
                                                                 ('spare', 'A spare skill'), ('triage', 'Sort new beads'))}
            cli('group', 'add', 'builders', '--runner', 'herdr', '--set', 'kind=claude')
            cli('group', 'add', 'web/reviewers', '--runner', 'herdr', '--set', 'kind=claude')
            cli('crew', 'add', 'clerk', '--dir', str(root), '--on=', '--set', 'kind=codex')
            cli('group', 'set', 'builders', 'skills=[notes,triage]')
            cli('group', 'set', 'web/reviewers', 'skills=[notes]')
            cli('crew', 'set', 'clerk', 'skills=[notes]')

            def propose(run):
                result = cli('bead', 'ask', 'sample-1', 'Synthetic: ' + run + '?', '--to', 'pat', '--context', 'Isolated skill card',
                             '--run', run, actor='proposer', ok=False)
                if result.returncode and '--run takes a command of the list' in result.stderr:
                    self.skipTest('fork-only skill administration cards: this skill core predates their card rows')
                self.assertEqual(result.returncode, 0, result.stderr)
                return client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]

            def current(number):
                return next(a for a in client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'] if a['ask'] == number)

            def events(kind):
                return client.call('EventsOfKind', [kind, 1000]) or []

            self.config.update(yardr_home=str(home), yardr_operator='pat', yardr_source='tgbridge')
            ids = iter(range(77, 900))
            def telegram(url, body, *args, **kwargs):
                status, response = self.transport(url, body, *args, **kwargs)
                if url.endswith('/sendMessage'):
                    response['result']['message_id'] = next(ids)
                elif url.endswith('/editMessageText'):
                    response = dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=123)))
                return status, response
            self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=telegram)
            self.bridge.hook_secret = None
            self.documents = []
            self.sent_count = 10000
            self.bridge.request_multipart = lambda *args, **kwargs: NativeCase.pack_transport(self, *args, **kwargs)
            updates = iter(range(70000, 71000))

            def poll(batch):
                fetch = self.bridge.telegram
                def fetched(method, body, timeout=15):
                    return (200, dict(ok=True, result=batch)) if method == 'getUpdates' else fetch(method, body, timeout)
                with patch.object(self.bridge, 'telegram', side_effect=fetched):
                    self.bridge.poll_once()

            def event(row, action='approve'):
                if action == 'confirm':
                    action += ':' + self.bridge.run_asks.get(row['id'])['pending']['nonce']
                return dict(update_id=next(updates), callback_query=dict(id='skill-callback', data=f"runask:{row['id']}:{action}",
                    message=dict(message_id=row['message_id'], chat=dict(id=123, type='private'), **{'from': dict(id=999, is_bot=True)}),
                    **{'from': dict(id=123, is_bot=False)}))

            def publish(ask):
                receipt = self.bridge.api_run_ask(dict(bead='sample-1', ask=ask['ask'], key='skill-' + str(ask['ask'])))
                self.assertTrue(receipt['delivered'], receipt)
                row = self.bridge.run_asks.get(self.bridge.run_asks.ident('sample-1', ask['ask']))
                self.assertTrue(row['weighty'])
                self.assertEqual(self.documents[-1][2].decode(), row['bundle']['document']['text'])
                return row

            def finish(ask, timeout=15):
                deadline = time.monotonic() + timeout
                while (value := current(ask['ask'])).get('ran', {}).get('result') in (None, n.PENDING):
                    self.assertLess(time.monotonic(), deadline, 'native skill carry did not settle')
                    time.sleep(.02)
                poll([])
                return value

            def footer(row):
                return self.bridge.run_asks.get(row['id'])['display']['footer']

            # Removal: the card shows the standing tree and every holder, and is listed and opened by /decide.
            removal = propose('skill rm notes --force')
            row = publish(removal)
            card = b.html_visible_text(self.calls[-1][1]['text'])[0]
            self.assertIn('tree ' + trees['notes'], card)
            for holder, after in (('crew member clerk', '(none)'), ('group builders', 'triage'), ('group web/reviewers', '(none)')):
                self.assertIn(holder + ': notes' + (', triage' if holder == 'group builders' else '') + ' -> ' + after, card)
            listed, _ = self.bridge.run_asks.decide_list()
            self.assertIn(f"run · sample-1#{removal['ask']}", listed)
            self.assertEqual(self.bridge.run_asks.decide_open('sample-1', removal['ask'], '123')[1][0], row['message_id'])

            # Spoofed taps, a forged nonce and an expired confirmation write nothing.
            for change in ('user', 'bot', 'chat', 'message'):
                wrong = event(row)
                query = wrong['callback_query']
                if change == 'user': query['from']['id'] = 124
                if change == 'bot': query['message']['from']['id'] = 998
                if change == 'chat': query['message']['chat']['id'] = 124
                if change == 'message': query['message']['message_id'] += 1
                poll([wrong])
                self.assertIsNone(self.bridge.run_asks.get(row['id'])['pending'])
            poll([event(row, 'confirm:0000000000000000')])
            poll([event(row)])
            armed = self.bridge.run_asks.get(row['id'])['pending']
            expired = event(row, 'confirm')
            with patch.object(self.bridge.run_asks, 'now', return_value=armed['expires'] + 1):
                poll([expired])
            # The poll's refresh retires the expired confirmation before its update drains; neither runs anything.
            self.assertIn(n.CONFIRM_EXPIRED, [body['text'].rpartition('\n')[2] for url, body in self.calls if url.endswith('/editMessageText')])
            self.assertIsNone(self.bridge.run_asks.get(row['id'])['pending'])
            self.assertEqual((current(removal['ask'])['state'], events('skill_removed')), ('open', []))

            # Holders changed since the card: the yard refuses the approval, nothing is removed, the card retires.
            cli('group', 'set', 'web/reviewers', '--unset', 'skills')
            poll([event(row)]); poll([event(row, 'confirm')])
            self.assertEqual((current(removal['ask'])['state'], self.bridge.run_asks.get(row['id'])['state']), ('open', 'stale'))
            self.assertEqual((client.call('SkillDigest', {'name': 'notes'}), events('skill_removed')), (trees['notes'], []))
            self.assertIn('Stale skill proposal; nothing ran.', footer(row))

            # A tree replaced since the card refuses the same way.
            moved = propose('skill rm spare')
            moved_row = publish(moved)
            trees['spare'] = skill('spare', 'A spare skill, replaced')
            poll([event(moved_row)]); poll([event(moved_row, 'confirm')])
            self.assertEqual((current(moved['ask'])['state'], self.bridge.run_asks.get(moved_row['id'])['state']), ('open', 'stale'))
            self.assertEqual(client.call('SkillDigest', {'name': 'spare'}), trees['spare'])

            # A fresh removal whose approval reply is lost: canonical state settles it, the server carries it out once.
            removal = propose('skill rm notes --force')
            row = publish(removal)
            poll([event(row)])
            confirmation = event(row, 'confirm')
            rpc = self.bridge.run_asks.client.call
            def lost(method, args):
                value = rpc(method, args)
                if method == 'ApproveAsk':
                    raise n.RPCAmbiguous('synthetic lost approval reply')
                return value
            with patch.object(self.bridge.run_asks.client, 'call', side_effect=lost):
                poll([confirmation])
            settled = finish(removal)
            self.assertFalse(settled['ran'].get('failed', False), settled)
            receipt = settled['ran']['result']
            self.assertRegex(receipt, rf"^skill notes removed \(operation card-[0-9a-f]{{32}}, tree {trees['notes']}\)\n")
            self.assertIn(receipt, footer(row))
            # Restart and replay: a new bridge state machine reconciles from the yard and writes nothing again.
            self.bridge.run_asks = n.RunAsks(self.bridge)
            poll([confirmation, event(row)])
            removed = events('skill_removed')
            self.assertEqual(len(removed), 1)
            data = removed[0]['data']
            self.assertEqual((data['name'], data['before'], data['after'], data['proposal'], data['actor']),
                             ('notes', trees['notes'], 'absent', f"sample-1#{removal['ask']}", 'pat'))
            self.assertEqual(sorted((h['holder'], h['before'], h['after']) for h in data['holders']),
                             [('crew member clerk', ['notes'], []), ('group builders', ['notes', 'triage'], ['triage'])])
            self.assertEqual(client.call('SkillDigest', {'name': 'notes'}), 'absent')
            print('Skill removal receipt:', json.dumps(dict(ran=receipt, event=data), sort_keys=True))

            # Assignment: the card binds the target's definition and each tree; a config set since stales it.
            stale = propose('group set builders skills=[spare,triage]')
            stale_row = publish(stale)
            cli('group', 'set', 'builders', 'effort=high')
            poll([event(stale_row)]); poll([event(stale_row, 'confirm')])
            self.assertEqual((current(stale['ask'])['state'], self.bridge.run_asks.get(stale_row['id'])['state']), ('open', 'stale'))
            self.assertEqual(client.call('GetGroup', ['builders'])['config'].get('skills'), ['triage'])
            for run, target in (('group set builders skills=[spare,triage]', ('GetGroup', ['builders'], 'group_set')),
                                ('crew set clerk skills=[triage]', ('GetCrew', ['clerk'], 'crew_defined'))):
                ask = propose(run)
                row = publish(ask)
                before = len(events(target[2]))
                poll([event(row)])
                self.assertEqual(len(events(target[2])), before)
                poll([event(row, 'confirm')])
                self.assertEqual(current(ask['ask'])['state'], 'answered')
                self.assertIn('✓ approved, ran: ' + ask['run']['command'], footer(row))
                written = events(target[2])[before:]
                self.assertEqual(len(written), 1)
                audit = written[0]['data']
                self.assertEqual(audit['skills_audit']['proposal'], f"sample-1#{ask['ask']}")
                print('Skill assignment receipt:', json.dumps(dict(run=run, ran=current(ask['ask'])['ran']['result'], event=audit), sort_keys=True))
                self.assertEqual(audit['skills']['after'], ['spare', 'triage'] if target[2] == 'group_set' else ['triage'])
            # The store keeps the key as the terminal's word, a list or its bracketed text alike.
            self.assertIn(client.call('GetGroup', ['builders'])['config']['skills'], (['spare', 'triage'], '[spare,triage]'))
            declined = propose('group set web/reviewers skills=[triage]')
            row = publish(declined)
            poll([event(row, 'decline')])
            self.assertEqual(current(declined['ask'])['state'], 'declined')
            print('Native skill cards: real SkillDigest/Bead/ApproveAsk/carry, /decide, spoofed taps, expired confirmation, '
                  'stale holders/tree/config, lost approval reply, restart replay, removal and assignment receipts')
        finally:
            testyardr.stop(server)

    def test_real_native_group_add_and_set_apply_once_through_their_gates(self):
        binary = testyardr.binary()
        if binary is None:
            self.skipTest('yardr binary required for isolated native integration')
        root = Path(self.tmp.name)
        server, home, command, env = testyardr.start(root, binary)
        try:
            client = n.YardSocketClient(home, 'tgbridge')

            def cli(*args, actor='pat', ok=True):
                result = subprocess.run([binary, '--home', str(home), '--actor', actor, *args], env=env, cwd=root,
                                        text=True, capture_output=True, timeout=30)
                if ok is not None:
                    self.assertEqual(result.returncode == 0, ok, result.stderr)
                return result

            def group(name):
                try:
                    return client.call('GetGroup', [name])
                except n.RPCRefused:
                    return None

            def ask(run, question):
                cli('bead', 'ask', 'sample-1', question, '--to', 'pat', '--context', 'Synthetic ' + question, '--run', run, actor='proposer')
                return client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]

            def current(number):
                return next(a for a in client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'] if a['ask'] == number)

            def writes(name):
                # Group add and group set both save through the store's one group write, which records group_set.
                return len([e for e in client.call('EventsOfKind', ['group_set', 1000]) if e['data'].get('name') == name])

            # A yard counts as set up once it stores default_kind; no yardmaster is made or started.
            cli('settings', 'set', 'default_kind', 'claude')
            cli('settings', 'set', 'operator', 'pat')
            cli('user', 'add', 'proposer')
            source = root / 'synthetic.yaml'
            source.write_text('name: tgbridge\nbase: https://example.invalid\ncalls:\n  health:\n    kind: read\n    request: {method: GET, path: /health}\nhook:\n  bodies: unique\n  verify:\n    scheme: secret\n    signature: {header: X-Secret}\n  normalize: [/bin/false]\n')
            cli('source', 'add', str(source))
            cli('user', 'identity', 'pat', '--source', 'tgbridge', '--id', '123')
            cli('bead', 'create', 'Synthetic group configuration', '--id', 'sample-1')
            asked = home / 'roles-in'
            asked.mkdir()
            builder = ('# Synthetic builder <b>role</b> & "quotes"\n\tTabbed line 🙂  \n'
                       'Each session will start with the model a-line-of-the-role.\n\n')
            (asked / 'builder.md').write_text(builder)
            planner = '# Synthetic planner, revised\nPlan the work in small steps.'
            (asked / 'planner.md').write_text(planner)
            (home / 'roles').mkdir()
            (home / 'roles' / 'planner.md').write_text('# Synthetic planner\nPlan the work.\n')
            (home / 'work').mkdir()
            cli('group', 'add', 'planners', '--runner', 'herdr', '--set', 'kind=claude', '--set', 'model=claude-opus-4',
                '--set', f"prompt={home / 'roles' / 'planner.md'}", '--set', 'effort=high')
            cli('group', 'add', 'drafters', '--runner', 'herdr', '--set', 'kind=claude')

            self.config.update(yardr_home=str(home), yardr_operator='pat', yardr_source='tgbridge')
            ids = iter(range(77, 400))
            def telegram(url, body, *args, **kwargs):
                status, response = self.transport(url, body, *args, **kwargs)
                if url.endswith('/sendMessage'):
                    response['result']['message_id'] = next(ids)
                elif url.endswith('/editMessageText'):
                    response = dict(ok=True, result=dict(message_id=body['message_id'], chat=dict(id=123)))
                return status, response
            self.bridge = b.Bridge(self.config, self.store, 'TOKEN', 'CAPABILITY', request=telegram)
            self.bridge.hook_secret = None
            self.documents = []
            self.sent_count = 10000
            self.bridge.request_multipart = lambda *args, **kwargs: NativeCase.pack_transport(self, *args, **kwargs)
            updates = iter(range(40, 400))

            def card(value, fixture=None):
                if fixture:
                    group_fixture(self, value, root, fixture)
                self.assertTrue(self.bridge.api_run_ask(dict(bead='sample-1', ask=value['ask'], key=f"group-{value['ask']}"))['delivered'])
                row = next(r for r in self.bridge.run_asks.rows() if r['ask'] == value['ask'])
                self.assertTrue(row['bundle']['document']['message_id'])
                self.assertEqual(row['weighty'], bool(value.get('weight')))
                return row

            def press(row, action='approve'):
                if action == 'confirm':
                    action += ':' + (self.bridge.run_asks.get(row['id']).get('pending') or {}).get('nonce', '0' * 16)
                update = dict(update_id=next(updates), callback_query=dict(
                    id=f'query-{action}', data=f"runask:{row['id']}:{action}", **{'from': dict(id=123, is_bot=False)},
                    message=dict(message_id=row['message_id'], chat=dict(id=123, type='private'), **{'from': dict(id=999, is_bot=True)})))
                self.bridge.inbound(update)
                return update

            def approve(value, row, before):
                # Weighted: Approve arms and writes nothing, Confirm writes once; a routine card writes on its one tap.
                name = value['run']['argv'][2]
                count = writes(name)
                if value.get('weight'):
                    press(row)
                    self.assertEqual((group(name), writes(name)), (before, count))
                    confirmation = press(row, 'confirm')
                else:
                    confirmation = press(row)
                self.assertEqual(current(value['ask'])['state'], 'answered')
                self.assertEqual(self.bridge.run_asks.get(row['id'])['state'], 'approved')
                after = group(name)
                self.bridge.inbound(confirmation)
                press(row)
                self.assertEqual((group(name), writes(name)), (after, count + 1))
                return after

            def written(value, text):
                # The file the card named holds the card's text, read-only, and the group's prompt names it.
                parsed = n.parse_group(value)['prompt']
                path = Path(parsed['path'])
                self.assertEqual((parsed['text'], parsed['sha256']), (text, hashlib.sha256(text.encode()).hexdigest()))
                self.assertEqual(path.read_text(), text)
                self.assertEqual(path.stat().st_mode & 0o777, 0o400)
                return str(path)

            add = ask(f"group add builders --runner herdr --limit 2 --set kind=claude --set args=--verbose --set prompt={asked / 'builder.md'}",
                      'Add the synthetic builders?')
            row = card(add, 'group_add')
            made = approve(add, row, None)
            self.assertEqual((made['runner'], made['limit']), ('herdr', 2))
            self.assertEqual(made['config'], dict(kind='claude', args='--verbose', prompt=written(add, builder)))

            run = ask(f"group add runners --runner exec --set cmd=make --set dir={home / 'work'} --set worktrees={home / 'work'}",
                      'Add the synthetic runners?')
            made = approve(run, card(run, 'group_add_exec'), None)
            self.assertEqual((made['runner'], made['limit']), ('exec', 1))
            self.assertEqual(made['config'], dict(cmd='make', dir=str(home / 'work'), worktrees=str(home / 'work')))

            planners = group('planners')
            prompt = ask(f"group set planners prompt={asked / 'planner.md'}", 'Replace the planners prompt?')
            row = card(prompt, 'group_set_prompt')
            self.assertEqual(n.parse_group(prompt)['standing']['path'], str(home / 'roles' / 'planner.md'))
            changed = approve(prompt, row, planners)
            self.assertEqual(changed['config'], dict(planners['config'], prompt=written(prompt, planner)))

            drafters = group('drafters')
            preset = ask('group set drafters preset=bypass', 'Let the drafters skip permissions?')
            changed = approve(preset, card(preset, 'group_set_preset'), drafters)
            self.assertEqual(changed['config'], dict(drafters['config'], args=['--dangerously-skip-permissions']))

            drafters = changed
            effort = ask('group set drafters effort=high', 'Raise the drafters effort?')
            self.assertNotIn('weight', effort)
            changed = approve(effort, card(effort, 'group_set_effort'), drafters)
            self.assertEqual(changed['config'], dict(drafters['config'], effort='high'))

            planners = group('planners')
            default = ask('group set planners effort=default', 'Return the planners effort to the harness?')
            self.assertNotIn('weight', default)
            changed = approve(default, card(default, 'group_set_effort_default'), planners)
            self.assertEqual(changed['config'], {k: v for k, v in planners['config'].items() if k != 'effort'})

            # model= rides the aligned build's group set row alone; upstream refuses it before any card.
            planners = changed
            result = cli('bead', 'ask', 'sample-1', 'Move the planners?', '--to', 'pat', '--context', 'Synthetic model', '--run',
                         'group set planners model=claude-opus-5-5 effort=low', actor='proposer', ok=None)
            if result.returncode == 0:
                model = client.call('Bead', dict(bead='sample-1', viewer='pat'))['asks'][-1]
                changed = approve(model, card(model, 'group_set_model_effort'), planners)
                self.assertEqual(changed['config'], dict(planners['config'], model='claude-opus-5-5', effort='low'))
            else:
                self.assertIn('model is no part of this card', result.stderr)

            # A group changed after its card refuses the approval with nothing run; the card stays open.
            stale = ask('group set drafters effort=low', 'Lower the drafters effort?')
            row = card(stale)
            cli('group', 'set', 'drafters', 'effort=medium')
            drafters, count = group('drafters'), writes('drafters')
            press(row)
            self.assertEqual(current(stale['ask'])['state'], 'open')
            self.assertEqual((group('drafters'), writes('drafters')), (drafters, count))
            self.assertIn('The yard refused this action', b.html_visible_text(
                [body for url, body in self.calls if url.endswith('/editMessageText')][-1]['text'])[0])

            declined = ask(f"group add reviewers --runner herdr --set kind=claude --set prompt={asked / 'builder.md'}", 'Add the reviewers?')
            row = card(declined)
            press(row, 'decline')
            self.assertEqual(current(declined['ask'])['state'], 'declined')
            self.assertEqual((group('reviewers'), writes('reviewers')), (None, 0))
        finally:
            testyardr.stop(server)

    def test_real_native_repo_edit_end_to_end_and_boundaries(self):
        binary = testyardr.binary()
        if binary is None:
            self.skipTest('yardr binary required for isolated native integration')
        root = Path(self.tmp.name)
        server, home, command, env = testyardr.start(root, binary)
        try:
            client, cli, _ = self.exercise_source_limit_and_trust(binary, root, home, command, env)
            if not testyardr.serves(home, 'RepoEditCapabilities'):
                with self.assertRaises(n.RPCUnsupported):
                    client.call('RepoEditCapabilities', {})
                self.bridge.run_asks = n.RunAsks(self.bridge)
                v2_refused = self.bridge.api_run_ask(dict(bead='sample-1', ask=1, key='public-core-ok'))
                self.assertTrue(v2_refused['delivered'], v2_refused)
                bad = copy.deepcopy(repo_edit_proposal(2))
                with patch.object(self.bridge.run_asks, 'fetch', return_value=(dict(id='sample-1', title='Synthetic'), bad)):
                    rejected = self.bridge.api_run_ask(dict(bead='sample-1', ask=7, key='no-cap-refuse'))
                self.assertFalse(rejected['delivered'])
                self.assertTrue(rejected['reason'].endswith(n.FALLBACK), rejected)
                self.skipTest('core serves no RepoEditCapabilities')

            cap = client.call('RepoEditCapabilities', {})
            version = cap['version']
            self.assertIn(version, (1, 2))
            self.assertIs(cap['materialize'], version == 2)

            depot = root / 'depot'
            depot.mkdir()
            for args in (['init', '-q', '-b', 'main', str(depot)],):
                subprocess.run(['git', *args], env=env, check=True, capture_output=True)
            (depot / 'README.md').write_text('Old guide <alpha> & "beta".\nline two\n')
            (depot / 'remove.txt').write_text('Retire me\r\n')
            (depot / 'keep.md').write_text('unchanged\n')
            yardr_dir = depot / '.yardr'
            yardr_dir.mkdir()
            test_log = root / 'test-env.log'
            test_script = yardr_dir / 'test'
            test_script.write_text(
                f'#!/bin/sh\nset -eu\nprintf "base=%s landing=%s\\n" "${{YARDR_BASE-unset}}" "${{YARDR_LANDING-unset}}" >> "{test_log}"\ntest ! -f fail.txt\n'
            )
            test_script.chmod(0o755)
            for args in (
                ['-C', str(depot), 'add', '.'],
                ['-C', str(depot), '-c', 'user.name=Synthetic', '-c', 'user.email=synthetic@example.invalid',
                 'commit', '-q', '-m', 'Initial commit'],
            ):
                subprocess.run(['git', *args], env=env, check=True, capture_output=True)

            bin_dir = root / 'bin'
            bin_dir.mkdir()
            landed_log = root / 'landed.log'
            merge_local = bin_dir / 'merge-local'
            merge_local.write_text(
                '#!/bin/sh\nset -eu\n'
                f'BIN="{binary}"\n'
                'BASE=$(git -C "$YARDR_DEPOT_PATH" rev-parse "$YARDR_BASE")\n'
                'HEAD=$(git -C "$YARDR_DEPOT_PATH" rev-parse "$YARDR_BRANCH")\n'
                '"$BIN" --home "$YARDR_HOME" --actor pat depot edit verify "$YARDR_BEAD" --base "$BASE" --head "$HEAD" --test "$YARDR_TEST"\n'
                'YARDR_LANDING=1 sh -c "$YARDR_TEST"\n'
                '"$BIN" --home "$YARDR_HOME" --actor pat depot edit verify "$YARDR_BEAD" --base "$BASE" --head "$HEAD" --test "$YARDR_TEST"\n'
                'git -C "$YARDR_DEPOT_PATH" merge --ff-only "$YARDR_BRANCH"\n'
                '"$BIN" --home "$YARDR_HOME" --actor pat bead advance "$YARDR_BEAD" --to merged --note "landed $HEAD"\n'
                f'printf "landed=%s\\n" "$HEAD" >> "{landed_log}"\n'
            )
            merge_local.chmod(0o755)

            cli('depot', 'add', 'synthetic', str(depot), '--base', 'main', '--test', '.yardr/test')
            cli('bead', 'create', 'Repository edit approvals', '--id', 'sample-2', '--depot', 'synthetic')
            for g in ('builders', 'reviewers', 'experts'):
                cli('group', 'add', g, '--runner', 'herdr', '--set', 'kind=codex')
            cli('group', 'add', 'assembly', '--runner', 'exec', '--set', f'cmd={merge_local}')
            cli('group', 'add', 'decisions', '--runner', 'manual')
            flow_file = root / 'native.flow'
            flow_file.write_text(
                'backlog -> new | decide\n'
                'new -> review on done | decide on question | backlog\n'
                'review -> review-expert on done | review-expert on changes | approved on done-safe\n'
                'review-expert -> approved on done | repair on changes | decide on question\n'
                'repair -> review-expert on done | decide on question\n'
                'approved -> merged on done | decide on failed\n'
                'decide -> backlog | new | repair | review-expert | approved\n'
                'merged terminal\n'
            )
            cli('flow', 'set', 'native', '-f', str(flow_file))
            cli('depot', 'flow', 'synthetic', 'native')
            for stage, grp in (('backlog', 'decisions'), ('new', 'builders'), ('review', 'reviewers'),
                               ('review-expert', 'experts'), ('repair', 'builders'),
                               ('approved', 'assembly'), ('decide', 'decisions')):
                cli('route', 'add', '--stage', stage, '--depot', 'synthetic', '--group', grp)

            self.config.update(topics=(dict(name='ops', agent='yardmaster'),))
            self.bridge.topic_settings = self.config['topics']
            self.store.record_topic('ops', 12, 'created')
            clock = [1000]

            def reload_run_asks():
                self.bridge.run_asks = n.RunAsks(self.bridge, now=lambda: clock[0])

            reload_run_asks()
            updates = iter(range(90000, 91000))

            def poll(batch):
                telegram = self.bridge.telegram
                def fetched(method, body, timeout=15):
                    return (200, dict(ok=True, result=batch)) if method == 'getUpdates' else telegram(method, body, timeout)
                with patch.object(self.bridge, 'telegram', side_effect=fetched):
                    self.bridge.poll_once()

            def propose(op, files, subject='docs: Update repository text'):
                head = subprocess.check_output(['git', '-C', str(depot), 'rev-parse', 'HEAD'], text=True).strip()
                spec = dict(version=version, depot='synthetic', base_ref='main', base_commit=head,
                            operation=op, subject=subject, files=files)
                spec_path = root / f'{op}.json'
                spec_path.write_text(json.dumps(spec))
                r = subprocess.run([binary, '--home', str(home), '--actor', 'proposer', 'depot', 'edit', 'synthetic',
                                    '--proposal', str(spec_path), '--bead', 'sample-2', '--to', 'pat',
                                    '--context', f'Synthetic {op}'], env=env, cwd=root, capture_output=True, text=True)
                self.assertEqual(r.returncode, 0, r.stderr)
                return client.call('Bead', dict(bead='sample-2', viewer='pat'))['asks'][-1]

            def current(ask_no):
                return next(a for a in client.call('Bead', dict(bead='sample-2', viewer='pat'))['asks'] if a['ask'] == ask_no)

            def open_beads():
                return json.loads(cli('bead', 'list', '--json'))

            def event(row, action='approve', uid=None, thread=12, sender=None, **changes):
                if action == 'confirm':
                    action += ':' + self.bridge.run_asks.get(row['id'])['pending']['nonce']
                message = dict(message_id=row['message_id'], chat=dict(id=123, type='private'),
                               message_thread_id=thread, is_topic_message=True,
                               **{'from': dict(id=999, is_bot=True)})
                message.update(changes)
                if thread is None:
                    message.pop('message_thread_id')
                    message.pop('is_topic_message')
                return dict(update_id=next(updates) if uid is None else uid, callback_query=dict(
                    id='cb-' + str(next(updates)), data=f"runask:{row['id']}:{action}",
                    message=message, **{'from': sender or dict(id=123, is_bot=False)}))

            def press(row, action='approve', **changes):
                update = event(row, action, **changes)
                poll([update])
                return update

            # Exercise the checked-in run_ask declaration through the real source HTTP path.
            import yaml
            with patch.dict(os.environ, env, clear=True):
                port = self.start_callback_server()
            declaration = yaml.safe_load((Path(__file__).resolve().parents[1] / 'yardr/tgbridge.yaml').read_text())
            source = root / 'native-bridge.yaml'
            source.write_text(yaml.safe_dump(dict(name='tgbridge', base=f'http://127.0.0.1:{port}',
                credential=dict(env='TGBRIDGE_SECRET', header='Authorization: Bearer {credential}'),
                calls={'run_ask': declaration['calls']['run_ask']})))
            credential = root / 'native-bridge.env'
            credential.write_text('TGBRIDGE_SECRET=CAPABILITY\n')
            credential.chmod(0o600)
            cli('source', 'add', str(source))
            cli('source', 'set', 'tgbridge', 'env_file=' + str(credential))
            cli('source', 'set', 'tgbridge', 'calls_per_hour=600')
            cli('source', 'set', 'tgbridge', 'updates_per_hour=600')

            def publish(ask, key, topic=True):
                args = ['bead=sample-2', 'ask=' + str(ask['ask']), 'key=' + key]
                if topic:
                    args.append('topic=ops')
                return json.loads(cli('call', 'tgbridge', 'run_ask', *args, '--json'))

            head_readme = subprocess.check_output(['git', '-C', str(depot), 'rev-parse', 'HEAD:README.md'], text=True).strip()
            head_remove = subprocess.check_output(['git', '-C', str(depot), 'rev-parse', 'HEAD:remove.txt'], text=True).strip()
            files_edit = [
                {'path': 'README.md', 'blob': head_readme, 'mode': '100644',
                 'text': 'New guide <α雪🙂> & "beta".\nno trailing lf'},
                {'path': 'docs/new.md', 'absent': True, 'text': 'Created note\nSecond line\n'},
                {'path': 'remove.txt', 'blob': head_remove, 'mode': '100644', 'delete': True, 'text': ''},
            ]

            # 1. /decide lists and opens a repo_edit card into a City topic, delivering 1 document + 1 bound card.
            main_ask = propose('op-main', files_edit)
            queue_entries = self.bridge.run_asks.queue()
            entry = next(e for e in queue_entries if e['bead'] == 'sample-2' and e['ask'] == main_ask['ask'])
            self.assertTrue(self.bridge.run_asks.showable(entry))
            before_docs = len(self.documents)
            notice, opened = self.bridge.run_asks.decide_open('sample-2', main_ask['ask'], '123:topic:12')
            self.assertIsNone(notice)
            self.assertEqual(opened[1], '123:topic:12')
            self.assertEqual(len(self.documents), before_docs + 1)
            doc_text = self.documents[-1][2].decode('utf-8')
            review_parts(doc_text)
            self.assertEqual(review_content(doc_text, 'New UTF-8 text "README.md"'), files_edit[0]['text'])
            row = self.bridge.run_asks.get(self.bridge.run_asks.ident('sample-2', main_ask['ask']))
            self.assertEqual(row['conversation'], '123:topic:12')

            # Approve arms 60s Confirm without creating any bead or commit; expired confirm and wrong topic write nothing.
            beads_before = len(open_beads())
            press(row, 'approve')
            self.assertEqual(len(open_beads()), beads_before)
            self.assertEqual(current(main_ask['ask'])['state'], 'open')
            press(row, 'confirm', thread=99)
            self.assertEqual(current(main_ask['ask'])['state'], 'open')
            clock[0] += 61
            press(row, 'confirm')
            self.assertIsNone(self.bridge.run_asks.get(row['id'])['pending'])
            self.assertEqual(current(main_ask['ask'])['state'], 'open')

            # V1 core path: confirm writes capture-only answer with repo_edit_version='1' and skips v2 materialization.
            if version == 1:
                press(row, 'approve')
                press(row, 'confirm')
                self.assertEqual(current(main_ask['ask'])['state'], 'answered')
                self.assertEqual(len(open_beads()), beads_before)
                self.skipTest('v1 capture-only core (materialize=false); v2 materialization skipped')

            # Bound identities and native ownership are checked even with a live confirmation.
            press(row, 'confirm:' + '0' * 16)
            press(row, 'approve')
            for changes in (dict(message_id=row['message_id'] + 1),
                            dict(chat=dict(id=456, type='private')),
                            {'from': dict(id=998, is_bot=True)},
                            dict(sender=dict(id=456, is_bot=False)),
                            dict(forward_date=123), dict(thread=99)):
                press(row, 'confirm', **changes)
                self.assertEqual(current(main_ask['ask'])['state'], 'open')
                self.assertEqual(len(open_beads()), beads_before)
            with patch.object(self.bridge, '_relay', return_value=self.bridge.conversation):
                for words in ('7', 'approve', 'confirm'):
                    poll([dict(update_id=next(updates), message=dict(message_id=next(updates), text=words,
                        chat=dict(id=123, type='private'), message_thread_id=12, is_topic_message=True,
                        reply_to_message=dict(message_id=row['message_id'], **{'from': dict(id=999, is_bot=True)}),
                        **{'from': dict(id=123, is_bot=False)}))])
            self.assertEqual(current(main_ask['ask'])['state'], 'open')
            original_home = self.config['yardr_home']
            self.config['yardr_home'] = str(root / 'missing-yard')
            try:
                reload_run_asks()
                press(row, 'confirm')
                self.assertEqual(current(main_ask['ask'])['state'], 'open')
            finally:
                self.config['yardr_home'] = original_home
                reload_run_asks()
            self.assertEqual(len(open_beads()), beads_before)

            # A title/card drift and dirty captured file each refuse the existing card.
            cli('bead', 'update', 'sample-2', '--title', 'Changed proposal owner title')
            press(row, 'confirm')
            self.assertEqual(self.bridge.run_asks.get(row['id'])['state'], 'stale')
            file_ask = propose('op-file-drift', files_edit)
            self.assertTrue(publish(file_ask, 'key-file')['delivered'])
            file_row = self.bridge.run_asks.get(self.bridge.run_asks.ident('sample-2', file_ask['ask']))
            press(file_row, 'approve')
            before = (depot / 'README.md').read_bytes()
            try:
                (depot / 'README.md').write_text('Unapproved dirty file\n')
                press(file_row, 'confirm')
                self.assertEqual(current(file_ask['ask'])['state'], 'open')
                self.assertEqual(len(open_beads()), beads_before)
            finally:
                (depot / 'README.md').write_bytes(before)

            # Uncertain sends remain reserved through restart, with no working controls.
            for phase in ('document', 'card'):
                with self.subTest(native_uncertain_delivery=phase):
                    ask = propose('op-uncertain-' + phase, files_edit)
                    key = 'key-uncertain-' + phase
                    sends_before = len([1 for url, _ in self.calls if url.endswith('/sendMessage')])
                    def unknown(*args, **kwargs):
                        raise b.RequestUnconfirmed('synthetic uncertain send')
                    if phase == 'document':
                        with patch.object(self.bridge, 'request_multipart', side_effect=unknown):
                            self.assertFalse(publish(ask, key)['delivered'])
                    else:
                        request = self.bridge.request
                        def unknown_card(url, body, *args, **kwargs):
                            if url.endswith('/sendMessage'):
                                return unknown()
                            return request(url, body, *args, **kwargs)
                        with patch.object(self.bridge, 'request', side_effect=unknown_card):
                            self.assertFalse(publish(ask, key)['delivered'])
                    reload_run_asks()
                    self.assertFalse(publish(ask, key)['delivered'])
                    blocked = self.bridge.run_asks.get(self.bridge.run_asks.ident('sample-2', ask['ask']))
                    self.assertIsNone(blocked['message_id'])
                    self.assertEqual(len([1 for url, _ in self.calls if url.endswith('/sendMessage')]), sends_before)
                    self.assertEqual(current(ask['ask'])['state'], 'open')
                    self.assertEqual(len(open_beads()), beads_before)

            # A card receipt can commit before the native row; restart reuses that receipt.
            crash_ask = propose('op-card-crash', files_edit)
            save = self.bridge.run_asks.save
            def crash_after_card(value):
                if value['ask'] == crash_ask['ask'] and value['message_id'] is not None:
                    raise RuntimeError('synthetic crash after card receipt')
                save(value)
            with patch.object(self.bridge.run_asks, 'save', side_effect=crash_after_card):
                with self.assertRaises(RuntimeError):
                    self.bridge.api_run_ask(dict(bead='sample-2', ask=crash_ask['ask'], key='key-card-crash'))
            sent = len(self.calls), len(self.documents)
            reload_run_asks()
            self.assertTrue(publish(crash_ask, 'key-card-crash', topic=False)['delivered'])
            self.assertEqual((len(self.calls), len(self.documents)), sent)
            crash_row = self.bridge.run_asks.get(self.bridge.run_asks.ident('sample-2', crash_ask['ask']))
            press(crash_row, 'decline', thread=None)
            self.assertEqual(current(crash_ask['ask'])['state'], 'declined')
            collision = publish(file_ask, 'key-card-crash', topic=False)
            self.assertFalse(collision['delivered'])
            self.assertEqual(collision['reason'], 'idempotency_key_conflict')

            # 2. Stale drift (base advance after card) refuses Confirm cleanly with zero patch beads created.
            stale_ask = propose('op-stale', [{'path': 'docs/stale.md', 'absent': True, 'text': 'stale\n'}],
                                subject='docs: Stale test')
            r_stale = self.bridge.api_run_ask(dict(bead='sample-2', ask=stale_ask['ask'], key='key-stale', topic='ops'))
            self.assertTrue(r_stale['delivered'])
            stale_row = self.bridge.run_asks.get(self.bridge.run_asks.ident('sample-2', stale_ask['ask']))
            (depot / 'keep.md').write_text('moved base\n')
            subprocess.run(['git', '-C', str(depot), 'add', 'keep.md'], env=env, check=True, capture_output=True)
            subprocess.run(['git', '-C', str(depot), '-c', 'user.name=S', '-c', 'user.email=s@e.invalid',
                            'commit', '-q', '-m', 'Advance base'], env=env, check=True, capture_output=True)
            press(stale_row, 'approve')
            press(stale_row, 'confirm')
            self.assertEqual(self.bridge.run_asks.get(stale_row['id'])['state'], 'stale')
            self.assertEqual(len(open_beads()), beads_before)

            # Refresh main_ask on the new base (since base moved!) and test Decline + Crash/Replay + Full Land.
            dec_ask = propose('op-decline', [{'path': 'docs/decl.md', 'absent': True, 'text': 'declined\n'}],
                              subject='docs: Declined edit')
            self.assertTrue(publish(dec_ask, 'key-dec')['delivered'])
            press(self.bridge.run_asks.get(self.bridge.run_asks.ident('sample-2', dec_ask['ask'])), 'decline')
            self.assertEqual(current(dec_ask['ask'])['state'], 'declined')
            self.assertEqual(len(open_beads()), beads_before)

            # Crash after document delivery before card send -> restart -> idempotent replay reuses doc without double-send.
            final_ask = propose('op-final', [
                {'path': 'README.md', 'blob': head_readme, 'mode': '100644', 'text': files_edit[0]['text']},
                {'path': 'docs/new.md', 'absent': True, 'text': files_edit[1]['text']},
                {'path': 'remove.txt', 'blob': head_remove, 'mode': '100644', 'delete': True, 'text': ''},
            ], subject='docs: Final materialized edit')
            orig_reserve = self.bridge._reserve_and_deliver
            def crash_before_card_send(delivery_key, identity, send_fn):
                if delivery_key.endswith(':send'):
                    raise RuntimeError('simulated crash after document send, before card reserve')
                return orig_reserve(delivery_key, identity, send_fn)
            docs_before = len(self.documents)
            with patch.object(self.bridge, '_reserve_and_deliver', side_effect=crash_before_card_send):
                with self.assertRaises(RuntimeError):
                    self.bridge.api_run_ask(dict(bead='sample-2', ask=final_ask['ask'], key='key-final', topic='ops'))
            self.assertEqual(len(self.documents), docs_before + 1)
            reload_run_asks()
            replay = self.bridge.api_run_ask(dict(bead='sample-2', ask=final_ask['ask'], key='key-final', topic='ops'))
            self.assertTrue(replay['delivered'], replay)
            self.assertEqual(len(self.documents), docs_before + 1)
            final_row = self.bridge.run_asks.get(self.bridge.run_asks.ident('sample-2', final_ask['ask']))

            # Lost ApproveAsk reply simulation -> restart / poll reconciles canonical bead with AT MOST ONE patch bead.
            press(final_row, 'approve')
            real_call = self.bridge.run_asks.client.call
            def drop_approve_reply(method, args):
                value = real_call(method, args)
                if method == 'ApproveAsk':
                    raise n.RPCAmbiguous('simulated lost reply after commit')
                return value
            with patch.object(self.bridge.run_asks.client, 'call', side_effect=drop_approve_reply):
                press(final_row, 'confirm')
            # Restart both store server and bridge after the committed approval reply is lost.
            testyardr.stop(server)
            server = testyardr.restart(root, binary, home, command, env)
            reload_run_asks()
            poll([])
            final_row = self.bridge.run_asks.get(final_row['id'])
            reserved = final_row.get('native_bead')
            self.assertTrue(reserved, final_row)
            self.assertEqual(len([b_item for b_item in open_beads() if b_item['id'] == reserved]), 1)

            # Wait for real server carry to prepare commit + run .yardr/test + release bead to review.
            deadline = time.monotonic() + 25
            prepared_status = None
            while time.monotonic() < deadline:
                poll([])
                st = self.bridge.run_asks.get(final_row['id']).get('native_edit_status', {})
                if st.get('state') == 'prepared':
                    prepared_status = st
                    break
                time.sleep(0.1)
            self.assertIsNotNone(prepared_status, 'server carry did not prepare commit in time')
            self.assertEqual(prepared_status['depot'], 'synthetic')
            edits = [body for url, body in self.calls if url.endswith('/editMessageText')]
            self.assertIn('not landed or installed', edits[-1]['text'])
            commit_author = subprocess.check_output(
                ['git', '-C', str(depot), 'show', '-s', '--format=%an <%ae>', prepared_status['branch']], text=True).strip()
            commit_msg = subprocess.check_output(
                ['git', '-C', str(depot), 'show', '-s', '--format=%B', prepared_status['branch']], text=True).strip()
            self.assertEqual(commit_author, n.repo_edit.AUTHOR_IDENTITY)
            self.assertEqual(commit_msg, 'docs: Final materialized edit')
            self.assertIn(f"base={final_ask['run']['repo_edit']['head']} landing=unset", test_log.read_text())

            # Review advances through its configured stages before normal assembly dispatch.
            cli('bead', 'done', reserved)
            self.assertEqual(client.call('Bead', dict(bead=reserved, viewer='pat'))['stage'], 'review-expert')
            cli('bead', 'done', reserved)
            self.assertEqual(client.call('Bead', dict(bead=reserved, viewer='pat'))['stage'], 'approved')
            daemon_proc = subprocess.Popen(command + ['daemon', '--interval', '200ms'], env=env, cwd=root,
                                           stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
            try:
                deadline = time.monotonic() + 25
                landed_status = None
                while time.monotonic() < deadline:
                    poll([])
                    st = self.bridge.run_asks.get(final_row['id']).get('native_edit_status', {})
                    if st.get('state') == 'landed':
                        landed_status = st
                        break
                    time.sleep(0.1)
            finally:
                testyardr.stop(daemon_proc)
            self.assertIsNotNone(landed_status, f"assembly merge-local did not land commit in time; status={client.call('NativeEditStatus', [reserved])}; show={cli('bead', 'show', reserved)}")
            edits = [body for url, body in self.calls if url.endswith('/editMessageText')]
            self.assertIn('source landed; not installed or live', edits[-1]['text'])
            self.assertEqual((depot / 'README.md').read_text(), files_edit[0]['text'])
            self.assertEqual((depot / 'docs/new.md').read_text(), files_edit[1]['text'])
            self.assertFalse((depot / 'remove.txt').exists())
            self.assertIn('landing=1', test_log.read_text())
            self.assertTrue(landed_log.exists())
            self.assertEqual((depot / 'keep.md').read_bytes(), b'moved base\n')
            self.assertEqual(subprocess.check_output(['git', '-C', str(depot), 'rev-parse', 'HEAD'], text=True).strip(), prepared_status['head'])
            replay_before = len(open_beads())
            press(final_row, 'approve')
            self.assertEqual(len(open_beads()), replay_before)

            collision_spec = dict(version=version, depot='synthetic', base_ref='main',
                base_commit=prepared_status['head'], operation='op-final', subject='docs: Different edit',
                files=[dict(path='collision.txt', absent=True, text='Other bytes\n')])
            collision_path = root / 'collision.json'
            collision_path.write_text(json.dumps(collision_spec))
            before_collision = client.call('Head', [])
            result = subprocess.run([*command, 'depot', 'edit', 'synthetic', '--proposal', str(collision_path),
                '--bead', 'sample-2', '--to', 'pat', '--context', 'Synthetic operation collision'],
                cwd=root, env=env, capture_output=True, text=True, timeout=20)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn('operation', result.stderr)
            self.assertEqual(client.call('Head', []), before_collision)
            self.assertEqual(len(open_beads()), replay_before)

            # A test failure is reported canonically and survives another full restart without a second bead.
            failed_ask = propose('op-test-failed', [dict(path='fail.txt', absent=True, text='Synthetic failure\n')])
            self.assertTrue(publish(failed_ask, 'key-test-failed')['delivered'])
            failed_row = self.bridge.run_asks.get(self.bridge.run_asks.ident('sample-2', failed_ask['ask']))
            press(failed_row, 'approve')
            press(failed_row, 'confirm')
            failed_bead = self.bridge.run_asks.get(failed_row['id'])['native_bead']
            deadline = time.monotonic() + 25
            while time.monotonic() < deadline:
                poll([])
                failed_status = self.bridge.run_asks.get(failed_row['id']).get('native_edit_status', {})
                if failed_status.get('state') in ('failed', 'held'):
                    break
                time.sleep(.1)
            self.assertIn(failed_status.get('state'), ('failed', 'held'))
            self.assertIn('approved, failed', self.bridge.run_asks.get(failed_row['id'])['display']['footer'])
            self.assertFalse((depot / 'fail.txt').exists())
            self.assertEqual(len(open_beads()), replay_before + 1)
            testyardr.stop(server)
            server = testyardr.restart(root, binary, home, command, env)
            reload_run_asks()
            poll([])
            self.assertEqual(client.call('NativeEditStatus', [failed_bead]), failed_status)
            self.assertEqual(len(open_beads()), replay_before + 1)
            print(f'Native repo_edit v{version}: declaration run_ask, /decide, private/topic routing, '
                  'binding refusals, base/file/card drift, delivery and RPC crashes, replay/collision, '
                  'core+ledger restart, exact commit/test/review/assembly landing and failed-test status verified')
        finally:
            testyardr.stop(server)
