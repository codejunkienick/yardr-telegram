#!/usr/bin/env python3
"""Native yard commands of the Telegram bridge, built on the yardr CLI.

/quota runs `yardr limits --json` in the yard this bridge serves and renders
the providers it burns, the yard's self-reporting sources and the configured
groups as one monospace table. /mayor_model shows a crew member's kind,
model and effort with one button per configured pick; a tap by the yard's
operator runs `yardr crew set ... --revision` and then `yardr crew restart`,
once per tap, through a durable reservation in the bridge's store. Both run
yardr through the child runner below.

The table rendering ports window_row, short_label, row_labels, fit, reason
and clean from the telegram-quota add-on's quota_command.py (MIT, same
author), reshaped from aiquokka's answers to yardr limits' reports; the
/mayor_model view, tap and dry run port its yardr target (read_crew,
effort_of, apply_yardr_pick, yardr_dry_run).

Nothing here reads a credential, a login directory, an env file or a reading
file, and nothing runs aiquokka. The only writes are the delivery ledger
rows the command path already writes and a /mayor_model tap's row in the
store's mayor_model_taps table.
"""
import concurrent.futures
import datetime
import getpass
import json
import math
import os
import re
import selectors
import shlex
import signal
import subprocess
import time
import zoneinfo
from html import escape as html_escape
from pathlib import Path

import run_asks

# The registry entry load_settings registers for /quota: `native` in place
# of `handler`/`url`, so _match_command, set_commands, _callback_command and
# keyboard validation need no parallel path.
NATIVE_QUOTA = 'quota'
QUOTA_COMMAND = dict(name='quota', description='Usage limits and time to each reset',
                     menu=True, callback_prefix='quota:', native=NATIVE_QUOTA)
QUOTA_MENU_TEXT = QUOTA_COMMAND['description']
# /mayor_model's entry: the mayor-model: prefix is the bridge's own while
# [yardr.mayor_model] is set, as decide: is with native home and operator.
NATIVE_MAYOR_MODEL = 'mayor_model'
MAYOR_MODEL_PREFIX = 'mayor-model:'


def mayor_model_command(mayor):
    """The registry entry of /mayor_model for the [yardr.mayor_model] settings."""
    return dict(name=NATIVE_MAYOR_MODEL, description=f'Show the {mayor["crew"]} model, tap to switch it',
                menu=True, callback_prefix=MAYOR_MODEL_PREFIX, native=NATIVE_MAYOR_MODEL,
                timeout=mayor['timeout'])

# The yardr child may print this much stdout before its group is killed; its
# reports are small, and anything past it is unreadable, never truncated.
YARDR_OUTPUT_MAX_BYTES = 4 * 1024 * 1024
# Stderr is captured bounded and only its size is ever logged; no byte of
# it reaches the reply or the log. The tail below always holds it.
YARDR_STDERR_MAX_BYTES = 64 * 1024
# The PATH a yardr child inherits when the bridge's own environment names none.
FALLBACK_PATH = '/usr/local/bin:/usr/bin:/bin'

# --- the yardr child runner, shared with the sibling command --------------

OUTPUT_PROBLEMS = ('not_started', 'timeout', 'too_large')


def child_env(config):
    """The fresh environment of a yardr child: the bridge's own PATH
    (FALLBACK_PATH when it names none, so the yard's limits source finds
    aiquokka and the CLIs it runs), HOME, LANG, TZ from the settings, and
    USER/LOGNAME as the bridge's. No YARDR_* variable and nothing else."""
    user = os.environ.get('USER') or getpass.getuser()
    return {'PATH': os.environ.get('PATH') or FALLBACK_PATH,
            'HOME': os.environ.get('HOME') or str(Path.home()),
            'LANG': os.environ.get('LANG') or 'C.UTF-8',
            'TZ': config.get('timezone') or 'UTC',
            'USER': user, 'LOGNAME': user}


def run_yardr(argv, home, env, deadline, max_out=YARDR_OUTPUT_MAX_BYTES,
              monotonic=time.monotonic):
    """Run argv (no shell, stdin closed, cwd home, its own process group)
    until it exits or `deadline` (a monotonic time) passes. Return (status,
    stdout, stderr, problem): problem is None, 'not_started' (it never ran),
    'timeout' (its whole group was killed at the deadline) or 'too_large'
    (past `max_out` bytes of stdout; its group was killed)."""
    if deadline - monotonic() <= 0:
        return None, b'', b'', 'timeout'
    try:
        proc = subprocess.Popen(argv, stdin=subprocess.DEVNULL, stdout=subprocess.PIPE,
                                stderr=subprocess.PIPE, cwd=home, env=env,
                                start_new_session=True, close_fds=True)
    except (OSError, ValueError):
        return None, b'', b'', 'not_started'
    out, err, problem = bytearray(), bytearray(), None
    sel = selectors.DefaultSelector()
    try:
        sel.register(proc.stdout, selectors.EVENT_READ, 'out')
        sel.register(proc.stderr, selectors.EVENT_READ, 'err')

        def read(key):
            nonlocal out, err, problem
            try:
                chunk = os.read(key.fd, 65536)
            except OSError:
                chunk = b''
            if not chunk:
                sel.unregister(key.fileobj)
            elif key.data == 'out':
                out += chunk
                if len(out) > max_out:
                    problem = 'too_large'
            else:
                err += chunk
                del err[:-YARDR_STDERR_MAX_BYTES]

        while True:
            if proc.poll() is not None:
                # Exited: take what is already waiting, then stop. A helper
                # the child left holding a pipe open does not hold us: the
                # kill below ends its group, and the answer is judged on what
                # the child itself printed.
                while sel.get_map():
                    if monotonic() >= deadline:
                        problem = problem or 'timeout'
                        break
                    ready = sel.select(0)
                    if not ready:
                        break
                    for key, _ in ready:
                        read(key)
                        if problem is not None:
                            break
                    if problem is not None:
                        break
                break
            remaining = deadline - monotonic()
            if remaining <= 0:
                problem = 'timeout'
                break
            for key, _ in sel.select(min(remaining, 0.05)):
                read(key)
                if problem is not None:
                    break
            if problem is not None:
                break
    finally:
        sel.close()
        # The leader may have exited while a helper it forked still runs
        # in its group: end the whole group either way, so no helper
        # survives the run and executes past the deadline.
        try:
            os.killpg(proc.pid, signal.SIGKILL)
        except OSError:
            pass
        proc.wait()
        for stream in (proc.stdout, proc.stderr):
            try:
                stream.close()
            except OSError:
                pass
    return proc.returncode, bytes(out), bytes(err), problem


class NativeUnknown(Exception):
    """A native command name the bridge registered but no handler answers."""


def handle_native(bridge, name, payload):
    """The {"actions": [...]} answer of the native command `name` for the
    command path's JSON payload. Never raises for a known command."""
    if name == NATIVE_QUOTA:
        return handle_quota(bridge, payload)
    if name == NATIVE_MAYOR_MODEL:
        return handle_mayor_model(bridge, payload)
    raise NativeUnknown(name)


# --- strict reading of `limits --json` -------------------------------------

class Unreadable(Exception):
    """Stdout that is not the JSON object `limits --json` prints."""


def _strict_object(pairs):
    seen = {}
    for key, value in pairs:
        if key in seen:
            raise ValueError('duplicate key')
        seen[key] = value
    return seen


def _strict_json(raw):
    try:
        value = json.loads(raw.decode('utf-8'), object_pairs_hook=_strict_object,
                           parse_constant=_reject_constant)
    except (UnicodeError, ValueError, RecursionError):
        raise Unreadable('not JSON') from None
    try:
        json.dumps(value, ensure_ascii=False).encode('utf-8')
    except (UnicodeError, RecursionError):
        raise Unreadable('not JSON') from None
    return value


def _reject_constant(_name):
    raise ValueError('not JSON')


def _number(value):
    if isinstance(value, bool) or not isinstance(value, (int, float)):
        return None
    try:
        return value if math.isfinite(value) else None
    except (OverflowError, TypeError):
        return None


def _window(item):
    if not isinstance(item, dict):
        raise Unreadable('window shape')
    label = item.get('label')
    if not isinstance(label, str):
        raise Unreadable('window label')
    resets = item.get('resets_at')
    if resets is not None and not isinstance(resets, str):
        raise Unreadable('window reset')
    model = item.get('model')
    if model is not None and not isinstance(model, str):
        raise Unreadable('window model')
    used = _number(item.get('used_percent'))
    if item.get('used_percent') is not None and used is None:
        raise Unreadable('window percent')
    target = _number(item.get('target_percent'))
    if item.get('target_percent') is not None and target is None:
        raise Unreadable('window target')
    if target is not None and not 0 <= target <= 100:
        target = None
    used_count = item.get('used')
    if used_count is not None and (isinstance(used_count, bool) or not isinstance(used_count, int)):
        raise Unreadable('window count')
    limit = item.get('limit')
    if limit is not None and (isinstance(limit, bool) or not isinstance(limit, int)):
        raise Unreadable('window limit')
    return {'label': label, 'model': model or '', 'used_percent': used, 'target_percent': target,
            'resets_at': resets, 'used': used_count, 'limit': limit}


def _report(item):
    if item is None:
        return None
    if not isinstance(item, dict):
        raise Unreadable('report shape')
    provider = item.get('provider')
    if not isinstance(provider, str) or not provider:
        raise Unreadable('report provider')
    windows = item.get('windows', [])
    if not isinstance(windows, list):
        raise Unreadable('report windows')
    return {'provider': provider, 'windows': [_window(window) for window in windows]}


def _unknown(item):
    value = item.get('limits_unknown')
    if value is not None and not isinstance(value, str):
        raise Unreadable('unknown reason')
    return value


def _strings(value, what):
    if not isinstance(value, list) or any(not isinstance(item, str) for item in value):
        raise Unreadable(what)
    return list(value)


def parse_limits_answer(raw):
    """The normalized plain or --group answer of `limits --json`: (providers,
    sources, unmapped) of normalized entries. Raises Unreadable."""
    data = _strict_json(raw)
    if not isinstance(data, dict):
        raise Unreadable('top shape')
    top_limits, top_sources, top_unmapped = data.get('limits', []), data.get('sources', []), data.get('unmapped', [])
    if not isinstance(top_limits, list) or not isinstance(top_sources, list) or not isinstance(top_unmapped, list):
        raise Unreadable('top shape')
    providers, sources, unmapped = [], [], []
    for item in top_limits:
        if not isinstance(item, dict):
            raise Unreadable('entry shape')
        report = _report(item.get('limits'))
        if 'provider' in item:
            if not isinstance(item['provider'], str) or not item['provider']:
                raise Unreadable('provider name')
            providers.append({'provider': item['provider'], 'burned_by': _strings(item.get('burned_by', []), 'burned_by'),
                              'kinds': _strings(item.get('kinds', []), 'kinds'),
                              'limits': report, 'unknown': _unknown(item)})
        elif 'kind' in item:
            if not isinstance(item['kind'], str) or not item['kind']:
                raise Unreadable('kind name')
            group = item.get('group')
            if not isinstance(group, str) or not group:
                raise Unreadable('group name')
            providers.append({'kind': item['kind'], 'group': group, 'limits': report, 'unknown': _unknown(item)})
        else:
            raise Unreadable('entry kind')
    for item in top_sources:
        if not isinstance(item, dict) or not isinstance(item.get('source'), str) or not item['source']:
            raise Unreadable('source entry')
        group = item.get('group', '')
        if not isinstance(group, str):
            raise Unreadable('source group')
        sources.append({'source': item['source'], 'group': group, 'limits': _report(item.get('limits')),
                        'unknown': _unknown(item)})
    for kind in top_unmapped:
        if not isinstance(kind, str):
            raise Unreadable('unmapped kind')
        unmapped.append(kind)
    return providers, sources, unmapped


# --- the /quota table -------------------------------------------------------

TEXT_MAX_UNITS = 4096
HTML_SOURCE_MAX_UNITS = 16384
NAME_MAX = 32
HEADER_MAX = 96
LABEL_WIDTH = 8
WHOLE_LABEL_MAX = 32
PERCENT_WIDTH = 4
DASH = '–'
ELLIPSIS = '…'
SEPARATOR = ' · '
WHITE, GREEN, YELLOW, RED = '⚪', '🟢', '🟡', '🔴'
DAYS = ('Mon', 'Tue', 'Wed', 'Thu', 'Fri', 'Sat', 'Sun')
SHORT_LABELS = {'5h': '5h', 'Weekly': 'week', 'Weekly Fable': 'Fable wk'}
SHORT_GROUPS = {'Gemini Models': 'Gem', 'Claude and GPT models': 'C+GPT', 'Gemini 4 Argon': 'Argon'}
SHORT_SPANS = {'Weekly': 'wk', '5h': '5h'}
LABEL_SUFFIXES = (' Remaining', ' Limit')
DISPLAY_NAMES = {'claude': 'Claude', 'codex': 'Codex', 'muse': 'Muse', 'antigravity': 'Antigravity',
                 'grok': 'Grok', 'deepseek': 'DeepSeek', 'kimi': 'Kimi'}


def clean(text, limit):
    """Printable characters only (no control, format or surrogate), stripped
    and cut to `limit` characters."""
    return ''.join(c for c in str(text) if c.isprintable()).strip()[:limit]


# The reply and the bridge log never carry free text yardr or a limits
# source wrote: no stderr line, no limits_unknown or other diagnostic
# string, no exception message built from one. They show only structured
# fields and a fixed reason from the closed set below, so there is no
# scrubber to extend: a new credential shape is another fixed reason away.
_SAFE_NAME_RE = re.compile(r'[A-Za-z0-9][A-Za-z0-9._-]{0,63}')


def _safe_name(value, fallback):
    """`value` for the reply when it is a plain structured name, else
    `fallback`: a diagnostic smuggled into a name never reaches the reply."""
    return value if isinstance(value, str) and _SAFE_NAME_RE.fullmatch(value) else fallback


def _exit_reason(status):
    """The fixed reason for stdout that does not parse: the yardr exit code
    when it is a plain small integer, else that the answer is not valid."""
    if isinstance(status, bool) or not isinstance(status, int):
        return 'answer not valid JSON'
    if status != 0 and -128 <= status <= 255:
        return f'yardr exited {status}'
    return 'answer not valid JSON'


def utf16_units(text):
    return len(text.encode('utf-16-le', 'surrogatepass')) // 2


def _ascii(text):
    return ''.join(c for c in text if ' ' <= c <= '~')


def _strip(label):
    text = str(label)
    for suffix in LABEL_SUFFIXES:
        text = text.removesuffix(suffix)
    return text


def short_label(label):
    text = _strip(label)
    group, dash, span = text.rpartition(' - ')
    if dash and group and span:
        span = _ascii(SHORT_SPANS.get(span, span)).strip()[:LABEL_WIDTH].rstrip()
        room = LABEL_WIDTH - len(span) - 1
        group = _ascii(SHORT_GROUPS.get(group, (group.split() or [''])[0])).strip()[:max(room, 0)]
        return f'{group} {span}' if group else span
    return _ascii(SHORT_LABELS.get(text, text)).strip()[:LABEL_WIDTH].rstrip()


def row_labels(windows):
    """The label column of one block: the short labels padded to 8, or, when
    two short labels would read the same, the whole labels (32 characters at
    most) padded to the longest of them, so no two rows read the same."""
    labels = [short_label(window['label']) for window in windows]
    if len(set(labels)) < len(labels):
        labels = [_ascii(_strip(window['label'])).strip()[:WHOLE_LABEL_MAX] for window in windows]
    width = max([LABEL_WIDTH] + [len(label) for label in labels])
    return [label.ljust(width) for label in labels]


def _parse_time(text):
    """An aware datetime from ISO-8601 text with a zone, else None."""
    if not isinstance(text, str):
        return None
    try:
        value = datetime.datetime.fromisoformat(text)
    except ValueError:
        return None
    return value if value.tzinfo is not None else None


def zone(name):
    try:
        return zoneinfo.ZoneInfo(name)
    except Exception:
        return datetime.timezone.utc


def stamp(moment, tz):
    local = moment.astimezone(tz)
    return f'{DAYS[local.weekday()]} {local:%H:%M}'


def time_left(seconds):
    minutes = int(seconds // 60)
    if minutes < 60:
        return f'{minutes}m'
    if minutes < 24 * 60:
        return f'{minutes // 60}h{minutes % 60:02d}m'
    return f'{minutes // (24 * 60)}d{minutes // 60 % 24}h'


def window_row(window, tz, now, label=None):
    """One table row: the dot, the label, the used share, the reset as Ddd
    HH:MM in the bridge's timezone and the time left. A per-model window
    shows its model. A window with a used figure and no reset (the machine's
    load, swap, disk and mem) shows its figure and 'no reset'. The dot is
    yellow when the window is ahead of its target share."""
    label = short_label(window['label']).ljust(LABEL_WIDTH) if label is None else label
    resets = window['resets_at']
    used = window['used_percent']
    percent = None if used is None else math.floor(used + 0.5)
    raw = used
    if used is None and window['used'] is not None and window['limit']:
        percent = math.floor(window['used'] * 100 / window['limit'] + 0.5)
        raw = window['used'] * 100 / window['limit']
    target = window['target_percent']
    if percent is None:
        dot = WHITE
    elif percent >= 90:
        dot = RED
    elif percent >= 70 or (target is not None and raw is not None and raw > target):
        dot = YELLOW
    else:
        dot = GREEN
    shown = DASH if percent is None else f'{percent}%'
    model = f'  model {clean(window["model"], NAME_MAX)}' if window['model'] else ''
    if resets is None or resets.startswith('0001-01-01'):
        if percent is None:
            return f'{WHITE} {label}{DASH:>{PERCENT_WIDTH}}  no window{model}'
        return f'{dot} {label}{shown:>{PERCENT_WIDTH}}  no reset{model}'
    moment = _parse_time(resets)
    if moment is None:
        return f'{dot} {label}{shown:>{PERCENT_WIDTH}}  unknown{model}'
    left = (moment - now).total_seconds()
    return f'{dot} {label}{shown:>{PERCENT_WIDTH}}  {stamp(moment, tz)}  {time_left(left) if left > 0 else DASH}{model}'


def _block_header(name, extra=''):
    header = name + extra
    return clean(header, HEADER_MAX) or name


def provider_block(entry, tz, now):
    """One block of the plain read: the provider with who burns it, then its
    windows, or the one unavailable row."""
    name = DISPLAY_NAMES.get(entry['provider'], clean(entry['provider'], NAME_MAX) or entry['provider'])
    burned = ', '.join(burn for burn in entry['burned_by'] if isinstance(burn, str) and burn.strip())
    header = _block_header(name + (SEPARATOR + 'burned by ' + clean(burned, HEADER_MAX) if burned else ''))
    if entry['limits'] is None:
        return [header, f'{WHITE} limits unknown for {_safe_name(entry["provider"], "provider")}']
    windows = entry['limits']['windows']
    if not windows:
        return [header, f'{WHITE} unavailable: no windows']
    labels = row_labels(windows)
    return [header] + [window_row(window, tz, now, label) for window, label in zip(windows, labels)]


def source_block(entry, tz, now):
    """One block of the plain read: the self-reporting source with its
    provider, then its windows, or the one unavailable row."""
    name = clean(entry['source'], NAME_MAX) or entry['source']
    if entry['limits'] is None:
        header = _block_header(name)
        return [header, f'{WHITE} limits unknown for {_safe_name(entry["source"], "source")}']
    provider = entry['limits']['provider']
    header = _block_header(name if provider == name else name + SEPARATOR + clean(provider, NAME_MAX))
    windows = entry['limits']['windows']
    if not windows:
        return [header, f'{WHITE} unavailable: no windows']
    labels = row_labels(windows)
    return [header] + [window_row(window, tz, now, label) for window, label in zip(windows, labels)]


def group_block(entry, tz, now):
    """One block of a group read: '<provider> · <group>', then its windows,
    or the one unavailable row."""
    report = entry['limits']
    name = clean(report['provider'], NAME_MAX) if report is not None else ''
    header = _block_header(f'{name or entry["kind"]}{SEPARATOR}{clean(entry["group"], NAME_MAX)}')
    if report is None:
        return [header, f'{WHITE} limits unknown for {_safe_name(entry["group"], "group")}']
    if not report['windows']:
        return [header, f'{WHITE} unavailable: no windows']
    labels = row_labels(report['windows'])
    return [header] + [window_row(window, tz, now, label) for window, label in zip(report['windows'], labels)]


def _fits(lines):
    text = '\n'.join(lines)
    return utf16_units(text) <= TEXT_MAX_UNITS and utf16_units(f'<pre>{html_escape(text)}</pre>') <= HTML_SOURCE_MAX_UNITS


def fit(blocks):
    """The lines of as many leading blocks as fit the message, a last line
    '…' when any were dropped. A first block too long alone is cut by line."""
    for keep in range(len(blocks), 0, -1):
        lines = [line for block in blocks[:keep] for line in block] + ([ELLIPSIS] if keep < len(blocks) else [])
        if _fits(lines):
            return lines
    first = blocks[0] if blocks else []
    for keep in range(len(first) - 1, 0, -1):
        if _fits(first[:keep] + [ELLIPSIS]):
            return first[:keep] + [ELLIPSIS]
    return [f'{WHITE} quota unavailable: reply too long']


def send(lines):
    return {'actions': [{'type': 'send', 'format': 'html', 'text': f'<pre>{html_escape(chr(10).join(lines))}</pre>'}]}


def unavailable(why=None):
    """One fixed line: `why` is always a bridge-defined literal or a value
    built from a validated integer or name, never yardr's or a source's
    free text."""
    return send([f'{WHITE} quota unavailable' + (f': {why}' if why else '')])


def render_quota(plain, groups, machine_login, tz, now):
    """Every block of the answer, in order: the plain read's providers
    (unless machine_login is false), its self sources, its unmapped line,
    then each group read. Text arguments are ignored; there are no buttons."""
    providers, sources, unmapped = plain
    blocks = []
    if machine_login:
        blocks.extend(provider_block(entry, tz, now) for entry in providers)
    blocks.extend(source_block(entry, tz, now) for entry in sources)
    if unmapped:
        kinds = ', '.join(clean(kind, NAME_MAX) or '?' for kind in unmapped)
        blocks.append([f'not asked: {kinds}'])
    for _name, read in groups:
        entry = read[0][0] if read[0] else None
        if entry is None:
            raise Unreadable('group answer')
        blocks.append(group_block(entry, tz, now))
    return blocks


def handle_quota(bridge, payload):
    """The {"actions": [...]} answer of /quota for the command path's JSON
    payload. The answer is a send, html <pre>, never a reply; text arguments
    are ignored, and there are no buttons. Never raises."""
    try:
        return _quota(bridge, payload)
    except Exception:
        return unavailable('internal error')


def _quota(bridge, payload):
    if isinstance(payload, dict) and payload.get('kind') == 'callback':
        return {'actions': [{'type': 'answer_callback', 'text': '', 'alert': False}]}
    config = bridge.config
    quota = config['yardr_quota']
    timeout = quota['timeout']
    groups = list(quota['groups'])
    deadline = time.monotonic() + timeout
    env = child_env(config)
    home, binary = config['yardr_home'], config['yardr_bin']
    with concurrent.futures.ThreadPoolExecutor(max_workers=1 + len(groups),
                                               thread_name_prefix='quota') as pool:
        plain = pool.submit(run_yardr, [binary, '--home', home, 'limits', '--json'],
                            home, env, deadline)
        reads = [(name, pool.submit(run_yardr, [binary, '--home', home, 'limits',
                                                '--group', name, '--json'], home, env, deadline))
                 for name in groups]
        try:
            results = [('limits', plain.result())] + [(name, future.result()) for name, future in reads]
        finally:
            pool.shutdown(wait=False, cancel_futures=True)
    # The log carries the structured outcome of each read alone: no stderr
    # byte, limits_unknown or other diagnostic string ever reaches it, and
    # none reaches the reply either. Only an off-course read logs at all.
    for name, (status, out, err, problem) in results:
        if problem is not None or (status is not None and status not in (0, 1)):
            bridge._log(f'command quota yardr {name}: status={status} problem={problem} '
                        f'stdout={len(out)}B stderr={len(err)}B')
    if any(result[3] == 'timeout' for _, result in results):
        return unavailable('timed out')
    if any(result[3] == 'not_started' for _, result in results):
        return unavailable('yardr not reachable')
    if any(result[3] is not None for _, result in results):
        return unavailable('answer too large')
    try:
        plain_answer = parse_limits_answer(results[0][1][1])
    except Unreadable:
        return unavailable(_exit_reason(results[0][1][0]))
    group_answers = []
    for name, (status, out, _err, _problem) in results[1:]:
        try:
            group_answers.append((name, parse_limits_answer(out)))
        except Unreadable:
            return unavailable(_exit_reason(status))
    tz = zone(config.get('timezone') or 'UTC')
    now = datetime.datetime.now(datetime.timezone.utc)
    try:
        blocks = render_quota(plain_answer, group_answers, quota['machine_login'], tz, now)
    except Unreadable:
        return unavailable('answer not valid JSON')
    if not blocks:
        return unavailable('no sources configured')
    return send(fit(blocks))


# --- /mayor_model: switch a crew member's kind, model and effort ----------
# The view reads `crew show <crew> --json` and offers one button per pick of
# [yardr.mayor_model]. A tap by the yard's operator that finds the crew
# member on another pick reserves the tap in the store's mayor_model_taps
# table, runs `crew set ... --revision <the revision it read>`, reads the
# crew member back and, only when it reads as the pick, runs `crew restart`.
# A re-delivered tap answers from its row and never acts again; a row left
# reserved reads as unknown. No yardr stdout or stderr byte reaches the
# answer or the log: answers carry fixed texts and structured fields.

# Each harness's own reasoning-effort flag (the add-on's effort_of): Muse
# and Grok take --reasoning-effort (alias --effort), Codex a -c/--config
# override whose key ends in reasoning_effort.
EFFORT_FLAGS = ('--reasoning-effort', '--effort')
CONFIG_FLAGS = ('-c', '--config')
REVISION_RE = re.compile(r'[A-Za-z0-9]{1,64}')
# yardr answers a crew member it does not have with this exit status.
CREW_NOT_FOUND_STATUS = 3
MODEL_SHOWN_MAX = 128
FIELD_SHOWN_MAX = 32
TAP_STATES = ('reserved', 'done', 'refused', 'unknown')


def effort_of(args):
    """The reasoning effort a harness's args give, else None: the value of
    `--reasoning-effort` or `--effort` (Muse, Grok), or of a `-c`/`--config`
    override whose key ends in `reasoning_effort` (Codex's
    model_reasoning_effort, a TOML value, quotes dropped). The last one
    given wins, as the harnesses read them."""
    effort, index = None, 0
    while index < len(args):
        flag, sep, inline = args[index].partition('=')
        if flag in EFFORT_FLAGS or flag in CONFIG_FLAGS:
            value = inline if sep else (args[index + 1] if index + 1 < len(args) else None)
            index += 1 if sep else 2
            if value is None:
                continue
            if flag in EFFORT_FLAGS:
                effort = value.strip() or None
                continue
            key, sep, setting = value.partition('=')
            if sep and key.strip().endswith('reasoning_effort'):
                setting = setting.strip()
                if len(setting) >= 2 and setting[0] == setting[-1] and setting[0] in '"\'':
                    setting = setting[1:-1]
                effort = setting.strip() or None
            continue
        index += 1
    return effort


class MayorUnavailable(Exception):
    """A fixed reason the crew member could not be read or set; never
    yardr's own text."""


def _crew_argv(config, *args, actor=False):
    argv = [config['yardr_bin'], '--home', config['yardr_home']]
    if actor:
        argv += ['--actor', config['yardr_operator']]
    return argv + list(args)


def set_argv(config, pick, revision):
    """The `crew set` a tap runs, as the operator: the pick's kind, model,
    args (one JSON list) and env_file when it names one. `--revision` makes
    yardr refuse when the crew member changed since it was read."""
    mayor = config['yardr_mayor_model']
    args = ['crew', 'set', mayor['crew'], f'kind={pick["kind"]}', f'model={pick["model"]}',
            'args=' + json.dumps(list(pick['args']), ensure_ascii=False, separators=(',', ':'))]
    if pick['env_file']:
        args.append(f'env_file={pick["env_file"]}')
    return _crew_argv(config, *args, '--revision', revision, actor=True)


def restart_argv(config):
    """The `crew restart` a tap runs once the set reads back, as the
    operator: the crew member's next session, once the current one is idle,
    starts on the pick."""
    return _crew_argv(config, 'crew', 'restart', config['yardr_mayor_model']['crew'], actor=True)


def _run_logged(config, argv, deadline, step, log):
    status, out, err, problem = run_yardr(argv, config['yardr_home'], child_env(config), deadline)
    if problem is not None or status != 0:
        log(f'command mayor_model yardr {step}: status={status} problem={problem} '
            f'stdout={len(out)}B stderr={len(err)}B')
    return status, out, problem


def _optional_text(value, what):
    if value is not None and not isinstance(value, str):
        raise MayorUnavailable(what)
    return value or ''


def parse_crew(raw):
    """The crew member of `crew show --json` stdout: {'kind', 'model',
    'args', 'env_file', 'effort', 'revision', 'state', 'status',
    'session_kind'}. MayorUnavailable for any other shape."""
    try:
        payload = _strict_json(raw)
    except Unreadable:
        raise MayorUnavailable('answer not valid JSON') from None
    crew = payload.get('crew') if isinstance(payload, dict) else None
    settings = crew.get('config') if isinstance(crew, dict) else None
    if not isinstance(settings, dict):
        raise MayorUnavailable('yardr answered no crew member')
    unexpected = 'yardr answered an unexpected crew member'
    kind = settings.get('kind') or crew.get('kind')
    model = settings.get('model') or crew.get('model') or ''
    args = settings.get('args', [])
    if isinstance(args, str):
        try:
            args = _strict_json(args.encode('utf-8'))
        except (Unreadable, UnicodeError):
            raise MayorUnavailable(unexpected) from None
    env_file = settings.get('env_file') or None
    revision = crew.get('revision')
    handle = crew.get('handle', {})
    if (not isinstance(kind, str) or not kind or not isinstance(model, str) or not isinstance(args, list)
            or not all(isinstance(arg, str) for arg in args)
            or not (env_file is None or isinstance(env_file, str))
            or not isinstance(revision, str) or not REVISION_RE.fullmatch(revision)
            or not isinstance(handle, dict)):
        raise MayorUnavailable(unexpected)
    return {'kind': kind, 'model': model, 'args': list(args), 'env_file': env_file,
            'effort': effort_of(args), 'revision': revision,
            'state': _optional_text(crew.get('state'), unexpected),
            'status': _optional_text(crew.get('status'), unexpected),
            'session_kind': _optional_text(handle.get('kind'), unexpected)}


def read_crew(config, deadline, log):
    """The crew member as `crew show <crew> --json` reads it now (parse_crew).
    MayorUnavailable with a fixed reason otherwise."""
    status, out, problem = _run_logged(
        config, _crew_argv(config, 'crew', 'show', config['yardr_mayor_model']['crew'], '--json'),
        deadline, 'crew show', log)
    if problem == 'not_started':
        raise MayorUnavailable('yardr not reachable')
    if problem == 'timeout':
        raise MayorUnavailable('timed out')
    if problem is not None:
        raise MayorUnavailable('answer too large')
    if status == CREW_NOT_FOUND_STATUS:
        raise MayorUnavailable('the crew member does not exist')
    if status != 0:
        raise MayorUnavailable(_exit_reason(status))
    return parse_crew(out)


def pick_of(mayor, current):
    """The configured pick whose kind, model and effort the crew member has,
    else None (picks differ in those, so at most one matches)."""
    for pick in mayor['picks']:
        if (pick['kind'], pick['model'], pick['effort']) == (current['kind'], current['model'], current['effort']):
            return pick
    return None


def has_pick(pick, current):
    """True when the crew member's settings are the pick's: kind, model and
    args exactly, and the env_file when the pick names one."""
    return ((current['kind'], current['model'], current['args']) == (pick['kind'], pick['model'], list(pick['args']))
            and (pick['env_file'] is None or current['env_file'] == pick['env_file']))


def unchanged(before, after):
    """True when the settings a tap sets read as they did before it (the
    revision moves with the crew member's runtime state, so it is not
    compared)."""
    return all(before[key] == after[key] for key in ('kind', 'model', 'args', 'env_file'))


def model_lines(mayor, current, status=None):
    """The view: the pick or the plain assignment, kind · model · effort, and
    the session's state, status and the kind it keeps until it restarts.
    Never the env file or anything of the env."""
    pick = pick_of(mayor, current)
    kind = clean(current['kind'], FIELD_SHOWN_MAX)
    model = clean(current['model'], MODEL_SHOWN_MAX) or 'default model'
    effort = clean(current['effort'] or '', FIELD_SHOWN_MAX) or 'default'
    lines = [f'{mayor["crew"]} model: {pick["label"] if pick else f"{model} ({kind}, effort {effort})"}',
             f'{kind} · {model} · effort {effort}']
    session = [clean(part, FIELD_SHOWN_MAX) for part in (current['state'], current['status'])
               if clean(part, FIELD_SHOWN_MAX)]
    session_kind = clean(current['session_kind'], FIELD_SHOWN_MAX)
    if session_kind and session_kind != current['kind']:
        session.append(f'on {session_kind} until it restarts')
    if session:
        lines.append('session: ' + ' · '.join(session))
    return ([status] if status else []) + lines


def model_keyboard(mayor, current):
    chosen = pick_of(mayor, current)
    return [[{'text': f'{"✓ " if pick is chosen else ""}{pick["label"]}',
              'callback_data': MAYOR_MODEL_PREFIX + pick['id']}]
            for pick in mayor['picks']]


def view_action(config, deadline, log, status=None):
    """The /mayor_model message: the crew member's state and one button per
    pick, or the unavailable line (after `status`) without buttons."""
    mayor = config['yardr_mayor_model']
    try:
        current = read_crew(config, deadline, log)
    except MayorUnavailable as error:
        lines = ([status] if status else []) + [f'{WHITE} mayor model unavailable: {error}']
        return send(lines)['actions'][0]
    action = send(model_lines(mayor, current, status))['actions'][0]
    action['keyboard'] = model_keyboard(mayor, current)
    return action


def dry_run(config, pick_id, log):
    """What a tap on `pick_id` would run: the crew member's current
    assignment and the two commands, shlex-joined. Runs `crew show` alone;
    nothing is set, restarted or reserved. MayorUnavailable otherwise."""
    mayor = config['yardr_mayor_model']
    pick = next((item for item in mayor['picks'] if item['id'] == pick_id), None)
    if pick is None:
        raise MayorUnavailable(f'no pick {pick_id!r} in [yardr.mayor_model]'
                               if isinstance(pick_id, str) and pick_id.isprintable() else 'unknown pick')
    current = read_crew(config, time.monotonic() + mayor['timeout'], log)
    lines = model_lines(mayor, current)
    if pick_of(mayor, current) is pick:
        return '\n'.join(lines + [f'a tap on {pick_id} changes nothing: {mayor["crew"]} already runs it'])
    return '\n'.join(lines + [f'a tap on {pick_id} runs:',
                              '  ' + shlex.join(set_argv(config, pick, current['revision'])),
                              '  ' + shlex.join(restart_argv(config))])


def handle_mayor_model(bridge, payload):
    """The {"actions": [...]} answer of /mayor_model or a mayor-model: tap.
    Text after /mayor_model is ignored: only a tap switches. Never raises."""
    callback = isinstance(payload, dict) and payload.get('kind') == 'callback'
    try:
        if callback:
            return {'actions': _tap(bridge, payload)}
        config = bridge.config
        deadline = time.monotonic() + config['yardr_mayor_model']['timeout']
        return {'actions': [view_action(config, deadline, bridge._log)]}
    except Exception:
        if callback:
            return {'actions': [_answer(f'{WHITE} mayor model unavailable: internal error', True)]}
        return send([f'{WHITE} mayor model unavailable: internal error'])


def _answer(text, alert=False):
    return {'type': 'answer_callback', 'text': text, 'alert': alert}


def _tap(bridge, payload):
    config, store, log = bridge.config, bridge.store, bridge._log
    mayor = config['yardr_mayor_model']
    data = payload.get('callback_data')
    pick_id = data[len(MAYOR_MODEL_PREFIX):] if isinstance(data, str) and data.startswith(MAYOR_MODEL_PREFIX) else None
    pick = next((item for item in mayor['picks'] if item['id'] == pick_id), None)
    if pick is None:
        # 1. A button of a pick the settings no longer have runs nothing.
        return [_answer('That model button is out of date.', True)]
    update_id = payload.get('update_id')
    if type(update_id) is not int:
        raise ValueError('update id')
    message_id = payload.get('callback_message_id')
    deadline = time.monotonic() + mayor['timeout']
    crew, label = mayor['crew'], pick['label']

    def finish(status, answer_text, alert=False, outcome=None):
        if outcome:
            log(f'command mayor_model tap (update {update_id}): pick {pick_id} {outcome}')
        action = view_action(config, deadline, log, status=status)
        if type(message_id) is int and message_id > 0:
            action = dict(action, type='edit', message_id=message_id)
        return [_answer(answer_text, alert), action]

    def unknown():
        return finish(f'{WHITE} Switch outcome unknown; not retried. The state below is re-read.',
                      'Switch outcome unknown', True, 'unknown')

    def refused(why):
        return finish(f'{WHITE} Switch refused: {why}; nothing was changed.', 'Switch refused', False, 'refused')

    def replay(row):
        if row['pick'] != pick_id:
            return [_answer('That tap is already recorded.', True)]
        state = row['state']
        if state == 'reserved':
            # A reservation without an outcome: the handler stopped after it,
            # so the set may have run. Unknown, and never retried.
            store.record_mayor_tap(update_id, 'unknown')
            state = 'unknown'
        log(f'command mayor_model tap (update {update_id}): pick {pick_id} replayed {state}')
        if state == 'done':
            return finish(f'✅ {crew} set to {label}.', 'Model set')
        if state == 'refused':
            return finish(f'{WHITE} That switch was refused; nothing was changed.', 'Switch refused')
        return finish(f'{WHITE} That switch outcome is unknown; not retried. The state below is re-read.',
                      'Switch outcome unknown', True)

    # 2. A re-delivered tap answers from its row and never acts again.
    recorded = store.mayor_tap(update_id)
    if recorded is not None:
        return replay(recorded)
    # 3. Only the yard's operator, as the yard maps the paired user, switches.
    try:
        run_asks.verify_operator(bridge.run_asks.client, config)
    except run_asks.NativeError as error:
        not_operator = str(error) == 'operator_not_authorized'
        log(f'command mayor_model tap (update {update_id}): pick {pick_id} refused: '
            + ('operator not authorized' if not_operator else 'operator not confirmed'))
        return [_answer('Switch refused: you are not this yard\'s operator; nothing was changed.' if not_operator
                        else 'Switch refused: the yard did not confirm the operator; nothing was changed.', True)]
    # 4. The crew member as it is now; its revision guards the set.
    try:
        before = read_crew(config, deadline, log)
    except MayorUnavailable as error:
        return refused(str(error))
    if pick_of(mayor, before) is pick:
        return finish(f'{WHITE} {crew} already runs {label}; nothing was changed.', 'Already on that model',
                      outcome='already current')
    # 5. The reservation is committed before anything is set.
    other = store.reserve_mayor_tap(update_id, pick_id, crew)
    if other is not None:
        return replay(other)
    try:
        # 6. The set, refused by yardr when the crew member moved since step 4.
        status, _out, problem = _run_logged(config, set_argv(config, pick, before['revision']),
                                            deadline, 'crew set', log)
        if problem == 'not_started':
            store.record_mayor_tap(update_id, 'refused')
            return refused('yardr not reachable')
        if problem is not None:
            # Killed at the deadline or for its output: the set may have
            # landed, and there is no time left to tell.
            store.record_mayor_tap(update_id, 'unknown')
            return unknown()
        # 7. The crew member read back decides the outcome.
        try:
            after = read_crew(config, deadline, log)
        except MayorUnavailable:
            after = None
        # The pick read back is done, whatever the set exited with.
        if after is None or not has_pick(pick, after):
            if status != 0 and after is not None and unchanged(before, after):
                store.record_mayor_tap(update_id, 'refused')
                return refused('yardr refused the change')
            store.record_mayor_tap(update_id, 'unknown')
            return unknown()
        store.record_mayor_tap(update_id, 'done')
    except Exception:
        # Something failed after the reservation without an outcome: the set
        # may have run, so the tap is unknown and never retried.
        try:
            store.record_mayor_tap(update_id, 'unknown')
        except Exception:
            pass  # the row stays reserved, which reads as unknown
        return unknown()
    # 8. The restart; the switch is done whether or not it is confirmed.
    status, _out, problem = _run_logged(config, restart_argv(config), deadline, 'crew restart', log)
    if problem is None and status == 0:
        return finish(f'✅ {crew} set to {label}; it restarts on it once idle.', 'Model set', outcome='done')
    return finish(f'✅ {crew} set to {label}; the restart was not confirmed, so its next session starts on it.',
                  'Model set; restart not confirmed', True, 'done, restart not confirmed')
