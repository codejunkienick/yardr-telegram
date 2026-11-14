"""Durable model selectors and native handoff cards.

The existing poller observes after inbound taps commit, about every 20 seconds
when Telegram is quiet. A shared three-second refresh budget bounds native I/O;
the core owns the 600-second wait and continuation even if the bridge stops.
"""
import contextlib
import ctypes
import datetime
import fcntl
import hashlib
from html import escape
import json
import os
import re
import secrets
import shutil
import subprocess
import sys
import time

import run_asks as n
import yard_commands as y

WAIT_SECONDS = 600
REFRESH_SECONDS = 3
TOKEN = r'[A-Za-z0-9_-]{16}'
PICK = re.compile(r'mayor-model:s:(' + TOKEN + r'):([a-z0-9][a-z0-9_-]{0,31})')
FORCE = re.compile(r'mayor-model:f:(' + TOKEN + r'):([1-9][0-9]{0,8})')
STATES = {'waiting', 'timed_out', 'handoff_written', 'restart_requested', 'restarting',
          'running', 'superseded', 'stopped'}
TERMINAL = {'running', 'superseded', 'stopped'}
NOT_FOUND = {'error': 'crew switch request: not found'}


class UnmatchedReceipt(y.MayorUnavailable):
    pass


def canonical(value):
    return n.canonical(value)


def initialize(db):
    # mayor_model_taps stays five columns for positional INSERTs in the previous release.
    db.execute('CREATE TABLE IF NOT EXISTS model_selectors (token TEXT PRIMARY KEY, update_id INTEGER UNIQUE, value TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS model_operations (token TEXT PRIMARY KEY, owner TEXT NOT NULL, active INTEGER NOT NULL, value TEXT NOT NULL)')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS model_operation_owner ON model_operations(owner) WHERE active=1')
    db.execute('CREATE TABLE IF NOT EXISTS model_actions (update_id INTEGER PRIMARY KEY, token TEXT NOT NULL, value TEXT NOT NULL)')


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise y.MayorUnavailable('invalid native switch timestamp')
    try:
        result = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.year < 2000:
            raise ValueError()
        return result
    except ValueError:
        raise y.MayorUnavailable('invalid native switch timestamp') from None


def binding(config, conversation):
    return dict(bot=config['bot_id'], account=config['account_id'], yard=config['yard'],
                source=config.get('yardr_source', 'tgbridge'), home=os.path.realpath(config['yardr_home']),
                operator=config['yardr_operator'], user=config['user_id'], chat=config['chat_id'],
                conversation=conversation)


def snapshot(config):
    return json.loads(canonical(config['yardr_mayor_model']))


def definition_of(crew, target):
    """Preserved native inputs, saved before admission without resolving any path."""
    return n.digest(dict(config={key: value for key, value in crew['config'].items() if key not in target},
                         runner=crew.get('runner'), dir=crew.get('dir')))


def selector_definition(crew):
    return n.digest(dict(config=crew['config'], runner=crew.get('runner'), dir=crew.get('dir'),
                         launch=crew.get('launch'), session=crew['session_generation'], enabled=crew.get('enabled')))


class _ProcBsdInfo(ctypes.Structure):
    # Apple's sys/proc_info.h: PROC_PIDTBSDINFO (3), microsecond start identity.
    _fields_ = [(name, ctypes.c_uint32) for name in
                'flags status xstatus pid ppid uid gid ruid rgid svuid svgid reserved'.split()] + [
        ('comm', ctypes.c_char * 16), ('name', ctypes.c_char * 32),
        ('nfiles', ctypes.c_uint32), ('pgid', ctypes.c_uint32), ('pjobc', ctypes.c_uint32),
        ('tdev', ctypes.c_uint32), ('tpgid', ctypes.c_uint32), ('nice', ctypes.c_int32),
        ('start_seconds', ctypes.c_uint64), ('start_microseconds', ctypes.c_uint64)]


def _process_start(pid):
    if sys.platform == 'darwin':
        info = _ProcBsdInfo()
        query = ctypes.CDLL('/usr/lib/libproc.dylib', use_errno=True).proc_pidinfo
        query.argtypes = [ctypes.c_int, ctypes.c_int, ctypes.c_uint64, ctypes.c_void_p, ctypes.c_int]
        query.restype = ctypes.c_int
        if (query(pid, 3, 0, ctypes.byref(info), ctypes.sizeof(info)) != ctypes.sizeof(info)
                or info.pid != pid or info.start_microseconds >= 1000000):
            return None
        return info.start_seconds * 1000000 + info.start_microseconds
    with open(f'/proc/{pid}/stat') as stream:
        return int(stream.read().rsplit(')', 1)[1].split()[19])


def _lsof_cwd(pid):
    lsof = shutil.which('lsof', path=os.pathsep.join(
        part for part in (os.environ.get('PATH', ''), '/usr/sbin', '/sbin') if part))
    if not lsof:
        return None
    try:
        result = subprocess.run([lsof, '-n', '-P', '-w', '-a', '-p', str(pid), '-d', 'cwd', '-Fn'],
                                stdin=subprocess.DEVNULL, capture_output=True, text=True, timeout=2)
    except (OSError, subprocess.SubprocessError):
        return None
    if result.returncode != 0:
        return None
    seen_pid = False
    for line in result.stdout.splitlines():
        if line == f'p{pid}':
            seen_pid = True
        elif line.startswith('p'):
            seen_pid = False
        elif seen_pid and line.startswith('n/'):
            return line[1:]
    return None


def server_cwd(config):
    """Best-effort native server working directory from its verified pidfile."""
    home = config.get('yardr_home')
    if not home:
        return None
    try:
        with open(os.path.join(home, 'server.pid')) as stream:
            pid = int(stream.read().strip())
        if pid <= 0:
            return None
        start_path = os.path.join(home, 'server.pid.start')
        if os.path.exists(start_path):
            with open(start_path) as stream:
                expected = int(stream.read().strip())
            if _process_start(pid) != expected:
                return None
        if sys.platform != 'darwin':
            try:
                return os.readlink(f'/proc/{pid}/cwd')
            except OSError:
                pass
        return _lsof_cwd(pid)
    except (OSError, ValueError, IndexError):
        return None


def launch_matches(config, crew, parsed):
    """Compare the v1 native launch digest, without opening any environment file."""
    launch = crew.get('launch')
    if not launch or (launch['kind'], launch['model']) != (parsed['kind'], parsed['model']):
        return False
    env = parsed['env_file'] or ''
    paths = {env}
    if env:
        env = os.path.expanduser(env)
        if os.path.isabs(env):
            paths = {os.path.abspath(env)}
        else:
            cwd = server_cwd(config)
            roots = {config['yardr_home'], os.environ.get('HOME') or config['yardr_home']}
            if cwd is not None:
                # Prefer the live server's cwd; keep fallback roots only when they name that directory.
                roots = {cwd} | {root for root in roots if os.path.realpath(root) == os.path.realpath(cwd)}
            paths = {os.path.abspath(os.path.join(root, env)) for root in roots}
    settings = crew['config']
    for path in paths:
        values = dict(Runner=crew.get('runner', ''), Dir=crew.get('dir', ''), Kind=parsed['kind'],
                      Model=parsed['model'], Env=path, Args=parsed['args'] or None,
                      Prompt=settings.get('prompt'), Provider=settings.get('provider'),
                      Command=settings.get('env_command'), Skills=settings.get('skills'), Trust=settings.get('trust'))
        raw = '{' + ','.join(json.dumps(key) + ':' + json.dumps(value, sort_keys=True, ensure_ascii=False,
            separators=(',', ':'), allow_nan=False) for key, value in values.items()) + '}'
        for char, escaped in (('&', '\\u0026'), ('<', '\\u003c'), ('>', '\\u003e'),
                              ('\u2028', '\\u2028'), ('\u2029', '\\u2029')):
            raw = raw.replace(char, escaped)
        if hashlib.sha256(raw.encode()).hexdigest() == launch['fingerprint']:
            return True
    return False


def native_error(out):
    try:
        value = y._strict_json(out)
    except y.Unreadable:
        return None
    if type(value) is dict and set(value) == {'error'} and type(value['error']) is str and value['error'].strip():
        return y.clean(value['error'], 320)
    return None


def read_json(config, args, deadline, log):
    status, out, problem = y._run_logged(config, y._crew_argv(config, *args), deadline, 'switch read', log)
    if problem or status != 0:
        raise y.MayorUnavailable('native switch unavailable')
    try:
        return y._strict_json(out)
    except y.Unreadable:
        raise y.MayorUnavailable('invalid native switch response') from None


def capability(config, deadline, log):
    try:
        value = read_json(config, ['crew', 'switch', 'capabilities', '--json'], deadline, log)
    except y.MayorUnavailable:
        raise y.MayorUnavailable('switch_model requires crew_handoff_switch v1; capability unavailable') from None
    if not isinstance(value, dict) or set(value) != {'crew_handoff_switch'} or type(value.get('crew_handoff_switch')) is not int or value['crew_handoff_switch'] != 1:
        raise y.MayorUnavailable('switch_model requires crew_handoff_switch v1')


def read_current(config, deadline, log):
    value = read_json(config, ['crew', 'show', config['yardr_mayor_model']['crew'], '--json'], deadline, log)
    # Reuse the established strict config reader, then retain only known structured telemetry.
    parsed = y.parse_crew(json.dumps(value).encode())
    crew = value['crew']
    if (crew.get('name') != config['yardr_mayor_model']['crew'] or not isinstance(crew.get('session_generation'), str)
            or crew['session_generation'] and not y.REVISION_RE.fullmatch(crew['session_generation'])):
        raise y.MayorUnavailable('invalid native crew generation')
    if crew.get('state') not in {'running', 'stopped', 'failed'} or crew.get('status', '') not in {'', 'working', 'idle', 'blocked', 'unknown'}:
        raise y.MayorUnavailable('invalid native crew status')
    for key in ('context_pct', 'context_window', 'failures'):
        if key in crew and (type(crew[key]) is not int or crew[key] < 0):
            raise y.MayorUnavailable('invalid native crew telemetry')
    if crew.get('context_pct', 0) > 100:
        raise y.MayorUnavailable('invalid native crew telemetry')
    if crew.get('context_at'):
        timestamp(crew['context_at'])
    if 'switch' in crew and crew['switch'] is not None:
        validate_operation(crew['switch'])
    launch = crew.get('launch')
    if launch is not None and (type(launch) is not dict or set(launch) != {'kind', 'model', 'fingerprint'}
            or any(type(value) is not str for value in launch.values())
            or not re.fullmatch('[0-9a-f]{64}', launch['fingerprint'])):
        raise y.MayorUnavailable('invalid native actual launch')
    for key in ('login_screen', 'enabled'):
        if key in crew and type(crew[key]) is not bool:
            raise y.MayorUnavailable('invalid native crew status')
    return crew, parsed


def validate_operation(op):
    required = dict(id=str, generation=str, crew=str, actor=str, previous_generation=str, target=dict,
                    mode=str, state=str, unresolved=bool, accepted_at=str, deadline=str)
    optional = dict(nudged_at=str, timed_out_at=str, handoff_written_at=str, handoff_note=int,
                    restarting_at=str, running_at=str, new_generation=str,
                    previous_ended_without_handoff=bool, reason=str)
    # The v1 public schema omits absent milestones; null and bool-as-int are not evidence.
    if (type(op) is not dict or not required.keys() <= op.keys() or op.keys() - (required.keys() | optional.keys())
            or any(type(op[key]) is not kind for key, kind in required.items())
            or any(type(op[key]) is not kind for key, kind in optional.items() if key in op)):
        raise y.MayorUnavailable('invalid native switch status')
    if op['state'] not in STATES or op['mode'] not in {'handoff', 'force'}:
        raise y.MayorUnavailable('invalid native switch status')
    if op['unresolved'] == (op['state'] in TERMINAL):
        raise y.MayorUnavailable('invalid native switch status')
    for key in ('id', 'generation', 'crew', 'actor', 'previous_generation'):
        if not op[key] or len(op[key]) > 128:
            raise y.MayorUnavailable('invalid native switch identity')
    target = op.get('target')
    if set(target) != {'kind', 'model', 'fingerprint'} or any(
            type(v) is not str for v in target.values()) or not re.fullmatch('[0-9a-f]{64}', target['fingerprint']):
        raise y.MayorUnavailable('invalid native switch target')
    accepted, deadline = timestamp(op.get('accepted_at')), timestamp(op.get('deadline'))
    if (deadline - accepted).total_seconds() != WAIT_SECONDS:
        raise y.MayorUnavailable('invalid native switch deadline')
    for key in ('nudged_at', 'timed_out_at', 'handoff_written_at', 'restarting_at', 'running_at'):
        if key in op:
            timestamp(op[key])
    if 'handoff_note' in op and not 0 <= op['handoff_note'] <= 2**64 - 1:
        raise y.MayorUnavailable('invalid native handoff evidence')
    if op.get('handoff_written_at') and (type(op.get('handoff_note')) is not int or op['handoff_note'] <= 0):
        raise y.MayorUnavailable('invalid native handoff evidence')
    if (op['state'] == 'timed_out' and not op.get('timed_out_at')
            or op['state'] == 'handoff_written' and not op.get('handoff_written_at')
            or op['state'] == 'restarting' and not op.get('restarting_at')
            or op['state'] == 'restart_requested' and op['mode'] != 'force'):
        raise y.MayorUnavailable('invalid native switch evidence')
    if op['state'] == 'running' and (not op.get('running_at') or not op.get('new_generation')
                                      or op['new_generation'] == op['previous_generation']):
        raise y.MayorUnavailable('invalid native running proof')


def receipt(value, row, request_id=None, *, admission=False):
    """Reject an unrelated/colliding receipt before giving it any lifecycle authority."""
    if (type(value) is not dict or set(value) != {'request_id', 'existing', 'operation'}
            or type(value['request_id']) is not str or type(value['existing']) is not bool):
        raise y.MayorUnavailable('invalid native switch receipt')
    if value['request_id'] != (request_id or row['request']['request_id']):
        raise y.MayorUnavailable('conflicting native switch receipt')
    op = value['operation']
    validate_operation(op)
    if (op['crew'] != row['crew'] or op['actor'] != row['binding']['operator']
            or op['previous_generation'] != row['request']['session_generation']
            or any(op['target'][key] != row['request']['target'][key] for key in ('kind', 'model'))):
        raise y.MayorUnavailable('conflicting native switch operation')
    if row.get('receipt') and row.get('admission_confirmed') is True:
        original = row['receipt']['operation']
        if any(op[key] != original[key] for key in ('id', 'generation', 'target', 'accepted_at', 'deadline')):
            raise y.MayorUnavailable('conflicting native switch operation')
    else:
        # V1 lookups expose launch identity, not the complete admitted request.
        if not admission:
            raise UnmatchedReceipt('Native request could not be matched; operator reconciliation required. Nothing retried.')
        if not value['existing'] and op['id'] != row['request']['request_id']:
            raise y.MayorUnavailable('conflicting native switch operation')
    previous = row.get('observed')
    if previous:
        for key in ('nudged_at', 'timed_out_at', 'handoff_written_at', 'handoff_note', 'restarting_at', 'running_at', 'new_generation', 'reason'):
            if previous.get(key) is not None and op.get(key) != previous[key]:
                raise y.MayorUnavailable('stale native switch observation')
        if (previous['state'] in TERMINAL and op['state'] != previous['state']
                or previous['mode'] == 'force' and op['mode'] != 'force'
                or previous.get('previous_ended_without_handoff') and not op.get('previous_ended_without_handoff')):
            raise y.MayorUnavailable('stale native switch observation')
    return value


def admission_argv(config, row):
    req = row['request']
    return y.set_argv(config, row['pick'], req['revision']) + [
        '--handoff', '--request-id', req['request_id'], '--session-generation', req['session_generation'],
        '--wait-seconds', str(WAIT_SECONDS), '--json']


def current_lines(crew, parsed):
    launch = crew.get('launch')
    if launch is None:
        assignment = 'Current launch unavailable.'
    else:
        if not isinstance(launch, dict) or set(launch) != {'kind', 'model', 'fingerprint'} or any(
                not isinstance(v, str) for v in launch.values()):
            raise y.MayorUnavailable('invalid native actual launch')
        assignment = 'Current: ' + y.clean(launch['kind'], 32) + ' · ' + y.clean(launch['model'], 128)
    return [assignment, 'Session: ' + (parsed['status'] or 'unknown')]


def dry_run(config, pick_id, log):
    mayor = config['yardr_mayor_model']
    pick = next((item for item in mayor['picks'] if item['id'] == pick_id), None)
    if pick is None:
        raise y.MayorUnavailable('unknown pick in [yardr.mayor_model]')
    deadline = time.monotonic() + mayor['timeout']
    capability(config, deadline, log)
    crew, parsed = read_current(config, deadline, log)
    lines = current_lines(crew, parsed)
    active = crew.get('switch')
    if active is not None:
        if not isinstance(active, dict) or type(active.get('unresolved')) is not bool:
            raise y.MayorUnavailable('invalid native switch status')
        if active['unresolved']:
            same = y.has_pick(pick, parsed)
            return '\n'.join(lines + ["A switch is already pending: " + (
                'this pick acknowledges the existing operation/card; no new admission or deadline.' if same else
                'a different pick is refused; no replacement or queue.')])
    return '\n'.join(lines + [
        'Target: ' + y.clean(pick['label'], 64),
        'A saved native launch proof plus an unchanged complete definition may acknowledge this exact pick without admission.',
        'One guarded atomic crew set --handoff admission uses the observed revision and session generation.',
        'Flags: --request-id <stable-id> --revision ' + parsed['revision'] +
        ' --session-generation ' + crew['session_generation'] + ' --wait-seconds 600 --json.',
        'Save kind/model/complete args and optional env_file before requesting the safe handoff; omitted env_file is preserved.',
        'Fixed 600-second deadline; no automatic force. Core owns continuation even while the bridge is down.',
        'Force apply is available immediately for the positively identified unresolved operation and generation,',
        'using a separate stable action ID: crew restart --switch <operation-id> --generation <generation> --action-id <action-id>.',
        'Force bypasses the handoff and schedules ordinary restart, waiting for idle when applicable.',
        'This dry run reserves nothing and makes no Telegram or native mutation.'])


class ModelSwitch:
    def __init__(self, bridge):
        self.bridge = bridge
        self.recovered = set()

    @property
    def config(self):
        return self.bridge.config

    def load(self, table, key, value):
        with self.bridge.store.connect() as db:
            row = db.execute(f'SELECT value FROM {table} WHERE {key}=?', (value,)).fetchone()
        return json.loads(row[0]) if row else None

    def save(self, row):
        with self.bridge.store.connect() as db:
            db.execute('UPDATE model_operations SET active=?, value=? WHERE token=?',
                       (int(row['active']), canonical(row), row['token']))

    def active(self):
        with self.bridge.store.connect() as db:
            return [json.loads(row[0]) for row in db.execute('SELECT value FROM model_operations WHERE active=1')]

    def running_pick(self, crew, parsed, pick):
        op = crew.get('switch')
        if crew['state'] != 'running' or not y.has_pick(pick, parsed):
            return False
        if launch_matches(self.config, crew, parsed):
            return True
        if not op or op['state'] != 'running' or crew.get('launch') != op['target']:
            return False
        with self.bridge.store.connect() as db:
            for item in db.execute('SELECT value FROM model_operations WHERE active=0'):
                row = json.loads(item[0])
                original = (row.get('receipt') or {}).get('operation')
                if (original and row.get('admission_confirmed') is True and row['owner'] == self.owner() and self.bound(row)
                        and all(original[key] == op[key] for key in ('id', 'generation', 'target'))
                        and y.has_pick(row['pick'], parsed)
                        and row.get('definition') == definition_of(crew, row['request']['target'])):
                    return True
        return False

    def bound(self, row):
        return row['binding'] == binding(self.config, row['binding']['conversation']) and bool(
            self.bridge.conversation_for_id(row['binding']['conversation']))

    def owner(self):
        # Native ownership identifies saved launch proof; pending ownership spans the ledger.
        return canonical([os.path.realpath(self.config['yardr_home']), self.config['yardr_mayor_model']['crew']])

    def verify(self):
        n.verify_operator(self.bridge.run_asks.client, self.config)

    def answer(self, uid, query, text, alert=False):
        return self.bridge._answer_callback(uid, query, text, alert)

    @contextlib.contextmanager
    def locked(self):
        path = self.bridge.store.path + '.model-switch.lock'
        with open(path, 'a') as stream:
            os.chmod(path, 0o600)
            fcntl.flock(stream, fcntl.LOCK_EX)
            try:
                yield
            finally:
                fcntl.flock(stream, fcntl.LOCK_UN)

    def command(self, uid, payload, query=None, thread=None):
        with self.locked():
            return self._command(uid, payload, query, thread)

    def _command(self, uid, payload, query=None, thread=None):
        try:
            if query is not None:
                return self.callback(uid, payload, query, thread)
            if payload['command'] == 'mayor_model':
                return self.bridge._command_message(f'command:{uid}:0', dict(
                    type='send', text='Use /switch_model to switch models.', plain=None), thread)
            return self.selector(uid, thread)
        except (y.MayorUnavailable, n.NativeError):
            if query is not None:
                self.answer(uid, query, 'Switch unavailable; nothing retried.', True)
            else:
                self.bridge._command_message(f'command:{uid}:notice', dict(
                    type='send', text='Model switch unavailable; check the native capability and operator mapping.', plain=None), thread)
        except Exception:
            self.bridge._log(f'command switch_model failed (update {uid}): internal error')
            if query is not None:
                self.answer(uid, query, 'Switch outcome unknown; nothing retried.', True)

    def selector(self, uid, thread):
        conversation = self.bridge.decide_conversation(thread)
        if conversation is None:
            raise y.MayorUnavailable('conversation not served')
        current_binding = binding(self.config, conversation['conversation_id'])
        for op in self.active():
            return self.bridge._command_message(f'command:{uid}:0', dict(type='send', plain=None,
                text=f'A switch is already pending. See its original card (message {op["message"]}).'), thread)
        deadline = time.monotonic() + self.config['yardr_mayor_model']['timeout']
        capability(self.config, deadline, self.bridge._log)
        crew, parsed = read_current(self.config, deadline, self.bridge._log)
        row = self.load('model_selectors', 'update_id', uid)
        if row is None:
            row = dict(token=secrets.token_urlsafe(12), binding=current_binding, snapshot=snapshot(self.config),
                       generation=1, message=None, retired=False, update_id=uid,
                       current=current_lines(crew, parsed), definition=selector_definition(crew))
            with self.bridge.store.connect() as db:
                db.execute('INSERT OR IGNORE INTO model_selectors VALUES (?, ?, ?)', (row['token'], uid, canonical(row)))
            row = self.load('model_selectors', 'update_id', uid)
        if row['binding'] != current_binding or row['snapshot'] != snapshot(self.config):
            raise y.MayorUnavailable('selector out of date')
        if row.get('notice'):
            return self.display_selector(row)
        keyboard = [[dict(text=pick['label'], callback_data=f'mayor-model:s:{row["token"]}:{pick["id"]}')]
                    for pick in row['snapshot']['picks']]
        keyboard = self.bridge._command_keyboard(keyboard)
        action = dict(type='send', format='html', text='<pre>' + escape('\n'.join(row['current'])) + '</pre>',
                      plain='\n'.join(row['current']), keyboard=keyboard)
        delivered = self.bridge._command_message(f'command:{uid}:0', action, thread)
        if delivered.get('delivered'):
            row['message'] = int(delivered['message_id'])
            with self.bridge.store.connect() as db:
                db.execute('UPDATE model_selectors SET value=? WHERE token=?', (canonical(row), row['token']))

    def callback(self, uid, payload, query, thread):
        data = payload.get('callback_data', '')
        select, force = PICK.fullmatch(data), FORCE.fullmatch(data)
        if not select and not force:
            return self.answer(uid, query, 'That model button is out of date.', True)
        conversation = self.bridge.decide_conversation(thread)
        if conversation is None:
            return self.answer(uid, query, 'That model button is out of date.', True)
        token = (select or force)[1]
        row = self.load('model_selectors' if select else 'model_operations', 'token', token)
        if (row is None or not self.bound(row) or row['binding']['conversation'] != conversation['conversation_id']
                or row['message'] != payload.get('callback_message_id')):
            return self.answer(uid, query, 'That model button is out of date.', True)
        # Every new mutation verifies today's native mapping, including Force.
        self.verify()
        if force:
            return self.force(uid, query, row, int(force[2]))
        if row['snapshot'] != snapshot(self.config):
            return self.answer(uid, query, 'That model button is out of date.', True)
        pick = next((p for p in row['snapshot']['picks'] if p['id'] == select[2]), None)
        if pick is None:
            return self.answer(uid, query, 'That model button is out of date.', True)
        previous = self.bridge.store.mayor_tap(uid)
        if previous:
            action = self.load('model_actions', 'update_id', uid)
            if action and action['kind'] == 'noop':
                if action.get('notice'):
                    self.display_selector(row)
                return self.answer(uid, query, action.get('notice', 'Already running this exact pick.'))
            if action and action['kind'] == 'refused':
                return self.answer(uid, query, action['notice'], True)
            target = self.load('model_operations', 'token', action['token']) if action else None
            if target:
                return self.ack_existing(uid, query, target, pick)
            return self.answer(uid, query, 'That tap is already recorded; nothing retried.', True)
        if row['retired']:
            return self.answer(uid, query, 'That model button is out of date.', True)
        for active in self.active():
            return self.reserve_existing(uid, query, active, pick)
        deadline = time.monotonic() + self.config['yardr_mayor_model']['timeout']
        capability(self.config, deadline, self.bridge._log)
        crew, parsed = read_current(self.config, deadline, self.bridge._log)
        # An external unresolved native switch also takes precedence over desired equality.
        if (crew.get('switch') or {}).get('unresolved'):
            return self.reserve_refusal(uid, query, row, pick)
        if row.get('definition') != selector_definition(crew):
            return self.reserve_refusal(uid, query, row, pick,
                'That model button is out of date; crew configuration or session changed.')
        if self.running_pick(crew, parsed, pick):
            notice = 'Already running ' + y.clean(pick['label'], 64) + '; nothing to switch.'
            with self.bridge.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                tap = db.execute('SELECT 1 FROM mayor_model_taps WHERE update_id=?', (uid,)).fetchone()
                existing = db.execute('SELECT value FROM model_operations WHERE active=1 LIMIT 1').fetchone()
                if not tap and not existing:
                    db.execute("INSERT INTO mayor_model_taps VALUES (?, ?, ?, 'done', ?)",
                               (uid, pick['id'], row['snapshot']['crew'], now().isoformat()))
                    db.execute('INSERT INTO model_actions VALUES (?, ?, ?)',
                               (uid, row['token'], canonical(dict(token=row['token'], kind='noop', notice=notice))))
                    row.update(retired=True, notice=notice)
                    db.execute('UPDATE model_selectors SET value=? WHERE token=?', (canonical(row), row['token']))
            if existing and not tap:
                return self.reserve_existing(uid, query, json.loads(existing[0]), pick)
            result = self.answer(uid, query, notice)
            self.display_selector(row)
            return result
        if not crew['session_generation']:
            raise y.MayorUnavailable('no current native session')
        op_token = secrets.token_urlsafe(12)
        request = dict(request_id='tg-switch-' + secrets.token_hex(24), crew=row['snapshot']['crew'],
                       revision=parsed['revision'], session_generation=crew['session_generation'],
                       target=dict(kind=pick['kind'], model=pick['model'], args=pick['args']), wait_seconds=WAIT_SECONDS)
        if pick['env_file'] is not None:
            request['target']['env_file'] = pick['env_file']
        operation = dict(token=op_token, owner=self.owner(), active=True, crew=row['snapshot']['crew'],
                         binding=row['binding'], pick=pick, request=request, fingerprint=n.digest(dict(request=request, binding=row['binding'])), target=None,
                         definition=definition_of(crew, request['target']),
                         message=row['message'], generation=1, update_id=uid, accepted=now().isoformat(),
                         deadline=(now() + datetime.timedelta(seconds=WAIT_SECONDS)).isoformat(),
                         receipt=None, admission_confirmed=False, observed=None, current=current_lines(crew, parsed)[0],
                         telemetry=self.telemetry(crew), telemetry_at=now().isoformat(),
                         problem='Switch requested; native outcome not yet confirmed.', revision=0, display=None,
                         force_action=None, display_failure=None)
        self.verify()
        if not self.bound(row):
            raise y.MayorUnavailable('model selector binding changed')
        existing, duplicate = None, False
        with self.bridge.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            tap = db.execute('SELECT 1 FROM mayor_model_taps WHERE update_id=?', (uid,)).fetchone()
            existing = db.execute('SELECT value FROM model_operations WHERE active=1 LIMIT 1').fetchone()
            if not tap and not existing:
                db.execute("INSERT INTO mayor_model_taps VALUES (?, ?, ?, 'reserved', ?)", (uid, pick['id'], operation['crew'], now().isoformat()))
                db.execute('INSERT INTO model_operations VALUES (?, ?, 1, ?)', (op_token, self.owner(), canonical(operation)))
                db.execute('INSERT INTO model_actions VALUES (?, ?, ?)', (uid, op_token, canonical(dict(token=op_token, kind='admit'))))
            elif tap:
                duplicate = True
        if duplicate:
            return self.answer(uid, query, 'That tap is already recorded; nothing retried.')
        if existing:
            return self.reserve_existing(uid, query, json.loads(existing[0]), pick)
        # Reservation and complete request are durable before the only mutating native call.
        self.answer(uid, query, 'Switch requested; please wait for the handoff.')
        status, out, problem = y._run_logged(self.config, admission_argv(self.config, operation), deadline, 'handoff admission', self.bridge._log)
        try:
            if status != 0 or problem:
                raise y.MayorUnavailable('native admission not confirmed')
            self.observe(operation, y._strict_json(out), admission=True)
            self.bridge.store.record_mayor_tap(uid, 'done')
        except (y.MayorUnavailable, y.Unreadable):
            error = native_error(out) if problem is None and status is not None and status != 0 else None
            if error:
                for path in (parsed['env_file'], pick['env_file']):
                    if path:
                        error = error.replace(path, '[env_file]')
                error = y.clean(error, 320)
                operation['admission_error'] = error
                self.bridge._log('command mayor_model yardr handoff admission error: ' + error)
            if problem == 'not_started':
                self.refuse(operation, 'native command did not start.')
            else:
                operation['problem'] = 'Switch outcome unknown; reconcile the original native request. Nothing retried.'
                self.bridge.store.record_mayor_tap(uid, 'unknown')
                self.save(operation)
            if error:
                try:
                    self.lookup(operation, deadline)
                except (y.MayorUnavailable, y.Unreadable, n.NativeError):
                    pass
        self.display(operation)

    def display_selector(self, row):
        b = self.bridge
        with b.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            latest = json.loads(db.execute('SELECT value FROM model_selectors WHERE token=?', (row['token'],)).fetchone()[0])
            latest['display_attempted_at'] = now().isoformat()
            db.execute('UPDATE model_selectors SET value=? WHERE token=?', (canonical(latest), row['token']))
        text = '\n'.join([latest['notice']] + latest['current'])
        extra = dict(message_id=latest['message'], reply_markup={'inline_keyboard': []})
        html = '<pre>' + escape(text) + '</pre>'
        return b._reserve_and_deliver('model-selector:' + latest['token'] + ':complete',
            dict(text=html, binding=latest['binding'], **extra), lambda pending: b._deliver_text(
                html, None, pending, text, method='editMessageText', extra=extra, not_modified=True), retry_undelivered=True)

    def refuse(self, row, reason):
        row.update(active=False, refused=True, settled_at=now().isoformat(), problem='Switch refused; ' + reason,
                   receipt=None, observed=None, target=None)
        with self.bridge.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            db.execute('UPDATE model_operations SET active=0, value=? WHERE token=?', (canonical(row), row['token']))
            db.execute("UPDATE mayor_model_taps SET state='refused' WHERE update_id=?", (row['update_id'],))
            self.retire_selectors(db, row)

    def retire_selectors(self, db, row):
        for token, value in db.execute('SELECT token, value FROM model_selectors').fetchall():
            selector = json.loads(value)
            if (selector['binding'] == row['binding'] and selector['snapshot']['crew'] == row['crew']
                    and selector['update_id'] <= row['update_id']):
                selector['retired'] = True
                db.execute('UPDATE model_selectors SET value=? WHERE token=?', (canonical(selector), token))

    def reserve_refusal(self, uid, query, selector, pick, notice='A switch is already pending; use its original operation.'):
        with self.bridge.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM mayor_model_taps WHERE update_id=?', (uid,)).fetchone():
                db.execute("INSERT INTO mayor_model_taps VALUES (?, ?, ?, 'refused', ?)",
                           (uid, pick['id'], selector['snapshot']['crew'], now().isoformat()))
                db.execute('INSERT INTO model_actions VALUES (?, ?, ?)', (uid, selector['token'],
                           canonical(dict(token=selector['token'], kind='refused', notice=notice))))
        return self.answer(uid, query, notice, True)

    def reserve_existing(self, uid, query, row, pick):
        with self.bridge.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM mayor_model_taps WHERE update_id=?', (uid,)).fetchone():
                same = (self.bound(row) and row['crew'] == self.config['yardr_mayor_model']['crew']
                        and canonical(row['pick']) == canonical(pick))
                db.execute('INSERT INTO mayor_model_taps VALUES (?, ?, ?, ?, ?)',
                           (uid, pick['id'], row['crew'], 'done' if same else 'refused', now().isoformat()))
                db.execute('INSERT INTO model_actions VALUES (?, ?, ?)', (uid, row['token'], canonical(dict(token=row['token'], kind='existing'))))
        return self.ack_existing(uid, query, row, pick)

    def ack_existing(self, uid, query, row, pick):
        if not self.bound(row) or row['crew'] != self.config['yardr_mayor_model']['crew']:
            return self.answer(uid, query, 'A switch is already pending; reconcile its original binding/card.', True)
        if canonical(row['pick']) != canonical(pick):
            return self.answer(uid, query, 'A switch is already pending; a different pick is refused.', True)
        state = 'refused' if row.get('refused') else (row['observed']['state'] if row.get('observed') else 'unknown')
        return self.answer(uid, query, f'Switch {state}; see the original card (message {row["message"]}).')

    def observe(self, row, value, *, admission=False):
        value = receipt(value, row, admission=admission)
        if admission:
            row['receipt'] = value
            row['admission_confirmed'] = True
        row['target'] = value['operation']['target']
        row['observed'] = value['operation']
        row['active'] = value['operation']['unresolved']
        if not row['active']:
            row.setdefault('settled_at', now().isoformat())
            # A terminal receipt carries no current session telemetry.
            row['telemetry'] = {}
        if value['operation']['state'] == 'running':
            row['current'] = 'Current: ' + y.clean(row['pick']['label'], 64)
        elif value['operation']['state'] in {'superseded', 'stopped'}:
            row['current'] = 'Current launch unavailable.'
        if (row['force_action'] and value['operation']['mode'] == 'handoff'
                and value['operation']['state'] in {'waiting', 'timed_out'}):
            # A confirmed pending operation allows a fresh Force tap, never replay of the saved one.
            row['force_action'] = None
            row['generation'] += 1
        row['problem'] = None
        row['accepted'], row['deadline'] = value['operation']['accepted_at'], value['operation']['deadline']
        self.save(row)
        if not row['active']:
            with self.bridge.store.connect() as db:
                self.retire_selectors(db, row)
        # Admission reconciliation upgrades only the auxiliary lifecycle, never legacy tap semantics.

    def lookup(self, row, deadline):
        status, out, problem = y._run_logged(self.config, y._crew_argv(self.config, 'crew', 'switch', 'show',
            row['request']['request_id'], '--json'), deadline, 'switch read', self.bridge._log)
        if problem or status != 0:
            try:
                missing = not problem and status == 3 and y._strict_json(out) == NOT_FOUND
            except y.Unreadable:
                missing = False
            if missing and row.get('admission_confirmed') is not True:
                self.refuse(row, row.get('admission_error') or 'not admitted (native request not found).')
                return
            raise y.MayorUnavailable('native switch unavailable')
        value = y._strict_json(out)
        self.observe(row, value)

    def force(self, uid, query, row, generation):
        if generation != row['generation'] or not row['active']:
            return self.answer(uid, query, 'That Force button is out of date.', True)
        previous = self.bridge.store.mayor_tap(uid)
        if previous or row['force_action']:
            return self.answer(uid, query, 'Force already recorded; nothing retried.')
        deadline = time.monotonic() + self.config['yardr_mayor_model']['timeout']
        capability(self.config, deadline, self.bridge._log)
        self.lookup(row, deadline)
        op = row['observed']
        if not row['active'] or op['mode'] == 'force':
            self.display(row)
            return self.answer(uid, query, 'That Force button is out of date.', True)
        action = dict(token=row['token'], kind='force', action_id='tg-force-' + secrets.token_hex(24),
                      operation_id=op['id'], generation=op['generation'])
        self.verify()
        if not self.bound(row):
            raise y.MayorUnavailable('model operation binding changed')
        duplicate = False
        with self.bridge.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            latest = json.loads(db.execute('SELECT value FROM model_operations WHERE token=?', (row['token'],)).fetchone()[0])
            if (latest['generation'] != generation or latest['force_action'] or not latest['active']
                    or db.execute('SELECT 1 FROM mayor_model_taps WHERE update_id=?', (uid,)).fetchone()):
                duplicate = True
            else:
                row = latest
                row['force_action'] = action
                db.execute("INSERT INTO mayor_model_taps VALUES (?, ?, ?, 'reserved', ?)", (uid, row['pick']['id'], row['crew'], now().isoformat()))
                db.execute('INSERT INTO model_actions VALUES (?, ?, ?)', (uid, row['token'], canonical(action)))
                db.execute('UPDATE model_operations SET value=? WHERE token=?', (canonical(row), row['token']))
        if duplicate:
            return self.answer(uid, query, 'Force already recorded; nothing retried.')
        self.answer(uid, query, 'Force apply requested; please wait.')
        self.display(row)
        row = self.load('model_operations', 'token', row['token'])
        argv = y._crew_argv(self.config, 'crew', 'restart', row['crew'], '--switch', op['id'],
                             '--generation', op['generation'], '--action-id', action['action_id'], '--json', actor=True)
        status, out, problem = y._run_logged(self.config, argv, deadline, 'guarded force', self.bridge._log)
        try:
            if status != 0 or problem:
                raise y.MayorUnavailable('native force not confirmed')
            value = receipt(y._strict_json(out), row, action['action_id'])
            # Force receipt's request_id is its action ID; observation retains original admission ID.
            value = dict(value, request_id=row['request']['request_id'])
            self.observe(row, value)
            self.bridge.store.record_mayor_tap(uid, 'done')
        except (y.MayorUnavailable, y.Unreadable):
            row['problem'] = 'Force outcome unknown; original operation remains observable. Nothing retried.'
            self.bridge.store.record_mayor_tap(uid, 'unknown')
            self.save(row)
        self.display(row)

    def telemetry(self, crew):
        data = dict(state=crew['state'], status=crew.get('status') or 'unknown')
        for key in ('context_pct', 'context_window', 'context_at'):
            if key in crew:
                data[key] = crew[key]
        if crew.get('state') == 'failed' or crew.get('failures'):
            data['start'] = 'Start failed; native retry/backoff applies.'
        elif crew.get('login_screen') or crew.get('status') == 'blocked':
            data['start'] = 'Start/session blocked; operator attention may be needed.'
        elif crew.get('starting'):
            data['start'] = 'Restarting; start not yet confirmed.'
        return data

    def render(self, row):
        op = row.get('observed')
        state = op['state'] if op else 'unknown'
        if op and state == 'waiting' and now() >= timestamp(row['deadline']):
            state = 'timed_out'
        if row.get('refused'):
            state = 'refused'
        if op and row['active'] and row['force_action'] and not row.get('problem') and op['mode'] == 'handoff':
            state = 'force_requested'
        notice = {
            'refused': row.get('problem') or 'Switch refused.',
            'waiting': 'Please wait for the handoff.', 'timed_out': 'Handoff timed out; target remains saved. Force apply is available.',
            'handoff_written': 'Handoff written; please wait for the restart.',
            'force_requested': 'Force apply accepted, please wait. Ordinary restart waits for idle when applicable.',
            'restart_requested': 'Force apply accepted, please wait. Ordinary restart waits for idle when applicable.',
            'restarting': 'Restarting; please wait.', 'running': 'Running on target.',
            'superseded': 'Switch superseded by an external change.', 'stopped': 'Switch stopped externally.',
            'unknown': 'Switch outcome unknown; operator reconciliation required. Nothing retried.'}[state]
        lines = [notice, row['current'], 'Target: ' + y.clean(row['pick']['label'], 64), 'Status: ' + state]
        target = op['target'] if op else None
        lines.append('Target launch: ' + (y.clean(target['kind'], 32) + ' · ' + y.clean(target['model'], 128) if target else 'unknown'))
        lines.append('Target effort: unknown · env: unknown.')
        if row.get('problem') and row['problem'] != notice:
            lines.append(row['problem'])
        trail = ['requested'] if op else []
        for key, label in (('nudged_at', 'handoff requested from the session'), ('handoff_written_at', 'handoff written'),
                           ('restarting_at', 'restarting'), ('running_at', 'running on target')):
            if op and op.get(key):
                trail.append(label)
        if trail:
            lines.append(' → '.join(trail))
        if op and op.get('previous_ended_without_handoff'):
            lines.append('Previous session ended without handoff.')
        end = timestamp(op['running_at']) if op and op.get('running_at') else (timestamp(row.get('settled_at', row['accepted'])) if not row['active'] else now())
        elapsed = max(0, int((end - timestamp(row['accepted'])).total_seconds()))
        lines.append(f'Waited {elapsed}s · deadline {row["deadline"]}')
        data = row.get('telemetry') or {}
        stale = row['active'] and (now() - timestamp(row['telemetry_at'])).total_seconds() > 45
        lines.append('Session: ' + data.get('status', 'unknown') + (' (stale observation)' if stale else ''))
        if data.get('start'):
            lines.append(data['start'])
        if data.get('context_at'):
            window = f' of {data["context_window"]}' if data.get('context_window') else ''
            lines.append(f'Context: {data.get("context_pct", 0)}%{window} · read {data["context_at"]}')
        else:
            lines.append('Context unavailable.')
        keyboard = []
        if op and row['active'] and not row.get('problem') and not row['force_action'] and op['mode'] != 'force' and self.bound(row):
            keyboard = [[dict(text='Force apply', callback_data=f'mayor-model:f:{row["token"]}:{row["generation"]}')]]
        text = '<pre>' + escape('\n'.join(lines)) + '</pre>'
        if n.units(text) > 4096:
            raise y.MayorUnavailable('model card exceeds message limit')
        if keyboard:
            keyboard = self.bridge._command_keyboard(keyboard)
        return dict(text=text, plain='\n'.join(lines), keyboard=keyboard)

    def display(self, row):
        if not self.bound(row):
            row['display_failure'] = 'binding changed; original card not editable from this binding'
            self.save(row)
            return
        b = self.bridge
        # The lifecycle file lock spans presentation and delivery; the revision commits before Telegram.
        with b.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            latest = json.loads(db.execute('SELECT value FROM model_operations WHERE token=?', (row['token'],)).fetchone()[0])
            desired = self.render(latest)
            if latest['display'] != desired:
                latest['revision'] += 1
                latest['display'] = desired
            key = f'model-switch:{row["token"]}:message:{row["generation"]}:revision:{latest["revision"]}'
            latest['display_attempted_at'] = now().isoformat()
            db.execute('UPDATE model_operations SET value=? WHERE token=?', (canonical(latest), row['token']))
        extra = dict(message_id=latest['message'], reply_markup={'inline_keyboard': desired['keyboard']})
        identity = dict(text=desired['text'], binding=latest['binding'], **extra)
        result = b._reserve_and_deliver(key, identity, lambda pending: b._deliver_text(
            desired['text'], None, pending, desired['plain'], method='editMessageText', extra=extra, not_modified=True), retry_undelivered=True)
        latest['display_failure'] = None if result.get('delivered') else 'Card edit unconfirmed/unavailable; inspect native operation.'
        with b.store.connect() as db:
            stored = json.loads(db.execute('SELECT value FROM model_operations WHERE token=?', (row['token'],)).fetchone()[0])
            stored['display_failure'] = latest['display_failure']
            db.execute('UPDATE model_operations SET value=? WHERE token=?', (canonical(stored), row['token']))
        if latest['display_failure']:
            b._log('command switch_model: local display failure; native progress remains observable')

    def refresh(self):
        if not self.config.get('yardr_mayor_model'):
            return
        with self.locked():
            self._refresh()

    def _refresh(self):
        if not self.config.get('yardr_mayor_model'):
            return
        deadline = time.monotonic() + REFRESH_SECONDS
        with self.bridge.store.connect() as db:
            rows = [json.loads(item[0]) for item in db.execute('SELECT value FROM model_operations')]
        active = [row for row in rows if row['active']]
        active.sort(key=lambda row: row.get('display_attempted_at', ''))
        for row in active:
            if time.monotonic() >= deadline:
                break
            if not self.bound(row):
                row['display_failure'] = 'binding changed; no native call or card edit'
                self.save(row)
                continue
            try:
                if row['token'] not in self.recovered or not row['receipt'] or row.get('lookup_next'):
                    self.lookup(row, deadline)
                    self.recovered.add(row['token'])
                    row['lookup_next'] = False
                else:
                    observed_config = dict(self.config, yardr_mayor_model=dict(self.config['yardr_mayor_model'], crew=row['crew']))
                    crew, parsed = read_current(observed_config, deadline, self.bridge._log)
                    op = crew.get('switch')
                    if not isinstance(op, dict) or op.get('id') != row['receipt']['operation']['id']:
                        row['lookup_next'] = True
                        raise y.MayorUnavailable('native operation unavailable or superseded')
                    current = current_lines(crew, parsed)[0] if op['state'] in {'superseded', 'stopped'} else None
                    self.observe(row, dict(request_id=row['request']['request_id'], existing=row['receipt']['existing'], operation=op))
                    if current is not None:
                        row['current'] = current
                    row['telemetry'], row['telemetry_at'] = self.telemetry(crew), now().isoformat()
            except UnmatchedReceipt as exc:
                row['receipt'], row['observed'], row['target'] = None, None, None
                row['problem'] = str(exc)
            except (y.MayorUnavailable, y.Unreadable, n.NativeError):
                row['problem'] = 'Native observation unavailable/stale; nothing retried. Reconcile original request if unknown.'
            self.save(row)
            self.display(row)
        with self.bridge.store.connect() as db:
            selectors = [json.loads(item[0]) for item in db.execute('SELECT value FROM model_selectors')]
        # Terminal operation and completed-selector edits rotate fairly by last attempt under the shared budget.
        terminal = [('op', row) for row in rows if not row['active']] + [
            ('sel', row) for row in selectors if row.get('notice') and self.bound(row)]
        terminal.sort(key=lambda item: item[1].get('display_attempted_at', ''))
        for kind, row in terminal:
            if time.monotonic() >= deadline:
                break
            if kind == 'op':
                self.display(row)
            else:
                self.display_selector(row)
