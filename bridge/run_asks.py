"""Native asks: socket authority, complete cards and durable interactions."""
import contextlib
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
import socket
import stat
import tempfile
import time
import unicodedata


_markdown_spec = importlib.util.spec_from_file_location('card_markdown', Path(__file__).resolve().with_name('markdown.py'))
markdown = importlib.util.module_from_spec(_markdown_spec)
_markdown_spec.loader.exec_module(markdown)


CAP = 3500
PACK_PARTS = 128
PACK_DOCUMENT_BYTES = 50 * 1024 * 1024
PENDING = 'approved, to be carried out'
UNCERTAIN = 'the yard did not answer; read the card again'
FALLBACK = '; use Footplate or the yard cards'
RECONCILE = 'Card delivery unconfirmed; decide in Footplate or the yard and delete this Telegram card manually'
READS = ('Bead', 'GetUser', 'UserAsks', 'GetGroup', 'SidingProposal', 'SidingOutcome', 'Groups', 'Routes')
WEIGHTS = {'credential', 'as-user', 'outside', 'code', 'trust', 'reads'}
CONFIRM_SECONDS = 60
CONFIRM_EXPIRED = 'Confirmation expired; nothing ran. Tap Approve again.'
TYPED_CONFIRM_ENDED = 'Typed numbers no longer approve in Telegram; tap Approve, then Confirm. Nothing ran.'
LEGACY_CONFIRM_ENDED = 'Confirmation by typed number ended with a bridge update; nothing ran. Tap Approve, then Confirm.'


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


class YardSocketClient:
    def __init__(self, home, source, timeout=10):
        self.home, self.source, self.timeout = Path(home), source, timeout

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
        path = self.home / 'yard.sock'
        try:
            info = path.lstat()
            if not stat.S_ISSOCK(info.st_mode) or info.st_uid != os.getuid():
                raise RPCUnavailable('native socket must be an owner-held Unix socket')
            sock = socket.socket(socket.AF_UNIX, socket.SOCK_STREAM)
            sock.settimeout(self.timeout)
            try:
                sock.connect(str(path))
            except OSError:
                sock.close()
                raise
        except OSError:
            raise RPCUnavailable(f'native {method} unavailable before send') from None
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
                    if (method in ('SidingProposal', 'SidingOutcome', 'FoldSidingExpected', 'ReleaseSidingExpected')
                            and error['code'] == 'error' and error['message'] == 'no such method ' + method):
                        raise RPCUnsupported('siding_card_requires_guarded_core' + FALLBACK)
                    code = error['code'] if error['code'] in {'not-found', 'taken', 'at-limit', 'conflict', 'ambiguous', 'replacing', 'error'} else 'unknown'
                    if (method == 'ApproveAsk' and code == 'error'
                            and error['message'].startswith(tuple(f"ask {arguments.get('ask')}{prefix}" for prefix in (
                                ' was written when the source ', ' was written from another declaration of the source ',
                                ' was carded under ', ' says ', ': the standing import record of pack ')))
                            and error['message'].endswith('Nothing ran. Decline it; it is asked again if it is still wanted.')):
                        raise RPCStale('native proposal is stale; nothing ran', code)
                    raise RPCRefused(f'native {method} refused ({code})', code)
                return value.get('result')  # successful void calls serialize as {}
            except (OSError, ValueError, http.client.HTTPException, UnicodeError):
                raise RPCAmbiguous(f'native {method} response uncertain') from None


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


def skill_metadata(value):
    """Read simple YAML strings; unfamiliar YAML needs the native full client."""
    rows = value.removeprefix('\ufeff').replace('\r\n', '\n').split('\n')
    if rows[0].strip() != '---':
        raise NativeError('invalid native skill metadata')
    try:
        end = next(i for i in range(1, len(rows)) if rows[i].strip() == '---')
    except StopIteration:
        raise NativeError('invalid native skill metadata') from None
    meta, at = {}, 1
    while at < end:
        row = rows[at]
        at += 1
        if not row.strip() or row.lstrip().startswith('#'):
            continue
        match = re.fullmatch(r'(name|description):\s*(.*)', row)
        if not match or match[1] in meta:
            raise NativeError('unsupported native skill metadata' + FALLBACK)
        key, scalar = match.groups()
        if scalar in ('|', '|-', '|+', '>', '>-', '>+'):
            block = []
            while at < end and (not rows[at].strip() or rows[at].startswith(' ')):
                block.append(rows[at])
                at += 1
            indent = min((len(line) - len(line.lstrip(' ')) for line in block if line.strip()), default=0)
            block = [line[indent:] for line in block]
            scalar = '\n'.join(block)
            if match[2].startswith('>'):
                scalar = re.sub(r'(?<=\S)\n(?=\S)', ' ', scalar)
        elif scalar.startswith('"'):
            try:
                scalar = json.loads(scalar)
            except ValueError:
                raise NativeError('unsupported native skill metadata' + FALLBACK) from None
        elif scalar.startswith("'") and scalar.endswith("'"):
            if not re.fullmatch(r"'(?:[^']|'')*'", scalar):
                raise NativeError('invalid native skill metadata')
            scalar = scalar[1:-1].replace("''", "'")
        else:
            scalar = scalar.split(' #', 1)[0].strip()
            if (not scalar or not scalar[0].isalpha() or ': ' in scalar
                    or re.fullmatch(r'(?i:null|true|false|yes|no|on|off)', scalar)):
                raise NativeError('unsupported native skill metadata' + FALLBACK)
        if not isinstance(scalar, str):
            raise NativeError('invalid native skill metadata')
        meta[key] = scalar.strip()
    if not meta.get('description') or (meta.get('name') and not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,63}', meta['name'])):
        raise NativeError('invalid native skill metadata')
    return meta.get('name', ''), meta['description']


def skill_text(value):
    text(value, 'skill file', controls=True)
    if (len(value.encode('utf-8')) > 32768 or any(
            (unicodedata.category(c) == 'Cc' and c not in '\n\r\t')
            or c in '\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u2028\u2029' for c in value)):
        raise NativeError('invalid native skill file')
    return skill_metadata(value)


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


def validate_skill(ask):
    run, argv = ask['run'], ask['run']['argv']
    fail = 'invalid native skill snapshot'
    if (len(argv) not in (3, 5) or not os.path.isabs(argv[2])
            or os.path.normpath('/' + argv[2].lstrip('/')) != argv[2] or any(c.isspace() for c in argv[2])
            or (len(argv) == 5 and argv[3] != '--name')
            or run['command'] != 'yardr ' + ' '.join(argv)
            or run.get('description') != "Copy a skill directory into the yard's skills"
            or run['back'] != 'no automatic undo; review and restore the previous skill in a terminal'
            or run.get('back_by') != 'only the operator'
            or not isinstance(run.get('carried'), list) or len(run['carried']) != 1):
        raise NativeError(fail)
    carried = run['carried'][0]
    if (not isinstance(carried, dict) or set(carried) - {'name', 'text', 'files', 'old', 'inventory'}
            or not isinstance(carried.get('name'), str)
            or not re.fullmatch(r'[a-z0-9][a-z0-9._-]{0,63}', carried['name'])
            or not isinstance(carried.get('files'), list) or len(carried['files']) != 1
            or not isinstance(carried.get('old'), list) or len(carried['old']) != 1):
        raise NativeError(fail)
    name, file, change = carried['name'], carried['files'][0], carried['old'][0]
    if (not isinstance(file, dict) or set(file) != {'program', 'text', 'sha256'} or file['program'] != 'SKILL.md'
            or not isinstance(change, dict) or set(change) - {'program', 'old_sha256', 'new_sha256', 'diff'}
            or change.get('program') != 'SKILL.md'):
        raise NativeError(fail)
    meta_name, description = skill_text(file['text'])
    old = text(carried.get('text'), 'skill standing file', controls=True)
    old_name, old_description = skill_text(old) if old else ('', '')
    if (name != (argv[4] if len(argv) == 5 else meta_name or os.path.basename(os.path.normpath(argv[2])))
            or file['sha256'] != file_hash(file['text']) or change.get('new_sha256') != file['sha256']
            or change.get('old_sha256', '') != (file_hash(old) if old else '') or old == file['text']):
        raise NativeError(fail)
    inventory = carried.get('inventory', [])
    if old:
        if not isinstance(inventory, list) or len(inventory) != 1 or not isinstance(inventory[0], dict):
            raise NativeError(fail)
        inv = inventory[0]
        if (set(inv) != {'path', 'sha256', 'mode', 'size'} or inv.get('path') != 'SKILL.md'
                or inv.get('sha256') != file_hash(old) or type(inv.get('size')) is not int
                or inv['size'] != len(old.encode('utf-8')) or not isinstance(inv.get('mode'), str)
                or not re.fullmatch(r'0[0-7]{3}', inv['mode'])):
            raise NativeError(fail)
    elif inventory != []:
        raise NativeError(fail)
    diff = text(change.get('diff', ''), 'skill diff', controls=True)
    verify_diff(diff, old, file['text'])
    does = run['does']
    if (len(does) < 13 or not does[0].startswith(f'skill {name}: ')
            or unquote(does[0][len(f'skill {name}: '):]) != description
            or does[1] != 'source directory is provenance only; installs the captured snapshot even if the source changes or disappears.'
            or does[2] != f"writes skills/{name}/SKILL.md, mode 0644; sha256 {change.get('old_sha256') or '(absent)'} -> {file['sha256']}"
            or not does[3].startswith('standing metadata name: ' + (old_name or '(none)') + '; description: ')
            or unquote(does[3].split('; description: ', 1)[1]) != (old_description or '(none)')
            or not does[4].startswith('current holders and affected readers: ')
            or not does[4].removeprefix('current holders and affected readers: ')
            or does[5] != 'changes no assignments, grants, credentials or accounts; future reads of this skill see the new instructions.'
            or does[6] != 'no automatic undo; any retained recovery copy requires an operator to review and restore it in a terminal.'):
        raise NativeError('invalid native skill review metadata')
    at = 7
    for label, content in [('proposed SKILL.md', file['text']), ('standing SKILL.md', old), ('replacement diff', diff)]:
        expected = [line + '\n' for line in content.split('\n')[:-1]]
        if not content or not content.endswith('\n'):
            expected.append(content.rsplit('\n', 1)[-1])
        if at >= len(does) or does[at] != label + ' (quoted UTF-8 lines, including line endings):':
            raise NativeError('invalid native skill complete review')
        at += 1
        for line in expected:
            if at >= len(does) or unquote(does[at]) != line:
                raise NativeError('invalid native skill complete review')
            at += 1
    if at != len(does) or ask.get('weight') != [{'class': 'code', 'line': 'brings a program the yard will run: writes agent instructions for skill ' + name}]:
        raise NativeError('invalid native skill complete review or weight')


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
    if ((run.get('carried') and argv[:2] not in (['skill', 'add'], ['source', 'add'], ['pack', 'apply'])) or (run.get('pack') is not None and argv[:2] != ['pack', 'apply'])
            or (argv[:2] == ['source', 'add'] and not run.get('carried'))
            or argv[:2] == ['crew', 'add']):
        raise NativeError('run_ask_carries_files_use_terminal' + FALLBACK)
    return argv


GROUP_ROWS = (['group', 'add'], ['group', 'set'])
GROUP_KEYS = {'add': ('runner', 'kind', 'model', 'limit', 'arg', 'prompt'), 'set': ('model', 'arg')}
GROUP_WORD = re.compile(r'[A-Za-z0-9._/=@:+-]{1,64}')
GROUP_NAME = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]*(?:/[A-Za-z0-9][A-Za-z0-9._-]*)*')
# A store value as core writes it in a sentence: bare, or in Go's double quotes.
VALUE = r'(?:"(?:[^"\\]|\\.)*"|[^\s",=\\]+)'
GROUP_HARNESS = ("Flags every agent of kind {0} gets (its harness file's flags.always, its memory-off switch, "
                 "the brief's own args) are outside this card and unchanged: yardr harness show {0}.")
HIDDEN = set('\u061c\u200e\u200f\u202a\u202b\u202c\u202d\u202e\u2066\u2067\u2068\u2069\u2028\u2029')


def display(value):
    """Core's displaySourceValue for run-line words, which are printable ASCII."""
    if not re.fullmatch(r'[\x20-\x7e]*', value):
        raise NativeError('unsupported native group value' + FALLBACK)
    return json.dumps(value) if value == '' or re.search(r'[ ,=\\"]', value) else value


def quoted_args(args):
    return ' '.join(json.dumps(word) for word in args) if args else '(none)'


def effort(args):
    if '--effort' in args and args.index('--effort') + 1 < len(args):
        value = display(args[args.index('--effort') + 1])
        return f'{value} (--effort {value})'
    return "the agent's own default (no --effort)"


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


class GroupCard:
    """Read core's group card sentence by sentence; anything unknown refuses the whole card."""

    def __init__(self, does):
        self.does, self.at, self.roles = does, 0, []

    def fail(self):
        raise NativeError('invalid native group review' + FALLBACK)

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

    def role(self, label, path, sha, size):
        """Consume the role's raw lines by byte count; the bytes must hash to the digest shown."""
        if size > 32768:
            self.fail()
        start, total = self.at, -1
        while self.at < len(self.does):
            total += len(self.does[self.at].encode('utf-8')) + 1
            self.at += 1
            body = '\n'.join(self.does[start:self.at])
            for candidate in (body, body + '\n'):
                if len(candidate.encode('utf-8')) == size and file_hash(candidate) == sha:
                    if any(c in HIDDEN for c in candidate):
                        self.fail()
                    self.roles.append(dict(label=label, path=path, sha256=sha, bytes=size,
                                           start=start, end=self.at, text=candidate))
                    return
            if total > size:
                break
        self.fail()

    def permissions(self, when):
        self.match(rf'Permissions{when}: .+\.')
        while self.match(rf'(?:Permissions{when}: |The \S+(?: \S+)* preset skips permissions: ).+\.', optional=True):
            pass


def group_words(argv):
    op = argv[1]
    if len(argv) < 4 or not GROUP_NAME.fullmatch(argv[2]) or '..' in argv[2] or len(argv) > 3 + 6 + 32:
        raise NativeError('invalid native group snapshot')
    words = {}
    order = list(GROUP_KEYS[op])
    for word in argv[3:]:
        key, sep, value = word.partition('=')
        if not sep or key not in GROUP_KEYS[op] or not value or (key != 'prompt' and not GROUP_WORD.fullmatch(value)):
            raise NativeError('unsupported native group field' + FALLBACK)
        if order.index(key) < max((order.index(k) for k in words), default=0) or (key in words and key != 'arg'):
            raise NativeError('invalid native group snapshot')
        words.setdefault(key, []).append(value)
    return words


def parse_group(ask):
    """Check a group add or group set card whole against its argv and return its role texts."""
    run, argv = ask['run'], ask['run']['argv']
    op, name = argv[1], argv[2]
    words = group_words(argv)
    args = words.get('arg', [])
    model = words.get('model', [None])[0]
    if op == 'add':
        prompt = words.get('prompt', [''])[0]
        if (words.get('runner') != ['herdr'] or 'kind' not in words or not re.fullmatch(r'[a-z0-9][a-z0-9_-]{0,31}', words['kind'][0])
                or not prompt.startswith('/') or os.path.normpath(prompt) != prompt or not re.fullmatch(r'[A-Za-z0-9._/=@:+-]+', prompt)
                or ('limit' in words and not re.fullmatch(r'[1-9][0-9]{0,3}', words['limit'][0]))):
            raise NativeError('unsupported native group field' + FALLBACK)
    elif not args and model is None:
        raise NativeError('unsupported native group field' + FALLBACK)
    expected = dict(
        add=("Create a new herdr group without replacing one", f'yardr group rm {name}', 'the operator or the yardmaster',
             f'a new group {name} whose agents the yard will run, with the args and role the card shows'),
        set=("Change some settings of a group and keep the rest", None, 'only the operator',
             f"the model and args group {name}'s agents run with"))[op]
    if (run['command'] != 'yardr ' + ' '.join(argv) or run.get('description') != expected[0]
            or (run['back'] != expected[1] if expected[1] else not run['back'].startswith(f'yardr group set {name} '))
            or '\n' in run['back'] or run.get('back_by') != expected[2] or run.get('carried')
            or ask.get('weight') != [{'class': 'code', 'line': 'brings a program the yard will run: ' + expected[3]}]):
        raise NativeError('invalid native group snapshot or weight')
    card = GroupCard(run['does'])
    sha = r'([0-9a-f]{64})'
    if op == 'add':
        kind = words['kind'][0]
        card.exact(f'creates the herdr group {name}, new: no group of that name stands, and this never replaces one.')
        limit = r'[1-9][0-9]{0,3}' if 'limit' not in words else words['limit'][0]
        shown = "the agent's own default" if model is None else display(model)
        card.match(rf"Kind {kind} \([^()\n]+\); model {re.escape(shown)}, passed as the kind's model flag; limit {limit} sessions at once\.")
        card.exact(f'Args, literal, in order: {quoted_args(args)}.')
        card.exact(f'Effort: {effort(args)}.')
        card.permissions('')
        card.exact("Login: the operator's default (no login_for); env: none (no env_file, no env_command); no skills or worktrees.")
        card.exact(f"Source grants: none is held for the name {name} now, plain or as any user; the approval's write checks again and refuses one given since.")
        card.exact("Routes: none names it or resolves to it now; a route added later, or a yard default's {depot} template once a depot of the matching name is added, would send it work.")
        card.exact(GROUP_HARNESS.format(kind))
        found = card.match(rf'Role read from {re.escape(display(prompt))} when the ask was written: provenance only, the yard never reads that file; sha256 {sha}, ([1-9][0-9]{{0,4}}) bytes\.')
        digest_, size = found[1], int(found[2])
        card.exact(f"On approval the role is written to roles/sha256/{digest_}.md (mode 0600; a file there already is kept only when it holds exactly these bytes) and the group's prompt names that absolute file, so no depot's .yardr/roles file stands before it.")
        card.exact(f"Back, group rm, removes the group and any route added to it since, and releases its live sessions; it leaves roles/sha256/{digest_}.md, and any source grant given to the name after the approval, which outlives the group (a later card for the name refuses it until it is revoked).")
        card.exact('Its role is:')
        card.role('proposed role', prompt, digest_, size)
        prompted = True
    else:
        found = card.match(rf'changes the herdr group {re.escape(name)}, which stands: kind ([a-z0-9][a-z0-9_-]{{0,31}}) \([^()\n]+\), limit (?:0|[1-9][0-9]*); its runner, kind, limit and every other setting are kept\.')
        kind = found[1]
        card.match(rf"Kind {kind} \([^()\n]+\), (?:from its own kind setting|from the yard's default_kind setting, as it names no kind of its own|yardr's own default, as neither it nor the yard's default_kind names one)\.")
        tail = r", passed as the kind's model flag; a bead's metadata\.model still stands before the group's, as today\."
        if model is None:
            card.match(rf"Model: .+(?: -> .+{tail}|, kept; a bead's metadata\.model still stands before the group's, as today\.)")
        else:
            card.match(rf"Model: (?:.+ -> {re.escape(display(model))}{tail}|{re.escape(display(model))}, kept; a bead's metadata\.model still stands before the group's, as today\.)")
        if args:
            card.match(rf'Args, literal, in order: .+ -> {re.escape(quoted_args(args))}\.')
            card.match(rf'Effort: .+ -> {re.escape(effort(args))}\.')
        else:
            card.match(r'Args, literal, in order: .+, kept\.')
            card.match(r'Effort: .+ -> .+\.')
        card.permissions(' now')
        card.permissions(' after the approval')
        card.match(r'Other settings, kept as they stand: .+\.')
        card.match(r"Skills: none, and the card adds none\.|Skills, kept: .+; (?:what each holds is the yard's library, changed only through skill add's own card|this card does not change their contents; yardr skill add does)\.")
        card.match(r'Env and login: none set, and the card sets none\.|Env and login, kept unchanged and not read: .+\.')
        card.match(rf"Login: its sessions use (?:user {VALUE}'s login \(login_for\)|the operator's login \({VALUE}; no login_for\)), kept\.")
        if not card.match(rf'Source grants: group {re.escape(name)} holds none; one given before the approval refuses it\.', optional=True):
            card.exact(f'Source grants group {name} holds, kept and unchanged by the card; one given, taken back or changed before the approval refuses it:')
            card.match(r'- source .+\.')
            while card.match(r'- source .+\.', optional=True):
                pass
        card.exact(GROUP_HARNESS.format(kind))
        # Core writes no prompt sentence, and so no role, only for a group that names no prompt.
        prompted = by_name = False
        if card.match(rf'Its prompt stays {VALUE}: this card does not change it\.', optional=True):
            prompted = True
        elif card.match(rf"Its prompt stays {VALUE}: a depot that keeps {VALUE} gives its beads that file, read at the depot's base commit; any other bead the yard's {VALUE}\.", optional=True):
            prompted = by_name = True
        found = prompted and card.match(rf'Its sessions start with the role in ({VALUE}), read now: sha256 {sha}, ([0-9]{{1,5}}) bytes; a change of that file before the approval refuses it\. Its role is:', optional=True)
        if found:
            card.role('standing role', found[1], found[2], int(found[3]))
        elif prompted and (not by_name or not card.match(rf'{VALUE} does not stand: a bead of a depot without its own role file gets no brief\.', optional=True)):
            card.fail()
        if by_name and not card.match(rf'No depot of the yard keeps its own {VALUE} now\.', optional=True):
            shown, count = {}, 0
            while found := card.match(rf"Depot ([A-Za-z0-9._-]+)'s beads start with its own role, ({VALUE}), read now: sha256 {sha}, ([0-9]{{1,5}}) bytes; a change of it before the approval refuses it\. (?:Its role is:|It is the same text as depot ([A-Za-z0-9._-]+)'s above\.)", optional=True):
                count += 1
                if found[5] is None:
                    card.role(f'depot {found[1]} role', found[2], found[3], int(found[4]))
                    shown.setdefault(found[3], found[1])
                elif shown.get(found[3]) != found[5]:
                    card.fail()
            if not count:
                card.fail()
        card.exact('Sessions the group runs now keep the model and args they started with; sessions it starts after the approval start with these.')
        card.exact('Back restores the model and args it has now, by the operator alone; its revision moves on again, sessions started in between keep what they started with, and nothing is written to disk.')
    if card.at != len(card.does):
        card.fail()
    return dict(op=op, name=name, kind=kind, roles=card.roles, prompted=prompted)


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
    run = ask['run']
    if set(run) - {'argv', 'command', 'description', 'does', 'back', 'back_by', 'carried', 'pack', 'ask'}:
        raise NativeError('run_ask_unknown_payload' + FALLBACK)
    argv = run_scope(run)
    skill = argv[:2] == ['skill', 'add']
    for key in ('question', 'asker', 'to', 'state'):
        text(ask.get(key), key)
    lines(ask.get('context'), 'context')
    for key in ('command', 'back'):
        text(run.get(key), 'run.' + key)
    for key in ('description', 'back_by'):
        text(run.get(key, ''), 'run.' + key)
    lines(run.get('does'), 'run.does')
    weight = ask.get('weight', [])
    if not isinstance(weight, list):
        raise NativeError('invalid native weight')
    for item in weight:
        if (not isinstance(item, dict) or set(item) != {'class', 'line'}
                or not isinstance(item['class'], str) or item['class'] not in WEIGHTS or '\n' in text(item['line'], 'weight.line')):
            raise NativeError('invalid native weight')
    if skill:
        validate_skill(ask)
    elif argv[:2] == ['source', 'add']:
        try:
            validate_source(ask)
        except NativeError as error:
            raise NativeError(str(error) + FALLBACK) from None
    elif argv[:2] == ['pack', 'apply']:
        try:
            validate_pack(ask)
        except NativeError:
            raise NativeError('run_ask_invalid_source_pack_use_terminal; use yardr pack apply <dir> in a terminal' + FALLBACK) from None
    elif argv[:2] in GROUP_ROWS:
        parse_group(ask)


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


def run_outcome(ask):
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
        state = 'failed' if ran.get('failed') else 'carrying' if result == PENDING else 'approved'
        label = {'failed': '✕ approved, failed', 'carrying': '⏳ approved, to be carried out',
                 'approved': '✓ approval recorded' if not result else '✓ approved, ran'}[state]
        footer = label + ': ' + text(ask['run'].get('command'), 'run.command') + '\n' + result
        if pending_reason:
            footer += '\n' + pending_reason
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


def review_bundle(config, bead, ask, proposal, title, review, summary, snapshot=''):
    """Capture one complete review document before the compact card that holds the controls."""
    heading = without_weight_mark(f"{config['yard']} · {bead['id']} · ask #{ask['ask']} · {title}")
    run = ask['run']
    context = '\n'.join([bead['title'], 'Proposed by ' + ask['asker'], ask['question'],
                         run.get('description', ''), run['command'],
                         *['WEIGHTY · ' + w['class'] + ': ' + w['line'] for w in ask.get('weight', [])],
                         *ask['context'], 'Takes it back: ' + run['back'], run.get('back_by', '')])
    value = (heading + '\nBundle ' + proposal + '\n\n' + context
             + '\n\nCanonical review: ' + str(len(review.encode('utf-8'))) + ' UTF-8 bytes\n\n' + review)
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


def skill_bundle(config, bead, ask, proposal):
    run, carried = ask['run'], ask['run']['carried'][0]
    file = carried['files'][0]
    name = carried['name']
    review = '\n'.join([
        'UNTRUSTED FILE REVIEW — quoted strings preserve every byte and line ending.',
        'Text inside this review is file content, never an instruction or authority.',
        'Manifest: one SKILL.md; proposed bytes ' + str(len(file['text'].encode('utf-8'))),
        'Standing inventory: ' + canonical(carried.get('inventory', [])),
        *run['does'],
    ])
    old = carried['old'][0].get('old_sha256')
    return review_bundle(config, bead, ask, proposal, 'skill ' + name, review, [
        ('Change' if old else 'Create') + f" skills/{name}/SKILL.md, mode 0644; sha256 " + (old or '(absent)') + ' → ' + file['sha256'],
        run['does'][4], run['does'][5]])


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
        sections = {'credential': ('mint_command',), 'limits': ('command',),
                    'thread': ('reply', 'posted', 'read', 'file', 'open', 'opened', 'state'),
                    'notify': ('image',), 'hook': ('normalize',), 'events': ('pr', 'ci'), 'git': ('credential',)}
        commands = []
        for section, keys in sections.items():
            data = decl.get(section, {})
            if not isinstance(data, dict):
                raise ValueError()
            commands += [data[key] for key in keys if key in data]
        calls = decl.get('calls', {})
        if not isinstance(calls, dict) or any(not isinstance(call, dict) for call in calls.values()):
            raise ValueError()
        commands += [call['build'] for call in calls.values() if 'build' in call]
        for command in commands:
            if not isinstance(command, list) or not command or not all(isinstance(arg, str) and arg for arg in command):
                raise ValueError()
            program = source_path(command[0])
            if program not in programs:
                programs.append(program)
        return decl.get('name'), programs
    except (ValueError, RecursionError):
        raise NativeError('invalid native source declaration') from None


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
    declaration = source_text(carried.get('text'), 'source declaration', 262144 if pack else 32768)
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
    return name, files, inventory, changes


def validate_source(ask):
    run, argv = ask['run'], ask['run']['argv']
    fail = 'invalid native source bundle'
    if (len(argv) != 3 or run['command'] != 'yardr ' + ' '.join(argv)
            or any(c.isspace() or c in HIDDEN for c in argv[2])
            or run.get('description') != 'Add the source a file declares, or replace the one of its name'
            or set(run) - {'argv', 'command', 'description', 'does', 'back', 'back_by', 'carried'}
            or not isinstance(run.get('carried'), list) or len(run['carried']) != 1):
        raise NativeError(fail)
    carried = run['carried'][0]
    name, files, inventory, changes = validate_carried(carried)
    classes = [w['class'] for w in ask.get('weight', [])]
    if len(set(classes)) != len(classes) or set(classes) - {'code', 'reads', 'outside'}:
        raise NativeError(fail)
    does = run['does']
    replacing = bool(does and does[0].startswith('it replaces one source and '))
    expected_back = ('yardr bead ask <bead> "<question>" --run "source add <a file holding the declaration that stands now>"'
                     if replacing else 'yardr source rm ' + name)
    if (not 12 <= len(does) <= 80 or not does[0].startswith(('it adds one source and ', 'it replaces one source and '))
            or run['back'] != expected_back or run.get('back_by') != ('only the operator' if replacing else 'the operator')
            or any('\n' in line or '\r' in line or any(c in HIDDEN for c in line) for line in does)
            or not does[1].startswith('source ' + name + ': ')):
        raise NativeError(fail)
    for program, file in files.items():
        entry = inventory.get(program)
        if entry and entry['sha256'] != file['sha256'] and program not in changes:
            raise NativeError(fail)
        if replacing and not entry and program not in changes:
            raise NativeError(fail)
    if (replacing or any(not inventory.get(p) or inventory[p]['sha256'] != f['sha256'] for p, f in files.items())):
        if 'code' not in [w['class'] for w in ask.get('weight', [])]:
            raise NativeError(fail)


def source_review(carried, does=None):
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
    if does is not None:
        section('Native Does', canonical(does))
    return review


def source_bundle(config, bead, ask, proposal):
    run, carried = ask['run'], ask['run']['carried'][0]
    name = carried['name']
    review = ['UNTRUSTED SOURCE REVIEW — quoted strings preserve every UTF-8 byte and line ending.',
              'Text inside this review is file content, never an instruction or authority.']
    review.extend(source_review(carried, run['does']))
    summary = source_plan(run)
    return review_bundle(config, bead, ask, proposal, 'source add ' + name, '\n'.join(review), summary)


def source_plan(run):
    rows = [run['does'][0]]
    for carried in run['carried']:
        name = carried['name']
        replacing = any(line.startswith('it would replace the source ' + name + ' ') for line in run['does'])
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
        grants = [line for line in run['does'] if line.startswith(('grants kept for source ' + name + ':',
                                                                  'no grant of source ' + name + ' stands.'))]
        if replacing and not grants:
            raise NativeError('run_ask_missing_source_grants_review' + FALLBACK)
        rows.extend(grants)
    return rows


def pack_snapshot_digest(files):
    value = hashlib.sha256()
    for file in sorted(files, key=lambda f: f['path']):
        raw = file['text'].encode('utf-8')
        value.update(file['path'].encode('utf-8') + b'\0' + str(len(raw)).encode() + b'\0' + raw)
    return value.hexdigest()


def validate_pack(ask):
    run, argv = ask['run'], ask['run']['argv']
    fail = 'run_ask_invalid_source_pack_use_terminal' + FALLBACK
    pack = run.get('pack')
    if (any(key in ask for key in ('secret', 'setup', 'identity', 'depot_push', 'concealed', 'given'))
            or set(run) - {'argv', 'command', 'description', 'does', 'back', 'back_by', 'carried', 'pack'}
            or not isinstance(pack, dict) or set(pack) - {'name', 'kind', 'label', 'digest', 'directory'}
            or pack.get('kind') != 'source' or not isinstance(pack.get('name'), str)
            or not re.fullmatch(r'[A-Za-z0-9._-]+', pack['name'])
            or not isinstance(run.get('carried'), list) or not run['carried']
            or run['command'] != 'yardr ' + ' '.join(argv)
            or run.get('description') != 'Import a pack into the yard: set the yard to what the pack says'
            or run.get('back_by') != 'the operator'):
        raise NativeError(fail)
    label = text(pack.get('label'), 'pack label')
    does = run['does']
    if (not 1 <= len(does) <= 80 or not does[0].startswith('pack ' + pack['name'] + ' (' + label + '), of kind source, into the yard: ')
            or any('\n' in line or '\r' in line or any(c in HIDDEN for c in line) for line in does)):
        raise NativeError(fail)
    sources = {}
    needs_code = 'directory' in pack
    for carried in run['carried']:
        name, files, inventory, changes = validate_carried(carried, pack=True)
        if name in sources:
            raise NativeError(fail)
        sources[name] = carried
        replacing = any(line.startswith('it would replace the source ' + name + ' ') for line in does)
        for program, file in files.items():
            entry = inventory.get(program)
            if ((entry and entry['sha256'] != file['sha256']) or (replacing and not entry)) and program not in changes:
                raise NativeError(fail)
        needs_code |= bool(inventory or changes or files or any(line.startswith('it would replace the source ' + name + ' ') for line in does))
    declarations = sum(len(c['text'].encode('utf-8')) for c in sources.values())
    payload = sum(len(f['text'].encode('utf-8')) for c in sources.values() for f in c.get('files', []))
    payload += sum(len(f.get('diff', '').encode('utf-8')) for c in sources.values() for f in c.get('old', []))
    payload += sum(len(f.get('text', '').encode('utf-8')) for c in sources.values() for f in c.get('removed', []))
    if declarations > 262144 or payload > 65536:
        raise NativeError(fail)
    if run['back'] != 'yardr source rm ' + run['carried'][0]['name']:
        raise NativeError(fail)
    classes = [w['class'] for w in ask.get('weight', [])]
    weight_lines = {'code': 'brings a program the yard will run', 'reads': 'widens what a standing grant of read reaches',
                    'outside': 'lets the yard change something outside it'}
    if (len(set(classes)) != len(classes) or set(classes) - {'code', 'reads', 'outside'}
            or (needs_code and 'code' not in classes)
            or any(not w['line'].startswith(weight_lines[w['class']] + ': ') or '\r' in w['line']
                   or any(c in HIDDEN for c in w['line']) for w in ask.get('weight', []))):
        raise NativeError(fail)
    if 'directory' not in pack:
        if len(argv) != 3 or argv[2] != pack['name'] or 'digest' in pack:
            raise NativeError(fail)
        return
    directory = pack['directory']
    if (not isinstance(directory, dict) or set(directory) != {'path', 'files'}
            or not isinstance(directory['path'], str) or not directory['path'].startswith('/')
            or os.path.normpath(directory['path']) != directory['path'] or '://' in directory['path']
            or any(c.isspace() or c in HIDDEN or ord(c) < 32 for c in directory['path'])
            or not isinstance(pack.get('digest'), str) or not re.fullmatch('[0-9a-f]{64}', pack['digest'])
            or argv != ['pack', 'apply', directory['path'], '--sha256', pack['digest']]
            or label != directory['path'] + ' at sha256:' + pack['digest']
            or not isinstance(directory['files'], list) or not 1 <= len(directory['files']) <= 64):
        raise NativeError(fail)
    snapshot, total = {}, 0
    for file in directory['files']:
        if not isinstance(file, dict) or set(file) != {'path', 'text', 'sha256'}:
            raise NativeError(fail)
        path = source_path(file['path'])
        value = source_text(file['text'], 'pack snapshot file', 262144)
        total += len(value.encode('utf-8'))
        if '\\' in path or path in snapshot or file['sha256'] != file_hash(value) or total > 262144:
            raise NativeError(fail)
        snapshot[path] = value
    if 'pack.yaml' not in snapshot or pack_snapshot_digest(directory['files']) != pack['digest']:
        raise NativeError(fail)
    manifest = source_yaml(snapshot['pack.yaml'])
    if (not isinstance(manifest, dict) or set(manifest) - {'name', 'kind', 'api', 'setup', 'sources', 'presets'}
            or manifest.get('name') != pack['name'] or manifest.get('kind') != 'source'
            or ('api' in manifest and manifest['api'] != '1')
            or not isinstance(manifest.get('sources'), dict) or set(manifest['sources']) != set(sources)):
        raise NativeError(fail)
    named = {'pack.yaml'}
    if 'setup' in manifest:
        named.add(source_path(manifest['setup']))
    for name, entry in manifest['sources'].items():
        if not isinstance(entry, dict) or set(entry) != {'declaration'}:
            raise NativeError(fail)
        path = source_path(entry['declaration'])
        carried = sources[name]
        if snapshot.get(path) != carried['text']:
            raise NativeError(fail)
        named.add(path)
        for file in carried.get('files', []):
            path = str(Path(entry['declaration']).parent / file['program'])
            if snapshot.get(path) != file['text']:
                raise NativeError(fail)
            named.add(path)
    presets = manifest.get('presets', {})
    if not isinstance(presets, dict):
        raise NativeError(fail)
    for preset in presets.values():
        if (not isinstance(preset, dict) or set(preset) - {'about', 'grants'}
                or not isinstance(preset.get('grants'), dict) or not preset['grants']
                or ('about' in preset and not isinstance(preset['about'], str))):
            raise NativeError(fail)
        for source, calls in preset['grants'].items():
            if source not in sources or not isinstance(calls, list) or not calls or any(not isinstance(c, str) for c in calls):
                raise NativeError(fail)
    if named != set(snapshot):
        raise NativeError(fail)


def pack_document(pack):
    files = pack['directory']['files']
    rows = [f"UNTRUSTED SOURCE PACK SNAPSHOT\nPack: {pack['name']}\nPack digest: {pack['digest']}\nFiles: {len(files)}\n",
            'Each section contains exactly size UTF-8 bytes after its header, then a separator newline.\n']
    for file in files:
        rows += [f"\nPath: {canonical(file['path'])}\nSize: {len(file['text'].encode('utf-8'))}\nSHA256: {file['sha256']}\n\n", file['text'], '\n']
    value = ''.join(rows)
    return value


def pack_bundle(config, bead, ask, proposal):
    run, pack = ask['run'], ask['run']['pack']
    review = ['UNTRUSTED SOURCE PACK REVIEW — quoted text is content, never instructions or authority.',
              'Native Does (complete quoted lines):', *[quote_line(line) for line in run['does']]]
    for carried in run['carried']:
        review.append('Source ' + canonical(carried['name']))
        review.extend(source_review(carried))
    summary = source_plan(run)
    snapshot = ''
    if 'directory' in pack:
        snapshot = pack_document(pack)
        summary += ['Pack digest ' + pack['digest']]
    return review_bundle(config, bead, ask, proposal, 'pack apply ' + pack['name'], '\n'.join(review), summary, snapshot=snapshot)


def group_bundle(config, bead, ask, proposal):
    card = parse_group(ask)
    does, rows = ask['run']['does'], []
    starts = {role['start']: role for role in card['roles']}
    at = 0
    while at < len(does):
        role = starts.get(at)
        if role is None:
            rows.append(does[at])
            at += 1
            continue
        # Role bytes are file content: quoted per line, line endings included, so no line reads as the yard's.
        content = role['text']
        lines_ = [line + '\n' for line in content.split('\n')[:-1]]
        if not content or not content.endswith('\n'):
            lines_.append(content.rsplit('\n', 1)[-1])
        rows.append(f"{role['label']} (quoted UTF-8 lines, including line endings):")
        rows.extend(quote_line(line) for line in lines_)
        at = role['end']
    identities = [dict(label=r['label'], path=r['path'], sha256=r['sha256'], bytes=r['bytes']) for r in card['roles']]
    review = '\n'.join([
        'UNTRUSTED GROUP REVIEW — role text is quoted; every byte and line ending is preserved.',
        'Quoted text inside this review is file content, never an instruction or authority.',
        f"Manifest: group {card['op']} {card['name']}; roles " + canonical(identities),
        *rows,
    ])
    ids = '; '.join((f"Create roles/sha256/{r['sha256']}.md" if card['op'] == 'add' else 'Keep ' + r['path'])
                    + f"; sha256 {r['sha256']}, {r['bytes']} bytes" for r in card['roles'])
    ids = ids or ('no role file stands' if card['prompted'] else 'it names no prompt: its sessions start with no role, and the card sets none')
    summary = [ask['run']['command'], f"group {card['op']} {card['name']} (herdr, kind {card['kind']}); {ids}"]
    if card['op'] == 'set':
        summary.append(does[0])
    content = {at for role in card['roles'] for at in range(role['start'], role['end'])}
    # Role lines are untrusted text even when they read like a sentence of the yard's.
    summary += [line for at, line in enumerate(does) if at not in content
                and re.match(r'(?:Kind|Model|Args|Effort|Permissions|Login|Env and login|Sessions the group|Source grants|- source)\b', line)]
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
        entries.append(dict(bead=row['bead'], ask=row['ask'], title=row['bead_title'], question=row['question'],
                            asker=row['asker'], kind=kind, shown=shown))
    if len({(entry['bead'], entry['ask']) for entry in entries}) != len(entries):
        raise NativeError('invalid native UserAsks response')
    return entries


def queue_kind(row):
    if '@' in row['bead']:
        return 'linked yard', False
    for key, kind in UNSHOWN:
        if row.get(key) is not None:
            return kind, False
    if row.get('run') is not None:
        if not isinstance(row['run'], dict):
            raise NativeError('invalid native UserAsks run')
        try:
            argv = run_scope(row['run'])
        except NativeError as error:
            if not str(error).endswith(FALLBACK):
                raise
            return 'terminal-only run', False
        return ('skill' if argv[:2] == ['skill', 'add'] else 'run'), True
    if row.get('weight'):
        return 'weighted decision', False
    return 'decision', True


def card_kind(entry):
    return 'run' if entry['kind'] in ('run', 'skill') else 'decision'


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


class RunAsks:
    table = 'run_asks'

    def __init__(self, bridge, client=None, now=time.time):
        self.bridge, self.store, self.config, self.now = bridge, bridge.store, bridge.config, now
        self.client = client
        if self.config.get('yardr_home') and client is None:
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

    def binding(self):
        if self.client is None or not self.config.get('yardr_operator'):
            raise NativeError('native_run_asks_not_configured')
        return {key: str(self.config.get(key, 'tgbridge' if key == 'yardr_source' else '')) for key in
                ('yard', 'yardr_home', 'yardr_operator', 'yardr_source', 'bot_id', 'user_id', 'chat_id')}

    def ident(self, bead_id, number):
        return digest(dict(binding=self.binding(), bead=bead_id, ask=number))[:24]

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
            db.execute('INSERT OR REPLACE INTO ' + self.table + ' VALUES (?,?)', (row['id'], canonical(row)))

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
        if (kind != 'decision' and ask['state'] in {'open', 'put aside'} and ask['run']['argv'][:2] == ['group', 'set']
                and not parse_group(ask)['prompted']):
            # A role-less card is whole only for a group naming no prompt; ask core while the card can still be approved.
            group = self.client.call('GetGroup', [ask['run']['argv'][2]])
            if (not isinstance(group, dict) or group.get('name') != ask['run']['argv'][2]
                    or not isinstance(group.get('config'), dict) or group['config'].get('prompt') not in (None, '')):
                raise NativeError('invalid native group review' + FALLBACK)
        return bead, ask

    def proposal(self, bead, ask, conversation):
        value = dict(binding=self.binding(), bead=bead['id'], title=bead['title'], ask=ask['ask'],
                     to=ask['to'], asker=ask['asker'], question=ask['question'], context=ask['context'],
                     run=ask.get('run'), weight=ask.get('weight', []), conversation=conversation)
        if 'run' not in ask:
            value.update(options=ask['options'], recommend=ask.get('recommend', 0), why=ask.get('why', ''),
                         about=ask.get('about', []), since=ask['since'])
        if 'run' not in ask or ask['run'].get('argv', [])[:2] in (['skill', 'add'], ['source', 'add'], ['pack', 'apply'], *GROUP_ROWS):
            try:
                home = Path(self.config['yardr_home'])
                if validate_home(home) != home:
                    raise NativeError('native_home_changed')
                info = home.lstat()
            except OSError:
                raise NativeError('native_home_unavailable') from None
            value['home_identity'] = [info.st_dev, info.st_ino]
        return digest(value)

    def ensure(self, row):
        if row['state'] == 'stale':
            raise NativeError('run_ask_stale_proposal')
        if row['binding'] != self.binding():
            raise NativeError('run_ask_wrong_yard_or_pairing')
        verify_operator(self.client, self.config)
        bead, ask = self.fetch(row['bead'], row['ask'], row.get('kind', 'run'))
        if self.proposal(bead, ask, row['conversation']) != row['proposal']:
            row.update(pending=None, state='stale')
            self.save(row)
            self.display(row, 'Proposal changed; use Footplate or the yard cards', [])
            raise NativeError('run_ask_stale_proposal')
        return ask

    def keyboard(self, row):
        if row.get('kind') == 'decision':
            buttons = [[dict(text=f"Choose {index}", callback_data=f"askcard:{row['id']}:pick{index}:{row['turn']}")]
                       for index, option in enumerate(row['options'], 1)]
            return buttons + [[dict(text=label, callback_data=f"askcard:{row['id']}:{action}:{row['turn']}")
                               for label, action in [('Later', 'later'), ('✕ Decline', 'decline')]]]
        actions = [('Cancel', 'cancel')] if row.get('pending') else [(f"✓ Approve in {self.config['yard']}", 'approve')]
        actions.append(('✕ Decline', 'decline'))
        buttons = [[dict(text=label, callback_data=f"runask:{row['id']}:{action}") for label, action in actions]]
        if (row.get('pending') or {}).get('nonce'):
            buttons.append([dict(text=f"✓ Confirm ask {row['ask']}",
                                 callback_data=f"runask:{row['id']}:confirm:{row['pending']['nonce']}")])
        return buttons

    def confirm_step(self, row, ask):
        prefix = f"Confirm ask {row['ask']} on {row['bead']}: runs "
        suffix = f' · Tap Confirm within {CONFIRM_SECONDS} s, or Cancel.'
        command = ask['run']['command']
        available = 4096 - units(row['plain'] + '\n' + prefix + suffix)
        if available < 1:
            raise NativeError('run_ask_confirmation_too_large' + FALLBACK)
        if units(command) > available:
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
            with self.store.connect() as db:
                saved = db.execute('SELECT id,request FROM run_ask_keys WHERE key=?', (key,)).fetchone()
            if saved and saved[1] != request:
                raise NativeError('idempotency_key_conflict')
            proposal = self.proposal(bead, ask, conversation)
            ident = self.ident(bead_id, number)
            row = self.get(ident)
            if row:
                if row.get('kind', 'run') != kind:
                    raise NativeError('idempotency_key_conflict')
                if row['state'] == 'stale':
                    raise NativeError('run_ask_stale_proposal')
                if row['proposal'] != proposal:
                    raise NativeError('run_ask_stale_proposal')
                if (row['message_id'] is None and ask['state'] != 'open'
                        and (kind != 'decision' or not self.unconfirmed_receipt(row))):
                    raise NativeError('run_ask_not_open')
                if row['silent'] != silent:
                    raise NativeError('idempotency_key_conflict')
            else:
                if ask['state'] != 'open':
                    raise NativeError('run_ask_not_open')
                plain, card, bundle = self.card(bead, ask, kind, proposal)
                row = dict(id=ident, binding=binding, bead=bead_id, ask=number, conversation=conversation,
                           proposal=proposal, plain=plain, html=card, weighty=bool(ask.get('weight')),
                           state='open', pending=None, message_id=None, silent=silent, revision=0)
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
                if (db.execute('SELECT 1 FROM deliveries WHERE key=?', (key,)).fetchone()
                        or db.execute('SELECT 1 FROM asks WHERE ask_id=?', (self.bridge._ask_id_for_key(key),)).fetchone()):
                    raise NativeError('idempotency_key_conflict')
                db.execute('INSERT OR IGNORE INTO run_ask_keys VALUES (?,?,?)', (key, ident, request))
            self.save(row)
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

    def card(self, bead, ask, kind, proposal):
        """(plain, html, bundle) of a new card; one Telegram cannot show whole raises a FALLBACK error."""
        if kind == 'decision':
            if len(ask.get('options') or []) + 2 > self.bridge.COMMAND_KEYBOARD_MAX_BUTTONS:
                raise NativeError('ask_card_too_many_options' + FALLBACK)
            return (*render_decision(self.config, bead, ask), None)
        if ask['run']['argv'][:2] == ['skill', 'add']:
            return skill_bundle(self.config, bead, ask, proposal)
        if ask['run']['argv'][:2] == ['source', 'add']:
            return source_bundle(self.config, bead, ask, proposal)
        if ask['run']['argv'][:2] == ['pack', 'apply']:
            return pack_bundle(self.config, bead, ask, proposal)
        if ask['run']['argv'][:2] in GROUP_ROWS:
            return group_bundle(self.config, bead, ask, proposal)
        return (*render(self.config, bead, ask), None)

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
        except OSError:
            raise NativeError('native_pack_document_unavailable') from None

    def send_card(self, row):
        b = self.bridge
        blocked = []
        def check_open():
            if self.ensure(row)['state'] != 'open' and row['message_id'] is None:
                raise NativeError('run_ask_not_open')
        def deliver(markup, plain, reply, pending, extra):
            try:
                return b._deliver_text(markup, reply, pending, plain, extra=extra,
                                       before_attempt=check_open if row.get('bundle') else None)
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
                        raise blocked[0]
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
                            check_open()
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
                        raise blocked[0]
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
            raise blocked[0]
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
        state, footer = run_outcome(ask)
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

    def present_decision(self, row, ask):
        self.require_generations(row)
        if row['turn'] != ask['turn']:
            row.update(turn=ask['turn'], message_id=None, pending=None, uncertain=False)
            row.pop('display', None)
        row['state'] = 'open'
        self.save(row)
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
                              (digest(self.binding()), update_id)).rowcount == 1

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
        match = re.fullmatch(r'(runask|askcard):([0-9a-f]{24}):(approve|decline|cancel|confirm|later|pick[1-9][0-9]{0,3})(?::([0-9a-f]{16}|0|[1-9][0-9]{0,18}))?', query.get('data', ''))
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
                    if action not in ('later', 'decline') and not (action.startswith('pick') and 1 <= int(action[4:]) <= len(row['options'])):
                        return
                elif action == 'confirm':
                    if not re.fullmatch(r'[0-9a-f]{16}', match[4] or ''):
                        return
                elif match[4] is not None or action not in ('approve', 'decline', 'cancel'):
                    return
                if not self.authorized_message(row, query['message']):
                    if not decision:
                        return
                    notice = RECONCILE
                    if not self.reconcile_callback(row, query['message'], int(match[4]) if match[4] is not None else None):
                        return
                input_turn = None
                if decision:
                    bindings = self.bound_messages(query['message']['message_id'])
                    if len(bindings) != 1 or bindings[0][0]['id'] != row['id']:
                        notice = 'Native card message binding ambiguous'
                        return
                    input_turn = bindings[0][1]
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
                    notice = 'Cancelled; nothing ran'
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

    def reply(self, update_id, message):
        reply = message.get('reply_to_message')
        if not isinstance(reply, dict) or type(reply.get('message_id')) is not int or reply['message_id'] <= 0:
            return False
        with self.lock():
            # Retained run rows from an earlier pairing do not own this chat's message ids.
            bindings = [(row, turn) for row, turn in self.bound_messages(reply['message_id'])
                        if row.get('kind') == 'decision'
                        or (self.client is not None and self.config.get('yardr_operator')
                            and row['binding'] == self.binding())]
            known_decision = any(row.get('kind') == 'decision' for row, _ in bindings)
            text_reply = isinstance(message.get('text'), str) and self.bridge._extract_attachment(message) is None
            valid_reply = (
                ('from' not in reply or (isinstance(reply['from'], dict)
                 and reply['from'].get('id') == self.config['bot_id'] and reply['from'].get('is_bot') is True))
                and ('chat' not in reply or (isinstance(reply['chat'], dict)
                     and reply['chat'].get('id') == self.config['chat_id'])))
            if not known_decision:
                if not valid_reply or not text_reply:
                    return False
                with self.store.connect() as db:
                    seen = db.execute('SELECT 1 FROM run_ask_updates WHERE binding=? AND id=?',
                                      (digest(self.binding()), update_id)).fetchone() if self.client else None
                if seen and any(self.authorized_message(row, message, reply=True) for row, _ in bindings):
                    return True
            candidates = [(row, turn) for row, turn in bindings
                          if row.get('kind') == 'decision'
                          or (message['text'] == str(row['ask']) and self.authorized_message(row, message, reply=True))]
            if not candidates:
                return False
            row, input_turn = candidates[0]
            valid_reply = valid_reply and self.authorized_message(row, message, reply=True)
            if (len(bindings) if known_decision else len(candidates)) != 1 or not valid_reply:
                if not known_decision:
                    return False
                notice = 'Native card message binding ambiguous' if len(bindings) != 1 else 'Native card not authorized'
                self.bridge._command_message(f'runask:{update_id}:notice', dict(type='send', text=notice, plain=None), self.thread(row))
                return True
            if not known_decision:
                if self.claim_update(update_id):
                    self.bridge._command_message(f'runask:{update_id}:notice',
                        dict(type='send', text=TYPED_CONFIRM_ENDED, plain=None), self.thread(row))
                return True
            try:
                ask = self.ensure(row)
                if not self.claim_update(update_id):
                    return True
                if row.get('kind') == 'decision':
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
        decision = row.get('kind') == 'decision'
        if decision:
            self.require_generations(row)
            if turn is None or ask['turn'] != turn:
                self.settle(row, ask)
                return 'Ask changed; answer the newer card'
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
            args.update(dict(weighed=weighed, values={}) if action == 'approve' else dict(words='Declined in Telegram'))
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
        if refused and isinstance(refusal, RPCStale) and ask['run']['argv'][:2] in (['source', 'add'], ['pack', 'apply']):
            row.update(state='stale', pending=None, uncertain=False)
            self.save(row)
            kind = 'source pack' if ask['run']['argv'][:2] == ['pack', 'apply'] else 'source'
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
                row = self.get(found[0]) if found else None
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
            for row in self.rows():
                if (row['binding'] != self.binding() or row['state'] == 'stale'
                        or (row['message_id'] is None and row.get('kind') != 'decision')):
                    continue
                try:
                    ask = self.ensure(row)
                    if not self.settle(row, ask) and row.get('pending'):
                        pending, now = row['pending'], self.now()
                        legacy = 'nonce' not in pending
                        if legacy or now > pending['expires'] or now < pending['armed_at']:
                            row['pending'] = None
                            self.save(row)
                            self.display(row, LEGACY_CONFIRM_ENDED if legacy else CONFIRM_EXPIRED)
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
            # showable can recover and save a row: never inside the decide_ids write below.
            if self.showable(entry):
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
        if self.card_message(self.get(self.ident(entry['bead'], entry['ask']))):
            return True
        kind = card_kind(entry)
        try:
            bead, ask = self.fetch(entry['bead'], entry['ask'], kind)
            self.card(bead, ask, kind, self.proposal(bead, ask, ''))
        except NativeError as error:
            if not str(error).endswith(FALLBACK):
                raise
            return False
        return True

    def card_message(self, row):
        if row is None:
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
        row = self.get(ident)
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
        after = self.card_message(self.get(ident))
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
            return f'Ask {label} is no longer open: ' + run_outcome(ask)[1]
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
        b = self.bridge
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
            b._reserve_and_deliver(f'{key}:{index}', identity, deliver)

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
