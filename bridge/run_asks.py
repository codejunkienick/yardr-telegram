"""Native asks: socket authority, complete cards and durable interactions."""
import contextlib
import datetime
import fcntl
import hashlib
from html import escape, unescape
import http.client
import importlib.util
import json
import os
from pathlib import Path
import re
import secrets
import shlex
import socket
import stat
import string
import struct
import tempfile
import time
import unicodedata


_markdown_spec = importlib.util.spec_from_file_location('card_markdown', Path(__file__).resolve().with_name('markdown.py'))
markdown = importlib.util.module_from_spec(_markdown_spec)
_markdown_spec.loader.exec_module(markdown)

_repo_edit_spec = importlib.util.spec_from_file_location('repo_edit', Path(__file__).resolve().with_name('repo_edit.py'))
repo_edit = importlib.util.module_from_spec(_repo_edit_spec)
_repo_edit_spec.loader.exec_module(repo_edit)


CAP = 3500
PACK_PARTS = 128
PACK_DOCUMENT_BYTES = 50 * 1024 * 1024
REPORT_MAX_BYTES = 200 * 1024
REPORT_TZ = datetime.timezone(datetime.timedelta(hours=5))
REPORT_NON_REPORT_AUTHORS = frozenset({'yard', 'yardmaster'})
PENDING = 'approved, to be carried out'
UNCERTAIN = 'the yard did not answer; read the card again'
FALLBACK = '; use Footplate or the yard cards'
STALE_PROPOSAL = 'Proposal changed' + FALLBACK
REPLACED_PROPOSAL = 'Proposal changed; replaced by a newer card'
RECONCILE = 'Card delivery unconfirmed; decide in Footplate or the yard and delete this Telegram card manually'
READS = ('Bead', 'GetUser', 'UserAsks', 'GetGroup', 'GetDepot', 'Groups', 'Routes', 'Settings', 'ListBeads', 'SidingBeads',
         'NextStages', 'GetBead', 'Head', 'Events', 'Notes', 'SkillDigest', 'UserInbox', 'Conversation',
         'RepoEditCapabilities', 'NativeEditStatus', 'GetCrew', 'Peers', 'PeerOutbox', 'EventsSince')
WEIGHTS = {'credential', 'as-user', 'outside', 'code', 'trust', 'reads'}
CONFIRM_SECONDS = 60
CONFIRM_EXPIRED = 'Confirmation expired; nothing ran. Tap Approve again.'


class NativeError(Exception):
    """A fixed refusal reason, without input or response text."""


class RPCUnavailable(NativeError):
    pass


class RPCAmbiguous(NativeError):
    pass


class RPCRefused(NativeError):
    def __init__(self, message, code=None):
        super().__init__(message)
        self.code = code


class RPCUnsupported(RPCRefused):
    pass


class RPCStale(RPCRefused):
    pass


class RPCExpired(RPCRefused):
    pass


def canonical(value):
    return json.dumps(value, sort_keys=True, separators=(',', ':'), ensure_ascii=True)


def digest(value):
    return hashlib.sha256(canonical(value).encode()).hexdigest()


def units(value):
    return len(value.encode('utf-16-le')) // 2


def without_weight_mark(value):
    lines = []
    for line in value.split('\n'):
        rest = line.lstrip()
        leading = line[:len(line) - len(rest)]
        while rest.startswith('WEIGHTY · '):
            rest = rest[len('WEIGHTY · '):]
            if rest.lstrip().startswith('WEIGHTY · '):
                rest = rest.lstrip()
        lines.append(leading + rest)
    return '\n'.join(lines)


def validate_home(home):
    path = Path(home).expanduser().resolve()
    try:
        info = path.stat()
    except OSError:
        raise NativeError('yardr.home must be an existing private directory') from None
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise NativeError('yardr.home must be an owner-held directory without group/world writes')
    return path


STREAM_LINE_MAX = 8 * 1024 * 1024


def validate_stream_event(event):
    """Validate one NDJSON event from GET /events?after=<seq>."""
    required = {'seq', 'kind', 'actor', 'at'}
    if (not isinstance(event, dict) or not required <= set(event)
            or type(event['seq']) is not int or not 0 < event['seq'] < 2**64
            or any(not isinstance(event[field], str) or not event[field]
                   for field in ('kind', 'actor', 'at'))
            or ('bead' in event and event['bead'] is not None
                and (not isinstance(event['bead'], str) or not event['bead']))
            or not isinstance(event.get('data', {}), dict)):
        raise RPCAmbiguous('invalid native events row')
    return event


class YardSocketClient:
    def __init__(self, home, source, timeout=10, verify_owner=False):
        self.home, self.source, self.timeout = Path(home), source, timeout
        self.verify_owner = verify_owner

    def _connect_socket(self, label):
        home = validate_home(self.home) if self.verify_owner else self.home
        path = home / 'yard.sock'
        try:
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise RPCUnavailable('native socket must be an owner-held Unix socket')
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            try:
                sock.connect(str(path))
                if self.verify_owner and hasattr(socket, 'SO_PEERCRED'):
                    _, uid, _ = struct.unpack('iII', sock.getsockopt(socket.SOL_SOCKET, socket.SO_PEERCRED, 12))
                    if uid != os.getuid():
                        raise RPCUnavailable('native socket server must have the same UID')
            except (OSError, RPCUnavailable):
                sock.close()
                raise
        except OSError:
            raise RPCUnavailable(f'native {label} unavailable before send') from None
        return sock

    def call(self, method, arguments):
        # Self-replacement refuses reads before dispatch; writes always reconcile at their caller.
        for attempt in range(3):
            try:
                return self._call(method, arguments)
            except RPCUnavailable:
                if method not in READS or attempt == 2:
                    raise
                time.sleep(.05 * (attempt + 1))

    def _call(self, method, arguments):
        sock = self._connect_socket(method)
        with sock:
            data = canonical(arguments).encode()
            request = (f'POST /call/{method} HTTP/1.1\r\nHost: localhost\r\n'
                       f'X-Yardr-Connection: {self.source}\r\nContent-Type: application/json\r\n'
                       f'Content-Length: {len(data)}\r\nConnection: close\r\n\r\n').encode() + data
            try:
                sock.sendall(request)
                response = http.client.HTTPResponse(sock)
                try:
                    response.begin()
                    raw = response.read(8 * 1024 * 1024 + 1)
                finally:
                    response.close()
                if response.status == 503 and method in READS:
                    raise RPCUnavailable(f'native {method} unavailable (503)')
                if response.status != 200 or len(raw) > 8 * 1024 * 1024:
                    raise ValueError()
                value = json.loads(raw, object_pairs_hook=strict_object)
                if not isinstance(value, dict) or set(value) - {'result', 'error', 'more'}:
                    raise ValueError()
                if 'error' in value:
                    error = value['error']
                    if (not isinstance(error, dict) or set(error) != {'code', 'message'}
                            or not all(isinstance(v, str) for v in error.values()) or 'result' in value):
                        raise ValueError()
                    if (method in ('AnswerAskExpected', 'DeferAskExpected', 'DeclineAskExpected')
                            and error['code'] == 'error' and error['message'] == 'no such method ' + method):
                        raise RPCUnsupported('ask_card_requires_guarded_core' + FALLBACK)
                    if method == 'SkillDigest' and error['code'] == 'error' and error['message'] == 'no such method SkillDigest':
                        raise RPCUnsupported('run_ask_skill_cards_unavailable; this yard has no skill topic' + FALLBACK)
                    if method == 'NudgeSession' and error['code'] == 'error' and error['message'] == 'no such method NudgeSession':
                        raise RPCUnsupported('native_nudge_unavailable; this yard has no session nudge')
                    if method == 'RepoEditCapabilities' and error['code'] == 'error' and error['message'] == 'no such method RepoEditCapabilities':
                        raise RPCUnsupported('run_ask_repo_edit_unavailable; this yard has no repo edit cards' + FALLBACK)
                    if method == 'NativeEditStatus' and error['code'] == 'error' and error['message'] == 'no such method NativeEditStatus':
                        raise RPCUnsupported('run_ask_repo_edit_status_unavailable' + FALLBACK)
                    code =error['code'] if error['code'] in {'not-found', 'taken', 'at-limit', 'conflict', 'ambiguous', 'replacing', 'error'} else 'unknown'
                    if (method == 'ApproveAsk' and code == 'error'
                            and re.fullmatch(rf"ask {arguments.get('ask')} was good until (?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9] "
                                             r"and that time has passed: the general card has expired\. Nothing ran\. "
                                             r"Decline it; it is asked again if it is still wanted\.", error['message'])):
                        raise RPCExpired('Native general card expired; nothing ran. Native ask remains open; decline it.', code)
                    if (method == 'ApproveAsk' and code == 'error'
                            and ((error['message'].startswith(tuple(f"ask {arguments.get('ask')}{prefix}" for prefix in (
                                ' was written when the source ', ' was written from another declaration of the source ',
                                ' was written when the skill ', ' was written when the group ', ' was written when the crew member ',
                                ' was carded under ', ' says ', ' was written from ', ': was written from ', ': repo edit:')))
                                  or re.match(rf"ask {arguments.get('ask')}: pack .+: the pin is sha256:[0-9a-f]{{64}}, "
                                              r'and the directory .+ as read has sha256:[0-9a-f]{64}:', error['message'])
                                  or re.match(rf"ask {arguments.get('ask')}: pack .+: its plan asks or refuses, so no card can apply it: ", error['message']))
                                 and error['message'].endswith('Nothing ran. Decline it; it is asked again if it is still wanted.')
                                 or error['message'] == f"ask {arguments.get('ask')}: repo edit changed; capture a new proposal, nothing ran"
                                 or error['message'] == 'repo edit: the review route changed since capture; capture a new proposal, nothing ran')):
                        raise RPCStale('native proposal is stale; nothing ran', code)
                    raise RPCRefused(f'native {method} refused ({code})', code)
                return value.get('result')  # successful void calls serialize as {}
            except (OSError, ValueError, http.client.HTTPException, UnicodeError):
                raise RPCAmbiguous(f'native {method} response uncertain') from None

    def stream_events(self, after, stop_event=None, register_socket=None, on_open=None):
        """Stream NDJSON events from GET /events?after=<after> on the native socket."""
        if type(after) is not int or after < 0:
            raise ValueError('after must be a non-negative integer')
        sock = self._connect_socket('events')
        if register_socket is not None:
            register_socket(sock)
        response = None
        try:
            request = (f'GET /events?after={after} HTTP/1.1\r\nHost: localhost\r\n'
                       f'X-Yardr-Connection: {self.source}\r\nConnection: close\r\n\r\n').encode()
            try:
                sock.sendall(request)
                response = http.client.HTTPResponse(sock)
                response.begin()
            except (OSError, http.client.HTTPException, ValueError):
                if stop_event is not None and stop_event.is_set():
                    return
                raise RPCAmbiguous('native events response uncertain') from None
            if response.status == 503:
                raise RPCUnavailable('native events unavailable (503)')
            if response.status != 200:
                code = f'http_{response.status}'
                raise RPCRefused(f'native events refused ({code})', code)
            ctype = (response.getheader('Content-Type') or '').split(';', 1)[0].strip().lower()
            if ctype != 'application/x-ndjson':
                raise RPCAmbiguous('native events invalid content-type')
            sock.settimeout(None)
            if stop_event is not None and stop_event.is_set():
                return
            if on_open is not None:
                on_open()
            while stop_event is None or not stop_event.is_set():
                try:
                    raw = response.readline(STREAM_LINE_MAX + 1)
                except (OSError, http.client.HTTPException, ValueError, UnicodeError):
                    if stop_event is not None and stop_event.is_set():
                        return
                    raise RPCAmbiguous('native events response uncertain') from None
                if not raw:
                    return
                if len(raw) > STREAM_LINE_MAX or not raw.endswith(b'\n'):
                    raise RPCAmbiguous('native events response line invalid')
                line = raw.strip()
                if not line:
                    continue
                try:
                    event = json.loads(line, object_pairs_hook=strict_object)
                except (ValueError, UnicodeError):
                    raise RPCAmbiguous('invalid native events row') from None
                yield validate_stream_event(event)
        finally:
            if register_socket is not None:
                register_socket(None)
            if response is not None:
                try:
                    response.close()
                except OSError:
                    pass
            try:
                sock.close()
            except OSError:
                pass


def strict_object(pairs):
    result = {}
    for key, value in pairs:
        if key in result:
            raise ValueError()
        result[key] = value
    return result


def verify_operator(client, config):
    user = client.call('GetUser', {'name': config['yardr_operator']})
    identities = user.get('identities') if isinstance(user, dict) else None
    identity = identities.get(config.get('yardr_source', 'tgbridge')) if isinstance(identities, dict) else None
    if (not isinstance(user, dict) or user.get('name') != config['yardr_operator']
            or user.get('role') != 'operator' or not isinstance(identity, dict)
            or identity.get('id') != str(config['user_id'])):
        raise NativeError('operator_not_authorized')
    return user


def text(value, field, controls=False):
    if not isinstance(value, str):
        raise NativeError(f'invalid native {field}')
    try:
        units(value)
    except UnicodeError:
        raise NativeError(f'invalid native {field}') from None
    if not controls and any(ord(c) < 32 and c not in '\n\t' for c in value):
        raise NativeError(f'invalid native {field}')
    return value


def lines(value, field):
    if not isinstance(value, list):
        raise NativeError(f'invalid native {field}')
    return [text(item, field) for item in value]


def file_hash(value):
    return hashlib.sha256(value.encode('utf-8')).hexdigest()


def unquote(value):
    # Core uses strconv.Quote, including Go escapes JSON does not understand.
    if not isinstance(value, str) or not value.startswith('"') or not value.endswith('"'):
        raise NativeError('invalid native skill quoted review')
    result, at = [], 1
    escapes = {'a': '\a', 'b': '\b', 'f': '\f', 'n': '\n', 'r': '\r', 't': '\t', 'v': '\v', '\\': '\\', '"': '"'}
    while at < len(value) - 1:
        char = value[at]
        at += 1
        if char == '\\':
            kind = value[at:at+1]
            at += 1
            if kind in escapes:
                char = escapes[kind]
            elif kind in ('x', 'u', 'U'):
                width = {'x': 2, 'u': 4, 'U': 8}[kind]
                digits = value[at:at+width]
                if len(digits) != width or not re.fullmatch('[0-9a-fA-F]+', digits):
                    raise NativeError('invalid native skill quoted review')
                try:
                    char = chr(int(digits, 16))
                    char.encode('utf-8')
                except (ValueError, UnicodeError):
                    raise NativeError('invalid native skill quoted review') from None
                at += width
            else:
                raise NativeError('invalid native skill quoted review')
        elif char == '"' or ord(char) < 32:
            raise NativeError('invalid native skill quoted review')
        result.append(char)
    if at != len(value) - 1:
        raise NativeError('invalid native skill quoted review')
    return ''.join(result)


def diff_lines(value):
    value = value.removesuffix('\n')
    return value.split('\n') if value else []


def verify_diff(value, old, new):
    """Check every hunk against both captured sides, including omitted context."""
    before, after = diff_lines(old), diff_lines(new)
    if before == after:
        if value:
            raise NativeError('invalid native skill diff')
        return
    rows = value.split('\n')
    if rows[:2] != ['--- was', '+++ is'] or rows[-1] != '':
        raise NativeError('invalid native skill diff')
    at, left, right = 2, 0, 0
    while at < len(rows) - 1:
        match = re.fullmatch(r'@@ -([0-9]{1,5}),([0-9]{1,5}) \+([0-9]{1,5}),([0-9]{1,5}) @@', rows[at])
        if not match:
            raise NativeError('invalid native skill diff')
        a, na, b, nb = map(int, match.groups())
        a, b = a - bool(na), b - bool(nb)
        if a < left or b < right or a > len(before) or b > len(after) or before[left:a] != after[right:b]:
            raise NativeError('invalid native skill diff')
        left, right, at = a, b, at + 1
        used_a = used_b = 0
        while at < len(rows) - 1 and not rows[at].startswith('@@ '):
            line = rows[at]
            if not line or line[0] not in ' +-':
                raise NativeError('invalid native skill diff')
            if line[0] != '+':
                if left >= len(before) or before[left] != line[1:]:
                    raise NativeError('invalid native skill diff')
                left, used_a = left + 1, used_a + 1
            if line[0] != '-':
                if right >= len(after) or after[right] != line[1:]:
                    raise NativeError('invalid native skill diff')
                right, used_b = right + 1, used_b + 1
            at += 1
        if (used_a, used_b) != (na, nb):
            raise NativeError('invalid native skill diff')
    if before[left:] != after[right:]:
        raise NativeError('invalid native skill diff')


def run_scope(run):
    """The argv of a run proposal a card can carry whole; refuses input and file-carrying runs."""
    for key in ('ask', 'carried'):
        if key in run and not isinstance(run[key], list):
            raise NativeError('invalid native run.' + key)
    if run.get('ask'):
        raise NativeError('run_ask_requires_input_use_terminal' + FALLBACK)
    argv = lines(run.get('argv'), 'run.argv')
    if not argv or not all(argv):
        raise NativeError('invalid native run.argv')
    if argv[:2] == ["skill", "add"]:
        raise NativeError('run_ask_skill_add_removed; decide it in the yard' + FALLBACK)
    if argv[:3] == ['user', 'device', 'pair']:
        # The pairing code is answered once to the approver alone and kept
        # nowhere; this bridge shows only the approval's safe sentence, so
        # approving here would strand the code and the pairing would lapse.
        raise NativeError('run_ask_device_pair_code_unseen; pair it in the yard' + FALLBACK)
    if ((run.get('carried') and argv[:2] not in (['source', 'add'], ['pack', 'apply'])) or (run.get('pack') is not None and argv[:2] != ['pack', 'apply'])
            or (argv[:2] == ['source', 'add'] and not run.get('carried'))
            or argv[:2] == ['crew', 'add']):
        raise NativeError('run_ask_carries_files_use_terminal' + FALLBACK)
    return argv


RUN_KNOWN = {'argv', 'command', 'description', 'does', 'back', 'back_by', 'carried', 'pack', 'ask'}
GENERAL_DESCRIPTION = 'a yardr command no card fits, shown whole and unchecked'
GENERAL_WARNINGS = [
    'The yard has not checked this command: approving runs it as typed, as the operator.',
    'No way back is shown: nothing here says how to undo it.',
    'What the command prints is told to the asker.',
]
GENERAL_FILES = ('File operands are not captured, hashed or reviewed file bytes. Changes to files or yard state '
                 'do not make this general card stale; native expiry still applies.')
GENERAL_UNKNOWN = ('UNKNOWN approval outcome. Independently inspect the yard and command effects. '
                   'If still wanted, request a fresh human decision on a fresh native ask; never replay this command automatically.')
GENERAL_STOPPED = 'the yard stopped while this ran; whether it finished is not known. Ask again if it is still wanted.'


def general_command(words):
    # Upstream typedWords only quotes square brackets; argv below is the unambiguous review.
    return 'yardr ' + ' '.join("'" + word.replace("'", "'\\''") + "'" if '[' in word or ']' in word else word for word in words)


def general_shown(word):
    # Letters removes leading note-file marks; the separately escaped word retains them.
    rest = word.lstrip()
    leading = word[:len(word) - len(rest)]
    while rest.startswith('◆'):
        rest = rest[1:]
        if rest.lstrip().startswith('◆'):
            rest = rest.lstrip()
    shown = leading + rest
    if len(shown) > 128:
        shown = shown[:64] + f'…[+{len(shown) - 128} cut]…' + shown[-64:]
    if word.removeprefix('--set=').startswith(('log=', 'prompt=')):
        shown += ' (not read by the yard)'
    return shown


def validate_general(ask):
    run = ask['run']
    def fail():
        raise NativeError('invalid native general card' + FALLBACK)
    if (set(run) - {'argv', 'general', 'command', 'description', 'does', 'back', 'back_by'}
            or not {'argv', 'general', 'command', 'description', 'does', 'back'} <= set(run)):
        fail()
    words = run['general']
    if not isinstance(words, list) or not words or not all(isinstance(w, str) for w in words):
        fail()
    if not words[0] or words[0].startswith('-') or words[0] == 'yardr':
        fail()
    for word in words:
        try:
            word.encode('utf-8')
        except UnicodeError:
            fail()
        if any(unicodedata.category(c) in {'Cc', 'Cf', 'Cs', 'Zl', 'Zp'} for c in word):
            fail()
    if skill_card_words(words):
        raise NativeError('run_ask_skill_general_refused; this skill operation has its own card' + FALLBACK)
    command = general_command(words)
    if (run['argv'] != words or run['command'] != command or run['description'] != GENERAL_DESCRIPTION
            or run['back'] != '' or run.get('back_by', '') != ''):
        fail()
    does = run['does']
    if (not isinstance(does, list) or len(does) != len(words) + 4 or does[:2] != GENERAL_WARNINGS[:2]
            or not isinstance(does[2], str)
            or not re.fullmatch(r'It is good until (?:[01][0-9]|2[0-3]):[0-5][0-9]:[0-5][0-9]\.', does[2])
            or does[3] != GENERAL_WARNINGS[2]
            or does[4:] != [f'{i}: ({len(word)}) {general_shown(word)}' for i, word in enumerate(words, 1)]):
        fail()
    expected = [{'class': 'code', 'line': 'brings a program the yard will run: '
                 + ' '.join((GENERAL_DESCRIPTION + ': ' + command).split())}]
    if ask.get('weight') != expected:
        fail()
    return words


def general_bundle(config, bead, ask, proposal):
    words = validate_general(ask)
    run = ask['run']
    review = 'Native Does (complete quoted lines):\n' + '\n'.join(json.dumps(line, ensure_ascii=True) for line in run['does'])
    snapshot = ('## Complete verbatim argv\n\n' + review_fact('File boundary', GENERAL_FILES)
                + '\n\nOne JSON string per word, in order; JSON escapes preserve every Unicode scalar. '
                'Decode each string and encode UTF-8 to recover its exact bytes. No shell parsing is used.\n\n')
    for index, word in enumerate(words, 1):
        snapshot += (f'Word {index} · {len(word)} runes · {len(word.encode("utf-8"))} UTF-8 bytes\n\n'
                     + review_code(json.dumps(word, ensure_ascii=True)) + '\n\n')
    snapshot += review_fact('Native weight', ask['weight'][0]['line'])
    # The complete native weight is in the document; the compact warning must fit too.
    compact = dict(ask, weight=[{'class': 'code', 'line': GENERAL_DESCRIPTION}])
    return review_bundle(config, bead, compact, proposal, 'Unchecked general command', review,
        [*GENERAL_WARNINGS[:2], run['does'][2] + ' Native local time only; no public date or timezone.',
         GENERAL_WARNINGS[2], GENERAL_FILES, f'Complete argv: {len(words)} words in the bound document above.'], snapshot)


GROUP_ROWS = (['group', 'add'], ['group', 'set'])
# Upstream's stored forms: group add's --set keys and group set's words, each in the order the yard writes them.
GROUP_KEYS = {'add': ('cmd', 'dir', 'kind', 'args', 'prompt', 'worktrees'), 'set': ('prompt', 'cmd', 'preset', 'model', 'effort')}
# The settings group add's card shows for each runner; any other one would be written unseen.
GROUP_SHOWN = {'herdr': {'kind', 'args', 'prompt', 'worktrees'}, 'exec': {'cmd', 'dir', 'prompt', 'worktrees'}}
# What group set's weight names of each setting it carries (strongChanged); an effort alone weighs nothing.
GROUP_WEIGHED = (('prompt', 'its prompt'), ('cmd', 'its command'), ('model', 'its model'), ('preset', 'its permission flags'))
# Words Go's strconv.Quote leaves as they are, so a card's quoted value is the word itself.
GROUP_WORD = re.compile(r'[A-Za-z0-9._/=@:+,%~-]{1,256}')
GROUP_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*')
GROUP_KIND = r'[a-z0-9][a-z0-9_-]{0,31}'
# Flags as a card shows them (quotedWords): Go-quoted words, or none.
GROUP_FLAGS = r'(?:"(?:[^"\\\n]|\\.)*"(?: "(?:[^"\\\n]|\\.)*")*|no flags of their own)'
GROUP_PROMPT_BYTES = 8192
GROUP_REWRITTEN = 'The yard names the files it writes by the SHA-256 of their text, and this file no longer holds the text its name says'
HIDDEN = set('\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u2028\u2029')


def quote_line(value):
    """A role line as a JSON string: every byte visible, nothing the reader could take for markup or layout."""
    out = []
    for char in value:
        if char in '\\"':
            out.append('\\' + char)
        elif char == '\n':
            out.append('\\n')
        elif char == '\t':
            out.append('\\t')
        elif unicodedata.category(char)[0] in 'CZ' and char != ' ':
            # JSON escapes, so any JSON reader recovers the exact line.
            out.append(json.dumps(char)[1:-1])
        else:
            out.append(char)
    return '"' + ''.join(out) + '"'


def line_endings(content):
    """content's lines, each with its own line ending."""
    rows = [line + '\n' for line in content.split('\n')[:-1]]
    if not content or not content.endswith('\n'):
        rows.append(content.rsplit('\n', 1)[-1])
    return rows


class GroupCard:
    """Read upstream's group card sentence by sentence; anything unknown refuses the whole card."""

    def __init__(self, does, at=0):
        self.does, self.at, self.runner, self.prompt, self.standing = does, at, None, None, None
        # What each setting the card changes holds now, as its lines say (None: nothing); the back restores it.
        self.was = {}

    def fail(self):
        raise NativeError('invalid native group review' + FALLBACK)

    def unquote(self, value):
        # A \x escape is a byte that is no UTF-8, which the back would write otherwise.
        if '\\x' in value:
            self.fail()
        try:
            return unquote(value)
        except NativeError:
            self.fail()

    def peek(self):
        return self.does[self.at] if self.at < len(self.does) else None

    def exact(self, line):
        if self.peek() != line:
            self.fail()
        self.at += 1

    def match(self, pattern, optional=False):
        found = re.fullmatch(pattern, self.peek() or '\0')
        if not found:
            if optional:
                return None
            self.fail()
        self.at += 1
        return found

    def prompt_text(self, name):
        """The file the approval writes and the text below it, recovered by the digest the file is named by."""
        found = self.match(rf"On approval the yard writes the text below to the file (/[^\n]*/roles/{re.escape(name.replace('/', '_'))}"
                           r"\.([0-9a-f]{64})\.md), read-only\. Each session reads that file when it starts, "
                           r"so changing the file the ask names later changes no session:")
        start = self.at
        while self.at < len(self.does):
            self.at += 1
            body = '\n'.join(self.does[start:self.at])
            if len(body.encode('utf-8')) > GROUP_PROMPT_BYTES:
                break
            # The card drops one final line ending (textLines); the digest says whether there was one.
            for candidate in (body, body + '\n'):
                if file_hash(candidate) == found[2]:
                    if (not candidate.strip() or len(candidate.encode('utf-8')) > GROUP_PROMPT_BYTES
                            or 'WEIGHTY · ' in candidate or '◆' in candidate
                            or any(c in HIDDEN or (unicodedata.category(c) == 'Cc' and c not in '\n\t') for c in candidate)):
                        self.fail()
                    self.prompt = dict(path=found[1], sha256=found[2], bytes=len(candidate.encode('utf-8')),
                                       start=start, end=self.at, text=candidate)
                    return
        self.fail()

    def set_tail(self, sets):
        """group set's sentences after the prompt's, in the order the card writes them, to the card's end."""
        if 'cmd' in sets:
            found = self.match(r'Each session runs sh -c with ("(?:[^"\\\n]|\\.)*") now\.|It runs no command now\.')
            self.was['cmd'] = self.unquote(found[1]) if found[1] else None
            self.exact(f'Each session will run sh -c with "{sets["cmd"]}", as its program stands when the session starts.')
        if 'model' in sets:
            found = self.match(r"Each session starts with (?:the model ([^\n]+)|the agent's own default model) now\.")
            self.was['model'] = found[1]
            self.exact(f"Each session will start with the model {sets['model']}.")
        if sets.get('effort') == 'default':
            self.was['effort'] = self.match(r'Each session starts at the effort ([^\n]+) now\.')[1]
            self.exact("It will set no effort, so each session will start at its harness's own: no flag for an effort is added.")
        elif 'effort' in sets:
            found = self.match(r"Each session starts at the effort ([^\n]+) now\.|It sets no effort now, so each session starts at its harness's own\.")
            self.was['effort'] = found[1]
            self.match(rf"Each session will start at the effort {re.escape(sets['effort'])}: {GROUP_FLAGS} are added after its other flags\.")
        if 'preset' in sets:
            found = self.match(rf'It starts {GROUP_KIND} \([^()\n]+\) agents with ({GROUP_FLAGS})(?: \(the [^()\n]+ preset\))? now\.')
            # The preset is written as its flags, so the back restores those (args).
            self.was['args'] = None if found[1] == 'no flags of their own' else [self.unquote(word) for word in re.findall(r'"(?:[^"\\\n]|\\.)*"', found[1])]
            found = self.match(rf'It will start them with {GROUP_FLAGS}: the ([^\n]+?) preset, [^\n]+\.')
            # The argv names the preset by its one-word label (PresetLabel).
            if found[1].strip().lower().replace(' ', '-') != sets['preset']:
                self.fail()
            self.exact('It skips permissions: it runs without asking for permission.')
        if self.at != len(self.does):
            self.fail()


def group_words(argv):
    """(name, runner, limit, sets) of upstream's stored group add or group set argv; any other shape refuses."""
    op, rest, runner, limit, sets = argv[1], argv[3:], None, None, {}
    refuse = NativeError('unsupported native group field' + FALLBACK)
    if len(argv) < 4 or len(argv) > 20 or not GROUP_NAME.fullmatch(argv[2]) or '..' in argv[2]:
        raise refuse
    if op == 'add':
        if len(rest) < 2 or rest[0] != '--runner':
            raise refuse
        runner, rest = rest[1], rest[2:]
        if rest[:1] == ['--limit']:
            if len(rest) < 2 or not re.fullmatch(r'[1-9][0-9]{0,8}', rest[1]):
                raise refuse
            limit, rest = rest[1], rest[2:]
        if len(rest) % 2 or any(flag != '--set' for flag in rest[::2]):
            raise refuse
        rest = rest[1::2]
    order = GROUP_KEYS[op]
    for word in rest:
        key, sep, value = word.partition('=')
        if (not sep or key not in order or not GROUP_WORD.fullmatch(value)
                or any(order.index(key) <= order.index(k) for k in sets)):
            raise refuse
        sets[key] = value
    if (op == 'add' and (runner not in GROUP_SHOWN or set(sets) - GROUP_SHOWN[runner] or (runner == 'exec') != ('cmd' in sets))
            or op == 'set' and set(sets) in (set(), {'model'})
            or any(key in sets and (not sets[key].startswith('/') or os.path.normpath(sets[key]) != sets[key])
                   for key in ('dir', 'worktrees', 'prompt'))):
        raise refuse
    return argv[2], runner, limit, sets


def shell_quote(value):
    """value as one word of a terminal command, as the yard writes one (shellQuote)."""
    if value and not value.strip(string.ascii_letters + string.digits + '/._-+:,@%'):
        return value
    return "'" + value.replace("'", "'\\''") + "'"


def group_backs(name, was):
    """The commands that put back what a group set card says each setting it changes holds now (strongBack)."""
    words, unset = [], []
    for key in ('prompt', 'cmd', 'args', 'model', 'effort'):
        if key not in was:
            continue
        value = was[key]
        if value is None:
            unset.append(key)
        elif key == 'args':
            # Written as Go's json.Marshal writes the words; the few bytes it writes otherwise refuse.
            if any(c in '<>&\u2028\u2029' or unicodedata.category(c) == 'Cc' for word in value for c in word):
                return set()
            words.append(shell_quote('args=' + json.dumps(value, ensure_ascii=False, separators=(',', ':'))))
        else:
            try:
                json.loads(value)
                # The yard writes a value that reads as JSON as a JSON string, in bytes not worth matching here.
                return set()
            except ValueError:
                pass
            words.append(key + '=' + value if shell_quote(value) == value else shell_quote(key + '=' + value))
    backs = {' '.join(['yardr group set', name, *words] + [f'--unset {key}' for key in unset])}
    if 'args' in unset:
        # No flags of their own reads the same for flags absent, an empty list or none.
        rest = [key for key in unset if key != 'args']
        at = len([key for key in ('prompt', 'cmd') if was.get(key) is not None])
        for empty in ("'args=[]'", "'args=null'"):
            backs.add(' '.join(['yardr group set', name, *words[:at], empty, *words[at:]] + [f'--unset {key}' for key in rest]))
    return backs


def parse_group(ask):
    """Check an upstream group add or group set card whole against its argv; return what its review quotes."""
    run, argv = ask['run'], ask['run']['argv']
    op = argv[1]
    name, runner, limit, sets = group_words(argv)
    if op == 'add':
        description, back_by = 'Add a new group without replacing one', 'the operator or the yardmaster'
        weight = [f'a new group {name} on the {runner} runner, whose sessions the yard will start as the card shows']
    else:
        description, back_by = 'Change some settings of a group and keep the rest', 'only the operator'
        weighed = [what for key, what in GROUP_WEIGHED if key in sets]
        weight = [f"{' and '.join(weighed)} of the group {name}, which each session the yard starts for it then runs as the card shows"] if weighed else []
    # A set's back is held below, to what its card says stands now.
    if (run['command'] != 'yardr ' + ' '.join(argv) or run.get('description') != description
            or op == 'add' and run['back'] != f'yardr group rm {name}'
            or '\n' in run['back'] or run.get('back_by') != back_by or run.get('carried')
            or ask.get('weight', []) != [dict({'class': 'code'}, line='brings a program the yard will run: ' + line) for line in weight]):
        raise NativeError('invalid native group snapshot or weight' + FALLBACK)
    card = GroupCard(run['does'])
    if op == 'add':
        card.runner = runner
        if limit:
            card.exact(f'adds the group {name} on the {runner} runner, with limit {limit}.')
        else:
            # A herdr or exec group starts with limit 1 (DefaultLimit); the bridge carries no manual one.
            card.exact(f"adds the group {name} on the {runner} runner, with limit 1, the runner's default.")
        if runner == 'exec':
            card.exact(f'Each session runs sh -c with "{sets["cmd"]}", as its program stands when the session starts.')
            # The yard writes the directory with its symlinks resolved; one that resolves elsewhere refuses.
            card.exact(f"The command runs in {sets['dir']}." if 'dir' in sets else 'The command runs in the depot of the bead.')
        elif 'kind' in sets:
            card.match(rf'It starts {re.escape(sets["kind"])} \([^()\n]+\) agents\.')
        else:
            card.match(rf"It starts claude agents, the herdr runner's own default\.|It starts {GROUP_KIND} \([^()\n]+\) agents, the yard's default_kind\.")
        if 'args' in sets:
            card.exact(f'It starts them with "{sets["args"]}".')
            card.match(r'It skips permissions: it runs without asking for permission\.', optional=True)
        if 'worktrees' in sets:
            card.exact(f"Its bead worktrees are made under {sets['worktrees']}.")
        card.exact(f'It sets no env_file: the operator gives one in a terminal, with yardr group set {name} env_file=<file>.')
        if 'prompt' in sets:
            card.prompt_text(name)
        if card.at != len(card.does):
            card.fail()
    else:
        card.runner = card.match(rf'changes the group {re.escape(name)} on the ([a-z][a-z0-9_-]{{0,31}}) runner: what each of its sessions runs, as the lines below say, and nothing else of it\.')[1]
        card = group_set_card(card, name, sets)
        if run['back'] not in group_backs(name, card.was):
            card.fail()
    # Bead serves no role today; one that rides beside the card must be the text the card shows.
    if 'role' in run and run['role'] != (card.prompt['text'] if card.prompt else ''):
        card.fail()
    return dict(op=op, name=name, runner=card.runner, prompt=card.prompt, standing=card.standing)


def group_set_card(card, name, sets):
    """The rest of a group set card: what stands of the prompt, the new one and the other settings."""
    if 'prompt' not in sets:
        card.set_tail(sets)
        return card
    head = r'Its prompt is the file ([^\n]+?), which each session reads when it starts'
    shows = False
    if card.match(r'It has no prompt now\.', optional=True):
        card.was['prompt'] = None
    elif found := card.match(head + r', and which holds now:', optional=True):
        shows = True
    elif not (found := card.match(head + r'\. This card cannot show it: [^\n]+\.', optional=True)):
        found = card.match(head + r'\.')
        if card.match(re.escape(GROUP_REWRITTEN) + r'\. It holds now:', optional=True):
            shows = True
        else:
            card.exact(GROUP_REWRITTEN + '.')
            card.match(r'This card cannot show it: [^\n]+\.')
    if 'prompt' not in card.was:
        card.was['prompt'] = found[1]
    if not shows:
        card.prompt_text(name)
        card.set_tail(sets)
        return card
    # The standing text runs to the sentence of the file the approval writes: exactly one split may read whole.
    whole = []
    for split in range(card.at + 1, len(card.does)):
        trial = GroupCard(card.does, split)
        trial.runner, trial.was = card.runner, dict(card.was)
        try:
            trial.prompt_text(name)
            trial.set_tail(sets)
        except NativeError:
            continue
        trial.standing = dict(path=found[1], start=card.at, end=split)
        whole.append(trial)
    if len(whole) != 1:
        card.fail()
    return whole[0]


# The optional skill administration cards (core runask_skill.go): fork-only, absent from public core.
SKILL_NAME = r'[a-z0-9][a-z0-9._-]{0,63}'
SKILL_TREE = r'[0-9a-f]{64}'
SKILL_LIST = rf'no skills|the skills {SKILL_NAME}(?:, {SKILL_NAME})*'
SKILL_HOLDER = rf'group {GROUP_NAME.pattern}|crew member [A-Za-z0-9][A-Za-z0-9._-]*'
SKILL_SUBJECTS = {'group': 'group', 'crew': 'crew member'}
SKILL_DESCRIPTIONS = {'skill': 'Remove a skill from the yard', 'group': 'Change some settings of a group and keep the rest',
                      'crew': 'Change some settings of a crew member and keep the rest'}
SKILL_RM_CARRY = ("The yard's server carries the removal out after the approval, as one operation that takes the skill off "
                  'its holders and records skill_removed together; the tree and holders are held to these again first.')
SKILL_RM_NO_BACK = 'No way back on a card: a removed tree is added again only with yardr skill add <dir>.'


def skill_card_words(words):
    """Whether words name skill administration in any form (SkillCardWords): only its own card carries it."""
    words = [w for w in ' '.join(words).split() if w != '--json' and not w.startswith('--json=')]
    if words[:1] == ['yardr']:
        words = words[1:]
    if words[:2] == ['skill', 'rm']:
        return True
    if len(words) < 2 or words[0] not in ('group', 'crew') or words[1] != 'set':
        return False
    return any(word.removeprefix('--set=').startswith('skills=') or word.removeprefix('--set=') == '--unset=skills'
               or word == '--unset' and i + 3 < len(words) and words[i + 3] == 'skills'
               for i, word in enumerate(words[2:]))


def skill_kind(argv):
    """'rm' or 'set' for a skill administration card's argv, else None."""
    if argv[:2] == ['skill', 'rm']:
        return 'rm'
    if argv[:2] in (['group', 'set'], ['crew', 'set']) and skill_card_words(argv):
        return 'set'
    return None


class SkillCard(GroupCard):
    """Read upstream's skill card sentence by sentence; anything unknown refuses the whole card."""

    def fail(self):
        raise NativeError('invalid native skill card' + FALLBACK)

    def names(self, value):
        names = [] if value == 'no skills' else value.removeprefix('the skills ').split(', ')
        if len(set(names)) != len(names):
            self.fail()
        return names

    def sessions(self, subject):
        return self.match(re.escape(f'No session of the {subject} runs now.') + '|'
                          + re.escape(f'Sessions of the {subject} running now, which read its skills as they stand (yardr skill show): ')
                          + r'[^\n]+\.')[0]


def parse_skill(ask):
    """Check a skill rm or skills assignment card whole against its argv; return the facts its review shows."""
    run, argv = ask['run'], ask['run']['argv']
    card = SkillCard(run['does'])
    if skill_kind(argv) == 'rm':
        if len(argv) not in (3, 4) or argv[3:] not in ([], ['--force']) or not re.fullmatch(SKILL_NAME, argv[2]):
            card.fail()
        name, force = argv[2], len(argv) == 4
        if (set(run) != {'argv', 'command', 'description', 'does', 'back'} or run['command'] != 'yardr ' + ' '.join(argv)
                or run['description'] != SKILL_DESCRIPTIONS['skill'] or run['back'] != '' or ask.get('weight', []) != []):
            card.fail()
        # The count reads "entries" on core 5dedfc56 and "regular files" from 9677728c on: those two sentences only.
        found = card.match(rf'removes the skill {re.escape(name)} from the yard: its directory (/[^\n]*/skills/{re.escape(name)}) '
                           r'and all (0|[1-9][0-9]{0,8}) (?:entries|regular files) of its tree\.')
        directory, files = found[1], int(found[2])
        tree = card.match(rf'Its tree now is ({SKILL_TREE})\.')[1]
        described = card.match(r'Its description: ([^\n]+)', optional=True)
        holders = []
        if card.match(re.escape('No group or crew member is given it.'), optional=True) is None:
            while found := card.match(rf'The ({SKILL_HOLDER}) is given ({SKILL_LIST}) now, and will be given ({SKILL_LIST})\.', optional=True):
                before, after = card.names(found[2]), card.names(found[3])
                if name not in before or after != [skill for skill in before if skill != name]:
                    card.fail()
                holders.append(dict(holder=found[1], before=before, after=after, sessions=card.sessions(found[1])))
            # A removal without --force is refused while anyone is given the skill.
            if not holders or not force or len({h['holder'] for h in holders}) != len(holders):
                card.fail()
        card.exact(SKILL_RM_CARRY)
        if card.at != len(card.does):
            card.fail()
        return dict(kind='rm', name=name, force=force, directory=directory, files=files, tree=tree, trees={name: tree},
                    description=described[1] if described else None, holders=holders)
    of, name, words = argv[0], argv[2], argv[3:]
    subject = SKILL_SUBJECTS[of] + ' ' + name
    if (of == 'group' and (not GROUP_NAME.fullmatch(name) or '..' in name)
            or of == 'crew' and not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', name)):
        card.fail()
    unset = words == ['--unset', 'skills']
    found = None if unset or len(words) != 1 else re.fullmatch(rf'skills=\[({SKILL_NAME}(?:,{SKILL_NAME})*)\]', words[0])
    if not unset and not found:
        card.fail()
    wanted = [] if unset else found[1].split(',')
    if (wanted != sorted(set(wanted)) or set(run) != {'argv', 'command', 'description', 'does', 'back', 'back_by'}
            or run['command'] != general_command(argv) or run['description'] != SKILL_DESCRIPTIONS[of]
            or run['back_by'] != 'only the operator'):
        card.fail()
    card.exact(f"changes the skills the {subject} is given: what yardr names in its agents' briefs and primes and hands them "
               'with yardr skill show, and nothing else of it.')
    was = card.names(card.match(rf'It is given ({SKILL_LIST}) now\.')[1])
    if unset:
        card.exact('It will be given no skills: the skills key is unset.')
    elif card.names(card.match(rf'It will be given ({SKILL_LIST})\.')[1]) != wanted or was == wanted:
        card.fail()
    added = [skill for skill in wanted if skill not in was]
    trees, summaries = {}, {}
    for skill in sorted(set(was) | set(wanted)):
        if skill not in added and card.match(re.escape(f'Skill {skill}: the yard has none of that name now.'), optional=True):
            trees[skill] = 'absent'
            continue
        found = card.match(rf'Skill {re.escape(skill)}: tree ({SKILL_TREE})\.(?: ([^\n]+))?')
        trees[skill], summaries[skill] = found[1], found[2]
    sessions = card.sessions(subject)
    if card.at != len(card.does):
        card.fail()
    backs = {general_command([of, 'set', name, f"skills=[{','.join(was)}]"])} if was else {
        general_command([of, 'set', name, '--unset', 'skills']), general_command([of, 'set', name, 'skills=[]'])}
    weight = [dict({'class': 'code'}, line=f"brings a program the yard will run: the instructions of {and_list(added)} for the "
                   f'{subject}, which each of its sessions is handed as the card shows')] if added else []
    if run['back'] not in backs or ask.get('weight', []) != weight:
        card.fail()
    return dict(kind='set', of=of, name=name, subject=subject, unset=unset, before=was, after=wanted, added=added,
                removed=[skill for skill in was if skill not in wanted], trees=trees, summaries=summaries, sessions=sessions)


def and_list(words):
    return ' and '.join(words) if len(words) < 3 else ', '.join(words[:-1]) + ' and ' + words[-1]


def skill_names(names):
    return ', '.join(names) if names else '(none)'


def skill_bundle(config, bead, ask, proposal, card):
    """The complete review document and compact card of a skill administration card."""
    does, run = ask['run']['does'], ask['run']
    if card['kind'] == 'rm':
        manifest = dict(operation='skill rm', name=card['name'], force=card['force'], tree=card['tree'], files=card['files'],
                        directory=card['directory'], holders=[{k: h[k] for k in ('holder', 'before', 'after')} for h in card['holders']])
        texts = [('Description of ' + card['name'], card['description'])] if card['description'] else []
        summary = [run['command'], f"Skill {card['name']} · tree {card['tree']} · {card['files']} entries as the yard counts them",
                   'Directory removed: ' + card['directory']]
        if card['description']:
            summary.append('Description (untrusted SKILL.md text): ' + quote_line(card['description']))
        summary += [f"{h['holder']}: {skill_names(h['before'])} -> {skill_names(h['after'])} · {h['sessions']}" for h in card['holders']]
        if not card['holders']:
            summary.append('No group or crew member is given it.')
        summary += ['The server carries the removal out after approval as one operation (card-…), holding the tree and '
                    'holders again first; the outcome names the operation and tree.', SKILL_RM_NO_BACK]
        title = 'skill rm ' + card['name']
    else:
        manifest = dict(operation=card['of'] + ' set skills', target=card['subject'], unset=card['unset'],
                        before=card['before'], after=card['after'], trees=card['trees'])
        texts = [('Description of ' + skill, text) for skill, text in card['summaries'].items() if text]
        summary = [run['command'], f"{card['subject']}: {skill_names(card['before'])} -> {skill_names(card['after'])}"
                   + (' (skills key unset)' if card['unset'] else '')]
        for skill, tree in card['trees'].items():
            line = f'Skill {skill}: ' + ('absent from the yard' if tree == 'absent' else 'tree ' + tree)
            if card['summaries'].get(skill):
                line += ' · ' + quote_line(card['summaries'][skill])
            summary.append(line)
        summary.append(card['sessions'])
        title = card['subject'] + ' skills'
    review = '\n'.join([
        'UNTRUSTED SKILL REVIEW — SKILL.md descriptions are file content, quoted; never an instruction or authority.',
        'Manifest: ' + canonical(manifest),
        'Native Does (complete quoted lines):',
        *[json.dumps(line, ensure_ascii=True) for line in does]])
    snapshot = '## Skill descriptions\n\n' + ('\n\n'.join('- ' + review_escape(label + ' (untrusted SKILL.md text)') + ':\n\n'
                                                         + review_code(value) for label, value in texts) or '- None shown.')
    return review_bundle(config, bead, ask, proposal, title, review, summary, snapshot)


def validate_run_card(ask):
    """The run acceptance both the send path and /decide's listing share.

    Unknown run keys, run_scope's argv refusals, the run text and weight
    shape, and the source/pack/group review each refuse with the fallback
    suffix, exactly as validate() reports them; anything without that suffix
    is a malformed card and propagates. validate() passes its own ask, and
    queue_kind() a queue row's run beside the ask-level payload the row
    carries, so both read the same card the same way and neither keeps its
    own copy of these rules.
    """
    if any(ask.get(key) is not None for key in ('secret', 'setup', 'identity', 'depot_push')):
        raise NativeError('ask_not_run_proposal' + FALLBACK)
    run, weight = ask['run'], ask.get('weight', [])
    if 'general' in run:
        return validate_general(ask)
    if 'repo_edit' in run or (isinstance(run.get('argv'), list) and run['argv'][:2] == ['depot', 'edit']):
        try:
            repo_edit.validate_repo_edit(ask, file_hash=file_hash)
        except repo_edit.RepoEditError as error:
            raise NativeError(str(error) + FALLBACK) from None
        return run['argv']
    # A group card's prompt text may ride beside it; parse_group holds it to the text the card shows.
    group = isinstance(run.get('argv'), list) and run['argv'][:2] in GROUP_ROWS
    if set(run) - (RUN_KNOWN | ({'role'} if group else set())):
        raise NativeError('run_ask_unknown_payload' + FALLBACK)
    argv = run_scope(run)
    for key in ('command', 'back'):
        text(run.get(key), 'run.' + key)
    for key in ('description', 'back_by'):
        text(run.get(key, ''), 'run.' + key)
    lines(run.get('does'), 'run.does')
    if not isinstance(weight, list):
        raise NativeError('invalid native weight')
    for item in weight:
        if (not isinstance(item, dict) or set(item) != {'class', 'line'}
                or not isinstance(item['class'], str) or item['class'] not in WEIGHTS or '\n' in text(item['line'], 'weight.line')):
            raise NativeError('invalid native weight')
    if argv[:2] == ['source', 'add']:
        try:
            validate_source(ask)
        except NativeError as error:
            raise NativeError(str(error) + FALLBACK) from None
    elif argv[:2] == ['pack', 'apply']:
        try:
            validate_pack(ask)
        except NativeError:
            kind = 'depot' if depot_pack(run.get('pack')) else 'source'
            raise NativeError(f'run_ask_invalid_{kind}_pack_use_terminal; use yardr pack apply <dir> in a terminal' + FALLBACK) from None
    elif skill_kind(argv):
        parse_skill(ask)
    elif argv[:2] in GROUP_ROWS:
        parse_group(ask)
    return argv


def validate(ask, operator):
    known = {'ask', 'to', 'asker', 'since', 'question', 'context', 'options', 'recommend', 'why', 'about',
             'state', 'option', 'words', 'by', 'return_at', 'picks_before', 'secret', 'concealed', 'given',
             'run', 'ran', 'setup', 'identity', 'depot_push', 'weight', 'turn'}
    if set(ask) - known:
        raise NativeError('run_ask_unknown_payload' + FALLBACK)
    if ask.get('options') not in (None, []) or ask.get('about') or ask.get('recommend') or ask.get('why'):
        raise NativeError('ask_not_run_proposal' + FALLBACK)
    if ask.get('to') != operator:
        raise NativeError('run_ask_wrong_recipient')
    if (not isinstance(ask.get('run'), dict)
            or any(ask.get(key) is not None for key in ('secret', 'setup', 'identity', 'depot_push'))):
        raise NativeError('ask_not_run_proposal' + FALLBACK)
    for key in ('question', 'asker', 'to', 'state'):
        text(ask.get(key), key)
    lines(ask.get('context'), 'context')
    validate_run_card(ask)


def card_text(value, kind, *, headline=False):
    # Narrative Markdown cannot mint links, mentions or the native warning marker.
    try:
        markup = markdown.render(without_weight_mark(value), CAP, links=False, headline=headline)
    except markdown.MarkdownError as error:
        if str(error) == 'markdown_source_too_long':
            raise NativeError(kind + '_payload_too_large_for_card' + FALLBACK) from None
        raise NativeError('invalid native card Markdown: ' + str(error) + FALLBACK) from None
    visible = unescape(re.sub(r'<[^>]*>', '', markup))
    safe = without_weight_mark(visible)
    if safe != visible:
        return '<b>' + escape(safe) + '</b>' if headline else escape(safe)
    return markup


def card_result(markup, kind):
    # New cards store footer spacing; legacy edit reservations keep their exact bytes.
    markup += '\n'
    # Only renderer-owned tags exist here; escaped author text stays visible text.
    plain = unescape(re.sub(r'<[^>]*>', '', markup))
    if units(plain) > CAP:
        raise NativeError(kind + '_payload_too_large_for_card' + FALLBACK)
    return plain, markup


def render(config, bead, ask):
    run = ask['run']
    parts = []

    def add(value, tag=None, trusted=False):
        value = value if trusted else without_weight_mark(value)
        escaped = escape(value)
        parts.append(f'<{tag}>{escaped}</{tag}>' if tag else escaped)

    add(f"{config['yard']} · {bead['id']} · ask #{ask['ask']}", 'b')
    add(f"approving runs in {config['yard']} as {config['yardr_operator']}")
    for weight in ask.get('weight', []):
        add(f"WEIGHTY · {weight['class']}: {weight['line']}", 'b', trusted=True)
    parts.append('')
    add(text(bead.get('title'), 'bead.title'), 'b')
    add('Proposed by ' + ask['asker'])
    parts.extend(['', card_text(ask['question'], 'run_ask', headline=True), ''])
    if run.get('description'):
        add(run['description'])
    # Canonical command text remains exact; preformatted text cannot be a warning line.
    add(run['command'], 'pre', trusted=True)
    add('Does:')
    for line in run['does']:
        add(line, 'pre')
    parts.append('')
    add('Takes it back: ' + run['back'])
    if run.get('back_by'):
        add(run['back_by'] + ' can take it back')
    for line in ask['context']:
        parts.extend(['', card_text(line, 'run_ask')])
    return card_result('\n'.join(parts), 'run_ask')


def validate_decision(ask, operator):
    known = {'ask', 'to', 'asker', 'since', 'question', 'context', 'options', 'recommend', 'why', 'about',
             'state', 'option', 'words', 'by', 'return_at', 'picks_before', 'weight', 'turn'}
    if set(ask) - known:
        raise NativeError('ask_card_unsupported_payload' + FALLBACK)
    if 'turn' not in ask:
        raise NativeError('ask_card_requires_guarded_core' + FALLBACK)
    if type(ask['turn']) is not int or not 0 <= ask['turn'] <= 2**63 - 1:
        raise NativeError('invalid native turn')
    if ask.get('to') != operator:
        raise NativeError('ask_card_wrong_recipient')
    for key in ('question', 'asker', 'to', 'state', 'since'):
        text(ask.get(key), key)
    lines(ask.get('context'), 'context')
    if 'options' not in ask:
        raise NativeError('invalid native options')
    options = ask['options']
    # Go serializes nil slices as null.
    if options is not None and not isinstance(options, list):
        raise NativeError('invalid native options')
    for option in options or []:
        if not isinstance(option, dict) or set(option) != {'label', 'consequence'}:
            raise NativeError('invalid native option')
        for key in ('label', 'consequence'):
            if not text(option[key], 'option.' + key).strip():
                raise NativeError('invalid native option.' + key)
    recommend = ask.get('recommend', 0)
    if type(recommend) is not int or not 0 <= recommend <= len(options or []):
        raise NativeError('invalid native recommend')
    text(ask.get('why', ''), 'why')
    if not isinstance(ask.get('about', []), list):
        raise NativeError('invalid native about')
    for about in ask.get('about', []):
        if not isinstance(about, dict) or set(about) != {'id', 'title', 'stage'}:
            raise NativeError('invalid native about')
        for key in ('id', 'title', 'stage'):
            text(about[key], 'about.' + key)
    # Current ordinary weights belong to identity mapping, which needs its own complete review.
    if ask.get('weight', []) != []:
        raise NativeError('ask_card_weighted_action_unsupported' + FALLBACK)


def render_decision(config, bead, ask):
    identity = [f"{config['yard']} · {bead['id']} · ask #{ask['ask']}",
             f"Answering in {config['yard']} as {config['yardr_operator']}",
             text(bead.get('title'), 'bead.title'), 'Asked by ' + ask['asker']]
    def literal(value):
        return escape(without_weight_mark(value))

    parts = [literal('\n'.join(identity)), card_text(ask['question'], 'ask_card', headline=True)]
    parts.extend(card_text(line, 'ask_card') for line in ask['context'])
    for index, option in enumerate(ask.get('options') or [], 1):
        parts.append(f"<b>{index}. {literal(option['label'])}</b>\n  "
                     + literal(option['consequence']).replace('\n', '\n  '))
    recommendation = []
    if ask.get('recommend'):
        recommendation.append(f"Recommended: {ask['recommend']}. {ask['options'][ask['recommend'] - 1]['label']}")
    if ask.get('why'):
        recommendation.append('Why: ' + ask['why'])
    if recommendation:
        parts.append(literal('\n'.join(recommendation)))
    for about in ask.get('about', []):
        parts.append(literal(f"About: {about['id']} · {about['title']} · {about['stage']}"))
    parts.append('Reply directly to this card with ' +
                 ('an option number and optional words.' if ask.get('options') else 'your answer.'))
    return card_result('\n\n'.join(parts), 'ask_card')


def outcome(ask, options):
    """The canonical outcome line of a decision that is no longer open."""
    state, words = ask['state'], text(ask.get('words', ''), 'words')
    if state == 'answered':
        option = ask.get('option', 0)
        if type(option) is not int or (options and not 1 <= option <= len(options)) or (not options and option != 0):
            raise NativeError('invalid native answered option')
        footer = '✓ Answered'
        if option:
            footer += f": {option}. {options[option - 1]['label']}"
        if words:
            footer += '\n' + words
    elif state == 'put aside':
        footer = '⏳ Put aside'
        if ask.get('return_at'):
            footer += ' until ' + text(ask['return_at'], 'return_at')
    elif state == 'taken back':
        footer = '✕ Taken back by ' + text(ask.get('by'), 'by') + ': ' + words
    else:
        footer = '✕ Declined: ' + words
    return footer


def repo_edit_reserved_bead(ran):
    """Extract the reserved patch bead id from a v2 repo-edit answer's pending_reason or result."""
    pending_reason = ran.get('pending_reason', '')
    if isinstance(pending_reason, str):
        match = re.match(rf'^reserved held bead ({BEAD_ID});', pending_reason)
        if match:
            return match[1]
    result = ran.get('result', '')
    if isinstance(result, str):
        match = re.match(rf'^prepared ({BEAD_ID}) as commit ', result)
        if match:
            return match[1]
    return None


def validate_repo_edit_status(status, reserved_bead, payload):
    """Validate NativeEditStatus against the reserved bead and approved repo-edit payload."""
    required = {'state', 'step', 'bead', 'depot', 'operation', 'digest'}
    allowed = required | {'head', 'branch', 'worktree', 'words', 'landed'}
    if (not isinstance(status, dict) or not required <= set(status) or set(status) - allowed
            or not isinstance(status['state'], str)
            or status['state'] not in {'pending', 'preparing', 'prepared', 'reviewed', 'held', 'failed', 'landed', 'withdrawn'}
            or status['bead'] != reserved_bead
            or status['depot'] != payload['request']['depot']
            or status['operation'] != payload['request']['operation']
            or status['digest'] != payload['digest']):
        raise NativeError('invalid native edit status')
    out = {}
    for key, value in status.items():
        out[key] = text(value, 'edit_status.' + key, controls=(key == 'words'))
    return out


def repo_edit_status_line(edit_status):
    st = edit_status['state']
    bead = edit_status['bead']
    branch = edit_status.get('branch', '')
    head = edit_status.get('head', '')
    if st == 'landed':
        landed = edit_status.get('landed') or head
        return f"Patch bead {bead} · branch {branch} · landed commit {landed} (source landed; not installed or live)"
    if st == 'withdrawn':
        return f"Patch bead {bead} · withdrawn before landing"
    if st == 'reviewed':
        return f"Patch bead {bead} · branch {branch} · commit {head} · in assembly (reviewed; not landed or installed)"
    if st == 'prepared':
        return f"Patch bead {bead} · branch {branch} · commit {head} (prepared for review; not landed or installed)"
    if st in ('held', 'failed'):
        words = edit_status.get('words', '')
        return f"Patch bead {bead} · {st}" + (f': {words}' if words else '')
    return ''


def run_outcome(ask, edit_status=None):
    """(state, canonical outcome) of a run ask that is answered, declined or taken back."""
    state = ask['state']
    if state == 'answered':
        ran = ask.get('ran')
        if (not isinstance(ran, dict) or set(ran) - {'argv', 'result', 'pending_reason', 'failed'}
                or lines(ran.get('argv'), 'ran.argv') != ask['run'].get('argv')
                or type(ran.get('failed', False)) is not bool):
            raise NativeError('invalid native ran response')
        result = text(ran.get('result'), 'ran.result', controls=True)
        pending_reason = text(ran.get('pending_reason', ''), 'ran.pending_reason', controls=True)
        is_repo_edit = isinstance(ask.get('run'), dict) and 'repo_edit' in ask['run']
        state = 'failed' if ran.get('failed') else 'carrying' if result == PENDING else 'approved'
        if is_repo_edit and state != 'carrying':
            st = edit_status['state'] if edit_status else None
            if state == 'failed' or st in ('held', 'failed'):
                label = '✕ approved, held' if st == 'held' else '✕ approved, failed'
            elif st == 'landed':
                label = '✓ approved, landed (not installed)'
            elif st == 'withdrawn':
                label = '✕ approved, withdrawn'
            elif st in ('prepared', 'reviewed') or result.startswith('prepared '):
                label = '✓ approved, prepared (not landed or installed)'
            else:
                label = '✓ approval recorded' if not result else '✓ approved, ran'
        else:
            label = {'failed': '✕ approved, failed', 'carrying': '⏳ approved, to be carried out',
                     'approved': '✓ approval recorded' if not result else '✓ approved, ran'}[state]
        footer = label + ': ' + text(ask['run'].get('command'), 'run.command') + '\n' + result
        if pending_reason:
            footer += '\n' + pending_reason
        if is_repo_edit and edit_status is not None:
            status_line = repo_edit_status_line(edit_status)
            if status_line:
                footer += '\n' + status_line
        if 'general' in ask['run'] and ran.get('failed') and result == GENERAL_STOPPED:
            footer += '\nUNKNOWN command outcome. Independently inspect effects before requesting a fresh human decision; never replay automatically.'
        return state, footer
    words = text(ask.get('words', ''), 'words')
    return state, '✕ ' + ('Taken back by ' + text(ask.get('by'), 'by') if state == 'taken back' else 'Declined') + ': ' + words


def review_html(body, continuation=''):
    parts, quoted = [], []
    for index, line in enumerate(body.split('\n')):
        prefix = continuation if index == 0 else ''
        if (prefix + line).startswith('"'):
            quoted.append(line)
            continue
        if quoted:
            parts.append('<pre>' + escape('\n'.join(quoted)) + '</pre>')
            quoted = []
        rendered = escape(line)
        if not prefix and (line.startswith(('UNTRUSTED ', 'Manifest:', 'Standing inventory:')) or 'quoted UTF-8 lines' in line):
            rendered = '<b>' + rendered + '</b>'
        parts.append(rendered)
    if quoted:
        parts.append('<pre>' + escape('\n'.join(quoted)) + '</pre>')
    return '\n'.join(parts)


def review_escape(value):
    return re.sub(r'([!-/:-@\[-`{-~])', r'\\\1', value)


def review_code(value):
    fence = '`' * max(3, 1 + max((len(m[0]) for m in re.finditer(r'`+', value)), default=0))
    raw = value.encode('utf-8')
    hidden, offset = [], 0
    for char in value:
        if char not in ' \n' and unicodedata.category(char)[0] in 'CZ':
            hidden.append(f'{offset}={json.dumps(char, ensure_ascii=True)}')
        offset += len(char.encode('utf-8'))
    trailing = len(value) - len(value.rstrip('\n'))
    metadata = (f'Content: {len(raw)} UTF-8 bytes; SHA-256 {file_hash(value)}; '
                f'trailing LF bytes: {trailing}; '
                'non-LF controls/separators at byte offsets: ' + (', '.join(hidden) or '(none)'))
    return '- ' + review_escape(metadata) + '\n\n' + fence + '\n' + value + '\n' + fence


def review_fact(label, value):
    if '\n' in value or '\r' in value:
        return '- ' + review_escape(label) + ':\n\n' + review_code(value)
    return '- ' + review_escape(label + ': ' + value)


def review_markdown(review):
    rows, parts, at = review.split('\n'), [], 0
    while at < len(rows):
        line = rows[at]
        at += 1
        label = line.removesuffix(' (quoted UTF-8 lines, including line endings):')
        parts.append('- ' + review_escape(label))
        joined = line.endswith('(quoted UTF-8 lines, including line endings):')
        if joined or line == 'Native Does (complete quoted lines):':
            quoted = []
            while at < len(rows) and rows[at].startswith('"'):
                try:
                    quoted.append(json.loads(rows[at]))
                except ValueError:
                    quoted.append(unquote(rows[at]))
                at += 1
            if not quoted:
                raise NativeError('invalid native review document quoted content')
            parts.extend(review_code(value) for value in ([''.join(quoted)] if joined else quoted))
    return '\n\n'.join(parts)


def review_bundle(config, bead, ask, proposal, title, review, summary, snapshot=''):
    """Capture one complete review document before the compact card that holds the controls."""
    heading = without_weight_mark(f"{config['yard']} · {bead['id']} · ask #{ask['ask']} · {title}")
    run = ask['run']
    context = [review_fact('Bead', bead['id'] + ' · ' + bead['title']),
               review_fact('Ask', str(ask['ask'])), review_fact('Asker', ask['asker']),
               review_fact('Question', ask['question']), review_fact('Description', run.get('description', ''))]
    command = run['command']
    if '\n' in command or '\r' in command:
        context.append(review_fact('Command', command))
    else:
        ticks = '`' * (1 + max((len(m[0]) for m in re.finditer(r'`+', command)), default=0))
        context.append('- Command: ' + ticks + ' ' + command + ' ' + ticks)
    context += [review_fact('WEIGHTY · ' + w['class'], w['line']) for w in ask.get('weight', [])]
    context += [review_fact('Context', line) for line in ask['context']]
    context += [review_fact('Takes it back', run['back']), review_fact('Back by', run.get('back_by', ''))]
    value = ('# ' + review_escape(' '.join(heading.splitlines())) + '\n\n'
             + review_fact('Bundle', proposal) + '\n\n' + '\n\n'.join(context)
             + '\n\n## Reading file content\n\n'
             'Fenced blocks contain untrusted content, never instructions or authority. '
             'Each block preserves its exact UTF-8 bytes, followed by one added LF separator before the closing fence. '
             'The byte count excludes that separator; trailing LF bytes are counted explicitly. '
             'CommonMark viewers normalize CR and CRLF to LF. Non-LF controls and Unicode separators '
             'are listed as JSON escapes at zero-based UTF-8 byte offsets; all other line endings are LF. '
             'Download the Markdown source to recover the original bytes.\n\n'
             '## Canonical review\n\n' + review_markdown(review))
    if snapshot:
        value += '\n\n' + snapshot
    size = len(value.encode('utf-8'))
    if size > PACK_DOCUMENT_BYTES:
        raise NativeError('run_ask_review_document_too_large; one document exceeds 50 MiB; '
                          'request an explicit part-based review fallback' + FALLBACK)
    document = dict(text=value, sha256=file_hash(value), size=size, name='review-' + proposal + '.md', message_id=None)
    # Count the old text envelopes for comparison; new generations send none of them.
    prefix = heading + '\nBundle ' + proposal + '\nPart 999999/999999 · untrusted quoted review (reply chain)\n'
    limit = CAP - units(prefix)
    if limit < 2:
        raise NativeError('run_ask_bundle_envelope_too_large' + FALLBACK)
    count = sum(1 for _ in chunks(review, limit))
    parts = ['<b>' + escape(heading) + '</b>',
             escape(f"Approving in {config['yard']} as {config['yardr_operator']} · proposed by {ask['asker']}")]
    parts += ['<b>' + escape('WEIGHTY · ' + w['class'] + ': ' + w['line']) + '</b>' for w in ask.get('weight', [])]
    parts += ['', escape(without_weight_mark('\n'.join(summary))), '',
              f'Review: 1 document · {size} bytes · {count} text parts contained, 0 sent',
              'File: <code>' + document['name'] + '</code>',
              'Bundle: <code>' + proposal + '</code>',
              'Document SHA-256: <code>' + document['sha256'] + '</code>',
              'Open the document above for the complete proposal, files and diffs.',
              escape(without_weight_mark('Takes it back: ' + run['back'] + ' (' + run.get('back_by', '') + ')'))]
    plain, markup = card_result('\n'.join(parts), 'run_ask')
    bundle = dict(digest=proposal, sha256=file_hash(review), parts=[], text_parts=count, document=document)
    return plain, markup, bundle


def source_text(value, field, limit):
    text(value, field, controls=True)
    if len(value.encode('utf-8')) > limit:
        raise NativeError('invalid native ' + field)
    return value


def source_path(value):
    text(value, 'source path')
    if (not isinstance(value, str) or not value or value.startswith('/')
            or any(part in ('', '.', '..') for part in value.split('/'))
            or any(ord(c) < 32 or c in HIDDEN for c in value)):
        raise NativeError('invalid native source path')
    return value


def source_yaml(value):
    # Refuse aliases, tags and duplicate keys before reading either declaration or manifest.
    import yaml
    try:
        events = list(yaml.parse(value, Loader=yaml.BaseLoader))
        if any(isinstance(e, yaml.AliasEvent) or getattr(e, 'anchor', None) or getattr(e, 'tag', None) for e in events):
            raise ValueError()
        node = yaml.compose(value, Loader=yaml.BaseLoader)
        def read(node, depth=0):
            if depth > 40:
                raise ValueError()
            if isinstance(node, yaml.ScalarNode):
                return node.value
            if isinstance(node, yaml.SequenceNode):
                return [read(n, depth+1) for n in node.value]
            if not isinstance(node, yaml.MappingNode):
                raise ValueError()
            result = {}
            for k, v in node.value:
                if not isinstance(k, yaml.ScalarNode) or k.value in result:
                    raise ValueError()
                result[k.value] = read(v, depth+1)
            return result
        return read(node)
    except (yaml.YAMLError, ValueError, RecursionError):
        raise NativeError('invalid native source YAML') from None


def source_declaration(value):
    try:
        decl = source_yaml(value)
        # Match the core source reader's field names; None keys are names supplied by a declaration.
        schema = {
            'name': str, 'about': str, 'base': str, 'bases': {None: str}, 'check': str, 'idempotency': str,
            'credential': {'env': str, 'update_env': str, 'header': str, 'mint': [(str, {'name': str, 'secret': str})],
                           'mint_command': [str], 'in_path': str, 'url': str},
            'calls': {None: {'kind': str, 'about': str, 'build': [str],
                             'params': {None: {'type': str, 'pattern': str, 'max': str, 'values': [str],
                                               'optional': str, 'default': str, 'log': str}},
                             'request': {'method': str, 'path': str, 'query': {None: str}, 'body': None},
                             'answer': {'select': [str], 'max': str}}},
            'notify': {'call': str, 'address': {'param': str, 'env': str}, 'title': str, 'text': str,
                       'link': str, 'image': [str]},
            'hear': {'field': str, 'by': str, 'address': {'env': str}, 'from': str, 'subscribe': {None: str}},
            'limits': {'command': [str], 'kinds': {None: str}},
            'thread': {'reply': [str], 'posted': [str], 'read': [str], 'file': [str], 'open': [str],
                       'opened': [str], 'state': [str], 'place': str, 'states': [str], 'labels': str,
                       'assignee': str, 'files': str, 'formats': [str]},
            'link': {'host': str, 'issue': str, 'fixed': str, 'thread': str, 'fold': str, 'repos': str,
                     'pull': {'issue': str, 'fixed': str, 'thread': str}},
            'hook': {'verify': {'scheme': str, 'signed': str, 'algorithm': str,
                                'signature': {'header': str, 'encoding': str, 'prefix': str, 'list': str},
                                'key': {'prefix': str, 'encoding': str}, 'delivery': {'header': str, 'body': str},
                                'timestamp': {'body': str, 'header': str, 'unit': str, 'window': str}},
                     'bodies': str, 'challenge': {'body': str, 'value': str, 'answer': str}, 'normalize': [str],
                     'keys': [str], 'register': {'call': str, 'url': str, 'secret': str, 'ask': [str],
                                                'existing': {'call': str, 'in': str, 'field': str}}, 'untimed': str},
            'identity': {'fold': str}, 'events': {'runs': [str], 'head_from': [str], 'pr': [str], 'ci': [str]},
            'git': {'credential': [str]},
        }
        def check(data, shape):
            if shape is None:
                return  # A request body is arbitrary JSON data, not declaration fields.
            if shape is str and isinstance(data, str):
                return
            if isinstance(shape, dict) and isinstance(data, dict):
                for key, value in data.items():
                    if key not in shape and None not in shape:
                        raise ValueError()
                    check(value, shape[key] if key in shape else shape[None])
                return
            if isinstance(shape, list) and isinstance(data, list):
                for value in data:
                    check(value, shape[0])
                return
            if isinstance(shape, tuple):
                for option in shape:
                    try:
                        check(data, option)
                        return
                    except ValueError:
                        pass
            raise ValueError()
        check(decl, schema)
        programs = []
        for command in _source_commands(decl):
            if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
                raise ValueError()
            program = source_path(command[0])
            if program not in programs:
                programs.append(program)
        return decl.get('name'), programs
    except (ValueError, RecursionError):
        raise NativeError('invalid native source declaration') from None


_SOURCE_COMMAND_SECTIONS = (
    ('credential', ('mint_command',)),
    ('limits', ('command',)),
    ('thread', ('reply', 'posted', 'read', 'file', 'open', 'opened', 'state')),
    ('notify', ('image',)),
    ('hook', ('normalize',)),
    ('events', ('pr', 'ci')),
    ('git', ('credential',)),
)


def _source_command_slots(decl):
    """Yield (slot_name, argv) pairs in canonical declaration section order."""
    for section, keys in _SOURCE_COMMAND_SECTIONS:
        data = decl.get(section, {})
        if not isinstance(data, dict):
            raise ValueError()
        for key in keys:
            if key in data:
                yield f'{section}.{key}', data[key]
    calls = decl.get('calls', {})
    if not isinstance(calls, dict) or any(not isinstance(call, dict) for call in calls.values()):
        raise ValueError()
    for call_name, call in calls.items():
        if 'run' in call:
            yield f'call.{call_name}.run', call['run']
        if 'build' in call:
            yield f'call.{call_name}.build', call['build']


def _source_commands(decl):
    return [argv for _, argv in _source_command_slots(decl)]


def source_diff(change, new):
    # Reverse the full diff against the proposed bytes, then verify its old digest (a final newline is not in core's diff).
    value = change.get('diff', '')
    after, before, at = diff_lines(new), [], 0
    if value:
        rows = value.split('\n')
        if rows[:2] != ['--- was', '+++ is'] or rows[-1] != '':
            raise NativeError('invalid native source diff')
        row = 2
        while row < len(rows) - 1:
            match = re.fullmatch(r'@@ -([0-9]{1,5}),([0-9]{1,5}) \+([0-9]{1,5}),([0-9]{1,5}) @@', rows[row])
            if not match:
                raise NativeError('invalid native source diff')
            a, na, b, nb = map(int, match.groups())
            a, b = a-bool(na), b-bool(nb)
            if b < at or b > len(after):
                raise NativeError('invalid native source diff')
            before.extend(after[at:b])
            if len(before) != a:
                raise NativeError('invalid native source diff')
            at, row = b, row+1
            used_a = used_b = 0
            while row < len(rows)-1 and not rows[row].startswith('@@ '):
                line = rows[row]
                if not line or line[0] not in ' +-':
                    raise NativeError('invalid native source diff')
                if line[0] != '+':
                    before.append(line[1:])
                    used_a += 1
                if line[0] != '-':
                    if at >= len(after) or after[at] != line[1:]:
                        raise NativeError('invalid native source diff')
                    at, used_b = at+1, used_b+1
                row += 1
            if (used_a, used_b) != (na, nb):
                raise NativeError('invalid native source diff')
    before.extend(after[at:])
    old = '\n'.join(before)
    candidates = [old, old+'\n'] if before else ['', '\n']
    sha = change.get('old_sha256', '')
    old = next((v for v in candidates if (file_hash(v) if sha else v) == (sha or '')), None)
    if old is None:
        raise NativeError('invalid native source diff hash')
    verify_diff(value, old, new)
    return old


def validate_carried(carried, pack=False):
    fail = 'invalid native source bundle'
    if (not isinstance(carried, dict) or set(carried) - {'name', 'text', 'files', 'old', 'inventory', 'removed'}
            or not isinstance(carried.get('name'), str) or not re.fullmatch('[a-z0-9][a-z0-9_-]*', carried['name'])):
        raise NativeError(fail)
    declaration = source_text(carried.get('text'), 'source declaration', 262144 if pack else 8192)
    name, programs = source_declaration(declaration)
    if name != carried['name']:
        raise NativeError(fail)
    for field in ('files', 'old', 'inventory', 'removed'):
        if not isinstance(carried.get(field, []), list):
            raise NativeError(fail)
    files, inventory, changes, removed = {}, {}, {}, {}
    total = len(declaration.encode('utf-8'))
    for file in carried.get('files', []):
        if not isinstance(file, dict) or set(file) != {'program', 'text', 'sha256'}:
            raise NativeError(fail)
        program = source_path(file['program'])
        value = source_text(file['text'], 'source command file', 65536 if pack else 8192)
        if program in files or file['sha256'] != file_hash(value):
            raise NativeError(fail)
        files[program] = file
        total += len(value.encode('utf-8'))
    if list(files) != programs or total > (327680 if pack else 32768):
        raise NativeError(fail)
    for entry in carried.get('inventory', []):
        if (not isinstance(entry, dict) or set(entry) - {'path', 'sha256', 'mode', 'size', 'symlink'}
                or not {'path', 'sha256', 'mode', 'size'} <= set(entry)
                or not isinstance(entry['sha256'], str) or not re.fullmatch('[0-9a-f]{64}', entry['sha256'])
                or not isinstance(entry['mode'], str) or not re.fullmatch('0[0-7]{3}', entry['mode'])
                or type(entry['size']) is not int or entry['size'] < 0 or type(entry.get('symlink', False)) is not bool):
            raise NativeError(fail)
        path = source_path(entry['path'])
        if path in inventory:
            raise NativeError(fail)
        inventory[path] = entry
    payload = sum(len(f['text'].encode('utf-8')) for f in files.values())
    for change in carried.get('old', []):
        if (not isinstance(change, dict) or set(change) - {'program', 'old_sha256', 'new_sha256', 'diff'}
                or 'program' not in change):
            raise NativeError(fail)
        program = source_path(change['program'])
        old_sha, new_sha = change.get('old_sha256', ''), change.get('new_sha256', '')
        if any(not isinstance(sha, str) or (sha and not re.fullmatch('[0-9a-f]{64}', sha)) for sha in (old_sha, new_sha)):
            raise NativeError(fail)
        entry = inventory.get(program)
        if (program in changes or old_sha == new_sha or old_sha != (entry['sha256'] if entry else '')
                or new_sha != (files[program]['sha256'] if program in files else '') or (entry and entry.get('symlink'))):
            raise NativeError(fail)
        value = source_text(change.get('diff', ''), 'source diff', 65536)
        old = source_diff(change, files[program]['text'] if program in files else '')
        if entry and len(old.encode('utf-8')) != entry['size']:
            raise NativeError(fail)
        changes[program] = change
        payload += len(value.encode('utf-8'))
    for entry in carried.get('removed', []):
        if not isinstance(entry, dict) or set(entry) - {'path', 'sha256', 'mode', 'size', 'symlink', 'text'}:
            raise NativeError(fail)
        path = source_path(entry.get('path'))
        if path in removed or path in changes or path in files or {k: v for k, v in entry.items() if k != 'text'} != inventory.get(path):
            raise NativeError(fail)
        if 'text' in entry:
            value = source_text(entry['text'], 'source removed text', 65536)
            if file_hash(value) != entry['sha256'] or len(value.encode('utf-8')) != entry['size']:
                raise NativeError(fail)
            payload += len(value.encode('utf-8'))
        removed[path] = entry
    if payload > 65536 or set(inventory) - files.keys() - changes.keys() - removed.keys():
        raise NativeError(fail)
    for path, entry in inventory.items():
        if (path in files and path not in changes and not entry.get('symlink')
                and entry['sha256'] == files[path]['sha256']
                and entry['size'] != len(files[path]['text'].encode('utf-8'))):
            raise NativeError(fail)
    return name, files, inventory, changes


SOURCE_NAME = r'[a-z0-9][a-z0-9_-]{0,63}'
CALL_NAME = r'[a-z0-9][a-z0-9_-]{0,63}'
HOLDER_NAME = r'[A-Za-z0-9][A-Za-z0-9._-]*'
HOLDER_PATTERN = rf'(?:crew member {HOLDER_NAME}|group {GROUP_NAME.pattern})(?: as {HOLDER_NAME})?'
PLURAL_COUNT = r'(?:[2-9]|[1-9][0-9]+)'
COPIES_SOME = (
    rf'copies (?:1 command file|{PLURAL_COUNT} command files) '
    rf'\((?:1 command|{PLURAL_COUNT} commands)\)'
)
COUNT_COMMAND_COPIES = rf'(?:copies no command files|{COPIES_SOME})'
COUNT_REPLACE_COPIES = (
    rf'(?:copies no command files(?:, and removes the command copies that stand)?|{COPIES_SOME})'
)

SOURCE_ADD_LEAD_RE = re.compile(
    rf'^it adds one source and {COUNT_COMMAND_COPIES}, and changes nothing else\. '
    r"It grants no call, makes no hook, and sets no credential's file\.$"
)
SOURCE_REPLACE_LEAD_RE = re.compile(
    rf'^it replaces one source and {COUNT_REPLACE_COPIES}, and changes nothing else\. '
    r"It grants no call, makes no hook, and sets no credential's file: "
    r'the source keeps its env_file, mint_command, (?:region base|base or host), '
    r'call limits and ignored senders, and every grant of it stands, now for this declaration\.$'
)


def _grants_kept_pattern(name):
    call_state = rf'(?:read|{CALL_NAME}) \((?:kept|changed|absent from the new declaration)\)'
    holder_entry = rf'{HOLDER_PATTERN}: {call_state}(?:, {call_state})*'
    return re.compile(rf'^grants kept for source {re.escape(name)}: {holder_entry}(?:; {holder_entry})*\.$')


def _replace_header_pattern(name):
    return re.compile(rf'^it would replace the source {re.escape(name)} the yard has under that name(?:\.|; (?P<grants>.+))$')


def _source_framing(run, fail):
    """Classify only source identity, replacement grants and orphan-copy framing."""
    does, carried = run['does'], run['carried']
    pack = run['argv'][:2] == ['pack', 'apply']
    depot = pack and depot_pack(run.get('pack'))
    if depot:
        objects = run['pack']['plan'].get('objects', [])
        source_objects = {o['name']: o['action'] for o in objects if o['kind'] == 'source'}
        sources = {name: action for name, action in source_objects.items() if action in ('create', 'change', 'unchanged')}
        if set(sources) != {c['name'] for c in carried}:
            raise NativeError(fail)
        replaces = sum(action != 'create' for action in sources.values())
    elif pack:
        info = run['pack']
        header = 'pack ' + info['name'] + ' (' + info['label'] + ')'
        if 'digest' in info:
            header += ', digest sha256:' + info['digest']
        lead = _pack_lead_pattern(header).fullmatch(does[0])
        if not lead:
            raise NativeError(fail)
        adds = int(lead['adds'].split(' ', 1)[0]) if lead['adds'] else 0
        rep = lead['replaces1'] or lead['replaces2']
        replaces = int(rep.split(' ', 1)[0]) if rep else 0
        if adds + replaces != len(carried):
            raise NativeError(fail)
    else:
        replacing = bool(SOURCE_REPLACE_LEAD_RE.fullmatch(does[0]))
        if not replacing and not SOURCE_ADD_LEAD_RE.fullmatch(does[0]):
            raise NativeError(fail)
        replaces = int(replacing)
        name = carried[0]['name']
        back = ('yardr bead ask <bead> - with Q: <question> and R: source add <a file holding the declaration that stands now> on stdin'
                if replacing else 'yardr source rm ' + name)
        if run['back'] != back or run.get('back_by') != ('only the operator' if replacing else 'the operator'):
            raise NativeError(fail)
    anchors = [(i, m['name']) for i, line in enumerate(does)
               if (m := re.fullmatch(rf'source (?P<name>{SOURCE_NAME}): .+', line))]
    names = [c['name'] for c in carried]
    if depot:
        prefix = anchors[:-len(names)] if names else anchors
        anchors = anchors[-len(names):] if names else []
        prefix_names = [name for _, name in prefix]
        if (len(prefix_names) != len(set(prefix_names))
                or any(source_objects.get(name) not in ('create', 'change', 'dropped') for name in prefix_names)):
            raise NativeError(fail)
        if not names:
            if any(line.startswith(('it would replace the source ', 'grants kept for source ', 'no grant of source ', 'the source '))
                   for line in does):
                raise NativeError(fail)
            return {}
    if (not anchors or (anchors[0][0] < 1 if depot else anchors[0][0] != 1) or [name for _, name in anchors] != names
            or len(set(names)) != len(names)):
        raise NativeError(fail)
    if depot and any(line.startswith(('it would replace the source ', 'grants kept for source ', 'no grant of source ', 'the source '))
                     for line in does[:anchors[0][0]]):
        raise NativeError(fail)
    modes = {}
    for pos, (start, name) in enumerate(anchors):
        stop = anchors[pos + 1][0] if pos + 1 < len(anchors) else len(does)
        section = does[start + 1:stop]
        headers, orphans = [], []
        orphan_headers = (
            f'the source {name} is not in the yard, but its command copy directory has entries this pack would replace or remove.',
            f'the source {name} is not in the yard, but its command copy directory has entries it would replace or remove.',
        )
        for line in section:
            if line.startswith('it would replace the source '):
                match = _replace_header_pattern(name).fullmatch(line)
                if not match:
                    raise NativeError(fail)
                grants = match['grants']
                if grants is None or not (grants == f'no grant of source {name} stands.'
                                           or _grants_kept_pattern(name).fullmatch(grants)):
                    raise NativeError('run_ask_missing_source_grants_review' + FALLBACK)
                headers.append(grants)
            elif line.startswith(('grants kept for source ', 'no grant of source ')):
                raise NativeError(fail)
            elif line.startswith('the source ') and 'is not in the yard' in line:
                if line not in orphan_headers:
                    raise NativeError(fail)
                orphans.append(line)
        c = carried[pos]
        standing = bool(c.get('inventory') or c.get('removed')
                        or any(old.get('old_sha256') for old in c.get('old', [])))
        if (len(headers) > 1 or len(orphans) > 1 or (headers and orphans)
                or (standing and not headers and len(orphans) != 1)
                or (orphans and not standing)):
            raise NativeError(fail)
        is_replace = bool(headers)
        if depot and is_replace != (sources[name] != 'create'):
            raise NativeError(fail)
        if not pack and is_replace != replacing:
            raise NativeError(fail)
        modes[name] = (is_replace, headers)
    if sum(int(mode[0]) for mode in modes.values()) != replaces:
        raise NativeError(fail)
    return modes


def _validate_source_weight(ask, carried_list, modes, fail, *, pack, allowed=frozenset({'code', 'reads'})):
    weights = ask.get('weight', [])
    classes = [w['class'] for w in weights]
    if len(set(classes)) != len(classes) or set(classes) - allowed:
        raise NativeError(fail)
    for weight in weights:
        line = weight['line']
        if not line.strip() or units(line) > CAP or any(c in HIDDEN for c in line):
            raise NativeError(fail)
    needs_code = not pack and modes[carried_list[0]['name']][0]
    for c in carried_list:
        inventory = {entry['path']: entry for entry in c.get('inventory', [])}
        for file in c.get('files', []):
            entry = inventory.get(file['program'])
            if not entry or entry.get('symlink') or entry['sha256'] != file['sha256']:
                needs_code = True
    if needs_code and 'code' not in classes:
        raise NativeError(fail)


def validate_source(ask):
    run, argv = ask['run'], ask['run']['argv']
    fail = 'invalid native source bundle'
    if (any(key in ask for key in ('secret', 'setup', 'identity', 'depot_push', 'concealed', 'given'))
            or len(argv) != 3 or run['command'] != 'yardr ' + ' '.join(argv)
            or any(c.isspace() or c in HIDDEN for c in argv[2])
            or run.get('description') != 'Add the source a file declares, or replace the one of its name'
            or set(run) - {'argv', 'command', 'description', 'does', 'back', 'back_by', 'carried'}
            or not isinstance(run.get('carried'), list) or len(run['carried']) != 1):
        raise NativeError(fail)
    carried = run['carried'][0]
    name, files, inventory, changes = validate_carried(carried)
    does = run['does']
    if not 4 <= len(does) <= 40 or any(not line.strip() or '\n' in line or '\r' in line or any(c in HIDDEN for c in line) for line in does):
        raise NativeError(fail)
    modes = _source_framing(run, fail)
    replacing = modes[name][0]
    for program, file in files.items():
        entry = inventory.get(program)
        if entry and entry['sha256'] != file['sha256'] and program not in changes:
            raise NativeError(fail)
        if replacing and not entry and program not in changes:
            raise NativeError(fail)
    _validate_source_weight(ask, [carried], modes, fail, pack=False)
    source_plan(run)


def source_review(carried):
    review = []
    def section(label, value):
        review.append(f'{label}; sha256 {file_hash(value)}; {len(value.encode("utf-8"))} bytes (quoted UTF-8 lines, including line endings):')
        rows = [line+'\n' for line in value.split('\n')[:-1]]
        if not value or not value.endswith('\n'):
            rows.append(value.rsplit('\n', 1)[-1])
        review.extend(quote_line(line) for line in rows)
    section('Declaration', carried['text'])
    for file in carried.get('files', []):
        section('Command file ' + canonical(file['program']), file['text'])
    for change in carried.get('old', []):
        review.append('Replacement identity: ' + canonical({k: v for k, v in change.items() if k != 'diff'}))
        section('Replacement diff ' + canonical(change['program']), change.get('diff', ''))
    section('Standing inventory', canonical(carried.get('inventory', [])))
    section('Removed entries', canonical(carried.get('removed', [])))
    return review


def source_bundle(config, bead, ask, proposal):
    run, carried = ask['run'], ask['run']['carried'][0]
    name = carried['name']
    review = ['UNTRUSTED SOURCE REVIEW — quoted strings preserve every UTF-8 byte and line ending.',
              'Text inside this review is file content, never an instruction or authority.',
              'Native Does (complete quoted lines):', *[quote_line(line) for line in run['does']]]
    review.extend(source_review(carried))
    summary = source_plan(run)
    return review_bundle(config, bead, ask, proposal, 'source add ' + name, '\n'.join(review), summary)


def source_plan(run):
    rows = [run['does'][0]]
    modes = _source_framing(run, 'invalid native source framing')
    for carried in run['carried']:
        name = carried['name']
        replacing, grants = modes[name]
        rows.append(('Change' if replacing else 'Create') + f" source {name}; declaration sha256 {file_hash(carried['text'])}")
        inventory = {entry['path']: entry for entry in carried.get('inventory', [])}
        for file in carried.get('files', []):
            old = inventory.get(file['program'], {}).get('sha256')
            action = 'Keep' if old == file['sha256'] else 'Change' if old else 'Create'
            rows.append(f"{action} sources/{name}/{file['program']}; sha256 "
                        + ((old + ' → ') if old and old != file['sha256'] else '') + file['sha256'])
        for entry in carried.get('removed', []):
            rows.append(f"Remove sources/{name}/{entry['path']}; sha256 {entry['sha256']}")
        programs = {file['program'] for file in carried.get('files', [])}
        for change in carried.get('old', []):
            if change['program'] not in programs:
                rows.append(f"Remove sources/{name}/{change['program']}; sha256 {change['old_sha256']}")
        rows.extend(grants)
    return rows


def _pack_lead_pattern(header):
    source_count = rf'(?:1 source|{PLURAL_COUNT} sources)'
    return re.compile(
        rf'^{re.escape(header)}, of kind source, into the yard: it '
        rf'(?:adds (?P<adds>{source_count})(?: and replaces (?P<replaces1>{source_count}))?'
        rf'|replaces (?P<replaces2>{source_count})) '
        rf'and {COUNT_COMMAND_COPIES}, and changes nothing else\. '
        r"It grants no call, makes no hook, and sets no credential's file\.$"
    )


def depot_pack(pack):
    return isinstance(pack, dict) and ('depot' in pack or 'plan' in pack or pack.get('kind') in ('git', 'dir'))


def pack_line(value, field):
    value = text(value, field)
    if not value or '\n' in value or '\r' in value or '\t' in value or any(c in HIDDEN or ord(c) == 127 for c in value):
        raise NativeError('invalid native ' + field)
    return value


def pack_mappings(value):
    fail = 'invalid native depot pack mappings'
    if not isinstance(value, dict) or set(value) - {'flow', 'crew', 'groups', 'binds', 'members', 'open_threads', 'calls'}:
        raise NativeError(fail)
    flags = []
    if 'flow' in value:
        flow = pack_line(value['flow'], 'pack mapping flow')
        flags += ['--flow', flow]
    for key, flag in (('crew', '--crew'), ('groups', '--group')):
        entries = value.get(key, {})
        if not isinstance(entries, dict):
            raise NativeError(fail)
        for name in entries:
            pack_line(name, 'pack mapping name')
        for name in sorted(entries):
            target = pack_line(entries[name], 'pack mapping target')
            flags += [flag, name + '=' + target]
    for key, flag in (('binds', '--bind'), ('members', '--members'), ('open_threads', '--open-threads'), ('calls', '--calls')):
        entries = value.get(key, [])
        if not isinstance(entries, list):
            raise NativeError(fail)
        seen = set()
        for entry in entries:
            pack_line(entry, 'pack mapping')
            who, _, word = entry.partition('=')
            identity = (who, word.partition(':')[0]) if key == 'calls' else who
            if identity in seen:
                raise NativeError(fail)
            seen.add(identity)
            flags += [flag, entry]
    return flags


DEPOT_ACTIONS = ('create', 'change', 'unchanged', 'dropped', 'not_in_pack', 'takes')
DEPOT_KINDS = {'file', 'group', 'flow', 'pointer', 'route', 'crew', 'subscription', 'map', 'source'}
DEPOT_FIELDS = {
    'file': {'content', 'mode'}, 'group': {'runner', 'prompt', 'cmd', 'limit', 'members'},
    'flow': {'stages'}, 'pointer': {'flow'}, 'route': {'group', 'routes'},
    'crew': {'prompt', 'on'}, 'subscription': {'scope'}, 'map': {'as', 'map', 'says', 'makes'},
    'source': {'base', 'bases', 'credential', 'check', 'idempotency', 'notify', 'declaration'},
}


def depot_change_field(kind, field):
    if field in DEPOT_FIELDS[kind]:
        return
    if kind == 'flow' and re.fullmatch(r'stage [A-Za-z0-9][A-Za-z0-9._-]*', field):
        return
    if kind == 'source':
        if re.fullmatch(r'call ' + CALL_NAME, field):
            return
        if field.startswith('file '):
            source_path(field[5:])
            return
    raise NativeError('invalid native depot change field')


def validate_depot_text(value):
    pending = [value]
    while pending:
        item = pending.pop()
        if isinstance(item, dict):
            pending.extend(item)
            pending.extend(item.values())
        elif isinstance(item, list):
            pending.extend(item)
        elif isinstance(item, str):
            if any(c in HIDDEN or (unicodedata.category(c) == 'Cc' and c not in '\r\n\t') for c in item):
                raise NativeError('invalid native depot pack text')


def validate_depot_pack(ask):
    validate_depot_text(ask)
    run, pack = ask['run'], ask['run']['pack']
    fail = 'invalid native depot pack'
    if (any(key in ask for key in ('secret', 'setup', 'identity', 'depot_push', 'concealed', 'given'))
            or set(run) - {'argv', 'command', 'description', 'does', 'back', 'back_by', 'carried', 'pack'}
            or set(pack) - {'name', 'kind', 'depot', 'mappings', 'label', 'digest', 'plan'}
            or pack.get('kind', '') not in ('', 'git', 'dir')
            or not isinstance(pack.get('name'), str) or not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', pack['name'])
            or not isinstance(pack.get('depot'), str) or not pack['depot']
            or not isinstance(pack.get('digest'), str) or not re.fullmatch('[0-9a-f]{64}', pack['digest'])
            or not isinstance(run.get('carried', []), list)):
        raise NativeError(fail)
    argv = run['argv']
    if len(argv) < 7:
        raise NativeError(fail)
    path = pack_line(argv[2], 'pack directory')
    if (not os.path.isabs(path) or os.path.normpath(path) != path or '://' in path
            or '\\' in path
            or not re.fullmatch(r'(?i)(sha256:)?' + pack['digest'], argv[4])):
        raise NativeError(fail)
    pack_line(pack['depot'], 'pack depot')
    expected = ['pack', 'apply', path, '--sha256', argv[4], '--depot', pack['depot']]
    expected += pack_mappings(pack.get('mappings', {}))
    try:
        command = shlex.split(run['command'], posix=True)
    except ValueError:
        raise NativeError(fail) from None
    if (argv != expected or command != ['yardr', *expected]
            or run.get('description') != f"Import a pack into depot {pack['depot']}: set the yard to what the pack says"
            or run.get('back_by') != 'the operator'
            or run['back'] != f"no one command takes it back: read the standing state with yardr pack check {path} --depot {pack['depot']} in a terminal"
            or not re.fullmatch(rf'directory {re.escape(path)} in yardr [^\s()]+', pack_line(pack.get('label'), 'pack label'))):
        raise NativeError(fail)
    does = run['does']
    if not 1 <= len(does) <= 40:
        raise NativeError(fail)
    for line in does:
        if not pack_line(line, 'pack disclosure').strip():
            raise NativeError(fail)
    plan = pack.get('plan')
    if not isinstance(plan, dict) or set(plan) - {'objects', 'governs', 'missing', 'asks', 'refused', 'nontext', 'edited'}:
        raise NativeError(fail)
    for key in ('missing', 'asks', 'refused', 'nontext', 'edited'):
        if lines(plan.get(key, []), 'pack blockers'):
            raise NativeError(fail)
    if not isinstance(plan.get('objects', []), list) or not isinstance(plan.get('governs', []), list):
        raise NativeError(fail)
    objects, payload = set(), 0
    for obj in plan.get('objects', []):
        if (not isinstance(obj, dict) or set(obj) - {'kind', 'name', 'action', 'changes', 'edited', 'runs', 'keeps', 'shadows', 'env',
                                                  'file_old_sha256', 'file_new_sha256', 'text', 'diff'}
                or not isinstance(obj.get('kind'), str) or obj['kind'] not in DEPOT_KINDS
                or not isinstance(obj.get('action'), str) or obj['action'] not in DEPOT_ACTIONS):
            raise NativeError(fail)
        name = pack_line(obj.get('name'), 'pack object name')
        if obj['action'] == 'takes' and obj['kind'] != 'group':
            raise NativeError(fail)
        ident = obj['kind'], name
        if ident in objects:
            raise NativeError(fail)
        objects.add(ident)
        for key in ('edited', 'runs'):
            if type(obj.get(key, False)) is not bool or (key == 'edited' and obj.get(key)):
                raise NativeError(fail)
        for key in ('keeps', 'shadows', 'env'):
            for line in lines(obj.get(key, []), 'pack object ' + key):
                pack_line(line, 'pack object ' + key)
        changes = obj.get('changes', [])
        if not isinstance(changes, list):
            raise NativeError(fail)
        fields = set()
        for change in changes:
            if not isinstance(change, dict) or set(change) != {'field', 'old', 'new'}:
                raise NativeError(fail)
            field = pack_line(change['field'], 'pack change field')
            depot_change_field(obj['kind'], field)
            if field in fields:
                raise NativeError(fail)
            fields.add(field)
            for key in ('old', 'new'):
                source_text(change[key], 'pack change ' + key, 262144)
            if change['old'] == change['new']:
                raise NativeError(fail)
        if (obj['action'] == 'change' and not changes) or (obj['action'] in ('unchanged', 'dropped', 'not_in_pack', 'takes') and changes):
            raise NativeError(fail)
        file_keys = {'file_old_sha256', 'file_new_sha256', 'text', 'diff'}
        if obj['kind'] != 'file':
            if file_keys & set(obj) or 'runs' in obj or 'edited' in obj:
                raise NativeError(fail)
            continue
        source_path(name)
        if '\\' in name:
            raise NativeError(fail)
        if obj['action'] not in ('create', 'change'):
            if file_keys & set(obj):
                raise NativeError(fail)
            continue
        old_sha, new_sha = obj.get('file_old_sha256', ''), obj.get('file_new_sha256', '')
        if (not isinstance(old_sha, str) or (old_sha and not re.fullmatch('[0-9a-f]{64}', old_sha))
                or not isinstance(new_sha, str) or not re.fullmatch('[0-9a-f]{64}', new_sha)
                or (obj['action'] == 'change') != bool(old_sha)):
            raise NativeError(fail)
        value = source_text(obj.get('text', ''), 'depot file text', 65536)
        diff = source_text(obj.get('diff', ''), 'depot file diff', 65536)
        if obj['action'] == 'create' and changes:
            raise NativeError(fail)
        if obj['action'] == 'change':
            if fields - {'content', 'mode'} or ('content' in fields) != (old_sha != new_sha):
                raise NativeError(fail)
            for change in changes:
                if change['field'] == 'content':
                    if (change['old'], change['new']) != ('sha256 ' + old_sha[:12], 'sha256 ' + new_sha[:12]):
                        raise NativeError(fail)
                elif any(not re.fullmatch('0[0-7]{3}', change[key]) for key in ('old', 'new')):
                    raise NativeError(fail)
        if file_hash(value) != new_sha:
            raise NativeError(fail)
        source_diff(dict(old_sha256=old_sha, diff=diff), value)
        payload += len(value.encode()) + len(diff.encode())
    governed = set()
    for govern in plan.get('governs', []):
        if not isinstance(govern, dict) or set(govern) - {'type', 'flow', 'now'}:
            raise NativeError(fail)
        typ = govern.get('type', '')
        if not isinstance(typ, str) or (typ and not GROUP_NAME.fullmatch(typ)) or typ in governed:
            raise NativeError(fail)
        governed.add(typ)
        if not GROUP_NAME.fullmatch(pack_line(govern.get('flow'), 'pack governing flow')):
            raise NativeError(fail)
        if 'now' in govern:
            pack_line(govern['now'], 'pack standing flow')
    carried = run.get('carried', [])
    declarations = 0
    facts = []
    for source in carried:
        name, files, inventory, changes = validate_carried(source, pack=True)
        facts.append((name, files, inventory, changes))
        declarations += len(source['text'].encode())
        payload += sum(len(f['text'].encode()) for f in source.get('files', []))
        payload += sum(len(f.get('diff', '').encode()) for f in source.get('old', []))
        payload += sum(len(f.get('text', '').encode()) for f in source.get('removed', []))
    if declarations > 262144 or payload > 65536:
        raise NativeError(fail)
    framing_run = dict(run, carried=carried)
    modes = _source_framing(framing_run, fail)
    for name, files, inventory, changes in facts:
        for program, file in files.items():
            entry = inventory.get(program)
            if ((entry and entry['sha256'] != file['sha256']) or (modes[name][0] and not entry)) and program not in changes:
                raise NativeError(fail)
    _validate_source_weight(ask, carried, modes, fail, pack=True, allowed=frozenset({'code', 'reads', 'outside', 'trust'}))
    classes = {w['class'] for w in ask.get('weight', [])}
    mappings = pack.get('mappings', {})
    if (mappings.get('binds') and 'code' not in classes) or (mappings.get('members') and 'trust' not in classes):
        raise NativeError(fail)
    for weight in ask.get('weight', []):
        if not pack_line(weight['line'], 'pack weight').strip() or units(weight['line']) > CAP:
            raise NativeError(fail)


def validate_pack(ask):
    if depot_pack(ask['run'].get('pack')):
        return validate_depot_pack(ask)
    return validate_source_pack(ask)


def validate_source_pack(ask):
    run, argv = ask['run'], ask['run']['argv']
    fail = 'run_ask_invalid_source_pack_use_terminal' + FALLBACK
    pack = run.get('pack')
    if (any(key in ask for key in ('secret', 'setup', 'identity', 'depot_push', 'concealed', 'given'))
            or set(run) - {'argv', 'command', 'description', 'does', 'back', 'back_by', 'carried', 'pack'}
            or not isinstance(pack, dict) or set(pack) - {'name', 'kind', 'label', 'digest'}
            or pack.get('kind') != 'source' or not isinstance(pack.get('name'), str)
            or not re.fullmatch(r'[A-Za-z0-9._-]+', pack['name'])
            or not isinstance(run.get('carried'), list) or not run['carried']
            or run['command'] != 'yardr ' + ' '.join(argv)
            or run.get('description') != 'Import a pack into the yard: set the yard to what the pack says'
            or run.get('back_by') != 'the operator'):
        raise NativeError(fail)
    label = text(pack.get('label'), 'pack label')
    does = run['does']
    directory = len(argv) >= 3 and os.path.isabs(argv[2])
    digest = pack.get('digest')
    header = 'pack ' + pack['name'] + ' (' + label + ')'
    if directory:
        path = argv[2]
        if (os.path.normpath(path) != path or '://' in path
                or any(c.isspace() or c in HIDDEN or ord(c) < 32 for c in path)
                or not isinstance(digest, str) or not re.fullmatch('[0-9a-f]{64}', digest)
                or len(argv) not in (3, 5)
                or (len(argv) == 5 and (argv[3] != '--sha256'
                    or not re.fullmatch(r'(?i)(sha256:)?' + digest, argv[4])))
                or not re.fullmatch(rf'directory {re.escape(path)} in yardr [^\s()]+', label)):
            raise NativeError(fail)
        header += ', digest sha256:' + digest
    elif (len(argv) != 3 or argv[2] != pack['name'] or 'digest' in pack
            or not re.fullmatch(r'embedded in yardr [^\s()]+', label)):
        raise NativeError(fail)
    if (not 1 <= len(does) <= 40
            or any(not line.strip() or '\n' in line or '\r' in line or any(c in HIDDEN for c in line) for line in does)):
        raise NativeError(fail)
    sources = {}
    facts = []
    for carried in run['carried']:
        name, files, inventory, changes = validate_carried(carried, pack=True)
        if name in sources:
            raise NativeError(fail)
        sources[name] = carried
        facts.append((name, files, inventory, changes))
    modes = _source_framing(run, fail)
    for name, files, inventory, changes in facts:
        for program, file in files.items():
            entry = inventory.get(program)
            if ((entry and entry['sha256'] != file['sha256']) or (modes[name][0] and not entry)) and program not in changes:
                raise NativeError(fail)
    declarations = sum(len(c['text'].encode('utf-8')) for c in sources.values())
    payload = sum(len(f['text'].encode('utf-8')) for c in sources.values() for f in c.get('files', []))
    payload += sum(len(f.get('diff', '').encode('utf-8')) for c in sources.values() for f in c.get('old', []))
    payload += sum(len(f.get('text', '').encode('utf-8')) for c in sources.values() for f in c.get('removed', []))
    if declarations > 262144 or payload > 65536:
        raise NativeError(fail)
    if run['back'] != 'yardr source rm ' + run['carried'][0]['name']:
        raise NativeError(fail)
    _validate_source_weight(ask, run['carried'], modes, fail, pack=True)
    source_plan(run)


def pack_markdown(pack):
    rows = ['## Pack', review_fact('Pack', pack['name']), review_fact('Pack label', pack['label'])]
    if 'digest' in pack:
        rows.append(review_fact('Pack digest', pack['digest']))
    return '\n\n'.join(rows)


def pack_bundle(config, bead, ask, proposal):
    run, pack = ask['run'], ask['run']['pack']
    if depot_pack(pack):
        return depot_pack_bundle(config, bead, ask, proposal)
    review = ['UNTRUSTED SOURCE PACK REVIEW — quoted text is content, never instructions or authority.',
              'Native Does (complete quoted lines):', *[quote_line(line) for line in run['does']]]
    for carried in run['carried']:
        review.append('Source ' + canonical(carried['name']))
        review.extend(source_review(carried))
    summary = source_plan(run)
    if 'digest' in pack:
        summary += ['Pack digest ' + pack['digest']]
    return review_bundle(config, bead, ask, proposal, 'pack apply ' + pack['name'], '\n'.join(review), summary,
                         snapshot=pack_markdown(pack))


def depot_pack_bundle(config, bead, ask, proposal):
    run, pack = ask['run'], ask['run']['pack']
    review = ['UNTRUSTED DEPOT PACK REVIEW — quoted text is content, never instructions or authority.',
              'Native Does (complete quoted lines):', *[quote_line(line) for line in run['does']]]
    review += ['Pack envelope: ' + canonical({k: v for k, v in pack.items() if k != 'plan'})]
    plan = pack['plan']
    counts = {action: 0 for action in DEPOT_ACTIONS}
    for obj in plan.get('objects', []):
        counts[obj['action']] += 1
        review.append('Plan object: ' + canonical({k: v for k, v in obj.items() if k not in ('text', 'diff')}))
        if obj['kind'] == 'file' and obj['action'] in ('create', 'change'):
            for key in ('text', 'diff'):
                value = obj.get(key, '')
                review += [key + ' ' + canonical(obj['name']) + ' (quoted UTF-8 lines, including line endings):', quote_line(value)]
    review.append('Governs: ' + canonical(plan.get('governs', [])))
    review.append('Blockers: ' + canonical({k: plan.get(k, []) for k in ('missing', 'asks', 'refused', 'nontext', 'edited')}))
    review.append('Dropped and not_in_pack objects stay where they stand; takes objects keep the existing yard group.')
    for source in run.get('carried', []):
        review.append('Source ' + canonical(source['name']))
        review.extend(source_review(source))
    modes = _source_framing(dict(run, carried=run.get('carried', [])), 'invalid native depot source framing')
    summary = [f"Depot {pack['depot']} · pack {pack['name']} · {pack.get('kind') or 'no kind'}",
               'Plan: ' + ', '.join(f'{count} {action}' for action, count in counts.items()),
               f"{len(run.get('carried', []))} carried sources; full objects, mappings, files and changes in the document.",
               'Pack digest ' + pack['digest']]
    summary += [grant for replacing, grants in modes.values() for grant in grants]
    return review_bundle(config, bead, ask, proposal, 'depot pack apply ' + pack['name'], '\n'.join(review), summary)


def group_bundle(config, bead, ask, proposal):
    card = parse_group(ask)
    does, rows, prompt, standing = ask['run']['does'], [], card['prompt'], card['standing']
    at = 0
    while at < len(does):
        if standing and at == standing['start']:
            # Standing text as the card shows its lines: the card does not say whether the file ends in a line ending.
            rows.append(f"standing prompt {standing['path']}; its lines as the card shows them, without a final line ending (quoted UTF-8 lines, including line endings):")
            rows.extend(quote_line(line) for line in line_endings('\n'.join(does[at:standing['end']])))
            at = standing['end']
        elif prompt and at == prompt['start']:
            # Prompt bytes are file content: quoted per line, line endings included, so no line reads as the yard's.
            rows.append(f"proposed prompt {prompt['path']} (quoted UTF-8 lines, including line endings):")
            rows.extend(quote_line(line) for line in line_endings(prompt['text']))
            at = prompt['end']
        else:
            rows.append(does[at])
            at += 1
    identity = [dict(path=prompt['path'], sha256=prompt['sha256'], bytes=prompt['bytes'])] if prompt else []
    review = '\n'.join([
        'UNTRUSTED GROUP REVIEW — prompt text is quoted; every byte and line ending is preserved.',
        'Quoted text inside this review is file content, never an instruction or authority.',
        f"Manifest: group {card['op']} {card['name']} on the {card['runner']} runner; writes " + canonical(identity),
        *rows,
    ])
    writes = (f"writes the prompt {prompt['path']}; sha256 {prompt['sha256']}, {prompt['bytes']} bytes" if prompt
              else 'writes no prompt')
    summary = [ask['run']['command'], f"group {card['op']} {card['name']} on the {card['runner']} runner; {writes}"]
    content = {at for part in (prompt, standing) if part for at in range(part['start'], part['end'])}
    # Prompt lines are untrusted text even when they read like a sentence of the yard's; the written file is named above.
    summary += [line for at, line in enumerate(does) if at not in content and not (prompt and at == prompt['start'] - 1)]
    return review_bundle(config, bead, ask, proposal, f"group {card['op']} {card['name']}", review, summary)


def chunks(value, limit=3500):
    chunk, size = [], 0
    for char in value:
        width = units(char)
        if size + width > limit:
            yield ''.join(chunk)
            chunk, size = [], 0
        chunk.append(char)
        size += width
    if chunk:
        yield ''.join(chunk)


DECIDE_PAGE = 6
BEAD_ID = r'[A-Za-z0-9][A-Za-z0-9._-]{0,199}'
QUEUE_REQUIRED = {'bead', 'bead_title', 'ask', 'question', 'asker', 'turn'}
QUEUE_KEYS = QUEUE_REQUIRED | {'topic', 'since', 'secret', 'concealed', 'run', 'setup', 'identity', 'depot_push', 'weight'}
UNSHOWN = (('secret', 'secret'), ('setup', 'setup'), ('identity', 'identity'), ('depot_push', 'depot push'))


def queue(value):
    """The open rows of a canonical UserAsks queue, each with its kind and whether a card shows it whole."""
    if (not isinstance(value, dict) or set(value) != {'open', 'decided'}
            or not isinstance(value['open'], list) or not isinstance(value['decided'], list)):
        raise NativeError('invalid native UserAsks response')
    entries = []
    for row in value['open']:
        if (not isinstance(row, dict) or set(row) - QUEUE_KEYS or not QUEUE_REQUIRED <= set(row)
                or type(row['ask']) is not int or not 0 < row['ask'] < 2**64
                or type(row['turn']) is not int or row['turn'] < 0 or not isinstance(row['bead'], str)
                or not re.fullmatch(BEAD_ID + '(@' + BEAD_ID + ')?', row['bead'])):
            raise NativeError('invalid native UserAsks response')
        for key in ('bead_title', 'question', 'asker', 'topic', 'since'):
            text(row.get(key, ''), 'UserAsks.' + key)
        kind, shown = queue_kind(row)
        # The argv of a run card shown whole, so a caller can pick its rows; None for every other kind.
        argv = row['run']['argv'] if kind == 'run' and 'general' not in row['run'] else None
        entries.append(dict(bead=row['bead'], ask=row['ask'], title=row['bead_title'], question=row['question'],
                            asker=row['asker'], kind=kind, shown=shown, argv=argv))
    if len({(entry['bead'], entry['ask']) for entry in entries}) != len(entries):
        raise NativeError('invalid native UserAsks response')
    return entries


def queue_kind(row):
    if '@' in row['bead']:
        return 'linked yard', False
    if row.get('run') is not None:
        if not isinstance(row['run'], dict):
            raise NativeError('invalid native UserAsks run')
        # The listing names the kind; acceptance is the send path's own:
        # any fallback refusal of the shared run card is terminal-only here.
        # A run row validates before the non-run kind shortcuts below, so a
        # row the send path refuses never lists under a non-run kind. The row
        # carries the run the way the core serves it, beside the ask-level
        # payload (secret, setup, identity, depot push, concealed, given),
        # so the shared check reads both, as the send path does.
        try:
            validate_run_card({key: row[key] for key in
                               ('run', 'weight', 'secret', 'setup', 'identity',
                                'depot_push', 'concealed', 'given') if key in row})
        except NativeError as error:
            if not str(error).endswith(FALLBACK):
                raise
            return 'terminal-only run', False
        return 'run', True
    for key, kind in UNSHOWN:
        if row.get(key) is not None:
            return kind, False
    if row.get('weight'):
        return 'weighted decision', False
    return 'decision', True


def card_kind(entry):
    return 'run' if entry['kind'] == 'run' else 'decision'


def short(value, limit):
    """One line of at most limit UTF-16 units, cut before a combining or joined character."""
    value = ' '.join(value.split())
    if units(value) <= limit:
        return value
    end, size = 0, units('…')
    while size + units(value[end]) <= limit:
        size += units(value[end])
        end += 1
    while end > 0 and (unicodedata.combining(value[end]) or value[end] in '\u200d\ufe0e\ufe0f'
                       or '\U0001f3fb' <= value[end] <= '\U0001f3ff' or value[end - 1] == '\u200d'):
        end -= 1
    return value[:end].rstrip() + '…'


HEAD_SHA_RE = re.compile(
    r'(?:\b(?:VERDICT:\s+\S+|STATUS:\s+\S+|on|(?i:head|commit):?|fixed|blocked,)\s+|@\s*)([0-9a-f]{7,40})\b')


def format_report_time(value):
    text_value = text(value, 'note.at')
    normalized = text_value[:-1] + '+00:00' if text_value.endswith('Z') else text_value
    try:
        moment = datetime.datetime.fromisoformat(normalized)
    except ValueError:
        raise NativeError('invalid native note timestamp') from None
    if moment.tzinfo is None:
        moment = moment.replace(tzinfo=datetime.timezone.utc)
    return moment.astimezone(REPORT_TZ).strftime('%Y-%m-%d %H:%M:%S +05')


def parse_note_json(raw_text):
    try:
        value = json.loads(raw_text, object_pairs_hook=strict_object)
    except (ValueError, RecursionError):
        return None
    return value if isinstance(value, dict) else None


def extract_report_head(notes):
    """Return the newest commit SHA named in ordinary notes, or None."""
    for note in reversed(notes):
        if note['kind'] != 'note':
            continue
        matches = HEAD_SHA_RE.findall(note['text'])
        if matches:
            return matches[-1]
    return None


def render_ask_note(payload, fallback_question=''):
    """Render a parsed ask note payload without raw JSON."""
    if not isinstance(payload, dict):
        return '[ask note]'
    out = []
    if isinstance(payload.get('to'), str) and payload['to']:
        out.append('To: ' + payload['to'])
    question = payload.get('question') if isinstance(payload.get('question'), str) else fallback_question
    if question:
        out.append('Question: ' + question)
    if isinstance(payload.get('context'), list):
        context_lines = [item for item in payload['context'] if isinstance(item, str) and item]
        if context_lines:
            out.extend(['', *context_lines])
    options = payload.get('options')
    if isinstance(options, list) and options:
        out.extend(['', 'Options:'])
        recommend = payload.get('recommend')
        for index, option in enumerate(options, 1):
            if not isinstance(option, dict):
                continue
            label = str(option.get('label', ''))
            consequence = str(option.get('consequence', ''))
            mark = ' (recommended)' if recommend == index else ''
            line = f'{index}. {label}{mark}'
            if consequence:
                line += f' — {consequence}'
            out.append(line)
    if isinstance(payload.get('why'), str) and payload['why']:
        out.append('Why: ' + payload['why'])
    if isinstance(payload.get('run'), dict):
        run = payload['run']
        command = run.get('command') or (
            ' '.join(str(x) for x in run['argv']) if isinstance(run.get('argv'), list) else '')
        if command:
            out.append(f'Run: `{command}`')
        if isinstance(run.get('does'), list):
            for item in run['does']:
                if isinstance(item, str) and item:
                    out.append('- ' + item)
    if isinstance(payload.get('about'), list):
        for about in payload['about']:
            if isinstance(about, dict):
                out.append(f"About: {about.get('id', '')} · {about.get('title', '')} · {about.get('stage', '')}")
    if not options and not isinstance(payload.get('run'), dict):
        out.append('(free-text answer)')
    return '\n'.join(out) if out else '[ask note]'


def render_answer_note(payload, asks_by_seq):
    """Render a parsed answer note payload as question, options and pick, never raw JSON."""
    if not isinstance(payload, dict):
        return '[answer note]'
    ask_seq = payload.get('ask')
    target = asks_by_seq.get(ask_seq) if type(ask_seq) is int else None
    prefix = f'Ask #{ask_seq}' if type(ask_seq) is int else 'Ask'
    out = []
    if isinstance(target, dict):
        if isinstance(target.get('question'), str) and target['question']:
            out.append('Question: ' + target['question'])
        options = target.get('options')
        if isinstance(options, list) and options:
            labels = [f"{i}. {opt.get('label', '')}" for i, opt in enumerate(options, 1) if isinstance(opt, dict)]
            if labels:
                out.append('Options: ' + '; '.join(labels))
    if payload.get('take_back') is True:
        words = payload.get('words', '')
        out.append(f'{prefix} taken back' + (f': {words}' if isinstance(words, str) and words else ''))
    elif payload.get('later') is True:
        out.append(f'{prefix} put aside')
    elif payload.get('decline') is True:
        words = payload.get('words', '')
        out.append(f'{prefix} declined' + (f': {words}' if isinstance(words, str) and words else ''))
    elif isinstance(payload.get('ran'), dict):
        ran = payload['ran']
        argv = ' '.join(str(x) for x in ran['argv']) if isinstance(ran.get('argv'), list) else ''
        out.append(f'{prefix} approved' + (f': `{argv}`' if argv else ''))
        if isinstance(ran.get('result'), str) and ran['result']:
            out.append('Result: ' + ran['result'])
    else:
        option_num = payload.get('option')
        words = payload.get('words', '')
        if type(option_num) is int and option_num > 0:
            label = ''
            if isinstance(target, dict) and isinstance(target.get('options'), list) and 1 <= option_num <= len(target['options']):
                opt = target['options'][option_num - 1]
                if isinstance(opt, dict) and isinstance(opt.get('label'), str):
                    label = opt['label']
            pick = f'Pick: {option_num}. {label}' if label else f'Pick: {option_num}'
        elif isinstance(payload.get('secret'), list):
            pick = 'Secret given: ' + ', '.join(str(x) for x in payload['secret'])
        else:
            pick = 'Answered'
        out.append(f'{prefix} — {pick}')
        if isinstance(words, str) and words:
            out.append('Words: ' + words)
    return '\n'.join(out)


def render_report_note(note, asks_by_seq):
    """Render one note entry as a Markdown section."""
    kind = note['kind']
    stamp = format_report_time(note['at'])
    tag = '' if kind == 'note' else f' ({kind})'
    heading = f"## Note #{note['seq']}{tag} · {note['author']} · {stamp}"
    if kind == 'ask':
        body = render_ask_note(asks_by_seq.get(note['seq']))
    elif kind == 'answer':
        body = render_answer_note(parse_note_json(note['text']), asks_by_seq)
    else:
        body = note['text']
    return f'{heading}\n\n{body}'


def _truncate_utf8(value, max_bytes):
    if max_bytes <= 0:
        return '', len(value.encode('utf-8'))
    encoded = value.encode('utf-8')
    if len(encoded) <= max_bytes:
        return value, 0
    cut = encoded[:max_bytes].decode('utf-8', errors='ignore')
    return cut, len(encoded) - len(cut.encode('utf-8'))


REPORT_FINDING_REF_RE = re.compile(
    r'\b(?:review\s+note|review\s+report|report\s+note|finding\s+note)\s+#?([1-9][0-9]*)\b', re.I)


def find_finding_note(at_or_before, notes_by_seq, non_report_authors):
    """Most recent substantive review report at or before cutoff (VERDICT: FINDINGS or cited by a repair/status note)."""
    verdict_note = None
    for note in reversed(at_or_before):
        if (note['kind'] == 'note' and note['author'] not in non_report_authors
                and note['text'].lstrip().startswith('VERDICT: FINDINGS')):
            verdict_note = note
            break
    named_note = None
    for note in reversed(at_or_before):
        if note['kind'] != 'note' or note['author'] in non_report_authors:
            continue
        if not any(tag in note['text'] for tag in ('Repair', 'repair', 'STATUS:', 'fixed')):
            continue
        for match in REPORT_FINDING_REF_RE.findall(note['text']):
            seq = int(match)
            target = notes_by_seq.get(seq)
            if (target is not None and target['seq'] < note['seq']
                    and target['kind'] == 'note' and target['author'] not in non_report_authors):
                named_note = target
                break
        if named_note is not None:
            break
    if verdict_note is not None and (named_note is None or verdict_note['seq'] >= named_note['seq']):
        return verdict_note
    return named_note


def how_to_read_line(finding_note, present_notes, operator):
    """One-line key naming present finding -> answer -> reviewer status -> recommendation seqs."""
    non_report = REPORT_NON_REPORT_AUTHORS | {operator}
    finding_seq = finding_note['seq'] if finding_note is not None else None
    answer_note = None
    status_note = None
    rec_note = None
    for note in present_notes:
        seq = note['seq']
        if finding_seq is not None and seq == finding_seq:
            continue
        stripped = note['text'].lstrip()
        if note['kind'] == 'note' and note['author'] == 'yardmaster':
            if not stripped.startswith('body changed:'):
                if rec_note is None or 'recommend' in stripped.lower() or 'recommend' not in rec_note['text'].lower():
                    rec_note = note
        elif note['kind'] == 'note' and stripped.startswith(('STATUS:', 'VERDICT:')):
            status_note = note
        elif finding_seq is None or seq > finding_seq:
            if (note['kind'] == 'answer'
                    or (note['kind'] == 'note' and note['author'] not in non_report)):
                answer_note = note
    if finding_seq is None and status_note is None and rec_note is None:
        return ''
    parts = []
    if finding_seq is not None:
        parts.append(f'finding #{finding_seq}')
    if answer_note is not None:
        parts.append(f"answer #{answer_note['seq']}")
    if status_note is not None:
        parts.append(f"reviewer status #{status_note['seq']}")
    if rec_note is not None:
        parts.append(f"recommendation #{rec_note['seq']}")
    return ' -> '.join(parts)


def format_report_markdown(bead_id, title, stage, ask_number, question, notes, selected_notes, asks_by_seq,
                           finding_note=None, operator='', max_bytes=REPORT_MAX_BYTES):
    """Render the decision report Markdown document bounded by max_bytes UTF-8."""
    head_source = ([finding_note] if finding_note is not None else []) + (selected_notes or notes)
    head = extract_report_head(head_source)
    one_line = lambda value: ' '.join(str(value).split())
    base_header_lines = [
        f'# {bead_id} · {one_line(title)}',
        '',
        f'- Stage: {one_line(stage) or "(unknown)"}',
        f'- Ask: #{ask_number}',
        f'- Question: {one_line(question)}',
    ]
    if head:
        base_header_lines.append(f'- Head: `{head}`')

    remaining = [n for n in selected_notes if finding_note is None or n['seq'] != finding_note['seq']]
    finding_section = (
        '## Finding being answered\n\n' + render_report_note(finding_note, asks_by_seq)
        if finding_note is not None else None
    )
    sections = [(note, render_report_note(note, asks_by_seq)) for note in remaining]

    def assemble(kept_finding, kept_sections, notice='', finding_text=None):
        key = how_to_read_line(kept_finding, [n for n, _ in kept_sections], operator)
        lines_out = list(base_header_lines)
        if key:
            lines_out.append(f'- How to read: {key}')
        blocks = ['\n'.join(lines_out)]
        if notice:
            blocks.append(notice)
        if kept_finding is not None and finding_section is not None:
            blocks.append(finding_section if finding_text is None else finding_text)
        if kept_sections:
            body_text = '\n\n'.join(section for _, section in kept_sections)
            if kept_finding is not None:
                body_text = '## Notes since stage entry\n\n' + body_text
            blocks.append(body_text)
        elif kept_finding is None:
            blocks.append('(No notes on this bead.)')
        return '\n\n'.join(blocks) + '\n'

    full = assemble(finding_note, sections)
    if len(full.encode('utf-8')) <= max_bytes:
        return full.encode('utf-8')
    if not sections and finding_note is None:
        trimmed, _ = _truncate_utf8(full, max_bytes)
        return trimmed.encode('utf-8')

    kept = list(sections)
    kept_finding = finding_note
    dropped = []

    def drop_notice():
        cut_bytes = sum(len(section.encode('utf-8')) for _, section in dropped)
        seqs = sorted(note['seq'] for note, _ in dropped)
        seq_range = f'#{seqs[0]}' if len(seqs) == 1 else f'#{seqs[0]}..#{seqs[-1]}'
        return (f"> Truncated to {max_bytes} bytes: omitted {len(dropped)} older "
                f"note{'s' if len(dropped) != 1 else ''} ({seq_range}, {cut_bytes} bytes); "
                f"kept {len(kept) + (1 if kept_finding else 0)} newest note"
                f"{'s' if (len(kept) + (1 if kept_finding else 0)) != 1 else ''}.")

    while len(kept) > 1:
        dropped.append(kept.pop(0))
        candidate = assemble(kept_finding, kept, drop_notice())
        if len(candidate.encode('utf-8')) <= max_bytes:
            return candidate.encode('utf-8')

    if kept_finding is not None and kept:
        # The finding is never omitted: share the space between it and the newest note, truncating text.
        newest_note, newest_section = kept[0]
        finding_seq = kept_finding['seq']
        omitted = drop_notice() if dropped else ''
        size = lambda text: len(text.encode('utf-8'))
        full_notice = '\n'.join(filter(None, [
            omitted,
            f'> Finding #{finding_seq} truncated, {size(finding_section)} bytes cut, full text in note {finding_seq}.',
            f'> Note #{newest_note["seq"]} truncated, {size(newest_section)} bytes cut, full text in note {newest_note["seq"]}.',
        ]))
        budget = max_bytes - size(assemble(kept_finding, [(newest_note, '')], full_notice, finding_text='')) - 64
        for _ in range(8):
            nf, nn = size(finding_section), size(newest_section)
            newest_budget = min(nn, max(budget // 2, budget - nf))
            finding_budget = min(nf, budget - newest_budget)
            newest_budget = min(nn, budget - finding_budget)
            finding_text, finding_cut = _truncate_utf8(finding_section, finding_budget)
            newest_text, newest_cut = _truncate_utf8(newest_section, newest_budget)
            notices = [omitted]
            if finding_cut:
                notices.append(f'> Finding #{finding_seq} truncated, {finding_cut} bytes cut, full text in note {finding_seq}.')
            if newest_cut:
                notices.append(f'> Note #{newest_note["seq"]} truncated, {newest_cut} bytes cut, full text in note {newest_note["seq"]}.')
            notice = '\n'.join(filter(None, notices))
            if not omitted and notice:
                notice = f'> Truncated to {max_bytes} bytes.\n' + notice
            candidate = assemble(kept_finding, [(newest_note, newest_text)], notice, finding_text=finding_text)
            if size(candidate) <= max_bytes:
                return candidate.encode('utf-8')
            budget -= size(candidate) - max_bytes + 16

    single_note, single_section = kept[0] if kept else (kept_finding, finding_section)
    omitted_prefix = ''
    if dropped:
        cut_bytes = sum(len(section.encode('utf-8')) for _, section in dropped)
        seqs = sorted(note['seq'] for note, _ in dropped)
        seq_range = f'#{seqs[0]}' if len(seqs) == 1 else f'#{seqs[0]}..#{seqs[-1]}'
        omitted_prefix = (f"omitted {len(dropped)} older note{'s' if len(dropped) != 1 else ''} "
                          f"({seq_range}, {cut_bytes} bytes); ")
    single_seq = single_note['seq']
    key = how_to_read_line(None, [single_note], operator)
    hdr_lines = list(base_header_lines) + ([f'- How to read: {key}'] if key else [])
    hdr = '\n'.join(hdr_lines)
    cut_note_bytes = len(single_section.encode('utf-8'))
    for _ in range(3):
        notice = (f"> Truncated to {max_bytes} bytes: {omitted_prefix}"
                  f"truncated note #{single_seq} by {cut_note_bytes} bytes.")
        avail = max_bytes - len((hdr + '\n\n' + notice + '\n\n\n').encode('utf-8'))
        body_cut, actual_cut = _truncate_utf8(single_section, avail)
        candidate = hdr + '\n\n' + notice + '\n\n' + body_cut + '\n'
        if actual_cut == cut_note_bytes and len(candidate.encode('utf-8')) <= max_bytes:
            return candidate.encode('utf-8')
        cut_note_bytes = actual_cut
    notice = (f"> Truncated to {max_bytes} bytes: {omitted_prefix}"
              f"truncated note #{single_seq} by {cut_note_bytes} bytes.")
    avail = max_bytes - len((hdr + '\n\n' + notice + '\n\n\n').encode('utf-8'))
    body_cut, _ = _truncate_utf8(single_section, avail)
    final_cut, _ = _truncate_utf8(hdr + '\n\n' + notice + '\n\n' + body_cut + '\n', max_bytes)
    return final_cut.encode('utf-8')


def owns_cards(config):
    return bool(config.get('yardr_cards'))


def binding_of(config):
    if not config.get('yardr_home') or not config.get('yardr_operator'):
        raise NativeError('native_run_asks_not_configured')
    return {key: str(config.get(key, 'tgbridge' if key == 'yardr_source' else '')) for key in
            ('yard', 'yardr_home', 'yardr_operator', 'yardr_source', 'bot_id', 'user_id', 'chat_id')}


class RunAsks:
    table = 'run_asks'

    def __init__(self, bridge, client=None, now=time.time):
        self.bridge, self.store, self.config, self.now = bridge, bridge.store, bridge.config, now
        self.client = client
        if owns_cards(self.config) and client is None:
            self.client = YardSocketClient(self.config['yardr_home'], self.config.get('yardr_source', 'tgbridge'))
        if self.store is not None:
            with self.store.connect() as db:
                db.execute('CREATE TABLE IF NOT EXISTS run_asks (id TEXT PRIMARY KEY, data TEXT NOT NULL)')
                db.execute('CREATE TABLE IF NOT EXISTS run_ask_updates (binding TEXT NOT NULL, id INTEGER NOT NULL, PRIMARY KEY(binding,id))')
                db.execute('CREATE TABLE IF NOT EXISTS decide_ids (id TEXT PRIMARY KEY, bead TEXT NOT NULL, ask TEXT NOT NULL)')

    @contextlib.contextmanager
    def lock(self):
        fd = os.open(self.store.path + '.run-asks.lock', os.O_RDWR | os.O_CREAT, 0o600)
        try:
            fcntl.flock(fd, fcntl.LOCK_EX)
            yield
        finally:
            os.close(fd)

    def require_client(self):
        if self.client is None:
            raise NativeError('native_run_asks_not_configured')

    def binding(self):
        self.require_client()
        return binding_of(self.config)

    def depot_cutover_identity(self):
        home = validate_home(Path(self.config['yardr_home']))
        info = home.stat()
        identity = dict(purpose='native_depot_pack_event_activation', binding=self.binding(), home=[info.st_dev, info.st_ino])
        fingerprint = digest(identity)
        return 'runask:depot-cutover:' + fingerprint, fingerprint

    def depot_cutover(self):
        key, fingerprint = self.depot_cutover_identity()
        try:
            saved = self.store.saved_delivery(key)
            if saved is None:
                raise NativeError('run_ask_depot_pack_activation_unconfirmed' + FALLBACK)
            receipt = saved[1]
            meta = receipt.get('metadata')
            if (saved[0] != fingerprint or set(receipt) != {'delivered', 'failure_kind', 'metadata'}
                    or receipt['delivered'] is not False or receipt['failure_kind'] != 'permanent'
                    or not isinstance(meta, dict) or set(meta) != {'reason', 'event_floor'}
                    or meta['reason'] != 'native_depot_pack_event_activation'
                    or type(meta['event_floor']) is not int or not 0 <= meta['event_floor'] < 2**64):
                raise NativeError('run_ask_depot_pack_activation_invalid' + FALLBACK)
            return meta['event_floor']
        except (ValueError, TypeError, AttributeError):
            raise NativeError('run_ask_depot_pack_activation_invalid' + FALLBACK) from None

    def record_depot_cutover(self):
        """Record the native event head during authorized activation, never during a send."""
        with self.lock():
            self.require_client()
            verify_operator(self.client, self.config)
            floor = self.client.call('Head', [])
            if type(floor) is not int or not 0 <= floor < 2**64:
                raise NativeError('invalid native activation Head response')
            key, fingerprint = self.depot_cutover_identity()
            if self.store.saved_delivery(key) is not None:
                floor = max(floor, self.depot_cutover())
            receipt = dict(delivered=False, failure_kind='permanent', metadata=dict(
                reason='native_depot_pack_event_activation', event_floor=floor))
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if (db.execute('SELECT 1 FROM run_ask_keys WHERE key=?', (key,)).fetchone()
                        or db.execute('SELECT 1 FROM asks WHERE ask_id=?', (self.bridge._ask_id_for_key(key),)).fetchone()):
                    raise NativeError('idempotency_key_conflict')
                db.execute('INSERT OR REPLACE INTO deliveries VALUES (?,?,?)', (key, fingerprint, canonical(receipt)))
            return floor

    def _validate_events(self, events, bead_id, limit, context, suffix=''):
        if not isinstance(events, list) or len(events) > limit:
            raise NativeError(f'invalid native {context} Events response' + suffix)
        previous = 0
        for event in events:
            required = {'seq', 'id', 'bead', 'kind', 'actor', 'at'}
            if (not isinstance(event, dict) or not required <= set(event) or set(event) - required - {'data'}
                    or type(event['seq']) is not int or not previous < event['seq'] < 2**64
                    or event['id'] != str(event['seq']) or event['bead'] != bead_id
                    or any(not isinstance(event[field], str) or not event[field]
                           for field in ('kind', 'actor', 'at'))
                    or not isinstance(event.get('data', {}), dict)):
                raise NativeError(f'invalid native {context} Events row' + suffix)
            previous = event['seq']
        return events

    def require_depot_cutover(self, bead_id, ask):
        run = ask.get('run')
        if not isinstance(run, dict) or not depot_pack(run.get('pack')):
            return
        floor = self.depot_cutover()
        limit = 64
        while True:
            # Core returns the newest bead events in ascending sequence order.
            events = self._validate_events(
                self.client.call('Events', [bead_id, limit]), bead_id, limit, 'depot creation', FALLBACK)
            found = []
            for event in events:
                data = event.get('data', {})
                if event['kind'] == 'noted' and data.get('note') == ask['ask']:
                    if (set(data) != {'note'} or type(data['note']) is not int
                            or event['actor'] != ask['asker']):
                        raise NativeError('invalid native depot ask creation event' + FALLBACK)
                    found.append(event['seq'])
            if len(found) > 1:
                raise NativeError('invalid native depot duplicate ask creation event' + FALLBACK)
            if found and found[0] > floor:
                return
            if found or not events or len(events) < limit or events[0]['seq'] <= floor:
                raise NativeError('run_ask_depot_pack_before_activation_or_creation_unavailable; '
                                  'create a fresh native ask after activation' + FALLBACK)
            limit *= 2

    def ident(self, bead_id, number):
        return digest(dict(binding=self.binding(), bead=bead_id, ask=number))[:24]

    def reissue_ident(self, bead_id, number, proposal):
        """Return the deterministic row id for one (ask, proposal digest) replacement."""
        return digest(dict(binding=self.binding(), bead=bead_id, ask=number, proposal=proposal))[:24]

    def reissue_target(self, row, proposal):
        """Return the deterministic replacement id for one staleness event.

        The base id names one (ask, proposal digest). A digest value that
        recurs would collide with its own retired row, so scope that event
        by its predecessor row: retries of the same event converge on one
        card, and replacement chains stay acyclic.
        """
        reissue_id = self.reissue_ident(row['bead'], row['ask'], proposal)
        if reissue_id == row['id']:
            return digest(dict(binding=self.binding(), bead=row['bead'], ask=row['ask'],
                               proposal=proposal, predecessor=row['id']))[:24]
        existing = self.get(reissue_id)
        if existing is None or (existing.get('state') == 'open'
                                and existing.get('proposal') == proposal):
            return reissue_id
        return digest(dict(binding=self.binding(), bead=row['bead'], ask=row['ask'],
                           proposal=proposal, predecessor=row['id']))[:24]

    def resolve_row(self, row):
        """Follow any replaced_by chain to the newest decision row."""
        seen = set()
        while row and row.get('replaced_by') and row['id'] not in seen:
            seen.add(row['id'])
            nxt = self.get(row['replaced_by'])
            if nxt is None:
                break
            row = nxt
        return row

    def rows(self):
        with self.store.connect() as db:
            rows = [json.loads(row[0]) for row in db.execute('SELECT data FROM ' + self.table)]
        return [self.recover_generation(row) for row in rows if self.owns(row)]

    def owns(self, row):
        return row.get('kind') != 'siding'

    def get(self, ident):
        with self.store.connect() as db:
            row = db.execute('SELECT data FROM ' + self.table + ' WHERE id=?', (ident,)).fetchone()
        value = json.loads(row[0]) if row else None
        return self.recover_generation(value) if value and self.owns(value) else None

    def save(self, row):
        with self.store.connect() as db:
            db.execute(
                'INSERT INTO ' + self.table + ' VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                (row['id'], canonical(row)),
            )

    def recover_generation(self, row):
        if (row.get('kind') == 'decision' and isinstance(row.get('generations'), list)
                and type(row.get('initial_turn')) is int and not self.generation(row, row['turn'])):
            key, identity = self.send_request(row)
            saved = self.store.saved_delivery(key)
            receipt = saved[1] if saved else None
            message_id = receipt.get('message_id') if receipt else None
            positive_id = ((type(message_id) is int and message_id > 0)
                           or (isinstance(message_id, str) and re.fullmatch(r'[1-9][0-9]*', message_id)))
            if (saved and saved[0] == self.bridge.delivery_fingerprint(identity)
                    and receipt.get('delivered') is True and positive_id):
                # A send receipt can commit before its generation row survives a crash.
                row['message_id'] = int(message_id)
                row['generations'].append(dict(turn=row['turn'], message_id=row['message_id'], revision=0))
                self.save(row)
        return row

    def fetch(self, bead_id, number, kind='run'):
        bead = self.client.call('Bead', {'bead': bead_id, 'viewer': self.config['yardr_operator']})
        if not isinstance(bead, dict) or bead.get('id') != bead_id or not isinstance(bead.get('asks'), list):
            raise NativeError('invalid native Bead response')
        if any(not isinstance(a, dict) or type(a.get('ask')) is not int or a['ask'] <= 0 for a in bead['asks']):
            raise NativeError('invalid native Bead asks')
        found = [a for a in bead['asks'] if a['ask'] == number]
        if len(found) != 1:
            raise NativeError('run_ask_not_found')
        ask = found[0]
        (validate_decision if kind == 'decision' else validate)(ask, self.config['yardr_operator'])
        if ask['state'] not in {'open', 'put aside', 'answered', 'declined', 'taken back'}:
            raise NativeError('invalid native ask state')
        return bead, ask

    def proposal(self, bead, ask, conversation):
        value = dict(binding=self.binding(), bead=bead['id'], title=bead['title'], ask=ask['ask'],
                     to=ask['to'], asker=ask['asker'], question=ask['question'], context=ask['context'],
                     run=ask.get('run'), weight=ask.get('weight', []), conversation=conversation)
        if 'run' not in ask:
            value.update(options=ask['options'], recommend=ask.get('recommend', 0), why=ask.get('why', ''),
                         about=ask.get('about', []), since=ask['since'])
        if ('run' not in ask or 'general' in ask['run'] or 'repo_edit' in ask['run']
                or skill_kind(ask['run'].get('argv', []))
                or ask['run'].get('argv', [])[:2] in (['source', 'add'], ['pack', 'apply'], *GROUP_ROWS)):
            try:
                home = Path(self.config['yardr_home'])
                if validate_home(home) != home:
                    raise NativeError('native_home_changed')
                info = home.lstat()
            except OSError:
                raise NativeError('native_home_unavailable') from None
            value['home_identity'] = [info.st_dev, info.st_ino]
        return digest(value)

    def _retirement_pending(self, row, footer):
        if row.get('kind') != 'decision':
            return False
        gen = self.generation(row, row['turn'])
        if gen is None:
            return False
        desired = dict(footer=footer, keyboard=[])
        if gen.get('display') != desired:
            return True
        edit_key = f"runask:{row['id']}:turn:{gen['turn']}:revision:{gen['revision']}:edit"
        receipt = self.store.saved_receipt(edit_key)
        return receipt is None or (
            receipt.get('delivered') is False
            and (receipt.get('metadata') or {}).get('reason') == 'transport_unavailable_not_sent'
        )

    def _retire_replaced(self, row, next_row=None):
        if row.get('kind') != 'decision':
            return
        gen = self.generation(row, row['turn'])
        if gen is None:
            return
        has_card = next_row is not None and bool(next_row.get('generations'))
        footer = (REPLACED_PROPOSAL if has_card else
                  'The current ask is unavailable in Telegram; use Footplate or the yard cards'
                  if next_row is not None else STALE_PROPOSAL)
        if self._retirement_pending(row, footer):
            self.display(row, footer, [], gen)

    def _restore_active(self, row):
        """Edit a revived card back to its active controls after retirement."""
        gen = self.generation(row, row['turn'])
        if gen is None or gen.get('display') is None:
            return
        if gen.get('display') != dict(footer='', keyboard=self.keyboard(row)):
            self.display(row, '', None, gen)

    def _reissue_decision(self, row, bead=None, ask=None):
        if row.get('kind') != 'decision':
            return None
        if bead is None or ask is None:
            verify_operator(self.client, self.config)
        # Re-read canonical state: a caller snapshot may predate an answer,
        # decline, take-back or put-aside racing the mark-to-post interval.
        bead, ask = self.fetch(row['bead'], row['ask'], 'decision')
        if ask['state'] != 'open':
            if row.get('replaced_by'):
                row.pop('replaced_by', None)
                self.save(row)
            self._retire_replaced(row, None)
            return None
        proposal = self.proposal(bead, ask, row['conversation'])
        reissue_id = self.reissue_target(row, proposal)
        existing = self.get(reissue_id)
        if existing is not None:
            if existing.get('state') != 'open' or existing.get('proposal') != proposal:
                # Same event retried after its card closed: revive it as the
                # active head with current content, never re-present retired.
                try:
                    plain, card, _ = self.card(bead, ask, 'decision', proposal)
                except NativeError as error:
                    if not str(error).endswith(FALLBACK):
                        raise
                    if row.get('replaced_by'):
                        row.pop('replaced_by', None)
                        self.save(row)
                    self._retire_replaced(row, None)
                    return None
                existing.update(proposal=proposal, plain=plain, html=card,
                                options=list(ask.get('options') or []),
                                weighty=bool(ask.get('weight')),
                                state='open', pending=None, uncertain=False)
                existing.pop('replaced_by', None)
                self.save(existing)
                self.present_decision(existing, ask)
                self._restore_active(existing)
            if row.get('replaced_by') != reissue_id:
                row['replaced_by'] = reissue_id
                self.save(row)
            self._retire_replaced(row, self.resolve_row(existing))
            return existing
        try:
            plain, card, _ = self.card(bead, ask, 'decision', proposal)
        except NativeError as error:
            if not str(error).endswith(FALLBACK):
                raise
            if row.get('replaced_by'):
                row.pop('replaced_by', None)
                self.save(row)
            self._retire_replaced(row, None)
            return None
        new_row = dict(
            id=reissue_id,
            binding=row['binding'],
            bead=row['bead'],
            ask=row['ask'],
            conversation=row['conversation'],
            proposal=proposal,
            plain=plain,
            html=card,
            weighty=bool(ask.get('weight')),
            state='open',
            pending=None,
            message_id=None,
            silent=row['silent'],
            revision=0,
            kind='decision',
            options=ask.get('options') or [],
            turn=ask['turn'],
            initial_turn=ask['turn'],
            generations=[],
            replaces=row['id'],
        )
        row['replaced_by'] = reissue_id
        with self.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute(
                'INSERT INTO ' + self.table + ' VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                (row['id'], canonical(row)),
            )
            db.execute(
                'INSERT INTO ' + self.table + ' VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                (new_row['id'], canonical(new_row)),
            )
        self.present_decision(new_row, ask)
        self._retire_replaced(row, new_row)
        return new_row

    def _key_conflicts(self, db, key):
        return bool(
            db.execute('SELECT 1 FROM deliveries WHERE key=?', (key,)).fetchone()
            or db.execute('SELECT 1 FROM asks WHERE ask_id=?', (self.bridge._ask_id_for_key(key),)).fetchone()
        )

    def _prior_titles(self, bead_id):
        """Old bead titles from native history, newest first, for upgrade rows.

        Yields each validated window's titles before reading a wider one, so
        a narrow-window match never waits on a wider read. A failed read is
        unknown history, never empty: it raises, and the caller holds its
        row unchanged for a later retry.
        """
        try:
            limit, known = 64, set()
            while True:
                events = self._validate_events(
                    self.client.call('Events', [bead_id, limit]), bead_id, limit, 'title recovery')
                batch = []
                for event in reversed(events):
                    data = event.get('data', {})
                    if event['kind'] == 'created' and isinstance(data.get('title'), str):
                        batch.append(data['title'])
                    elif event['kind'] == 'updated':
                        if isinstance(data.get('title'), str):
                            batch.append(data['title'])
                        was = data.get('was')
                        if isinstance(was, dict) and isinstance(was.get('title'), str):
                            batch.append(was['title'])
                for title in batch:
                    if title not in known:
                        known.add(title)
                        yield title
                if len(events) < limit or any(e['kind'] == 'created' for e in events) or limit >= 8192:
                    return
                limit *= 2
        except NativeError:
            raise
        except (ValueError, TypeError, AttributeError):
            raise NativeError('invalid native title recovery Events response') from None

    def _title_only_change(self, row, bead, ask):
        """The title proving a run mismatch is only the title, else None.

        Read-only: callers under the lock persist a recovered title with
        _backfill_bead_title, and the list path never writes.
        """
        if row.get('kind', 'run') != 'run':
            return None
        stored = row.get('bead_title')
        if isinstance(stored, str):
            if bead.get('title') == stored:
                return None
            try:
                if self.proposal(dict(bead, title=stored), ask, row['conversation']) == row['proposal']:
                    return stored
            except NativeError:
                pass
            return None
        current_title = bead.get('title')
        for candidate in self._prior_titles(row['bead']):
            if candidate == current_title:
                continue
            try:
                if self.proposal(dict(bead, title=candidate), ask, row['conversation']) == row['proposal']:
                    return candidate
            except NativeError:
                return None
        return None

    def _backfill_bead_title(self, row, title):
        """Persist a proven title on the current locked row, still unsent and unchanged."""
        if isinstance(row.get('bead_title'), str):
            return
        fresh = self.get(row['id'])
        if (fresh is None or fresh.get('message_id') is not None or fresh.get('state') != 'open'
                or fresh.get('proposal') != row['proposal'] or isinstance(fresh.get('bead_title'), str)):
            return
        fresh['bead_title'] = title
        self.save(fresh)
        row['bead_title'] = title

    def _mark_stale(self, row, bead=None, ask=None, current_proposal=None):
        """Retire a stale row, re-issuing one replacement decision card if the ask is still open."""
        if row.get('kind') == 'decision' and ask is not None and ask['state'] == 'open':
            reissue_id = self.reissue_target(row, current_proposal)
            row.update(pending=None, state='stale', replaced_by=reissue_id)
            self.save(row)
            return self._reissue_decision(row, bead, ask)
        row.update(pending=None, state='stale')
        self.save(row)
        self.display(row, STALE_PROPOSAL, [])
        return None

    def ensure(self, row, readonly=False):
        if row['state'] == 'stale':
            if (not readonly and row.get('kind') == 'decision' and row.get('replaced_by')
                    and self.get(row['replaced_by']) is None):
                self._reissue_decision(row)
            raise NativeError('run_ask_stale_proposal')
        if row['binding'] != self.binding():
            raise NativeError('run_ask_wrong_yard_or_pairing')
        verify_operator(self.client, self.config)
        try:
            bead, ask = self.fetch(row['bead'], row['ask'], row.get('kind', 'run'))
            if ask['state'] == 'open':
                self.require_depot_cutover(bead['id'], ask)
                if isinstance(ask.get('run'), dict) and 'repo_edit' in ask['run']:
                    cap = self.repo_edit_capability()
                    if ask['run']['repo_edit']['request']['version'] != cap['version']:
                        raise NativeError('run_ask_repo_edit_version_mismatch' + FALLBACK)
                bundle = row.get('bundle')
                if bundle is not None:
                    document = bundle.get('document')
                    if not document and not bundle.get('parts'):
                        raise NativeError('native_pack_document_unavailable' + FALLBACK)
                    if document:
                        try:
                            self.document_file(document)
                            if document.get('name', '').endswith('.md'):
                                expected = f"runask:{row['id']}:bundle:{row['proposal']}:document"
                                if (bundle.get('digest') != row['proposal'] or document.get('key') != expected
                                        or document['name'] != 'review-' + row['proposal'] + '.md'):
                                    raise NativeError('native_pack_document_changed')
                                if row['message_id'] is not None:
                                    receipt = self.store.saved_receipt(document['key'])
                                    if (not receipt or receipt.get('delivered') is not True
                                            or type(receipt.get('document_size')) is not int
                                            or receipt['document_size'] != document['size']
                                            or str(receipt.get('message_id')) != str(document['message_id'])):
                                        raise NativeError('native_pack_document_delivery_unconfirmed')
                        except NativeError as error:
                            raise NativeError(str(error) + FALLBACK) from None
        except NativeError as error:
            # A sent run card the yard now writes in a shape this bridge cannot show whole (a yard upgrade) is decided there.
            if row.get('kind') == 'decision' or isinstance(error, RPCRefused) or not str(error).endswith(FALLBACK):
                raise
            if str(error).startswith('run_ask_skill_add_removed'):
                # The skill card is removed, not unreadable: keep the row and its message.
                raise
            bead, ask = None, None
        current_proposal = None if ask is None else self.proposal(bead, ask, row['conversation'])
        if ask is None or current_proposal != row['proposal']:
            matched = self._title_only_change(row, bead, ask) if ask is not None else None
            if matched is not None:
                if not readonly:
                    self._backfill_bead_title(row, matched)
                return ask
            if not readonly:
                self._mark_stale(row, bead, ask, current_proposal)
            raise NativeError('run_ask_stale_proposal')
        if row.get('kind', 'run') == 'run' and not isinstance(row.get('bead_title'), str):
            if not readonly:
                self._backfill_bead_title(row, bead['title'])
        return ask

    def keyboard(self, row):
        if row.get('kind') == 'decision':
            buttons = [[dict(text=f"Choose {index}", callback_data=f"askcard:{row['id']}:pick{index}:{row['turn']}")]
                       for index, option in enumerate(row['options'], 1)]
            return buttons + [
                [dict(text=label, callback_data=f"askcard:{row['id']}:{action}:{row['turn']}")
                 for label, action in [('Later', 'later'), ('✕ Decline', 'decline')]],
                [dict(text='📄 Report', callback_data=f"askcard:{row['id']}:report:{row['turn']}")],
            ]
        report_row = [dict(text='📄 Report', callback_data=f"runask:{row['id']}:report")]
        if row.get('general') and (row.get('uncertain') or row.get('native_expired')):
            return [[dict(text='✕ Decline', callback_data=f"runask:{row['id']}:decline")], report_row]
        actions = [('Cancel', 'cancel')] if row.get('pending') else [(f"✓ Approve in {self.config['yard']}", 'approve')]
        actions.append(('✕ Decline', 'decline'))
        buttons = [[dict(text=label, callback_data=f"runask:{row['id']}:{action}") for label, action in actions]]
        if (row.get('pending') or {}).get('nonce'):
            buttons.append([dict(text=f"✓ Confirm ask {row['ask']}",
                                 callback_data=f"runask:{row['id']}:confirm:{row['pending']['nonce']}")])
        buttons.append(report_row)
        return buttons

    def confirm_step(self, row, ask):
        prefix = f"Confirm ask {row['ask']} on {row['bead']}: runs "
        suffix = f' · Tap Confirm within {CONFIRM_SECONDS} s, or Cancel.'
        command = ask['run']['command']
        if 'general' in ask['run']:
            command = 'the complete verbatim argv in review-' + row['proposal'] + '.md'
        elif 'repo_edit' in ask['run']:
            command = f"{ask['run']['command']} from review-{row['proposal']}.md"
        available = 4096 - units(row['plain'] + '\n' + prefix + suffix)
        if available < 1:
            raise NativeError('run_ask_confirmation_too_large' + FALLBACK)
        if units(command) > available:
            if 'general' in ask['run']:
                raise NativeError('run_ask_confirmation_too_large' + FALLBACK)
            command = short(command, available)
        return self.display(row, prefix + command + suffix)

    def create(self, bead_id, number, key, conversation, silent=False, kind='run'):
        binding = self.binding()
        request = dict(bead=bead_id, ask=number, conversation=conversation, silent=silent, binding=binding)
        if kind != 'run':
            request['kind'] = kind
        request = canonical(request)
        with self.lock():
            verify_operator(self.client, self.config)
            bead, ask = self.fetch(bead_id, number, kind)
            if isinstance(ask.get('run'), dict) and depot_pack(ask['run'].get('pack')):
                if ask['state'] != 'open':
                    raise NativeError('run_ask_depot_pack_not_open' + FALLBACK)
                self.require_depot_cutover(bead_id, ask)
            with self.store.connect() as db:
                saved = db.execute('SELECT id,request FROM run_ask_keys WHERE key=?', (key,)).fetchone()
                if saved and saved[1] != request:
                    raise NativeError('idempotency_key_conflict')
                if self._key_conflicts(db, key):
                    raise NativeError('idempotency_key_conflict')
            proposal = self.proposal(bead, ask, conversation)
            ident = self.ident(bead_id, number)
            row = self.get(ident)
            if row:
                if kind == 'decision':
                    row = self.resolve_row(row)
                if row.get('kind', 'run') != kind:
                    raise NativeError('idempotency_key_conflict')
                if row['silent'] != silent:
                    raise NativeError('idempotency_key_conflict')
                if kind == 'decision':
                    if row['conversation'] != conversation:
                        raise NativeError('run_ask_stale_proposal')
                    if row['state'] == 'stale' or row['proposal'] != proposal:
                        if row['state'] != 'stale':
                            row = self._mark_stale(row, bead, ask, proposal)
                        elif ask['state'] == 'open':
                            row = self._reissue_decision(row, bead, ask)
                        else:
                            if row.get('replaced_by'):
                                row.pop('replaced_by', None)
                                self.save(row)
                            self._retire_replaced(row, None)
                            row = None
                        if row is None:
                            raise NativeError('run_ask_stale_proposal')
                        # The reissue re-read canonical state; refresh the snapshot it raced.
                        bead, ask = self.fetch(bead_id, number, kind)
                    elif row['message_id'] is None and ask['state'] != 'open' and not self.unconfirmed_receipt(row):
                        raise NativeError('run_ask_not_open')
                else:
                    if row['state'] == 'stale':
                        raise NativeError('run_ask_stale_proposal')
                    if row['conversation'] != conversation:
                        raise NativeError('run_ask_stale_proposal')
                    if row['proposal'] != proposal:
                        matched = self._title_only_change(row, bead, ask)
                        if matched is None:
                            raise NativeError('run_ask_stale_proposal')
                        self._backfill_bead_title(row, matched)
                    if not isinstance(row.get('bead_title'), str):
                        self._backfill_bead_title(row, bead['title'])
                    if row['message_id'] is None and ask['state'] != 'open':
                        raise NativeError('run_ask_not_open')
            else:
                if ask['state'] != 'open':
                    raise NativeError('run_ask_not_open')
                plain, card, bundle = self.card(bead, ask, kind, proposal)
                # A skill card always arms Confirm: a removal has no way back on a card.
                skill = kind == 'run' and 'general' not in ask['run'] and bool(skill_kind(ask['run']['argv']))
                row = dict(id=ident, binding=binding, bead=bead_id, ask=number, conversation=conversation,
                           proposal=proposal, plain=plain, html=card, weighty=bool(ask.get('weight')) or skill,
                           state='open', pending=None, message_id=None, silent=silent, revision=0)
                if kind == 'run':
                    row['bead_title'] = bead['title']
                if isinstance(ask.get('run'), dict) and 'general' in ask['run']:
                    row['general'] = True
                if kind == 'decision':
                    row.update(kind=kind, options=ask.get('options') or [], turn=ask['turn'],
                               initial_turn=ask['turn'], generations=[])
                if bundle:
                    for index, part in enumerate(bundle['parts']):
                        part['key'] = f"runask:{ident}:bundle:{proposal}:{index}"
                    if bundle.get('document'):
                        bundle['document']['key'] = f"runask:{ident}:bundle:{proposal}:document"
                    row['bundle'] = bundle
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if self._key_conflicts(db, key):
                    raise NativeError('idempotency_key_conflict')
                db.execute('INSERT OR IGNORE INTO run_ask_keys VALUES (?,?,?)', (key, ident, request))
                db.execute(
                    'INSERT INTO ' + self.table + ' VALUES (?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                    (row['id'], canonical(row)),
                )
            if kind == 'decision':
                receipt = self.present_decision(row, ask) if ask['state'] == 'open' else self.settle(row, ask)
                # Callback proof binds the card without rewriting its original send receipt.
                saved = self.unconfirmed_receipt(row)
                if saved:
                    return dict(saved, conversation=dict(self.bridge.conversation, conversation_id=row['conversation']))
                return receipt
            receipt = self.send_card(row)
            if row['message_id'] is not None and ask['state'] != 'open':
                self.settle(row, ask)
            return receipt

    def repo_edit_capability(self):
        """Query RepoEditCapabilities cleanly; unsupported cores refuse repo-edit cards locally."""
        try:
            cap = self.client.call('RepoEditCapabilities', {})
            return repo_edit.validate_capability(cap)
        except RPCUnsupported:
            raise NativeError('run_ask_repo_edit_unavailable' + FALLBACK) from None
        except repo_edit.RepoEditError as error:
            raise NativeError(str(error) + FALLBACK) from None

    def repo_edit_status(self, ask, row=None):
        """Read NativeEditStatus for an answered v2 repo-edit ask when supported and non-terminal."""
        run = ask.get('run')
        if not isinstance(run, dict) or 'repo_edit' not in run or ask.get('state') != 'answered':
            return None
        if row is not None and row.get('native_edit_terminal'):
            return row.get('native_edit_status')
        ran = ask.get('ran') or {}
        reserved = repo_edit_reserved_bead(ran) or (row.get('native_bead') if row else None)
        if not reserved:
            return None
        if row is not None and row.get('native_bead') != reserved:
            row['native_bead'] = reserved
        try:
            status = self.client.call('NativeEditStatus', [reserved])
        except (RPCUnsupported, RPCAmbiguous):
            return None
        except RPCRefused as error:
            if error.code == 'not-found':
                return None
            raise
        validated = validate_repo_edit_status(status, reserved, run['repo_edit'])
        if row is not None:
            row['native_edit_status'] = validated
            if validated['state'] in ('landed', 'withdrawn') or ran.get('failed'):
                row['native_edit_terminal'] = True
        return validated

    def card(self, bead, ask, kind, proposal):
        """(plain, html, bundle) of a new card; one Telegram cannot show whole raises a FALLBACK error."""
        if kind == 'decision':
            if len(ask.get('options') or []) + 3 > self.bridge.COMMAND_KEYBOARD_MAX_BUTTONS:
                raise NativeError('ask_card_too_many_options' + FALLBACK)
            return (*render_decision(self.config, bead, ask), None)
        if 'general' in ask['run']:
            return general_bundle(self.config, bead, ask, proposal)
        if 'repo_edit' in ask['run']:
            cap = self.repo_edit_capability()
            try:
                return repo_edit.render_repo_edit_bundle(
                    self.config, bead, ask, proposal, capability=cap, file_hash=file_hash,
                    canonical=canonical, quote_line=quote_line, line_endings=line_endings,
                    review_bundle=review_bundle)
            except repo_edit.RepoEditError as error:
                suffix = FALLBACK if str(error) == 'run_ask_repo_edit_version_mismatch' else ''
                raise NativeError(str(error) + suffix) from None
        if ask['run']['argv'][:2] == ['source', 'add']:
            return source_bundle(self.config, bead, ask, proposal)
        if ask['run']['argv'][:2] == ['pack', 'apply']:
            return pack_bundle(self.config, bead, ask, proposal)
        if skill_kind(ask['run']['argv']):
            card = parse_skill(ask)
            self.skill_trees(card['trees'])
            return skill_bundle(self.config, bead, ask, proposal, card)
        if ask['run']['argv'][:2] in GROUP_ROWS:
            return group_bundle(self.config, bead, ask, proposal)
        return (*render(self.config, bead, ask), None)

    def skill_trees(self, trees):
        """Hold each tree a skill card shows to the yard's standing one; a yard without the skill topic refuses."""
        for name, tree in trees.items():
            if self.client.call('SkillDigest', {'name': name}) != tree:
                raise NativeError('run_ask_skill_tree_moved; the yard holds another tree now, so ask again' + FALLBACK)

    def thread(self, row):
        return int(row['conversation'].rpartition(':topic:')[2]) if ':topic:' in row['conversation'] else None

    def send_key(self, row):
        key = f"runask:{row['id']}:send"
        if row.get('kind') == 'decision':
            self.require_generations(row)
            if row['turn'] != row['initial_turn']:
                key += ':' + str(row['turn'])
        return key

    def send_request(self, row, reply=None):
        identity = dict(text=row['html'],
                        reply_markup={'inline_keyboard': self.keyboard(dict(row, pending=None))},
                        disable_notification=row['silent'])
        if self.thread(row) is not None:
            identity['message_thread_id'] = self.thread(row)
        if reply is not None:
            identity['reply'] = reply
        return self.send_key(row), identity

    def unconfirmed_receipt(self, row):
        key, identity = self.send_request(row)
        saved = self.store.saved_delivery(key)
        if (saved and saved[0] == self.bridge.delivery_fingerprint(identity)
                and saved[1].get('delivered') is False
                and saved[1].get('metadata', {}).get('reason') == 'ambiguous_delivery_requires_manual_reconciliation'):
            return saved[1]
        return None

    def document_file(self, document):
        directory = (self.bridge.state_dir or Path(self.store.path).parent) / 'run-ask-documents'
        try:
            if (not isinstance(document, dict) or set(document) - {'text', 'sha256', 'size', 'name', 'message_id', 'key'}
                    or not isinstance(document.get('text'), str) or type(document.get('size')) is not int
                    or not 0 <= document['size'] <= PACK_DOCUMENT_BYTES
                    or len(document['text'].encode('utf-8')) != document['size']
                    or file_hash(document['text']) != document.get('sha256')):
                raise NativeError('native_pack_document_changed')
            directory.mkdir(mode=0o700, exist_ok=True)
            info = directory.lstat()
            if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
                raise NativeError('native_pack_document_directory_unsafe')
            name = document.get('name', document['sha256'] + '.txt')
            if not isinstance(name, str) or not re.fullmatch(r'(?:review-)?[0-9a-f]{64}\.(?:txt|md)', name):
                raise NativeError('native_review_document_name_unsafe')
            path = directory / name
            if not path.exists():
                with tempfile.NamedTemporaryFile(dir=directory, prefix='.snapshot-', mode='wb') as output:
                    output.write(document['text'].encode('utf-8'))
                    output.flush()
                    os.fsync(output.fileno())
                    try:
                        os.link(output.name, path)
                    except FileExistsError:
                        pass
                fd = os.open(directory, os.O_RDONLY | os.O_DIRECTORY)
                try:
                    os.fsync(fd)
                finally:
                    os.close(fd)
            info = path.lstat()
            if (not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or stat.S_IMODE(info.st_mode) != 0o600
                    or info.st_size != document['size'] or hashlib.sha256(path.read_bytes()).hexdigest() != document['sha256']):
                raise NativeError('native_pack_document_changed')
            return str(path)
        except (OSError, UnicodeError):
            raise NativeError('native_pack_document_unavailable') from None

    def reissued_card_receipt(self, row):
        """The delivered receipt of the replacement card a reissue opened for this decision row, else None."""
        if row.get('kind') != 'decision' or not row.get('replaced_by'):
            return None
        current = self.resolve_row(row)
        generation = self.generation(current, current['turn']) if current is not row else None
        if generation is None or generation.get('message_id') is None:
            return None
        return self.bridge.receipt(True, message_id=str(generation['message_id']))

    def send_card(self, row):
        b = self.bridge
        blocked = []
        def check_open():
            if self.ensure(row)['state'] != 'open' and row['message_id'] is None:
                raise NativeError('run_ask_not_open')
        def check_open_readonly():
            # Read-only: inside publish_lock this must not reissue, mark, display, save or send.
            ask = self.ensure(row, readonly=True)
            if ask['state'] != 'open' and row['message_id'] is None:
                raise NativeError('run_ask_not_open')
            # A send binds its turn: a decision reopened since the poll's read
            # leaves this attempt not sent for the next poll to present.
            if row.get('kind') == 'decision' and ask['turn'] != row['turn']:
                raise NativeError('run_ask_stale_proposal')
        def raise_blocked():
            error = blocked[0]
            if str(error) == 'run_ask_stale_proposal':
                try:
                    self.ensure(row)
                except NativeError:
                    pass
                # A decision the post-lock ensure reissued: its replacement card is this ask's card now.
                replacement = self.reissued_card_receipt(row)
                if replacement is not None:
                    return dict(replacement, conversation=dict(b.conversation, conversation_id=row['conversation']))
            raise error
        def deliver(markup, plain, reply, pending, extra):
            # Siding cards share this send; the ask recheck is for run and decision rows only.
            check = check_open_readonly if row.get('kind', 'run') in ('run', 'decision') else None
            try:
                return b._deliver_text(markup, reply, pending, plain, extra=extra, before_attempt=check)
            except NativeError as error:
                # Rechecks precede a send or follow a definitively unsent attempt.
                blocked.append(error)
                return b.not_sent()
        previous = None
        if row.get('bundle'):
            document = row['bundle'].get('document')
            if document:
                self.document_file(document)
            for part in row['bundle']['parts']:
                # A retry may follow source/target changes or a competing native decision.
                check_open()
                if part['message_id'] is None:
                    extra = dict(disable_notification=row['silent'])
                    if self.thread(row) is not None:
                        extra['message_thread_id'] = self.thread(row)
                    reply = previous
                    identity = dict(text=part['html'], reply=reply, **extra)
                    receipt = b._reserve_and_deliver(part['key'], identity,
                        lambda pending: deliver(part['html'], part['plain'], reply, pending, extra))
                    if blocked:
                        return raise_blocked()
                    if not receipt.get('delivered'):
                        return dict(receipt, conversation=dict(b.conversation, conversation_id=row['conversation']))
                    part['message_id'] = int(receipt['message_id'])
                    self.save(row)
                previous = part['message_id']
            if document:
                check_open()
                if document['message_id'] is None:
                    extra = dict(disable_notification=row['silent'])
                    if self.thread(row) is not None:
                        extra['message_thread_id'] = self.thread(row)
                    identity = dict(document_sha256=document['sha256'], size=document['size'], reply=previous, **extra)
                    def send_document(pending):
                        def check_document():
                            check_open_readonly()
                            self.document_file(document)
                        try:
                            return b._deliver_attachment(dict(kind='document', path=self.document_file(document)),
                                '', previous, pending, extra=extra, text_fallback=False,
                                confirmed_size=document['size'], before_attempt=check_document)
                        except NativeError as error:
                            blocked.append(error)
                            return b.not_sent()
                    receipt = b._reserve_and_deliver(document['key'], identity, send_document)
                    if blocked:
                        return raise_blocked()
                    if not receipt.get('delivered'):
                        return dict(receipt, conversation=dict(b.conversation, conversation_id=row['conversation']))
                    if type(receipt.get('document_size')) is not int or receipt['document_size'] != document['size']:
                        return b.receipt(reason='native_pack_document_size_unconfirmed_requires_manual_reconciliation')
                    document['message_id'] = int(receipt['message_id'])
                    self.save(row)
                previous = document['message_id']
            check_open()
        key, identity = self.send_request(row, previous)
        extra = {name: value for name, value in identity.items() if name not in ('text', 'reply')}
        receipt = b._reserve_and_deliver(key, identity,
                    lambda pending: deliver(row['html'], row['plain'], previous, pending, extra))
        if blocked:
            return raise_blocked()
        if receipt.get('delivered'):
            row['message_id'] = int(receipt['message_id'])
            if row.get('kind') == 'decision' and not self.generation(row, row['turn']):
                row['generations'].append(dict(turn=row['turn'], message_id=row['message_id'], revision=0))
            self.save(row)
        return dict(receipt, conversation=dict(b.conversation, conversation_id=row['conversation']))

    def display(self, row, footer='', keyboard=None, generation=None):
        decision = row.get('kind') == 'decision'
        generation = self.generation(row, row['turn']) if decision and generation is None else generation
        target = generation if decision else row
        if target is None or target['message_id'] is None:
            return self.bridge.receipt(reason='ask_message_unknown')
        keyboard = self.keyboard(row) if keyboard is None else keyboard
        # Approval-critical data stays on the card; long canonical outcomes are separate whole parts.
        separate = units(row['plain'] + '\n' + footer) > 4096
        suffix = 'Canonical outcome follows below.' if separate else footer
        desired = dict(footer=footer, keyboard=keyboard)
        if target.get('display') != desired:
            target['revision'] += 1
            target['display'] = desired
            if decision and target['turn'] == row['turn']:
                row['display'] = desired
            self.save(row)
        key = f"runask:{row['id']}" + (f":turn:{target['turn']}" if decision else '') + f":revision:{target['revision']}"
        extra = dict(message_id=target['message_id'], reply_markup={'inline_keyboard': keyboard})
        b = self.bridge
        receipt = b._reserve_and_deliver(key + ':edit', dict(text=row['html'] + '\n' + escape(suffix), **extra),
                    lambda pending: b._deliver_text(row['html'] + '\n' + escape(suffix), None, pending,
                        row['plain'] + '\n' + suffix, method='editMessageText', extra=extra, not_modified=True), retry_undelivered=True)
        if separate:
            for index, part in enumerate(chunks(footer)):
                sent = b._command_message(key + f':outcome:{index}', dict(type='send', text=part, plain=None), self.thread(row))
                if not sent.get('delivered'):
                    receipt = sent
        return receipt

    def settle(self, row, ask):
        if row.get('kind') == 'decision':
            return self.settle_decision(row, ask)
        state = ask['state']
        if state in ('open', 'put aside'):
            return False
        edit_status = self.repo_edit_status(ask, row)
        state, footer = run_outcome(ask, edit_status=edit_status)
        row.update(state=state, pending=None, uncertain=False)
        self.save(row)
        receipt = self.display(row, footer, [])
        # Revisions include outcome edits and pending_reason changes, not just the first answer.
        relay_key = f"runask-{row['id']}-{row['revision']}"
        if self.bridge.hook_secret and row.get('relayed') != relay_key:
            try:
                self.bridge._relay(row['message_id'], relay_key,
                    f"[native run-ask | bead: {row['bead']} | ask: {row['ask']}]\n{footer}",
                    thread_id=self.thread(row), kind='answer')
            except Exception:  # the native decision is already committed; poll retries this delivery
                self.bridge._log('native decision relay unconfirmed')
            else:
                row['relayed'] = relay_key
                self.save(row)
        return receipt

    def require_generations(self, row):
        if not isinstance(row.get('generations'), list) or type(row.get('initial_turn')) is not int:
            raise NativeError('ask_card_generation_unconfirmed' + FALLBACK)

    def generation(self, row, turn):
        return next((g for g in row.get('generations', []) if g['turn'] == turn), None)

    def message_bindings(self, row):
        if row.get('kind') == 'decision':
            bindings = [(g['message_id'], g['turn']) for g in row.get('generations', [])]
            # Unconfirmed rows are recognized for refusal, never upgraded into authority.
            if row['message_id'] is not None and not any(mid == row['message_id'] for mid, _ in bindings):
                bindings.append((row['message_id'], None))
            return bindings
        return [(row['message_id'], None)]

    def bound_messages(self, message_id):
        return [(row, turn) for row in self.rows() for mid, turn in self.message_bindings(row)
                if mid == message_id]

    def _ambiguous_predecessor(self, row):
        """The ambiguous send receipt of an earlier card of this ask, if any."""
        for other in self.rows():
            if not (other.get('binding') == row.get('binding')
                    and other.get('bead') == row.get('bead') and other.get('ask') == row.get('ask')):
                continue
            if (other.get('kind') == 'decision' and type(other.get('initial_turn')) is int
                    and type(other.get('turn')) is int):
                # Every turn up to the current one: a reopen reuses this row
                # under a new key, and a predecessor row keeps its older turns.
                # A bound turn is reconciled, so only unbound turns block.
                last = other['turn'] if other['id'] != row['id'] else row['turn'] - 1
                for turn in range(other['initial_turn'], last + 1):
                    if self.generation(other, turn) is not None:
                        continue
                    receipt = self.unconfirmed_receipt(dict(other, turn=turn))
                    if receipt is not None:
                        return receipt
            elif other['id'] != row['id']:
                receipt = self.unconfirmed_receipt(other)
                if receipt is not None:
                    return receipt
        return None

    def present_decision(self, row, ask):
        self.require_generations(row)
        if row['turn'] != ask['turn']:
            row.update(turn=ask['turn'], message_id=None, pending=None, uncertain=False)
            row.pop('display', None)
        row['state'] = 'open'
        self.save(row)
        if self.generation(row, row['turn']) is None:
            blocked = self._ambiguous_predecessor(row)
            if blocked is not None:
                return dict(blocked, conversation=dict(
                    self.bridge.conversation, conversation_id=row['conversation']))
        # Sends bind one open turn forever; retiring older controls is only presentation.
        receipt = self.send_card(row)
        footer = ('Re-presented; answer the newer card' if self.generation(row, row['turn']) else
                  'The current ask is unavailable in Telegram; use Footplate or the yard cards')
        for generation in row['generations']:
            if generation['turn'] != ask['turn']:
                self.display(row, footer, [], generation)
        return receipt

    def settle_decision(self, row, ask):
        self.require_generations(row)
        state = ask['state']
        if state == 'open':
            self.present_decision(row, ask)
            return False
        footer = outcome(ask, row['options'])
        row.update(state=state, pending=None, uncertain=False)
        self.save(row)
        latest = self.generation(row, row['turn'])
        if latest is None and row['generations']:
            latest = row['generations'][-1]
        receipt = self.display(row, footer, [], latest)
        for generation in row['generations']:
            if generation is not latest:
                self.display(row, 'Re-presented; answer the newer card', [], generation)
        return receipt

    def claim_update(self, update_id):
        with self.store.connect() as db:
            return db.execute('INSERT OR IGNORE INTO run_ask_updates VALUES (?,?)',
                              (digest(binding_of(self.config)), update_id)).rowcount == 1

    def authorized_origin(self, row, message, reply=False):
        if not isinstance(message, dict) or any(key in message for key in self.bridge.FORWARD_KEYS):
            return False
        chat, sender = message.get('chat', {}), message.get('from', {})
        if not isinstance(chat, dict) or not isinstance(sender, dict):
            return False
        expected_id = self.config['user_id'] if reply else self.config['bot_id']
        if (chat.get('id') != self.config['chat_id'] or type(chat.get('id')) is not int or chat.get('type') != 'private'
                or sender.get('id') != expected_id or type(sender.get('id')) is not int
                or sender.get('is_bot') is not (False if reply else True)):
            return False
        thread = self.bridge._topic_thread(message)
        if thread != self.thread(row) or (message.get('message_thread_id') is not None and thread is None):
            return False
        return True

    def authorized_message(self, row, message, reply=False):
        if not self.authorized_origin(row, message, reply):
            return False
        return reply or (type(message.get('message_id')) is int
                         and any(mid == message['message_id'] for mid, _ in self.message_bindings(row)))

    def reconcile_callback(self, row, message, turn):
        self.require_generations(row)
        message_id = message.get('message_id')
        if (turn is None or not row['initial_turn'] <= turn <= row['turn']
                or type(message_id) is not int or message_id <= 0 or self.generation(row, turn)):
            return False
        candidate = dict(row, turn=turn)
        if (not isinstance(message.get('text'), str) or message['text'].rstrip() != row['plain'].rstrip()
                or message.get('reply_markup') != {'inline_keyboard': self.keyboard(candidate)}
                or not self.unconfirmed_receipt(candidate) or self.bound_messages(message_id)):
            return False
        self.ensure(row)
        row['generations'].append(dict(turn=turn, message_id=message_id, revision=0, reconciled=True))
        if turn == row['turn']:
            row['message_id'] = message_id
        self.save(row)
        return True

    def callback(self, update_id, query):
        match = re.fullmatch(r'(runask|askcard):([0-9a-f]{24}):(approve|decline|cancel|confirm|later|report|pick[1-9][0-9]{0,3})(?::([0-9a-f]{16}|0|[1-9][0-9]{0,18}))?', query.get('data', ''))
        notice = 'Native card not authorized'
        try:
            with self.lock():
                row = self.get(match[2]) if match else None
                sender = query.get('from', {})
                if not row or sender.get('id') != self.config['user_id'] or sender.get('is_bot') is not False:
                    return
                decision = row.get('kind') == 'decision'
                if decision and match[4] is not None and not re.fullmatch(r'0|[1-9][0-9]{0,18}', match[4]):
                    return
                if not self.authorized_origin(row, query.get('message')):
                    candidate = dict(row, turn=int(match[4])) if decision and match[4] is not None else row
                    if decision and self.unconfirmed_receipt(candidate):
                        notice = RECONCILE
                    return
                action = match[3]
                if match[1] != ('askcard' if decision else 'runask'):
                    return
                if decision:
                    if action not in ('later', 'decline', 'report') and not (action.startswith('pick') and 1 <= int(action[4:]) <= len(row['options'])):
                        return
                elif action == 'confirm':
                    if not re.fullmatch(r'[0-9a-f]{16}', match[4] or ''):
                        return
                elif match[4] is not None or action not in ('approve', 'decline', 'cancel', 'report'):
                    return
                if not self.authorized_message(row, query['message']):
                    if not decision:
                        return
                    notice = RECONCILE
                    if action == 'report' or not self.reconcile_callback(row, query['message'], int(match[4]) if match[4] is not None else None):
                        return
                input_turn = None
                if decision:
                    bindings = self.bound_messages(query['message']['message_id'])
                    if len(bindings) != 1 or bindings[0][0]['id'] != row['id']:
                        notice = 'Native card message binding ambiguous'
                        return
                    input_turn = bindings[0][1]
                if action == 'report':
                    if row['binding'] != self.binding():
                        raise NativeError('run_ask_wrong_yard_or_pairing')
                    if decision and (input_turn is None or match[4] is None or int(match[4]) != input_turn):
                        return
                    notice = self.send_report(row, query['message']['message_id'], update_id)
                    return
                ask = self.ensure(row)
                if not self.claim_update(update_id):
                    notice = 'Already handled'
                elif ask['state'] != 'open':
                    self.settle(row, ask)
                    notice = 'Ask is ' + ask['state']
                elif decision and (input_turn is None or match[4] is None or int(match[4]) != input_turn
                                   or input_turn != ask['turn']):
                    self.settle(row, ask)
                    notice = 'Ask changed; answer the newer card'
                elif action == 'cancel':
                    row['pending'] = None
                    self.save(row)
                    notice = (GENERAL_UNKNOWN if row.get('general') and row.get('uncertain')
                              else 'Cancelled; nothing ran')
                    self.display(row, notice)
                elif row.get('general') and action in ('approve', 'confirm') and (row.get('uncertain') or row.get('native_expired')):
                    notice = 'Native general card expired; native ask remains open.' if row.get('native_expired') else GENERAL_UNKNOWN
                    self.display(row, notice)
                elif action == 'confirm':
                    pending = row.get('pending')
                    notice = 'Nothing to confirm; tap Approve first. Nothing ran.'
                    if pending:
                        row['pending'] = None
                        self.save(row)  # consume durably before the native write, including crashes
                        now = self.now()
                        if match[4] != pending.get('nonce'):
                            notice = 'Confirmation changed; nothing ran. Tap Approve again.'
                        elif now > pending['expires'] or now < pending['armed_at']:
                            notice = CONFIRM_EXPIRED
                        else:
                            notice = self.decide(row, 'approve', True)
                            return
                    self.display(row, notice)
                elif action == 'approve' and row['weighty']:
                    now = self.now()
                    pending = row.get('pending')
                    if not (pending and pending.get('nonce') and pending['armed_at'] <= now <= pending['expires']):
                        row['pending'] = dict(expires=now + CONFIRM_SECONDS, update=update_id,
                                              armed_at=now, nonce=secrets.token_hex(8))
                        self.save(row)
                    self.confirm_step(row, ask)
                    notice = f"Tap Confirm ask {row['ask']} within {CONFIRM_SECONDS} s, or Cancel."
                else:
                    notice = self.decide(row, action, False, turn=input_turn)
        except NativeError as error:
            notice = str(error)
        finally:
            self.bridge._answer_callback(update_id, query['id'], notice[:180], key=f'runask:{update_id}:answer')

    def validate_notes(self, raw_notes, bead_id):
        if not isinstance(raw_notes, list):
            raise NativeError('invalid native Notes response')
        out = []
        for item in raw_notes:
            if (not isinstance(item, dict) or type(item.get('seq')) is not int or item['seq'] <= 0
                    or not isinstance(item.get('kind'), str) or not item['kind']
                    or (item.get('bead') is not None and item['bead'] != bead_id)):
                raise NativeError('invalid native Notes row')
            out.append(dict(
                seq=item['seq'], kind=item['kind'],
                author=text(item.get('author'), 'note.author'),
                text=text(item.get('text'), 'note.text', controls=True).replace('\r\n', '\n'),
                at=text(item.get('at'), 'note.at'),
            ))
        out.sort(key=lambda item: item['seq'])
        return out

    def report_events(self, bead_id, entry_seq, notes_by_seq, non_report_authors):
        limit = 64
        while True:
            events = self._validate_events(
                self.client.call('Events', [bead_id, limit]), bead_id, limit, 'report')
            noted_at_or_before = {
                event['data']['note'] for event in events
                if event['seq'] <= entry_seq and event['kind'] == 'noted'
                and type(event.get('data', {}).get('note')) is int}
            has_report = any(
                seq in notes_by_seq and notes_by_seq[seq]['kind'] == 'note'
                and notes_by_seq[seq]['author'] not in non_report_authors
                for seq in noted_at_or_before)
            if has_report or not events or len(events) < limit or limit >= 8192:
                if events and events[0]['seq'] > entry_seq and not noted_at_or_before:
                    return None
                return noted_at_or_before
            limit *= 2

    def select_report_notes(self, notes, ask_number, bead_id):
        asks_by_seq = {}
        notes_by_seq = {}
        for note in notes:
            notes_by_seq[note['seq']] = note
            if note['kind'] == 'ask':
                parsed = parse_note_json(note['text'])
                if parsed is not None:
                    asks_by_seq[note['seq']] = parsed
        ask_meta = asks_by_seq.get(ask_number) or {}
        non_report_authors = REPORT_NON_REPORT_AUTHORS | {self.config['yardr_operator']}
        entry_seq = ask_meta.get('entry')
        cutoff_seqs = None
        if type(entry_seq) is int and entry_seq > 0:
            cutoff_seqs = self.report_events(bead_id, entry_seq, notes_by_seq, non_report_authors)
        if cutoff_seqs is None:
            at_or_before = [note for note in notes if note['seq'] <= ask_number]
        else:
            at_or_before = [note for note in notes if note['seq'] in cutoff_seqs]
        start_seq = None
        for note in reversed(at_or_before):
            if note['kind'] == 'note' and note['author'] not in non_report_authors:
                start_seq = note['seq']
                break
        if start_seq is None:
            for note in reversed(at_or_before):
                if note['kind'] == 'note':
                    start_seq = note['seq']
                    break
        if start_seq is None:
            if ask_number in notes_by_seq:
                start_seq = ask_number
            elif notes:
                start_seq = notes[0]['seq']
        selected = [note for note in notes if start_seq is not None and note['seq'] >= start_seq]
        finding_note = find_finding_note(at_or_before, notes_by_seq, non_report_authors)
        return selected, asks_by_seq, ask_meta, finding_note

    def build_report(self, bead_id, ask_number):
        verify_operator(self.client, self.config)
        bead = self.client.call('Bead', dict(bead=bead_id, viewer=self.config['yardr_operator']))
        if not isinstance(bead, dict) or bead.get('id') != bead_id or not isinstance(bead.get('asks'), list):
            raise NativeError('invalid native Bead response')
        found = [a for a in bead['asks'] if isinstance(a, dict) and a.get('ask') == ask_number]
        if len(found) != 1:
            raise NativeError('run_ask_not_found')
        ask = found[0]
        notes = self.validate_notes(self.client.call('Notes', [bead_id]), bead_id)
        selected, asks_by_seq, ask_meta, finding_note = self.select_report_notes(notes, ask_number, bead_id)
        stage = bead.get('stage') or ask_meta.get('stage') or ''
        title = text(bead.get('title'), 'bead.title')
        question = text(ask.get('question'), 'question')
        return format_report_markdown(
            bead_id, title, stage, ask_number, question, notes, selected, asks_by_seq,
            finding_note=finding_note, operator=self.config['yardr_operator'])

    def send_report(self, row, message_id, update_id):
        b = self.bridge
        safe_bead = re.sub(r'[^A-Za-z0-9._-]', '_', row['bead'])
        filename = f"{safe_bead}-ask-{row['ask']}.md"
        key = f"runask:{row['id']}:report:{message_id}:{update_id}"
        identity = dict(purpose='ask_report', row=row['id'], bead=row['bead'], ask=row['ask'],
                        message_id=message_id, update_id=update_id, conversation=row['conversation'])
        extra = {} if self.thread(row) is None else dict(message_thread_id=self.thread(row))
        yard_error = []

        def deliver(pending):
            try:
                content = self.build_report(row['bead'], row['ask'])
            except NativeError as error:
                yard_error.append(error)
                return b.not_sent()
            with tempfile.TemporaryDirectory(prefix='ask-report-') as tmpdir:
                Path(tmpdir).chmod(0o700)
                path = Path(tmpdir) / filename
                fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_EXCL, 0o600)
                try:
                    os.write(fd, content)
                finally:
                    os.close(fd)
                return b._deliver_attachment(
                    dict(kind='document', path=str(path)), '', message_id, pending,
                    extra=extra, text_fallback=False)

        receipt = b._reserve_and_deliver(key, identity, deliver)
        if receipt.get('delivered') is True:
            return 'Report sent'
        if yard_error:
            reason = self.decide_error(yard_error[0])
        else:
            reason = (receipt.get('metadata') or {}).get('reason') or 'delivery not confirmed'
        self.decide_notice(f'{key}:error', f"Could not send report for {row['bead']}#{row['ask']}: {reason}",
                           self.thread(row), (message_id, row['conversation']))
        return f'Report not sent: {reason}'

    def reply(self, update_id, message):
        reply = message.get('reply_to_message')
        if not isinstance(reply, dict) or type(reply.get('message_id')) is not int or reply['message_id'] <= 0:
            return False
        with self.lock():
            bindings = [(row, turn) for row, turn in self.bound_messages(reply['message_id'])
                        if row.get('kind') == 'decision']
            if not bindings:
                return False
            text_reply = isinstance(message.get('text'), str) and self.bridge._extract_attachment(message) is None
            valid_reply = (
                ('from' not in reply or (isinstance(reply['from'], dict)
                 and reply['from'].get('id') == self.config['bot_id'] and reply['from'].get('is_bot') is True))
                and ('chat' not in reply or (isinstance(reply['chat'], dict)
                     and reply['chat'].get('id') == self.config['chat_id'])))
            row, input_turn = bindings[0]
            valid_reply = valid_reply and self.authorized_message(row, message, reply=True)
            if len(bindings) != 1 or not valid_reply:
                notice = 'Native card message binding ambiguous' if len(bindings) != 1 else 'Native card not authorized'
                self.bridge._command_message(f'runask:{update_id}:notice', dict(type='send', text=notice, plain=None), self.thread(row))
                return True
            try:
                ask = self.ensure(row)
                if not self.claim_update(update_id):
                    return True
                if not text_reply:
                    raise NativeError('Native card replies must be text only; attachments are not supported')
                if ask['state'] != 'open':
                    self.settle(row, ask)
                    notice = 'Ask is ' + ask['state']
                else:
                    value = text(message['text'], 'reply words')
                    if not value.strip() or units(value) > CAP:
                        raise NativeError('invalid native reply words')
                    option, words = 0, value
                    if row['options']:
                        pick = re.fullmatch(r'([1-9][0-9]{0,3})(?:\s+(.*))?', value.strip(), re.DOTALL)
                        if not pick or not 1 <= int(pick[1]) <= len(row['options']):
                            raise NativeError('reply with a stored option number and optional words')
                        option, words = int(pick[1]), pick[2] or ''
                    notice = self.decide(row, 'answer', False, option, words, turn=input_turn)
                self.bridge._command_message(f'runask:{update_id}:notice', dict(type='send', text=notice, plain=None), self.thread(row))
                return True
            except NativeError as error:
                notice = str(error)
            self.bridge._command_message(f'runask:{update_id}:notice', dict(type='send', text=notice, plain=None), self.thread(row))
            return True

    def decide(self, row, action, weighed, option=0, words='', turn=None):
        ask = self.ensure(row)  # last ownership/proposal/state read before approval
        if ask['state'] != 'open':
            self.settle(row, ask)
            return 'Ask is ' + ask['state']
        if row.get('general') and action == 'approve' and (row.get('uncertain') or row.get('native_expired')):
            return 'Native general card expired; native ask remains open.' if row.get('native_expired') else GENERAL_UNKNOWN
        decision = row.get('kind') == 'decision'
        if decision:
            self.require_generations(row)
            if turn is None or ask['turn'] != turn:
                self.settle(row, ask)
                return 'Ask changed; answer the newer card'
        was_uncertain = row.get('uncertain', False)
        row.update(pending=None, uncertain=True)
        self.save(row)
        args = dict(bead=row['bead'], ask=row['ask'], actor=self.config['yardr_operator'])
        if decision and (action == 'answer' or action.startswith('pick')):
            method = 'AnswerAsk'
            args.update(option=int(action[4:]) if action.startswith('pick') else option, words=words, weighed=False)
        elif decision and action == 'later':
            method = 'DeferAsk'
        else:
            method = 'ApproveAsk' if action == 'approve' else 'DeclineAsk'
            values = {}
            if action == 'approve' and isinstance(ask.get('run'), dict) and 'repo_edit' in ask['run']:
                payload = ask['run']['repo_edit']
                values = dict(repo_edit_version=str(payload['request']['version']),
                              repo_edit_digest=payload['digest'])
            args.update(dict(weighed=weighed, values=values) if action == 'approve' else dict(words='Declined in Telegram'))
        if decision:
            method += 'Expected'
            args['turn'] = turn
        refusal = None
        try:
            self.client.call(method, args)
        except NativeError as error:
            refusal = error
        # Even a wire error or 5xx can follow a committed operation; only canonical state decides.
        known_open = False
        try:
            ask = self.ensure(row)
            if self.settle(row, ask):
                return 'Ask is ' + ask['state'] if decision else 'Canonical decision recorded'
            if decision and ask['turn'] != turn:
                return 'Ask changed; answer the newer card'
            known_open = ask['state'] == 'open'
        except NativeError:
            if row['state'] == 'stale':
                return 'run_ask_stale_proposal'
        refused = known_open and isinstance(refusal, RPCRefused)
        if row.get('general'):
            if refused:
                row.update(uncertain=was_uncertain if action == 'decline' else False,
                           native_expired=row.get('native_expired', False) or isinstance(refusal, RPCExpired))
                self.save(row)
            footer = str(refusal) if refused and not row.get('uncertain') else GENERAL_UNKNOWN
            self.display(row, footer)
            return footer
        argv = ask['run']['argv'] if refused and isinstance(refusal, RPCStale) else []
        if argv[:2] in (['source', 'add'], ['pack', 'apply'], ['depot', 'edit']) or skill_kind(argv):
            row.update(state='stale', pending=None, uncertain=False)
            self.save(row)
            kind = (('depot pack' if depot_pack(ask['run'].get('pack')) else 'source pack') if argv[:2] == ['pack', 'apply']
                    else 'skill' if skill_kind(argv) else 'repo edit' if argv[:2] == ['depot', 'edit'] else 'source')
            self.display(row, 'Stale ' + kind + ' proposal; nothing ran. Decline it in the yard and ask again.', [])
            return str(refusal)
        footer = str(refusal) if refused and isinstance(refusal, RPCUnsupported) else 'The yard refused this action; read the card again'
        self.display(row, footer if refused else UNCERTAIN)
        return str(refusal) if refused else UNCERTAIN

    def close(self, message_id=None, ask_key=None):
        with self.lock():
            if ask_key is not None:
                with self.store.connect() as db:
                    found = db.execute('SELECT id FROM run_ask_keys WHERE key=?', (ask_key,)).fetchone()
                row = self.resolve_row(self.get(found[0])) if found else None
            else:
                found = [r for r in self.rows() if any(mid == message_id for mid, _ in self.message_bindings(r))]
                if len(found) > 1:
                    return self.bridge.receipt(reason='run_ask_message_binding_ambiguous')
                row = found[0] if found else None
            if row is None:
                return None
            try:
                ask = self.ensure(row)
                if (row.get('kind') == 'decision' and not self.generation(row, row['turn'])
                        and self.unconfirmed_receipt(row)):
                    return self.bridge.receipt(reason='ask_card_delivery_unconfirmed; tap the original card to reconcile, '
                        'then retry ask_close; or decide in the yard and delete the card manually')
                return self.settle(row, ask) or self.bridge.receipt(reason='run_ask_still_open_in_yard')
            except NativeError as error:
                return self.bridge.receipt(reason=str(error))

    def refresh(self):
        if self.client is None:
            return
        with self.lock():
            binding = self.binding()
            for row in self.rows():
                if row['binding'] != binding or row['state'] == 'stale':
                    continue
                if row['message_id'] is None and row.get('kind') != 'decision':
                    if row.get('kind', 'run') == 'run' and not isinstance(row.get('bead_title'), str):
                        try:
                            bead, ask = self.fetch(row['bead'], row['ask'], 'run')
                            if (ask['state'] == 'open'
                                    and self.proposal(bead, ask, row['conversation']) == row['proposal']):
                                self._backfill_bead_title(row, bead['title'])
                        except NativeError:
                            pass
                    continue
                try:
                    ask = self.ensure(row)
                    if row.get('general') and row.get('uncertain') and ask['state'] == 'open':
                        self.display(row, GENERAL_UNKNOWN)
                        continue
                    if not self.settle(row, ask) and row.get('pending'):
                        pending, now = row['pending'], self.now()
                        if now > pending['expires'] or now < pending['armed_at']:
                            row['pending'] = None
                            self.save(row)
                            self.display(row, CONFIRM_EXPIRED)
                except NativeError:
                    continue
            for row in self.rows():
                if row['binding'] != binding or row.get('kind') != 'decision' or row['state'] != 'stale':
                    continue
                if not row.get('replaced_by'):
                    continue
                try:
                    target = self.get(row['replaced_by'])
                    if target is None:
                        self._reissue_decision(row)
                    else:
                        self._retire_replaced(row, self.resolve_row(target))
                except NativeError:
                    continue

    # ---- /decide: the operator's open canonical decisions, opened as the same native cards ----

    def queue(self):
        verify_operator(self.client, self.config)
        operator = self.config['yardr_operator']
        return queue(self.client.call('UserAsks', dict(name=operator, viewer=operator)))

    def decide_list(self, page=1):
        """The text and keyboard of one page of a fresh read of the queue; a page past its end shows the last."""
        entries, operator = self.queue(), self.config['yardr_operator']
        where = f"{self.config['yard']} for {operator}"
        if not entries:
            return f'No open decisions in {where}.', [[dict(text='↻ Refresh', callback_data='decide:p:1')]]
        pages = -(-len(entries) // DECIDE_PAGE)
        page = min(page, pages)
        first = (page - 1) * DECIDE_PAGE
        head = f"{len(entries)} open decision{'s' if len(entries) != 1 else ''} in {where}"
        out = [head + (f' · page {page}/{pages}' if pages > 1 else ''),
               'An ID is bead#ask: tap its button or send /decide <ID>.']
        keyboard, ids = [], []
        for index, entry in enumerate(entries[first:first + DECIDE_PAGE], first + 1):
            label = f"{entry['bead']}#{entry['ask']}"
            out += ['', f"{index}. {short(entry['question'], 100)}",
                    f"{entry['kind']} · {label} · {short(entry['title'], 48)} · asked by {short(entry['asker'], 32)}"]
            # showable can recover and save a decision generation: never inside the decide_ids write below.
            try:
                usable = self.showable(entry)
            except NativeError:
                # One row's failed read never blanks the page: it waits for a retry.
                out.append('Temporarily unavailable; send /decide again.')
                continue
            if usable:
                ident = self.ident(entry['bead'], entry['ask'])
                ids.append((ident, entry['bead'], str(entry['ask'])))
                keyboard.append([dict(text=f'{index}. {label}', callback_data=f'decide:o:{ident}')])
            else:
                out.append('Not complete in Telegram; use Footplate or the yard cards.')
        with self.store.connect() as db:
            db.executemany('INSERT OR IGNORE INTO decide_ids VALUES (?,?,?)', ids)
        nav = [('‹ Prev', page - 1)] if page > 1 else []
        nav.append(('↻ Refresh', page))
        nav += [('Next ›', page + 1)] if page < pages else []
        keyboard.append([dict(text=label, callback_data=f'decide:p:{number}') for label, number in nav])
        return '\n'.join(out), keyboard

    def showable(self, entry):
        """Whether selecting entry opens a card: one already sent, or one that renders whole now."""
        if not entry['shown']:
            return False
        row = self.resolve_row(self.get(self.ident(entry['bead'], entry['ask'])))
        if self.card_message(row):
            return True
        kind = card_kind(entry)
        if (row and row.get('kind', 'run') == 'run' and row.get('state') == 'open'
                and row['message_id'] is None):
            try:
                bead, ask = self.fetch(entry['bead'], entry['ask'], kind)
                if (ask['state'] == 'open' and self.proposal(bead, ask, row['conversation']) != row['proposal']
                        and self._title_only_change(row, bead, ask) is not None):
                    return True
            except NativeError as error:
                if not str(error).endswith(FALLBACK):
                    raise
        try:
            bead, ask = self.fetch(entry['bead'], entry['ask'], kind)
            self.card(bead, ask, kind, self.proposal(bead, ask, ''))
        except NativeError as error:
            if not str(error).endswith(FALLBACK):
                raise
            return False
        return True

    def card_message(self, row):
        if row is None or row.get('state') == 'stale':
            return None
        if row.get('kind') == 'decision':
            generation = self.generation(row, row['turn'])
            return generation and (generation['message_id'], row['conversation'])
        return row['message_id'] and (row['message_id'], row['conversation'])

    def decide_open(self, bead_id, number, conversation):
        """(notice, card message) after one selection; the notice is None once a card was sent."""
        label = f'{bead_id}#{number}'
        entry = next((e for e in self.queue() if e['bead'] == bead_id and e['ask'] == number), None)
        if entry is None:
            return self.decide_closed(bead_id, number), None
        if not self.showable(entry):
            return f"{label} is a {entry['kind']} ask a Telegram card cannot show whole; use Footplate or the yard cards.", None
        ident = self.ident(bead_id, number)
        row = self.resolve_row(self.get(ident))
        kind, silent = card_kind(entry), False
        if row:
            # A card already sent keeps its own conversation, silence and kind, never relocated.
            if self.bridge.conversation_for_id(row['conversation']) is None:
                return f'The card for {label} belongs to a conversation this bridge no longer serves; use Footplate or the yard cards.', None
            conversation, silent, kind = row['conversation'], row['silent'], row.get('kind', 'run')
        before = self.card_message(row)
        try:
            receipt = self.create(bead_id, number, f'decide:{ident}:{digest(conversation)[:12]}', conversation, silent, kind)
        except NativeError as error:
            if str(error) != 'run_ask_not_open':
                raise
            return self.decide_closed(bead_id, number), None
        after = self.card_message(self.resolve_row(self.get(ident)))
        if receipt.get('delivered') is not True:
            reason = (receipt.get('metadata') or {}).get('reason') or 'delivery not confirmed'
            return f'The card for {label} is not confirmed sent ({reason}); nothing was answered.', None
        if before is not None and before == after:
            return f'The card for {label} is already open.', after
        return None, after

    def decide_closed(self, bead_id, number):
        """What a selection of an ask that is not in the open queue reads: its canonical outcome."""
        label, operator = f'{bead_id}#{number}', self.config['yardr_operator']
        if '@' in bead_id:
            return f'{label} is not an open ask in your queue; answer a linked yard ask in Footplate.'
        try:
            bead = self.client.call('Bead', {'bead': bead_id, 'viewer': operator})
        except RPCRefused:
            return f"No ask {label} in {self.config['yard']}."
        if not isinstance(bead, dict) or bead.get('id') != bead_id or not isinstance(bead.get('asks'), list):
            raise NativeError('invalid native Bead response')
        found = [a for a in bead['asks'] if isinstance(a, dict) and a.get('ask') == number]
        if not found:
            return f"No ask {label} in {self.config['yard']}."
        ask = found[0]
        if ask.get('to') != operator:
            return f'Ask {label} is not addressed to {operator}.'
        if ask.get('state') == 'open':
            return f'Ask {label} is open but not in your queue; use Footplate or the yard cards.'
        if ask.get('state') not in ('put aside', 'answered', 'declined', 'taken back'):
            raise NativeError('invalid native ask state')
        if isinstance(ask.get('run'), dict) and ask['state'] != 'put aside':
            edit_status = None
            if 'repo_edit' in ask['run'] and ask['state'] == 'answered':
                validate_run_card(ask)
                edit_status = self.repo_edit_status(ask)
            return f'Ask {label} is no longer open: ' + run_outcome(ask, edit_status=edit_status)[1]
        options = ask.get('options') or []
        if not isinstance(options, list) or not all(isinstance(o, dict) and isinstance(o.get('label'), str) for o in options):
            raise NativeError('invalid native options')
        return f'Ask {label} is no longer open: ' + outcome(ask, options)

    def decide_select(self, value, conversation):
        """(notice, card message) for /decide <value>: an ID, or a bead with one open ask."""
        match = re.fullmatch(f'({BEAD_ID}(?:@{BEAD_ID})?)(?:#([1-9][0-9]{{0,19}}))?', value.strip())
        if not match or (match[2] and int(match[2]) >= 2**64):
            return f'{short(value, 64)} is no decision ID; send /decide for the list. An ID is bead#ask.', None
        if match[2]:
            return self.decide_open(match[1], int(match[2]), conversation)
        found = [e for e in self.queue() if e['bead'] == match[1]]
        if len(found) == 1:
            return self.decide_open(found[0]['bead'], found[0]['ask'], conversation)
        if not found:
            return f'No open decision on {match[1]} for you; send /decide for the list.', None
        ids = ', '.join(f"{e['bead']}#{e['ask']}" for e in found[:10]) + (', …' if len(found) > 10 else '')
        return f'{match[1]} has {len(found)} open decisions ({ids}); send /decide <ID>.', None

    def decide_error(self, error):
        if isinstance(error, (RPCUnavailable, RPCAmbiguous)):
            return f"The yard {self.config['yard']} is unavailable; nothing was listed or opened."
        return str(error)

    def decide_notice(self, key, value, thread, card=None):
        """Send value under key, a reply to card (message id, conversation) when given; the receipts of its parts."""
        b, receipts = self.bridge, []
        if card:
            thread = self.thread(dict(conversation=card[1]))
        for index, part in enumerate(chunks(value, 4000)):
            reply = card[0] if card and index == 0 else None
            extra = {} if thread is None else dict(message_thread_id=thread)
            identity = dict(text=part, reply=reply, **extra)
            def deliver(pending):
                receipt = b._deliver_text(part, reply, pending, extra=extra)
                if reply is not None and receipt.get('metadata', {}).get('reason') == 'telegram_rejected_not_sent_400':
                    # The quote was rejected before sending; keep the fallback under this reservation.
                    return b._deliver_text(part, None, pending, extra=extra)
                return receipt
            receipts.append(b._reserve_and_deliver(f'{key}:{index}', identity, deliver))
        return receipts

    def decide_command(self, update_id, args, thread):
        conversation = self.bridge.decide_conversation(thread)
        notice, card = None, None
        try:
            if conversation is None:
                notice = 'This topic is not one the bridge serves; send /decide in the chat or a served topic.'
            elif not args.strip():
                body, keyboard = self.decide_list()
                self.bridge._command_message(f'decide:{update_id}:list', dict(
                    type='send', text=body, keyboard=keyboard, plain=None), thread)
                return
            else:
                notice, card = self.decide_select(args, conversation['conversation_id'])
        except NativeError as error:
            notice = self.decide_error(error)
        if notice:
            self.decide_notice(f'decide:{update_id}:notice', notice, thread, card)

    def decide_message(self, message):
        if not isinstance(message, dict) or any(key in message for key in self.bridge.FORWARD_KEYS):
            return False
        chat, sender = message.get('chat'), message.get('from')
        return (isinstance(chat, dict) and isinstance(sender, dict) and type(chat.get('id')) is int
                and chat['id'] == self.config['chat_id'] and chat.get('type') == 'private'
                and type(sender.get('id')) is int and sender['id'] == self.config['bot_id'] and sender.get('is_bot') is True
                and type(message.get('message_id')) is int and message['message_id'] > 0
                and (message.get('message_thread_id') is None or self.bridge._topic_thread(message) is not None))

    def decide_callback(self, update_id, query):
        match = re.fullmatch(r'decide:(?:o:([0-9a-f]{24})|p:([1-9][0-9]{0,5}))', query.get('data') or '')
        message, sender = query.get('message'), query.get('from')
        notice = 'Decision list not authorized'
        try:
            thread = self.bridge._topic_thread(message) if isinstance(message, dict) else None
            conversation = self.bridge.decide_conversation(thread)
            if (not match or not isinstance(sender, dict) or type(sender.get('id')) is not int
                    or sender['id'] != self.config['user_id'] or sender.get('is_bot') is not False
                    or not self.decide_message(message) or conversation is None):
                return
            if match[2]:
                body, keyboard = self.decide_list(int(match[2]))
                extra = dict(message_id=message['message_id'], reply_markup={'inline_keyboard': keyboard})
                b = self.bridge
                receipt = b._reserve_and_deliver(f'decide:{update_id}:edit', dict(text=body, **extra),
                    lambda pending: b._deliver_text(body, None, pending, method='editMessageText', extra=extra,
                                                    not_modified=True))
                notice = '' if receipt.get('delivered') else 'The list is not updated; send /decide again'
                return
            with self.store.connect() as db:
                found = db.execute('SELECT bead, ask FROM decide_ids WHERE id=?', (match[1],)).fetchone()
            # The ID hashes the binding: a list from another yard/home on this store is out of date.
            if found is None or self.ident(found[0], int(found[1])) != match[1]:
                notice = 'This list is out of date; send /decide again'
                return
            result, card = self.decide_open(found[0], int(found[1]), conversation['conversation_id'])
            notice = 'Card sent' if result is None else result
            if result is not None:
                self.decide_notice(f'decide:{update_id}:notice', result, thread, card)
        except NativeError as error:
            notice = self.decide_error(error)
            self.decide_notice(f'decide:{update_id}:notice', notice, thread)
        finally:
            self.bridge._answer_callback(update_id, query['id'], notice[:180], key=f'decide:{update_id}:answer')
