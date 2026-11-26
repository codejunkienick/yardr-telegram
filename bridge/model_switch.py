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
PICK = re.compile(r'switch-model:s:(' + TOKEN + r'):([a-z0-9][a-z0-9_-]{0,31})')
FORCE = re.compile(r'switch-model:f:(' + TOKEN + r'):([1-9][0-9]{0,8})')
CREW = r'[A-Za-z0-9][A-Za-z0-9_-]{0,31}'
CREW_SELECT = re.compile(r'crew:c:(' + TOKEN + r'):(' + CREW + r')')
CREW_PICK = re.compile(r'crew:s:(' + TOKEN + r'):([a-z0-9][a-z0-9_-]{0,31})')
CREW_FORCE = re.compile(r'crew:f:(' + TOKEN + r'):([1-9][0-9]{0,8})')
PREFIX = r'(switch-model|crew)'
EFFORT_OPEN = re.compile(PREFIX + r':e:(' + TOKEN + r')')
EFFORT_APPLY = re.compile(PREFIX + r':el:(' + TOKEN + r'):([1-9][0-9]{0,8}):([a-z]{1,16})')
EFFORT_BACK = re.compile(PREFIX + r':eb:(' + TOKEN + r'):([1-9][0-9]{0,8})')
HANDOFF = re.compile(PREFIX + r':h:(' + TOKEN + r')')
LEVEL_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9_-]{0,31}')
STATES = {'waiting', 'timed_out', 'handoff_written', 'restart_requested', 'restarting',
          'running', 'superseded', 'stopped'}
TERMINAL = {'running', 'superseded', 'stopped'}
NOT_FOUND = {'error': 'crew switch request: not found'}


class UnmatchedReceipt(y.SwitchUnavailable):
    pass


def canonical(value):
    return n.canonical(value)


def initialize(db):
    db.execute('CREATE TABLE IF NOT EXISTS model_selectors (token TEXT PRIMARY KEY, update_id INTEGER UNIQUE, value TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS crew_selectors (token TEXT PRIMARY KEY, update_id INTEGER UNIQUE, value TEXT NOT NULL)')
    db.execute('CREATE TABLE IF NOT EXISTS model_operations (token TEXT PRIMARY KEY, owner TEXT NOT NULL, active INTEGER NOT NULL, value TEXT NOT NULL)')
    db.execute('CREATE UNIQUE INDEX IF NOT EXISTS model_operation_owner ON model_operations(owner) WHERE active=1')
    db.execute('CREATE TABLE IF NOT EXISTS model_actions (update_id INTEGER PRIMARY KEY, token TEXT NOT NULL, value TEXT NOT NULL)')


def now():
    return datetime.datetime.now(datetime.timezone.utc)


def timestamp(value):
    if not isinstance(value, str) or len(value) > 40:
        raise y.SwitchUnavailable('invalid native switch timestamp')
    try:
        result = datetime.datetime.fromisoformat(value.replace('Z', '+00:00'))
        if result.tzinfo is None or result.year < 2000:
            raise ValueError()
        return result
    except ValueError:
        raise y.SwitchUnavailable('invalid native switch timestamp') from None


def binding(config, conversation):
    return dict(bot=config['bot_id'], account=config['account_id'], yard=config['yard'],
                source=config.get('yardr_source', 'tgbridge'), home=os.path.realpath(config['yardr_home']),
                operator=config['yardr_operator'], user=config['user_id'], chat=config['chat_id'],
                conversation=conversation)


def snapshot(config):
    return crew_snapshot(config['yardr_switch_model'])


def crew_snapshot(entry):
    """One crew's canonical switch triple: labels never reach the guarded pipeline."""
    return json.loads(canonical({key: entry[key] for key in ('crew', 'timeout', 'picks')}))


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
        raise y.SwitchUnavailable('native switch unavailable')
    try:
        return y._strict_json(out)
    except y.Unreadable:
        raise y.SwitchUnavailable('invalid native switch response') from None


def capability(config, deadline, log):
    try:
        value = read_json(config, ['crew', 'switch', 'capabilities', '--json'], deadline, log)
    except y.SwitchUnavailable:
        raise y.SwitchUnavailable('switch_model requires crew_handoff_switch v1; capability unavailable') from None
    if not isinstance(value, dict) or set(value) != {'crew_handoff_switch'} or type(value.get('crew_handoff_switch')) is not int or value['crew_handoff_switch'] != 1:
        raise y.SwitchUnavailable('switch_model requires crew_handoff_switch v1')


def harness_effort(config, kind, deadline, log):
    """(levels, template, aliases) of the harness's reasoning effort, or None
    when it names none: `harness show` levels plus the flag template
    carrying the {level} slot and its flag aliases. Only levels a button can
    carry are offered."""
    value = read_json(config, ['harness', 'show', kind, '--json'], deadline, log)
    try:
        spec = value['flags']['effort']
        levels, template, aliases = spec['levels'], spec['args'], spec.get('aliases', [])
    except (KeyError, TypeError, AttributeError):
        return None
    if (not isinstance(levels, list) or not levels
            or any(not isinstance(item, str) or not LEVEL_RE.fullmatch(item) for item in levels)
            or len(set(levels)) != len(levels)
            or not isinstance(template, list) or not template
            or any(not isinstance(part, str) or not part or len(part) > 64 for part in template)
            or not any('{level}' in part for part in template)
            or not isinstance(aliases, list)
            or any(not isinstance(item, str) or not item or len(item) > 64 or not item.startswith('-')
                   for item in aliases)
            or len(set(aliases)) != len(aliases)):
        return None
    offered = [item for item in levels if re.fullmatch(r'[a-z]{1,16}', item)]
    return (offered, template, aliases) if offered else None


def codex_supported_levels(model, offered):
    """`offered` filtered to the levels `model` takes: a snapshot of `codex
    debug models --bundled` (0.167.2), where luna stops at max and gpt-5.5 at
    xhigh while the rest take the harness-wide list. Unknown models keep the
    harness list, so a new model is offered until the snapshot learns it."""
    lowered = model.lower()
    if model == 'gpt-5.5' or model.startswith('gpt-5.5-') or model.startswith('gpt-5.5.'):
        allowed = {'low', 'medium', 'high', 'xhigh'}
    elif model == 'codex-auto-review' or 'auto-review' in lowered:
        allowed = {'low', 'medium', 'high', 'xhigh', 'max'}
    elif 'luna' in lowered:
        allowed = {'low', 'medium', 'high', 'xhigh', 'max'}
    else:
        return list(offered)
    return [level for level in offered if level in allowed]


def agy_siblings(picks, model, offered):
    """{level: pick} of the configured sibling model IDs for an AGY `model`
    ending in -<level>, or None when the model names no level (effort then
    rides the flag). Only siblings the operator configured are used."""
    family, sep, _ = model.rpartition('-')
    if not sep or not family:
        return None
    levels = set(offered)
    current_suffix = model.rpartition('-')[2]
    if current_suffix not in levels:
        return None
    siblings = {}
    for pick in picks:
        if pick['kind'] != 'agy':
            continue
        fam, psep, suf = pick['model'].rpartition('-')
        if not psep or fam != family or suf not in levels:
            continue
        existing = siblings.get(suf)
        if existing is None or (existing.get('effort') != suf and pick.get('effort') == suf):
            siblings[suf] = pick
    return siblings


def sibling_level(model, offered):
    """The effort level an AGY `model` pins via its -<level> suffix, else
    None: the suffix counts only when it names an offered level."""
    family, sep, suffix = model.rpartition('-')
    if not sep or not family or suffix not in offered:
        return None
    return suffix


def read_current(config, deadline, log):
    value = read_json(config, ['crew', 'show', config['yardr_switch_model']['crew'], '--json'], deadline, log)
    # Reuse the established strict config reader, then retain only known structured telemetry.
    parsed = y.parse_crew(json.dumps(value).encode())
    crew = value['crew']
    if (crew.get('name') != config['yardr_switch_model']['crew'] or not isinstance(crew.get('session_generation'), str)
            or crew['session_generation'] and not y.REVISION_RE.fullmatch(crew['session_generation'])):
        raise y.SwitchUnavailable('invalid native crew generation')
    if crew.get('state') not in {'running', 'stopped', 'failed'} or crew.get('status', '') not in {'', 'working', 'idle', 'blocked', 'unknown'}:
        raise y.SwitchUnavailable('invalid native crew status')
    for key in ('context_pct', 'context_window', 'failures'):
        if key in crew and (type(crew[key]) is not int or crew[key] < 0):
            raise y.SwitchUnavailable('invalid native crew telemetry')
    if crew.get('context_at'):
        timestamp(crew['context_at'])
    if 'switch' in crew and crew['switch'] is not None:
        validate_operation(crew['switch'])
    launch = crew.get('launch')
    if launch is not None and (type(launch) is not dict or set(launch) != {'kind', 'model', 'fingerprint'}
            or any(type(value) is not str for value in launch.values())
            or not re.fullmatch('[0-9a-f]{64}', launch['fingerprint'])):
        raise y.SwitchUnavailable('invalid native actual launch')
    for key in ('login_screen', 'enabled'):
        if key in crew and type(crew[key]) is not bool:
            raise y.SwitchUnavailable('invalid native crew status')
    # The saved effort is the harness's exact setting: an AGY model suffix
    # where it pins one, else the exact flag or -c key. An unreadable
    # harness keeps the legacy reading for display; one naming no effort
    # leaves none. The launch model ID pins the running effort the same way.
    parsed['running_effort'] = None
    try:
        spec = harness_effort(config, parsed['kind'], deadline, log)
    except y.SwitchUnavailable:
        return crew, parsed
    if spec is not None:
        offered, template, aliases = spec
        if parsed['kind'] == 'agy':
            level = sibling_level(parsed['model'], offered)
            parsed['effort'] = level if level is not None else y.effort_of(parsed['args'], template, aliases)
        else:
            parsed['effort'] = y.effort_of(parsed['args'], template, aliases)
    else:
        parsed['effort'] = None
    launch = crew.get('launch')
    if launch is not None and launch.get('kind') == 'agy':
        if spec is not None and launch['kind'] == parsed['kind']:
            launch_offered = spec[0]
        else:
            try:
                launch_spec = harness_effort(config, launch['kind'], deadline, log)
            except y.SwitchUnavailable:
                launch_spec = None
            launch_offered = launch_spec[0] if launch_spec else ()
        parsed['running_effort'] = sibling_level(launch['model'], launch_offered)
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
        raise y.SwitchUnavailable('invalid native switch status')
    if op['state'] not in STATES or op['mode'] not in {'handoff', 'force'}:
        raise y.SwitchUnavailable('invalid native switch status')
    if op['unresolved'] == (op['state'] in TERMINAL):
        raise y.SwitchUnavailable('invalid native switch status')
    for key in ('id', 'generation', 'crew', 'actor', 'previous_generation'):
        if not op[key] or len(op[key]) > 128:
            raise y.SwitchUnavailable('invalid native switch identity')
    target = op.get('target')
    if set(target) != {'kind', 'model', 'fingerprint'} or any(
            type(v) is not str for v in target.values()) or not re.fullmatch('[0-9a-f]{64}', target['fingerprint']):
        raise y.SwitchUnavailable('invalid native switch target')
    accepted, deadline = timestamp(op.get('accepted_at')), timestamp(op.get('deadline'))
    if (deadline - accepted).total_seconds() != WAIT_SECONDS:
        raise y.SwitchUnavailable('invalid native switch deadline')
    for key in ('nudged_at', 'timed_out_at', 'handoff_written_at', 'restarting_at', 'running_at'):
        if key in op:
            timestamp(op[key])
    if 'handoff_note' in op and not 0 <= op['handoff_note'] <= 2**64 - 1:
        raise y.SwitchUnavailable('invalid native handoff evidence')
    if op.get('handoff_written_at') and (type(op.get('handoff_note')) is not int or op['handoff_note'] <= 0):
        raise y.SwitchUnavailable('invalid native handoff evidence')
    if (op['state'] == 'timed_out' and not op.get('timed_out_at')
            or op['state'] == 'handoff_written' and not op.get('handoff_written_at')
            or op['state'] == 'restarting' and not op.get('restarting_at')
            or op['state'] == 'restart_requested' and op['mode'] != 'force'):
        raise y.SwitchUnavailable('invalid native switch evidence')
    if op['state'] == 'running' and (not op.get('running_at') or not op.get('new_generation')
                                      or op['new_generation'] == op['previous_generation']):
        raise y.SwitchUnavailable('invalid native running proof')


def receipt(value, row, request_id=None, *, admission=False):
    """Reject an unrelated/colliding receipt before giving it any lifecycle authority."""
    if (type(value) is not dict or set(value) != {'request_id', 'existing', 'operation'}
            or type(value['request_id']) is not str or type(value['existing']) is not bool):
        raise y.SwitchUnavailable('invalid native switch receipt')
    if value['request_id'] != (request_id or row['request']['request_id']):
        raise y.SwitchUnavailable('conflicting native switch receipt')
    op = value['operation']
    validate_operation(op)
    if (op['crew'] != row['crew'] or op['actor'] != row['binding']['operator']
            or op['previous_generation'] != row['request']['session_generation']
            or any(op['target'][key] != row['request']['target'][key] for key in ('kind', 'model'))):
        raise y.SwitchUnavailable('conflicting native switch operation')
    if row.get('receipt') and row.get('admission_confirmed') is True:
        original = row['receipt']['operation']
        if any(op[key] != original[key] for key in ('id', 'generation', 'target', 'accepted_at', 'deadline')):
            raise y.SwitchUnavailable('conflicting native switch operation')
    else:
        # V1 lookups expose launch identity, not the complete admitted request.
        if not admission:
            raise UnmatchedReceipt('Native request could not be matched; operator reconciliation required. Nothing retried.')
        if not value['existing'] and op['id'] != row['request']['request_id']:
            raise y.SwitchUnavailable('conflicting native switch operation')
    previous = row.get('observed')
    if previous:
        for key in ('nudged_at', 'timed_out_at', 'handoff_written_at', 'handoff_note', 'restarting_at', 'running_at', 'new_generation', 'reason'):
            if previous.get(key) is not None and op.get(key) != previous[key]:
                raise y.SwitchUnavailable('stale native switch observation')
        if (previous['state'] in TERMINAL and op['state'] != previous['state']
                or previous['mode'] == 'force' and op['mode'] != 'force'
                or previous.get('previous_ended_without_handoff') and not op.get('previous_ended_without_handoff')):
            raise y.SwitchUnavailable('stale native switch observation')
    return value


def admission_argv(config, row):
    req = row['request']
    # The operation's own crew: set_argv reads only the switch crew of its config.
    crew = row.get('crew')
    if crew is None:
        crew = (config.get('yardr_switch_model') or {}).get('crew')
    switch = dict(config.get('yardr_switch_model') or {}, crew=crew)
    return y.set_argv(dict(config, yardr_switch_model=switch), row['pick'], req['revision']) + [
        '--handoff', '--request-id', req['request_id'], '--session-generation', req['session_generation'],
        '--wait-seconds', str(WAIT_SECONDS), '--json']


def effort_argv(config, crew, args, revision):
    """The config-only `crew set` an effort tap runs, as the operator: the
    crew's args with one effort level, revision-guarded, no handoff, so the
    session continues undisturbed and the next one starts at the new effort."""
    return y._crew_argv(config, 'crew', 'set', crew,
        'args=' + json.dumps(list(args), ensure_ascii=False, separators=(',', ':')),
        '--revision', revision, actor=True)


def sibling_argv(config, crew, sibling, revision):
    """The config-only `crew set` for an AGY sibling level: the configured
    sibling's model and complete args (and its env_file when it names one),
    revision-guarded, no handoff."""
    args = ['crew', 'set', crew, f'model={sibling["model"]}',
            'args=' + json.dumps(list(sibling['args']), ensure_ascii=False, separators=(',', ':'))]
    if sibling['env_file']:
        args.append(f'env_file={sibling["env_file"]}')
    return y._crew_argv(config, *args, '--revision', revision, actor=True)


def effort_line(parsed):
    effort = parsed.get('effort')
    return 'Effort: ' + (y.clean(effort, 32) if effort else 'unavailable.')


def context_line(crew):
    if crew.get('context_at'):
        window = f' of {crew["context_window"]}' if crew.get('context_window') else ''
        return f'Context: {crew.get("context_pct", 0)}%{window} · read {crew["context_at"]}'
    return 'Context unavailable.'


def launch_matches_saved(config, crew, parsed):
    """True when the running launch is the saved config: the saved effort is
    then the session's, else the running effort is unavailable and the saved
    level is for the next session."""
    if crew.get('state') != 'running':
        return False
    try:
        return launch_matches(config, crew, parsed)
    except Exception:
        return False


def effort_display(config, crew, parsed):
    """The selector's Effort line: the saved level when the running launch
    matches it, else the running level when the launch model ID pins one
    (an AGY -<level> suffix) with the saved level labelled for next, else
    unavailable with the saved level labelled for next."""
    saved = parsed.get('effort')
    if launch_matches_saved(config, crew, parsed):
        return effort_line(parsed)
    running = parsed.get('running_effort')
    if running and saved:
        return (f'Effort: {y.clean(running, 32)}. '
                f'Saved: {y.clean(saved, 32)} for the next session.')
    if running:
        return f'Effort: {y.clean(running, 32)}.'
    if not saved:
        return 'Effort: unavailable.'
    return f'Effort: unavailable. Saved: {y.clean(saved, 32)} for the next session.'


def current_lines(config, crew, parsed):
    launch = crew.get('launch')
    if launch is None:
        assignment = 'Current launch unavailable.'
    else:
        if not isinstance(launch, dict) or set(launch) != {'kind', 'model', 'fingerprint'} or any(
                not isinstance(v, str) for v in launch.values()):
            raise y.SwitchUnavailable('invalid native actual launch')
        assignment = 'Current: ' + y.clean(launch['kind'], 32) + ' · ' + y.clean(launch['model'], 128)
    return [assignment, 'Session: ' + (parsed['status'] or 'unknown'),
            effort_display(config, crew, parsed), context_line(crew)]


def stale_native_callback(bridge, data, message_id):
    """Retired native switch buttons whose route is off: their cards are
    retired, and no custom handler under the same prefix may receive them.
    Only a saved native token counts: on its own message, or on an ambiguous
    send whose message was never recorded. A custom handler's own
    native-shaped buttons have no such row and still dispatch."""
    config = bridge.config
    token, table = None, None
    if config.get('yardr_switch_model') is None:
        match = PICK.fullmatch(data)
        if match is not None:
            token, table = match[1], 'model_selectors'
        else:
            match = FORCE.fullmatch(data)
            if match is not None:
                token, table = match[1], 'model_operations'
    if token is None and config.get('yardr_switch_crews') is None:
        for pattern, name in ((CREW_SELECT, 'crew_selectors'), (CREW_PICK, 'model_selectors'),
                              (CREW_FORCE, 'model_operations')):
            match = pattern.fullmatch(data)
            if match is not None:
                token, table = match[1], name
                break
    if token is None:
        # Effort and handoff buttons name a model selector under either
        # prefix; each retires with its own route.
        for pattern in (EFFORT_OPEN, EFFORT_APPLY, EFFORT_BACK, HANDOFF):
            match = pattern.fullmatch(data)
            if match is None:
                continue
            route = 'yardr_switch_model' if match[1] == 'switch-model' else 'yardr_switch_crews'
            if config.get(route) is None:
                token, table = match[2], 'model_selectors'
            break
    if token is None:
        return False
    message_id = message_id if type(message_id) is int and message_id > 0 else None
    try:
        with bridge.store.connect() as db:
            row = db.execute(f'SELECT value FROM {table} WHERE token=?', (token,)).fetchone()
    except Exception:
        return False
    if row is None:
        return False
    try:
        saved = json.loads(row[0])
    except ValueError:
        return False
    # An ambiguous native send (Telegram accepted the card but its reply was
    # lost) leaves message None while the buttons are live; the saved token
    # still proves native provenance on any tapped message.
    return saved.get('message') is None or saved.get('message') == message_id


def route_action(data):
    """(kind, via, match) of an effort/handoff button, else None: `open` and
    `back` switch the selector's view, `apply` saves one level, `handoff`
    admits the current launch itself."""
    for pattern, kind in ((EFFORT_OPEN, 'open'), (EFFORT_APPLY, 'apply'),
                          (EFFORT_BACK, 'back'), (HANDOFF, 'handoff')):
        match = pattern.fullmatch(data)
        if match is not None:
            return kind, 'switch_model' if match[1] == 'switch-model' else 'crew', match
    return None


def dry_run(config, pick_id, log):
    switch = config['yardr_switch_model']
    pick = next((item for item in switch['picks'] if item['id'] == pick_id), None)
    if pick is None:
        raise y.SwitchUnavailable('unknown pick in [yardr.switch_model]')
    deadline = time.monotonic() + switch['timeout']
    capability(config, deadline, log)
    crew, parsed = read_current(config, deadline, log)
    lines = current_lines(config, crew, parsed)
    active = crew.get('switch')
    if active is not None:
        if not isinstance(active, dict) or type(active.get('unresolved')) is not bool:
            raise y.SwitchUnavailable('invalid native switch status')
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

    def crews(self):
        """Every switchable crew: the [yardr.switch_model] crew first, then each [[yardr.switch_crews]] entry."""
        switch = self.config.get('yardr_switch_model')
        entries = list(self.config.get('yardr_switch_crews') or ())
        if switch is not None:
            entries = [dict(crew=switch['crew'], label=switch['crew'], timeout=switch['timeout'],
                            picks=switch['picks'])] + entries
        return entries

    def crew_entry(self, name):
        for entry in self.crews():
            if entry['crew'] == name:
                return entry
        return None

    def effective(self, crew):
        """This config switching `crew`: reads and guarded writes take it as the switch table."""
        entry = self.crew_entry(crew)
        return dict(self.config, yardr_switch_model={key: entry[key] for key in ('crew', 'timeout', 'picks')})

    def expected_snapshot(self, crew):
        entry = self.crew_entry(crew)
        return None if entry is None else crew_snapshot(entry)

    def active(self, crew=None):
        # Scoped by crew, never by home: reopening the ledger on another home
        # cannot hide a pending operation of the same crew.
        with self.bridge.store.connect() as db:
            rows = [json.loads(row[0]) for row in db.execute('SELECT value FROM model_operations WHERE active=1')]
        return rows if crew is None else [row for row in rows if row['crew'] == crew]

    def guard_scope(self, crew):
        """Pending-operation guard scope for `crew`: None (every active
        operation) when only [yardr.switch_model] is configured, else that
        crew alone. Legacy setups admit one handoff at a time."""
        if self.config.get('yardr_switch_crews') is None:
            return None
        return crew

    @staticmethod
    def _pending_row(db, crew):
        """The active operation row blocking `crew`, if any: any active row
        when `crew` is None (the legacy single-operation guard), else that
        crew's. Same-crew races meet here."""
        for item in db.execute('SELECT value FROM model_operations WHERE active=1').fetchall():
            if crew is None or json.loads(item[0])['crew'] == crew:
                return item
        return None

    def running_pick(self, name, crew, parsed, pick):
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
                if (original and row.get('admission_confirmed') is True and row['owner'] == self.owner_for(name) and self.bound(row)
                        and all(original[key] == op[key] for key in ('id', 'generation', 'target'))
                        and y.has_pick(row['pick'], parsed)
                        and row.get('definition') == definition_of(crew, row['request']['target'])):
                    return True
        return False

    def bound(self, row):
        return row['binding'] == binding(self.config, row['binding']['conversation']) and bool(
            self.bridge.conversation_for_id(row['binding']['conversation']))

    def owner_for(self, crew):
        # Native ownership identifies saved launch proof; pending ownership spans the ledger.
        return canonical([os.path.realpath(self.config['yardr_home']), crew])

    def verify(self):
        n.verify_operator(self.bridge.yard_client(), self.config)

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
            return self.selector(uid, thread)
        except (y.SwitchUnavailable, n.NativeError):
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
        return self.open_selector(uid, thread, self.config['yardr_switch_model']['crew'], via='switch_model')

    def picks_keyboard(self, row, via):
        """One crew's model picks plus its effort and handoff controls."""
        prefix = y.CREW_PREFIX if via == 'crew' else y.SWITCH_MODEL_PREFIX
        keyboard = [[dict(text=pick['label'], callback_data=f'{prefix}s:{row["token"]}:{pick["id"]}')]
                    for pick in row['snapshot']['picks']]
        keyboard.append([dict(text='Change effort', callback_data=f'{prefix}e:{row["token"]}'),
                         dict(text='Handoff now', callback_data=f'{prefix}h:{row["token"]}')])
        return self.bridge._command_keyboard(keyboard)

    def open_selector(self, uid, thread, crew, via, edit=None, query=None, crew_token=None):
        """One crew's model picks: a new card for a text command, or the crew
        picker message edited in place after a crew tap (`edit`, `query`, `crew_token`)."""
        conversation = self.bridge.decide_conversation(thread)
        if conversation is None:
            raise y.SwitchUnavailable('conversation not served')
        current_binding = binding(self.config, conversation['conversation_id'])
        entry = self.crew_entry(crew)
        if entry is None:
            raise y.SwitchUnavailable('crew not configured')
        if edit is None:
            for op in self.active(self.guard_scope(crew)):
                return self.bridge._command_message(f'command:{uid}:0', dict(type='send', plain=None,
                    text=f'A switch is already pending. See its original card (message {op["message"]}).'), thread)
        effective = self.effective(crew)
        deadline = time.monotonic() + entry['timeout']
        capability(self.config, deadline, self.bridge._log)
        native, parsed = read_current(effective, deadline, self.bridge._log)
        row = self.load('model_selectors', 'update_id', uid)
        if row is None:
            row = dict(token=secrets.token_urlsafe(12), binding=current_binding, snapshot=crew_snapshot(entry),
                       generation=1, message=edit, retired=False, update_id=uid,
                       current=current_lines(self.config, native, parsed), definition=selector_definition(native),
                       effort_seq=0, effort_view=None)
            with self.bridge.store.connect() as db:
                db.execute('INSERT OR IGNORE INTO model_selectors VALUES (?, ?, ?)', (row['token'], uid, canonical(row)))
            row = self.load('model_selectors', 'update_id', uid)
        if (row['binding'] != current_binding or row['snapshot'] != crew_snapshot(entry)
                or (edit is not None and row['message'] != edit)):
            if query is not None:
                return self.answer(uid, query, 'That crew button is out of date.', True)
            raise y.SwitchUnavailable('selector out of date')
        if row.get('notice'):
            return self.display_selector(row)
        action = dict(type='send', format='html', text='<pre>' + escape('\n'.join(row['current'])) + '</pre>',
                      plain='\n'.join(row['current']), keyboard=self.picks_keyboard(row, via))
        if edit is not None:
            if crew_token is not None:
                claimed = self.load('crew_selectors', 'token', crew_token)
                if claimed is None:
                    return self.answer(uid, query, 'That crew button is out of date.', True)
                if claimed.get('selected_crew') is not None and (
                        claimed['selected_crew'] != crew or claimed.get('selected_token') not in (None, row['token'])):
                    return self.answer(uid, query, 'That crew button is out of date.', True)
                # Claim the listing message for this selector before editing it,
                # so a delayed tap for another crew cannot take the same card.
                claimed.update(selected_crew=crew, selected_token=row['token'])
                with self.bridge.store.connect() as db:
                    db.execute('UPDATE crew_selectors SET value=? WHERE token=?', (canonical(claimed), crew_token))
            self.answer(uid, query, '')
            return self.edit_selector(row, action)
        delivered = self.bridge._command_message(f'command:{uid}:0', action, thread)
        if delivered.get('delivered'):
            row['message'] = int(delivered['message_id'])
            with self.bridge.store.connect() as db:
                db.execute('UPDATE model_selectors SET value=? WHERE token=?', (canonical(row), row['token']))

    def edit_selector(self, row, action, view='picks'):
        b = self.bridge
        # Picks and effort views alternate on one message, so every render
        # takes a fresh key: a stable key would resend nothing (Back) or
        # refuse (a changed card). Identical repeats collapse via not_modified.
        row['render_seq'] = row.get('render_seq', 0) + 1
        with self.bridge.store.connect() as db:
            db.execute('UPDATE model_selectors SET value=? WHERE token=?', (canonical(row), row['token']))
        extra = dict(message_id=row['message'], reply_markup={'inline_keyboard': action['keyboard']})
        identity = dict(text=action['text'], binding=row['binding'], **extra)
        return b._reserve_and_deliver(f'model-selector:{row["token"]}:{view}:{row["render_seq"]}', identity,
            lambda pending: b._deliver_text(action['text'], None, pending, action['plain'],
                method='editMessageText', extra=extra, not_modified=True), retry_undelivered=True)

    def crew(self, uid, payload, query=None, thread=None):
        with self.locked():
            return self._crew(uid, payload, query, thread)

    def _crew(self, uid, payload, query=None, thread=None):
        try:
            if query is not None:
                return self.crew_callback(uid, payload, query, thread)
            return self.crew_text(uid, payload, thread)
        except (y.SwitchUnavailable, n.NativeError):
            if query is not None:
                self.answer(uid, query, 'Switch unavailable; nothing retried.', True)
            else:
                self.bridge._command_message(f'command:{uid}:notice', dict(
                    type='send', text='Crew switch unavailable; check the native capability and operator mapping.', plain=None), thread)
        except Exception:
            self.bridge._log(f'command crew failed (update {uid}): internal error')
            if query is not None:
                self.answer(uid, query, 'Switch outcome unknown; nothing retried.', True)

    def crew_text(self, uid, payload, thread):
        args = (payload.get('args') or '').strip()
        if not args:
            return self.crew_picker(uid, thread)
        if self.crew_entry(args) is None:
            names = ', '.join(entry['crew'] for entry in self.crews())
            return self.bridge._command_message(f'command:{uid}:0', dict(type='send', plain=None,
                text=f'Unknown crew {y.clean(args, 64)!r}; switchable: {names}.'), thread)
        return self.open_selector(uid, thread, args, via='crew')

    def crew_picker(self, uid, thread):
        conversation = self.bridge.decide_conversation(thread)
        if conversation is None:
            raise y.SwitchUnavailable('conversation not served')
        current_binding = binding(self.config, conversation['conversation_id'])
        entries = self.crews()
        deadline = time.monotonic() + max(entry['timeout'] for entry in entries)
        capability(self.config, deadline, self.bridge._log)
        lines = []
        for entry in entries:
            try:
                native, parsed = read_current(self.effective(entry['crew']), deadline, self.bridge._log)
            except (y.SwitchUnavailable, n.NativeError):
                lines.append(entry['label'] + ': model unavailable')
                continue
            launch = native.get('launch')
            current = 'launch unavailable' if launch is None else (
                y.clean(launch['kind'], 32) + ' · ' + y.clean(launch['model'], 128))
            if parsed.get('effort'):
                label = 'effort' if launch_matches_saved(self.config, native, parsed) else 'saved'
                current += f' · {label} ' + y.clean(parsed['effort'], 32)
            if native.get('context_at'):
                current += f' · ctx {native.get("context_pct", 0)}%'
            if (native.get('switch') or {}).get('unresolved'):
                current += ' · switch pending'
            lines.append(entry['label'] + ': ' + current)
        row = self.load('crew_selectors', 'update_id', uid)
        if row is None:
            row = dict(token=secrets.token_urlsafe(12), binding=current_binding,
                       snapshot=[crew_snapshot(entry) for entry in entries],
                       labels={entry['crew']: entry['label'] for entry in entries},
                       message=None, retired=False, update_id=uid, current=lines,
                       selected_crew=None, selected_token=None)
            with self.bridge.store.connect() as db:
                db.execute('INSERT OR IGNORE INTO crew_selectors VALUES (?, ?, ?)', (row['token'], uid, canonical(row)))
            row = self.load('crew_selectors', 'update_id', uid)
        if (row['binding'] != current_binding or row['snapshot'] != [crew_snapshot(entry) for entry in entries]
                or row['labels'] != {entry['crew']: entry['label'] for entry in entries}):
            raise y.SwitchUnavailable('crew picker out of date')
        keyboard = [[dict(text=entry['label'], callback_data=f'crew:c:{row["token"]}:{entry["crew"]}')]
                    for entry in entries]
        keyboard = self.bridge._command_keyboard(keyboard)
        action = dict(type='send', format='html', text='<pre>' + escape('\n'.join(row['current'])) + '</pre>',
                      plain='\n'.join(row['current']), keyboard=keyboard)
        delivered = self.bridge._command_message(f'command:{uid}:0', action, thread)
        if delivered.get('delivered'):
            row['message'] = int(delivered['message_id'])
            with self.bridge.store.connect() as db:
                db.execute('UPDATE crew_selectors SET value=? WHERE token=?', (canonical(row), row['token']))

    def crew_callback(self, uid, payload, query, thread):
        data = payload.get('callback_data', '')
        select = CREW_SELECT.fullmatch(data)
        if not select:
            # Crew picks and Force share the guarded pipeline with /switch_model.
            return self.callback(uid, payload, query, thread)
        conversation = self.bridge.decide_conversation(thread)
        if conversation is None:
            return self.answer(uid, query, 'That crew button is out of date.', True)
        return self.crew_select(uid, payload, query, thread, conversation, select)

    def crew_select(self, uid, payload, query, thread, conversation, select):
        token, crew = select[1], select[2]
        row = self.load('crew_selectors', 'token', token)
        if (row is None or not self.bound(row) or row['binding']['conversation'] != conversation['conversation_id']
                or row['message'] != payload.get('callback_message_id')):
            return self.answer(uid, query, 'That crew button is out of date.', True)
        # Every new mutation verifies today's native mapping, including crew selection.
        self.verify()
        entry = self.crew_entry(crew)
        if (entry is None or row['snapshot'] != [crew_snapshot(item) for item in self.crews()]
                or row['labels'] != {item['crew']: item['label'] for item in self.crews()}):
            return self.answer(uid, query, 'That crew button is out of date.', True)
        if row.get('selected_crew') is not None:
            if crew != row['selected_crew']:
                return self.answer(uid, query, 'That crew button is out of date.', True)
            # Repeat the claimed selection with its own ledger key; a pending
            # operation keeps its status card instead of being re-edited.
            if self.active(crew):
                return self.answer(uid, query, 'That crew button is out of date.', True)
            selector = self.load('model_selectors', 'token', row.get('selected_token')) if row.get('selected_token') else None
            if (selector is None or selector.get('notice') or selector.get('retired')
                    or selector['binding'] != binding(self.config, conversation['conversation_id'])
                    or selector['snapshot'] != crew_snapshot(entry) or selector['message'] != row['message']):
                return self.answer(uid, query, 'That crew button is out of date.', True)
            action = dict(type='send', format='html', text='<pre>' + escape('\n'.join(selector['current'])) + '</pre>',
                          plain='\n'.join(selector['current']), keyboard=self.picks_keyboard(selector, 'crew'))
            self.answer(uid, query, '')
            result = self.edit_selector(selector, action)
            if isinstance(result, dict) and result.get('delivered'):
                row['retired'] = True
                with self.bridge.store.connect() as db:
                    db.execute('UPDATE crew_selectors SET value=? WHERE token=?', (canonical(row), row['token']))
            return result
        if row['retired'] and self.load('model_selectors', 'update_id', uid) is None:
            return self.answer(uid, query, 'That crew button is out of date.', True)
        result = self.open_selector(uid, thread, crew, via='crew', edit=row['message'], query=query, crew_token=token)
        if isinstance(result, dict) and result.get('delivered'):
            row = self.load('crew_selectors', 'token', token) or row
            row['retired'] = True
            with self.bridge.store.connect() as db:
                db.execute('UPDATE crew_selectors SET value=? WHERE token=?', (canonical(row), row['token']))
        return result

    def callback(self, uid, payload, query, thread):
        data = payload.get('callback_data', '')
        routed = route_action(data)
        if routed is not None:
            kind, via, match = routed
            if kind == 'handoff':
                return self.handoff_now(uid, payload, query, thread, via, match[2])
            return self.effort_tap(uid, payload, query, thread, via, kind, match)
        select, force = PICK.fullmatch(data), FORCE.fullmatch(data)
        via = 'switch_model'
        if not select and not force:
            select, force = CREW_PICK.fullmatch(data), CREW_FORCE.fullmatch(data)
            via = 'crew'
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
        if via == 'switch_model':
            # The shortcut follows the primary: a reassigned primary retires
            # its old cards even when that crew stays configured elsewhere.
            primary = self.config.get('yardr_switch_model')
            if primary is None or row['snapshot'] != crew_snapshot(primary):
                return self.answer(uid, query, 'That model button is out of date.', True)
        elif row['snapshot'] != self.expected_snapshot(row['snapshot']['crew']):
            return self.answer(uid, query, 'That model button is out of date.', True)
        pick = next((p for p in row['snapshot']['picks'] if p['id'] == select[2]), None)
        if pick is None:
            return self.answer(uid, query, 'That model button is out of date.', True)
        previous = self.bridge.store.switch_tap(uid)
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
                return self.ack_existing(uid, query, target, pick, row['snapshot']['crew'])
            return self.answer(uid, query, 'That tap is already recorded; nothing retried.', True)
        if row['retired']:
            return self.answer(uid, query, 'That model button is out of date.', True)
        crew_name = row['snapshot']['crew']
        for active in self.active(self.guard_scope(crew_name)):
            return self.reserve_existing(uid, query, active, pick, crew_name)
        entry = self.crew_entry(crew_name)
        deadline = time.monotonic() + entry['timeout']
        capability(self.config, deadline, self.bridge._log)
        crew, parsed = read_current(self.effective(crew_name), deadline, self.bridge._log)
        # An external unresolved native switch also takes precedence over desired equality.
        if (crew.get('switch') or {}).get('unresolved'):
            return self.reserve_refusal(uid, query, row, pick)
        if row.get('definition') != selector_definition(crew):
            return self.reserve_refusal(uid, query, row, pick,
                'That model button is out of date; crew configuration or session changed.')
        if self.running_pick(crew_name, crew, parsed, pick):
            notice = 'Already running ' + y.clean(pick['label'], 64) + '; nothing to switch.'
            with self.bridge.store.connect() as db:
                db.execute('BEGIN IMMEDIATE')
                tap = db.execute('SELECT 1 FROM switch_model_taps WHERE update_id=?', (uid,)).fetchone()
                existing = self._pending_row(db, self.guard_scope(crew_name))
                if not tap and not existing:
                    db.execute("INSERT INTO switch_model_taps VALUES (?, ?, ?, 'done', ?)",
                               (uid, pick['id'], row['snapshot']['crew'], now().isoformat()))
                    db.execute('INSERT INTO model_actions VALUES (?, ?, ?)',
                               (uid, row['token'], canonical(dict(token=row['token'], kind='noop', notice=notice))))
                    row.update(retired=True, notice=notice)
                    db.execute('UPDATE model_selectors SET value=? WHERE token=?', (canonical(row), row['token']))
            if existing and not tap:
                return self.reserve_existing(uid, query, json.loads(existing[0]), pick, crew_name)
            result = self.answer(uid, query, notice)
            self.display_selector(row)
            return result
        return self.admit(uid, query, row, pick, crew_name, deadline, crew, parsed, via)

    def admit(self, uid, query, row, pick, crew_name, deadline, crew, parsed, via, handoff=False):
        """Reserve and run one guarded handoff admission: a model pick, or the
        current launch itself for Handoff now (`handoff`)."""
        if not crew['session_generation']:
            raise y.SwitchUnavailable('no current native session')
        op_token = secrets.token_urlsafe(12)
        request = dict(request_id='tg-switch-' + secrets.token_hex(24), crew=row['snapshot']['crew'],
                       revision=parsed['revision'], session_generation=crew['session_generation'],
                       target=dict(kind=pick['kind'], model=pick['model'], args=pick['args']), wait_seconds=WAIT_SECONDS)
        if pick['env_file'] is not None:
            request['target']['env_file'] = pick['env_file']
        operation = dict(token=op_token, owner=self.owner_for(crew_name), active=True, crew=row['snapshot']['crew'],
                         via=via, binding=row['binding'], pick=pick, request=request, fingerprint=n.digest(dict(request=request, binding=row['binding'])), target=None,
                         definition=definition_of(crew, request['target']),
                         message=row['message'], generation=1, update_id=uid, accepted=now().isoformat(),
                         deadline=(now() + datetime.timedelta(seconds=WAIT_SECONDS)).isoformat(),
                         receipt=None, admission_confirmed=False, observed=None, current=current_lines(self.config, crew, parsed)[0],
                         telemetry=self.telemetry(crew), telemetry_at=now().isoformat(), effort=parsed.get('effort'),
                         running_effort=parsed.get('running_effort'),
                         effort_matched=launch_matches_saved(self.config, crew, parsed),
                         handoff=handoff,
                         problem='Switch requested; native outcome not yet confirmed.', revision=0, display=None,
                         force_action=None, display_failure=None)
        self.verify()
        if not self.bound(row):
            raise y.SwitchUnavailable('model selector binding changed')
        existing, duplicate = None, False
        with self.bridge.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            tap = db.execute('SELECT 1 FROM switch_model_taps WHERE update_id=?', (uid,)).fetchone()
            existing = self._pending_row(db, self.guard_scope(crew_name))
            if not tap and not existing:
                db.execute("INSERT INTO switch_model_taps VALUES (?, ?, ?, 'reserved', ?)", (uid, pick['id'], operation['crew'], now().isoformat()))
                db.execute('INSERT INTO model_operations VALUES (?, ?, 1, ?)', (op_token, self.owner_for(crew_name), canonical(operation)))
                db.execute('INSERT INTO model_actions VALUES (?, ?, ?)', (uid, op_token, canonical(dict(token=op_token, kind='admit'))))
            elif tap:
                duplicate = True
        if duplicate:
            return self.answer(uid, query, 'That tap is already recorded; nothing retried.')
        if existing:
            return self.reserve_existing(uid, query, json.loads(existing[0]), pick, crew_name)
        # Reservation and complete request are durable before the only mutating native call.
        self.answer(uid, query, 'Handoff requested; please wait.' if handoff else 'Switch requested; please wait for the handoff.')
        status, out, problem = y._run_logged(self.config, admission_argv(self.config, operation), deadline, 'handoff admission', self.bridge._log)
        try:
            if status != 0 or problem:
                raise y.SwitchUnavailable('native admission not confirmed')
            self.observe(operation, y._strict_json(out), admission=True)
            self.bridge.store.record_switch_tap(uid, 'done')
        except (y.SwitchUnavailable, y.Unreadable):
            error = native_error(out) if problem is None and status is not None and status != 0 else None
            if error:
                for path in (parsed['env_file'], pick['env_file']):
                    if path:
                        error = error.replace(path, '[env_file]')
                error = y.clean(error, 320)
                operation['admission_error'] = error
                self.bridge._log('command switch_model yardr handoff admission error: ' + error)
            if problem == 'not_started':
                self.refuse(operation, 'native command did not start.')
            else:
                operation['problem'] = 'Switch outcome unknown; reconcile the original native request. Nothing retried.'
                self.bridge.store.record_switch_tap(uid, 'unknown')
                self.save(operation)
            if error:
                try:
                    self.lookup(operation, deadline)
                except (y.SwitchUnavailable, y.Unreadable, n.NativeError):
                    pass
        self.display(operation)

    def live_selector(self, uid, payload, query, thread, via, token):
        """(row, conversation) for a live effort/handoff tap, else answer stale and None."""
        conversation = self.bridge.decide_conversation(thread)
        if conversation is None:
            self.answer(uid, query, 'That model button is out of date.', True)
            return None
        row = self.load('model_selectors', 'token', token)
        if (row is None or not self.bound(row) or row['binding']['conversation'] != conversation['conversation_id']
                or row['message'] != payload.get('callback_message_id')):
            self.answer(uid, query, 'That model button is out of date.', True)
            return None
        # Every new mutation verifies today's native mapping, as picks do.
        self.verify()
        if via == 'switch_model':
            primary = self.config.get('yardr_switch_model')
            if primary is None or row['snapshot'] != crew_snapshot(primary):
                self.answer(uid, query, 'That model button is out of date.', True)
                return None
        elif row['snapshot'] != self.expected_snapshot(row['snapshot']['crew']):
            self.answer(uid, query, 'That model button is out of date.', True)
            return None
        return row, conversation

    def effort_tap(self, uid, payload, query, thread, via, kind, match):
        found = self.live_selector(uid, payload, query, thread, via, match[2])
        if found is None:
            return
        row, _ = found
        if row['retired']:
            return self.answer(uid, query, 'That model button is out of date.', True)
        if kind == 'open':
            return self.effort_open(uid, query, row, via)
        if kind == 'back':
            return self.effort_back(uid, query, row, via, match[3])
        return self.effort_apply(uid, query, row, via, match[3], match[4])

    def effort_guarded_read(self, row):
        """(crew, parsed, deadline) of the selector's crew."""
        crew_name = row['snapshot']['crew']
        entry = self.crew_entry(crew_name)
        deadline = time.monotonic() + entry['timeout']
        capability(self.config, deadline, self.bridge._log)
        return read_current(self.effective(crew_name), deadline, self.bridge._log) + (deadline,)

    def effort_pending(self, uid, query, crew_name):
        """Answer when an operation already blocks `crew_name`, else None."""
        for active in self.active(self.guard_scope(crew_name)):
            return self.answer(uid, query, f'A switch is already pending; see the original card (message {active["message"]}).', True)
        return None

    def effort_options(self, crew_name, parsed, deadline):
        """(levels, template, siblings, aliases) for the crew's current model,
        or None when its harness names none. Codex levels are filtered to
        what the model takes; an AGY model ending in -<level> offers only
        its configured sibling IDs, mapped here for the save."""
        entry = self.crew_entry(crew_name)
        picks = entry['picks'] if entry is not None else ()
        spec = harness_effort(self.config, parsed['kind'], deadline, self.bridge._log)
        if spec is None:
            return None
        offered, template, aliases = spec
        kind, model = parsed['kind'], parsed['model']
        if kind == 'agy':
            siblings = agy_siblings(picks, model, offered)
            if siblings is None:
                return (offered, template, {}, aliases)
            return ([level for level in offered if level in siblings], template, siblings, aliases)
        if kind == 'codex':
            return (codex_supported_levels(model, offered), template, {}, aliases)
        return (offered, template, {}, aliases)

    def effort_open(self, uid, query, row, via):
        crew_name = row['snapshot']['crew']
        blocked = self.effort_pending(uid, query, crew_name)
        if blocked is not None:
            return blocked
        crew, parsed, deadline = self.effort_guarded_read(row)
        if (crew.get('switch') or {}).get('unresolved'):
            return self.answer(uid, query, 'A switch is already pending; use its original operation.', True)
        if row.get('definition') != selector_definition(crew):
            return self.answer(uid, query, 'That model button is out of date; crew configuration or session changed.', True)
        spec = self.effort_options(crew_name, parsed, deadline)
        if spec is None:
            return self.answer(uid, query, "This crew's harness offers no effort levels.", True)
        levels, _, _, _ = spec
        if not levels:
            return self.answer(uid, query, 'No usable effort levels for this model.', True)
        prefix = y.CREW_PREFIX if via == 'crew' else y.SWITCH_MODEL_PREFIX
        current = parsed.get('effort')
        head = (f'Effort for {crew_name} ({y.clean(parsed["kind"], 32)} · {y.clean(parsed["model"], 128)}): '
                + (f'current {y.clean(current, 32)}.' if current else 'currently unset.'))
        lines = [head, 'A level saves to the crew config; the next session uses it.']
        seq = row.get('effort_seq', 0) + 1
        row['effort_seq'] = seq
        row['effort_view'] = seq
        keyboard = [[dict(text=level + (' ✓' if level == current else ''),
                           callback_data=f'{prefix}el:{row["token"]}:{seq}:{level}')] for level in levels]
        keyboard.append([dict(text='Back', callback_data=f'{prefix}eb:{row["token"]}:{seq}')])
        keyboard = self.bridge._command_keyboard(keyboard)
        action = dict(type='send', format='html', text='<pre>' + escape('\n'.join(lines)) + '</pre>',
                      plain='\n'.join(lines), keyboard=keyboard)
        self.answer(uid, query, '')
        return self.edit_selector(row, action, 'effort')

    def effort_back(self, uid, query, row, via, seq):
        # A delayed Back must not paint picks over a pending operation's card.
        blocked = self.effort_pending(uid, query, row['snapshot']['crew'])
        if blocked is not None:
            return blocked
        if row.get('effort_view') is None or str(row['effort_view']) != str(seq):
            return self.answer(uid, query, 'That model button is out of date.', True)
        crew, _, _ = self.effort_guarded_read(row)
        if row.get('definition') != selector_definition(crew):
            return self.answer(uid, query, 'That model button is out of date; crew configuration or session changed.', True)
        row['effort_view'] = None
        action = dict(type='send', format='html', text='<pre>' + escape('\n'.join(row['current'])) + '</pre>',
                      plain='\n'.join(row['current']), keyboard=self.picks_keyboard(row, via))
        self.answer(uid, query, '')
        return self.edit_selector(row, action)

    def reserve_effort(self, uid, query, row, level, notice):
        with self.bridge.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM switch_model_taps WHERE update_id=?', (uid,)).fetchone():
                db.execute("INSERT INTO switch_model_taps VALUES (?, ?, ?, 'refused', ?)",
                           (uid, 'effort:' + level, row['snapshot']['crew'], now().isoformat()))
                db.execute('INSERT INTO model_actions VALUES (?, ?, ?)', (uid, row['token'],
                           canonical(dict(token=row['token'], kind='effort', level=level, notice=notice, alert=True))))
        return self.answer(uid, query, notice, True)

    def commit_effort(self, uid, row, level, args, via, notice, state, alert=False, model=None):
        with self.bridge.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM switch_model_taps WHERE update_id=?', (uid,)).fetchone():
                db.execute('INSERT INTO switch_model_taps VALUES (?, ?, ?, ?, ?)',
                           (uid, 'effort:' + level, row['snapshot']['crew'], state, now().isoformat()))
                db.execute('INSERT INTO model_actions VALUES (?, ?, ?)', (uid, row['token'],
                           canonical(dict(token=row['token'], kind='effort', level=level, args=args, via=via,
                                          notice=notice, alert=alert, model=model))))
            else:
                db.execute('UPDATE switch_model_taps SET state=? WHERE update_id=?', (state, uid))
                db.execute('UPDATE model_actions SET value=? WHERE update_id=?',
                           (canonical(dict(token=row['token'], kind='effort', level=level, args=args, via=via,
                                           notice=notice, alert=alert, model=model)), uid))

    def replay_effort(self, uid, query, row, crew_name):
        action = self.load('model_actions', 'update_id', uid)
        if action and action.get('kind') == 'effort' and action.get('notice'):
            return self.answer(uid, query, action['notice'], action.get('alert', False))
        if action and action.get('kind') == 'effort' and action.get('args') is not None:
            # Reserved but outcome unknown (a crash between the native write
            # and its record): reconcile against the saved desired args and
            # sibling model, never retry.
            entry = self.crew_entry(crew_name)
            if entry is None:
                return self.answer(uid, query, 'That model button is out of date.', True)
            deadline = time.monotonic() + entry['timeout']
            try:
                crew, parsed = read_current(self.effective(crew_name), deadline, self.bridge._log)
            except (y.SwitchUnavailable, n.NativeError):
                return self.answer(uid, query, 'Effort outcome unknown; reopen the card.', True)
            if parsed['args'] == action['args'] and (
                    action.get('model') is None or parsed['model'] == action['model']):
                return self.effort_saved(uid, query, row, action.get('via') or 'switch_model',
                                         crew, parsed, action['level'])
            return self.answer(uid, query, 'Effort outcome unknown; reopen the card.', True)
        return self.answer(uid, query, 'That tap is already recorded; nothing retried.', True)

    def effort_saved(self, uid, query, row, via, crew, parsed, level):
        """Record a confirmed effort save, refresh the selector's card and return to picks."""
        notice = f'Effort set to {level}; the next session uses it.'
        row['definition'] = selector_definition(crew)
        row['effort_view'] = None
        row['current'] = current_lines(self.config, crew, parsed)
        with self.bridge.store.connect() as db:
            db.execute('UPDATE model_selectors SET value=? WHERE token=?', (canonical(row), row['token']))
        self.commit_effort(uid, row, level, parsed['args'], via, notice, 'done', model=parsed['model'])
        self.answer(uid, query, notice)
        action = dict(type='send', format='html', text='<pre>' + escape('\n'.join(row['current'])) + '</pre>',
                      plain='\n'.join(row['current']), keyboard=self.picks_keyboard(row, via))
        return self.edit_selector(row, action)

    def effort_apply(self, uid, query, row, via, seq, level):
        crew_name = row['snapshot']['crew']
        if self.bridge.store.switch_tap(uid):
            return self.replay_effort(uid, query, row, crew_name)
        for active in self.active(self.guard_scope(crew_name)):
            return self.reserve_effort(uid, query, row, level,
                f'A switch is already pending; see the original card (message {active["message"]}).')
        if row.get('effort_view') is None or str(row['effort_view']) != str(seq):
            return self.reserve_effort(uid, query, row, level, 'That model button is out of date.')
        crew, parsed, deadline = self.effort_guarded_read(row)
        if (crew.get('switch') or {}).get('unresolved'):
            return self.reserve_effort(uid, query, row, level,
                'A switch is already pending; use its original operation.')
        if row.get('definition') != selector_definition(crew):
            return self.reserve_effort(uid, query, row, level,
                'That model button is out of date; crew configuration or session changed.')
        spec = self.effort_options(crew_name, parsed, deadline)
        if spec is None:
            return self.reserve_effort(uid, query, row, level, "This crew's harness offers no effort levels.")
        levels, template, siblings, aliases = spec
        if level not in levels:
            return self.reserve_effort(uid, query, row, level, 'That effort level is out of date.')
        sibling = siblings.get(level)
        if sibling is not None:
            if (parsed['model'] == sibling['model'] and list(parsed['args']) == list(sibling['args'])
                    and (sibling['env_file'] is None or parsed['env_file'] == sibling['env_file'])):
                notice = f'Already at effort {level}; the next session uses it.'
                self.commit_effort(uid, row, level, parsed['args'], via, notice, 'done', model=parsed['model'])
                return self.answer(uid, query, notice)
            desired, desired_model = list(sibling['args']), sibling['model']
            argv = sibling_argv(self.config, crew_name, sibling, parsed['revision'])
        else:
            if parsed.get('effort') == level:
                notice = f'Already at effort {level}; the next session uses it.'
                self.commit_effort(uid, row, level, parsed['args'], via, notice, 'done', model=parsed['model'])
                return self.answer(uid, query, notice)
            desired, desired_model = y.set_effort(parsed['args'], template, level, aliases), None
            argv = effort_argv(self.config, crew_name, desired, parsed['revision'])
        revision = parsed['revision']
        self.verify()
        if not self.bound(row):
            raise y.SwitchUnavailable('model selector binding changed')
        duplicate = False
        with self.bridge.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            tap = db.execute('SELECT 1 FROM switch_model_taps WHERE update_id=?', (uid,)).fetchone()
            existing = self._pending_row(db, self.guard_scope(crew_name))
            if not tap and not existing:
                db.execute("INSERT INTO switch_model_taps VALUES (?, ?, ?, 'reserved', ?)",
                           (uid, 'effort:' + level, crew_name, now().isoformat()))
                db.execute('INSERT INTO model_actions VALUES (?, ?, ?)', (uid, row['token'],
                           canonical(dict(token=row['token'], kind='effort', level=level, args=desired,
                                          via=via, notice=None, alert=False, model=desired_model))))
            elif tap:
                duplicate = True
        if duplicate:
            return self.answer(uid, query, 'That tap is already recorded; nothing retried.')
        if existing:
            return self.reserve_effort(uid, query, row, level,
                f'A switch is already pending; see the original card (message {json.loads(existing[0])["message"]}).')
        # The reservation is durable before the only mutating native call, a
        # config-only crew set: the session continues undisturbed.
        status, _, problem = y._run_logged(self.config, argv, deadline, 'effort save', self.bridge._log)
        try:
            crew, parsed = read_current(self.effective(crew_name), deadline, self.bridge._log)
        except (y.SwitchUnavailable, n.NativeError):
            notice = 'Effort outcome unknown; reopen the card.'
            self.commit_effort(uid, row, level, desired, via, notice, 'unknown', True, model=desired_model)
            return self.answer(uid, query, notice, True)
        saved = parsed['args'] == desired and (desired_model is None or parsed['model'] == desired_model)
        if sibling is not None and sibling['env_file']:
            saved = saved and parsed['env_file'] == sibling['env_file']
        if saved:
            return self.effort_saved(uid, query, row, via, crew, parsed, level)
        if status == 0 and problem is None:
            notice = 'Effort outcome unknown; reopen the card.'
            self.commit_effort(uid, row, level, desired, via, notice, 'unknown', True, model=desired_model)
            return self.answer(uid, query, notice, True)
        # The saved args are the truth: a moved revision means someone else
        # changed the crew, an unmoved one that the save did not apply.
        if parsed['revision'] != revision:
            notice = 'That model button is out of date; crew configuration changed.'
        else:
            notice = 'Effort change unavailable; nothing retried.'
        self.commit_effort(uid, row, level, desired, via, notice, 'refused', True, model=desired_model)
        return self.answer(uid, query, notice, True)

    def handoff_now(self, uid, payload, query, thread, via, token):
        found = self.live_selector(uid, payload, query, thread, via, token)
        if found is None:
            return
        row, _ = found
        crew_name = row['snapshot']['crew']
        if self.bridge.store.switch_tap(uid):
            action = self.load('model_actions', 'update_id', uid)
            if action and action['kind'] == 'refused':
                return self.answer(uid, query, action['notice'], True)
            target = self.load('model_operations', 'token', action['token']) if action else None
            if target:
                return self.ack_existing(uid, query, target, target['pick'], crew_name)
            return self.answer(uid, query, 'That tap is already recorded; nothing retried.', True)
        if row['retired']:
            return self.answer(uid, query, 'That model button is out of date.', True)
        for active in self.active(self.guard_scope(crew_name)):
            if active.get('handoff') and active['crew'] == crew_name and self.bound(active):
                return self.reserve_existing(uid, query, active, active['pick'], crew_name)
            return self.reserve_refusal(uid, query, row, dict(id='handoff'),
                'A switch is already pending; use its original operation.')
        entry = self.crew_entry(crew_name)
        deadline = time.monotonic() + entry['timeout']
        capability(self.config, deadline, self.bridge._log)
        crew, parsed = read_current(self.effective(crew_name), deadline, self.bridge._log)
        # An external unresolved native switch also takes precedence, as for picks.
        if (crew.get('switch') or {}).get('unresolved'):
            return self.reserve_refusal(uid, query, row, dict(id='handoff'))
        if row.get('definition') != selector_definition(crew):
            return self.reserve_refusal(uid, query, row, dict(id='handoff'),
                'That model button is out of date; crew configuration or session changed.')
        pick = dict(id='handoff', label=y.clean(parsed['kind'], 32) + ' · ' + y.clean(parsed['model'], 128),
                    kind=parsed['kind'], model=parsed['model'], args=list(parsed['args']),
                    env_file=parsed['env_file'])
        return self.admit(uid, query, row, pick, crew_name, deadline, crew, parsed, via, handoff=True)

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
            db.execute("UPDATE switch_model_taps SET state='refused' WHERE update_id=?", (row['update_id'],))
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
            if not db.execute('SELECT 1 FROM switch_model_taps WHERE update_id=?', (uid,)).fetchone():
                db.execute("INSERT INTO switch_model_taps VALUES (?, ?, ?, 'refused', ?)",
                           (uid, pick['id'], selector['snapshot']['crew'], now().isoformat()))
                db.execute('INSERT INTO model_actions VALUES (?, ?, ?)', (uid, selector['token'],
                           canonical(dict(token=selector['token'], kind='refused', notice=notice))))
        return self.answer(uid, query, notice, True)

    def reserve_existing(self, uid, query, row, pick, crew):
        with self.bridge.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            if not db.execute('SELECT 1 FROM switch_model_taps WHERE update_id=?', (uid,)).fetchone():
                same = (self.bound(row) and row['crew'] == crew
                        and canonical(row['pick']) == canonical(pick))
                db.execute('INSERT INTO switch_model_taps VALUES (?, ?, ?, ?, ?)',
                           (uid, pick['id'], row['crew'], 'done' if same else 'refused', now().isoformat()))
                db.execute('INSERT INTO model_actions VALUES (?, ?, ?)', (uid, row['token'], canonical(dict(token=row['token'], kind='existing'))))
        return self.ack_existing(uid, query, row, pick, crew)

    def ack_existing(self, uid, query, row, pick, crew):
        if not self.bound(row) or row['crew'] != crew:
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
            raise y.SwitchUnavailable('native switch unavailable')
        value = y._strict_json(out)
        self.observe(row, value)

    def force(self, uid, query, row, generation):
        if generation != row['generation'] or not row['active']:
            return self.answer(uid, query, 'That Force button is out of date.', True)
        previous = self.bridge.store.switch_tap(uid)
        if previous or row['force_action']:
            return self.answer(uid, query, 'Force already recorded; nothing retried.')
        entry = self.crew_entry(row['crew'])
        if entry is None:
            # A pending operation keeps its Force path after its crew leaves
            # the picker: the primary's timeout bounds the native calls, as on
            # a single-crew setup, else the first remaining crew's.
            entry = self.config.get('yardr_switch_model')
            if entry is None:
                entries = self.config.get('yardr_switch_crews') or ()
                entry = entries[0] if entries else None
            if entry is None:
                raise y.SwitchUnavailable('crew not configured')
        deadline = time.monotonic() + entry['timeout']
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
            raise y.SwitchUnavailable('model operation binding changed')
        duplicate = False
        with self.bridge.store.connect() as db:
            db.execute('BEGIN IMMEDIATE')
            latest = json.loads(db.execute('SELECT value FROM model_operations WHERE token=?', (row['token'],)).fetchone()[0])
            if (latest['generation'] != generation or latest['force_action'] or not latest['active']
                    or db.execute('SELECT 1 FROM switch_model_taps WHERE update_id=?', (uid,)).fetchone()):
                duplicate = True
            else:
                row = latest
                row['force_action'] = action
                db.execute("INSERT INTO switch_model_taps VALUES (?, ?, ?, 'reserved', ?)", (uid, row['pick']['id'], row['crew'], now().isoformat()))
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
                raise y.SwitchUnavailable('native force not confirmed')
            value = receipt(y._strict_json(out), row, action['action_id'])
            # Force receipt's request_id is its action ID; observation retains original admission ID.
            value = dict(value, request_id=row['request']['request_id'])
            self.observe(row, value)
            self.bridge.store.record_switch_tap(uid, 'done')
        except (y.SwitchUnavailable, y.Unreadable):
            row['problem'] = 'Force outcome unknown; original operation remains observable. Nothing retried.'
            self.bridge.store.record_switch_tap(uid, 'unknown')
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
        lines = [notice, row['current'], 'Target: ' + y.clean(row['pick']['label'], 64)]
        if row.get('handoff'):
            lines.append('Handoff of the current launch (no model change).')
        lines.append('Status: ' + state)
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
        effort, running = row.get('effort'), row.get('running_effort')
        if row.get('effort_matched', True):
            lines.append('Effort: ' + (y.clean(effort, 32) if effort else 'unavailable.'))
        elif running and effort:
            lines.append(f'Effort: {y.clean(running, 32)}. Saved: {y.clean(effort, 32)} for the next session.')
        elif running:
            lines.append(f'Effort: {y.clean(running, 32)}.')
        elif effort:
            lines.append(f'Effort: unavailable. Saved: {y.clean(effort, 32)} for the next session.')
        else:
            lines.append('Effort: unavailable.')
        if data.get('start'):
            lines.append(data['start'])
        if data.get('context_at'):
            window = f' of {data["context_window"]}' if data.get('context_window') else ''
            lines.append(f'Context: {data.get("context_pct", 0)}%{window} · read {data["context_at"]}')
        else:
            lines.append('Context unavailable.')
        keyboard = []
        if op and row['active'] and not row.get('problem') and not row['force_action'] and op['mode'] != 'force' and self.bound(row):
            prefix = y.CREW_PREFIX if row.get('via') == 'crew' else y.SWITCH_MODEL_PREFIX
            data = f'{prefix}f:{row["token"]}:{row["generation"]}'
            routed = self.bridge._callback_command(data)
            native = y.NATIVE_CREW if row.get('via') == 'crew' else y.NATIVE_SWITCH_MODEL
            # A reconfigured bridge may no longer route the originating native
            # command, or may route its prefix to a custom handler instead;
            # reconcile the operation without its Force control then.
            if routed is not None and routed.get('native') == native:
                keyboard = [[dict(text='Force apply', callback_data=data)]]
        text = '<pre>' + escape('\n'.join(lines)) + '</pre>'
        if n.units(text) > 4096:
            raise y.SwitchUnavailable('model card exceeds message limit')
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
        if not self.config.get('yardr_switch_model') and not self.config.get('yardr_switch_crews'):
            return
        with self.locked():
            self._refresh()

    def _refresh(self):
        if not self.config.get('yardr_switch_model') and not self.config.get('yardr_switch_crews'):
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
                    entry = self.crew_entry(row['crew'])
                    triple = ({key: entry[key] for key in ('crew', 'timeout', 'picks')} if entry is not None
                              else dict(self.config.get('yardr_switch_model') or {}, crew=row['crew']))
                    crew, parsed = read_current(dict(self.config, yardr_switch_model=triple), deadline, self.bridge._log)
                    op = crew.get('switch')
                    if not isinstance(op, dict) or op.get('id') != row['receipt']['operation']['id']:
                        row['lookup_next'] = True
                        raise y.SwitchUnavailable('native operation unavailable or superseded')
                    current = current_lines(self.config, crew, parsed)[0] if op['state'] in {'superseded', 'stopped'} else None
                    self.observe(row, dict(request_id=row['request']['request_id'], existing=row['receipt']['existing'], operation=op))
                    if current is not None:
                        row['current'] = current
                    row['telemetry'], row['telemetry_at'] = self.telemetry(crew), now().isoformat()
                    row['effort'] = parsed.get('effort')
                    row['running_effort'] = parsed.get('running_effort')
                    row['effort_matched'] = launch_matches_saved(self.config, crew, parsed)
            except UnmatchedReceipt as exc:
                row['receipt'], row['observed'], row['target'] = None, None, None
                row['problem'] = str(exc)
            except (y.SwitchUnavailable, y.Unreadable, n.NativeError):
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
