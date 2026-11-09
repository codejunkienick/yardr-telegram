"""Native asks: socket authority, complete cards and durable interactions."""
import contextlib
import fcntl
import hashlib
from html import escape
import http.client
import json
import os
from pathlib import Path
import re
import socket
import stat
import time
import unicodedata


CAP = 3500
PENDING = 'approved, to be carried out'
UNCERTAIN = 'the yard did not answer; read the card again'
FALLBACK = '; use Footplate or the yard cards'
READS = ('Bead', 'GetUser', 'UserAsks', 'GetGroup', 'SidingProposal', 'SidingOutcome', 'Groups', 'Routes')
WEIGHTS = {'credential', 'as-user', 'outside', 'code', 'trust', 'reads'}


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
    if ((run.get('carried') and argv[:2] != ['skill', 'add']) or run.get('pack') is not None
            or argv[:2] in (['source', 'add'], ['pack', 'apply'], ['crew', 'add'])):
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
        card.match(r"Skills: none, and the card adds none\.|Skills, kept: .+; what each holds is the yard's library, changed only through skill add's own card\.")
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
    elif argv[:2] in GROUP_ROWS:
        parse_group(ask)


def render(config, bead, ask):
    run = ask['run']
    parts = []

    def add(value, tag=None, trusted=False):
        value = value if trusted else without_weight_mark(value)
        escaped = escape(value)
        parts.append((value, f'<{tag}>{escaped}</{tag}>' if tag else escaped))

    add(f"{config['yard']} · {bead['id']} · ask #{ask['ask']}", 'b')
    add(f"approving runs in {config['yard']} as {config['yardr_operator']}")
    for weight in ask.get('weight', []):
        add(f"WEIGHTY · {weight['class']}: {weight['line']}", 'b', trusted=True)
    add(text(bead.get('title'), 'bead.title'), 'b')
    add('Proposed by ' + ask['asker'])
    add(ask['question'])
    if run.get('description'):
        add(run['description'])
    # Canonical command text remains exact; preformatted text cannot be a warning line.
    add(run['command'], 'pre', trusted=True)
    add('Does:')
    for line in run['does']:
        add(line, 'pre')
    add('Takes it back: ' + run['back'])
    if run.get('back_by'):
        add(run['back_by'] + ' can take it back')
    for line in ask['context']:
        add(line)
    plain = '\n'.join(p for p, _ in parts)
    if units(plain) > CAP:
        raise NativeError('run_ask_payload_too_large_for_card' + FALLBACK)
    return plain, '\n'.join(h for _, h in parts)


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
    parts = [f"{config['yard']} · {bead['id']} · ask #{ask['ask']}",
             f"Answering in {config['yard']} as {config['yardr_operator']}",
             text(bead.get('title'), 'bead.title'), 'Asked by ' + ask['asker'], ask['question'],
             *ask['context']]
    for index, option in enumerate(ask.get('options') or [], 1):
        parts.extend([f"{index}. {option['label']}", option['consequence']])
    if ask.get('recommend'):
        parts.append(f"Recommended: {ask['recommend']}. {ask['options'][ask['recommend'] - 1]['label']}")
    if ask.get('why'):
        parts.append('Why: ' + ask['why'])
    for about in ask.get('about', []):
        parts.append(f"About: {about['id']} · {about['title']} · {about['stage']}")
    parts.append('Reply directly to this card with ' +
                 ('an option number and optional words.' if ask.get('options') else 'your answer.'))
    plain = '\n'.join(without_weight_mark(part) for part in parts)
    if units(plain) > CAP:
        raise NativeError('ask_card_payload_too_large_for_card' + FALLBACK)
    return plain, escape(plain)


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


def review_bundle(config, bead, ask, proposal, title, review, summary, identities=1):
    """Split a complete untrusted review into reply-chained parts before the card that holds the controls."""
    heading = without_weight_mark(f"{config['yard']} · {bead['id']} · ask #{ask['ask']} · {title}")
    # Reserve the entire envelope, including the largest possible part counters.
    prefix = heading + '\nBundle ' + proposal + '\nPart 999999/999999 · untrusted quoted review (reply chain)\n'
    limit = CAP - units(prefix)
    if limit < 2:
        raise NativeError('run_ask_bundle_envelope_too_large' + FALLBACK)
    bodies = list(chunks(review, limit))
    parts = []
    for index, body in enumerate(bodies, 1):
        header = heading + '\nBundle ' + proposal + f'\nPart {index}/{len(bodies)} · untrusted quoted review (reply chain)\n'
        plain, markup = header + body, escape(header) + '<pre>' + escape(body) + '</pre>'
        if units(plain) > CAP:
            raise NativeError('run_ask_bundle_envelope_too_large' + FALLBACK)
        parts.append(dict(plain=plain, html=markup, sha256=file_hash(plain), message_id=None))
    # The ordinary renderer still supplies the complete question/context/weight/undo card.
    tail = [f'Complete review bundle {proposal} · {len(parts)} parts.',
            'This card replies to the last part. Open that reply and follow the part reply chain to part 1.']
    try:
        plain, markup = render(config, bead, dict(ask, run=dict(ask['run'], does=summary + tail)))
    except NativeError:
        # Long excerpts stay in the bundle; the card keeps only the identities.
        plain, markup = render(config, bead, dict(ask, run=dict(ask['run'], does=summary[:identities] + tail)))
    return plain, markup, dict(digest=proposal, sha256=file_hash(review), parts=parts)


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
    return review_bundle(config, bead, ask, proposal, 'skill ' + name, review, [
        f"skill {name}; SKILL.md sha256 {file['sha256']}",
        'Standing sha256 ' + (carried['old'][0].get('old_sha256') or '(absent)')], identities=2)


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
    ids = '; '.join(f"{r['label']} sha256 {r['sha256']}, {r['bytes']} bytes" for r in card['roles'])
    ids = ids or ('no role file stands' if card['prompted'] else 'it names no prompt: its sessions start with no role, and the card sets none')
    summary = [f"group {card['op']} {card['name']} (herdr, kind {card['kind']}); {ids}"]
    content = {at for role in card['roles'] for at in range(role['start'], role['end'])}
    # Role lines are untrusted text even when they read like a sentence of the yard's.
    summary += [line for at, line in enumerate(does) if at not in content
                and re.match(r'(?:Kind|Model|Args|Effort|Permissions|Login|Env and login|Sessions the group)\b', line)]
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
        if 'run' not in ask or ask['run'].get('argv', [])[:2] in (['skill', 'add'], *GROUP_ROWS):
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
        return [[dict(text=label, callback_data=f"runask:{row['id']}:{action}") for label, action in actions]]

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
                if row['message_id'] is None and ask['state'] != 'open':
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
                    row['bundle'] = bundle
            with self.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                if (db.execute('SELECT 1 FROM deliveries WHERE key=?', (key,)).fetchone()
                        or db.execute('SELECT 1 FROM asks WHERE ask_id=?', (self.bridge._ask_id_for_key(key),)).fetchone()):
                    raise NativeError('idempotency_key_conflict')
                db.execute('INSERT OR IGNORE INTO run_ask_keys VALUES (?,?,?)', (key, ident, request))
            self.save(row)
            if kind == 'decision':
                return self.present_decision(row, ask) if ask['state'] == 'open' else self.settle(row, ask)
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

    def authorized_message(self, row, message, reply=False):
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
        return reply or (type(message.get('message_id')) is int
                         and any(mid == message['message_id'] for mid, _ in self.message_bindings(row)))

    def callback(self, update_id, query):
        match = re.fullmatch(r'(runask|askcard):([0-9a-f]{24}):(approve|decline|cancel|later|pick[1-9][0-9]{0,3})(?::(0|[1-9][0-9]{0,18}))?', query.get('data', ''))
        notice = 'Native card not authorized'
        try:
            with self.lock():
                row = self.get(match[2]) if match else None
                sender = query.get('from', {})
                if (not row or sender.get('id') != self.config['user_id'] or sender.get('is_bot') is not False
                        or not self.authorized_message(row, query.get('message'))):
                    return
                decision = row.get('kind') == 'decision'
                action = match[3]
                if match[1] != ('askcard' if decision else 'runask'):
                    return
                if decision:
                    if action not in ('later', 'decline') and not (action.startswith('pick') and 1 <= int(action[4:]) <= len(row['options'])):
                        return
                elif match[4] is not None or action not in ('approve', 'decline', 'cancel'):
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
                    self.display(row)
                    notice = 'Cancelled'
                elif action == 'approve' and row['weighty']:
                    row['pending'] = dict(expires=self.now() + 300, update=update_id)
                    self.save(row)
                    self.display(row, f"type the ask's number, {row['ask']}, to approve this weighty card. Reply directly to this card with {row['ask']}.")
                    notice = f"Reply to this card with {row['ask']}"
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
            bindings = self.bound_messages(reply['message_id'])
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
                          or (row.get('pending') and self.authorized_message(row, message, reply=True))]
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
                pending = row['pending']
                row['pending'] = None
                self.save(row)  # consumed durably before the write, including crashes
                if self.settle(row, ask):
                    notice = 'Ask is ' + ask['state']
                elif self.now() > pending['expires']:
                    notice = 'Weighty confirmation expired; tap Approve on the card again'
                    self.display(row)
                elif not isinstance(message.get('text'), str) or message['text'].strip() != str(row['ask']):
                    notice = f"ask {row['ask']}'s number was not typed back, so nothing was done"
                    self.display(row)
                elif ask['state'] != 'open':
                    notice = 'Ask is ' + ask['state']
                else:
                    notice = self.decide(row, 'approve', True)
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
                    if not self.settle(row, ask) and row.get('pending') and self.now() > row['pending']['expires']:
                        row['pending'] = None
                        self.save(row)
                        self.display(row, 'Weighty confirmation expired; tap Approve again')
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

    def decide_notice(self, key, value, thread, reply=None):
        b = self.bridge
        for index, part in enumerate(chunks(value, 4000)):
            extra = {} if thread is None else dict(message_thread_id=thread)
            identity = dict(text=part, reply=reply if index == 0 else None, **extra)
            b._reserve_and_deliver(f'{key}:{index}', identity, lambda pending, part=part, index=index:
                                   b._deliver_text(part, reply if index == 0 else None, pending, extra=extra))

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
            reply = card[0] if card and card[1] == conversation['conversation_id'] else None
            self.decide_notice(f'decide:{update_id}:notice', notice, thread, reply)

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
                reply = card[0] if card and card[1] == conversation['conversation_id'] else None
                self.decide_notice(f'decide:{update_id}:notice', result, thread, reply)
        except NativeError as error:
            notice = self.decide_error(error)
            self.decide_notice(f'decide:{update_id}:notice', notice, thread)
        finally:
            self.bridge._answer_callback(update_id, query['id'], notice[:180], key=f'decide:{update_id}:answer')
