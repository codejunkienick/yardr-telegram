#!/usr/bin/env python3
"""Hand one bot's bridge state from its previous owner to a new installation, and back.

capture copies a stopped previous owner's ledger (SQLite backup), heartbeat and
inbound files of the same bot into a new instance's empty state directory, and
nothing else of the previous directory. handback projects the new ledger's
latest cursor, receipts, asks, pins and accepted rows into the previous
owner's ledger, which must be unchanged since capture. yardr/SETUP.md, "Hand
a bot's state to a new installation", has the steps around both.
"""
import argparse
import contextlib
import ctypes
import datetime
import fcntl
import hashlib
import json
import os
from pathlib import Path
import shutil
import sqlite3
import stat
import subprocess
import sys
import tempfile
import tomllib

sys.path.insert(0, str(Path(__file__).resolve().parents[1] / 'bridge'))
from bridge import SettingsError, load_settings

LEDGER = 'telegram-bridge.sqlite3'
HEARTBEAT = 'telegram-bridge-heartbeat.json'
INBOUND = 'telegram-inbound'
MANIFEST = 'handover.json'
BEFORE_HANDBACK = LEDGER + '.before-handback'
PENDING_SUFFIX = '-transcript.pending'
# The ledger's original tables and columns: every one a previous owner can have.
ORIGINAL = {
    'cursor': ('id', 'offset'),
    'deliveries': ('key', 'fingerprint', 'receipt'),
    'asks': ('ask_id', 'question', 'format', 'options', 'message_id', 'answered', 'answer', 'update_id', 'relayed',
             'kind', 'decided', 'conversation_id'),
    'accepted': ('id', 'message_id', 'accepted_at'),
    'accepted_conversations': ('conversation_id', 'message_id', 'accepted_at'),
    'pins': ('slot', 'generation', 'message_id', 'revision', 'fingerprint'),
    'topics': ('name', 'thread_id', 'source', 'recorded_at'),
    'topic_attempts': ('name', 'attempt'),
    'auto_topics': ('thread_id', 'name', 'name_implicit', 'agent', 'detected_at', 'bound_at'),
}
# Added since, with the columns the bridge creates them with: the message log
# stays with the new state; native rows have no meaning to a previous owner, so
# a handback refuses while any exists.
LATER = {
    'messages': ('seq', 'conversation_id', 'direction', 'message_id', 'sender', 'text', 'at'),
    'run_ask_keys': ('key', 'id', 'request'),
    'run_asks': ('id', 'data'),
    'run_ask_updates': ('binding', 'id'),
    'decide_ids': ('id', 'bead', 'ask'),
    'siding_fold_cards': ('id', 'data'),
    'siding_cards': ('id', 'data'),
    'mayor_model_taps': ('update_id', 'pick', 'crew', 'state', 'at'),
}
NATIVE = set(LATER) - {'messages'}


class Refusal(Exception):
    pass


def bot_ids(path):
    """The [bot] bot_id, user_id and chat_id of an owner-only settings file, read without its other keys."""
    fd = os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK)
    with os.fdopen(fd, 'rb') as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise Refusal(f'{path} must be an owner-only regular file')
        try:
            bot = tomllib.loads(stream.read().decode('utf-8')).get('bot')
        except (ValueError, UnicodeError):
            raise Refusal(f'{path} is not readable TOML') from None
    ids = tuple(bot.get(key) if isinstance(bot, dict) else None for key in ('bot_id', 'user_id', 'chat_id'))
    if any(type(value) is not int or value < 1 for value in ids):
        raise Refusal(f'{path} has no numeric [bot] bot_id, user_id and chat_id')
    return ids


def sha256(path):
    """sha256 of a regular file; a link, FIFO or other special file refuses without blocking on it."""
    digest = hashlib.sha256()
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise Refusal(f'{path} is not a regular file')
    with os.fdopen(os.open(path, os.O_RDONLY | os.O_NOFOLLOW | os.O_NONBLOCK), 'rb') as stream:
        if not stat.S_ISREG(os.fstat(stream.fileno()).st_mode):
            raise Refusal(f'{path} is not a regular file')
        for block in iter(lambda: stream.read(1 << 20), b''):
            digest.update(block)
    return digest.hexdigest()


def heartbeat(state_dir):
    """The heartbeat's path, None when there is none; a link or special file there refuses."""
    path = state_dir / HEARTBEAT
    if not os.path.lexists(path):
        return None
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise Refusal(f'{path} is not a regular file')
    return path


def owner_running(state_dir):
    """True while the heartbeat's pid is a live bridge process of this user."""
    path = heartbeat(state_dir)
    if path is None:
        return False
    try:
        beat = json.loads(path.read_text())
    except (OSError, ValueError):
        raise Refusal(f'{state_dir / HEARTBEAT} is unreadable; stop the owner and check it by hand') from None
    pid = beat.get('pid') if isinstance(beat, dict) else None
    if type(pid) is not int or pid < 1:
        return False
    # ps, not /proc: darwin has none.
    try:
        found = subprocess.run(['ps', '-ww', '-p', str(pid), '-o', 'command='], capture_output=True, timeout=30)
    except (OSError, subprocess.SubprocessError):
        raise Refusal(f'cannot tell whether pid {pid} of {state_dir / HEARTBEAT} runs; stop the owner and check '
                      'it by hand') from None
    return found.returncode == 0 and b'bridge.py' in found.stdout


@contextlib.contextmanager
def bot_lock(settings):
    """Hold the bot's host lock, the one every installed poller of it takes, or refuse."""
    path = settings.lock_dir / ('telegram-bot-' + str(settings.config['bot_id']) + '.lock')
    with path.open('a') as lock:
        try:
            fcntl.flock(lock, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except BlockingIOError:
            raise Refusal('a bridge of this bot is running here; stop its unit first') from None
        yield


def read_only(path):
    for suffix in ('-journal', '-wal'):
        if os.path.lexists(str(path) + suffix):
            raise Refusal(f'{path}{suffix} exists: the owner did not stop cleanly; start and stop it once')
    if not stat.S_ISREG(os.lstat(path).st_mode):
        raise Refusal(f'{path} is not a regular file')
    return sqlite3.connect(uri(path, 'ro'), uri=True, timeout=10)


def uri(path, mode):
    return Path(path).resolve().as_uri() + '?mode=' + mode


def schema(db):
    """{table: columns} of a ledger, refused when it holds a table or column this script does not know."""
    tables = {}
    for (name,) in db.execute("SELECT name FROM sqlite_master WHERE type='table' AND name NOT LIKE 'sqlite_%'"):
        columns = tuple(row[1] for row in db.execute(f'PRAGMA table_info("{name}")'))
        known = ORIGINAL.get(name, LATER.get(name))
        if known is None:
            raise Refusal(f'ledger has an unknown table {name}')
        if set(columns) != set(known):
            raise Refusal(f'ledger table {name} has unknown columns')
        tables[name] = columns
    if db.execute('PRAGMA quick_check').fetchone()[0] != 'ok':
        raise Refusal('ledger failed its integrity check')
    if 'cursor' not in tables or db.execute('SELECT count(*) FROM cursor WHERE id=1').fetchone()[0] != 1:
        raise Refusal('ledger has no cursor')
    return tables


def projection(db, tables, database='main'):
    """sha256 of the rows of these tables, in these columns: the part of a ledger a handback writes."""
    digest = hashlib.sha256()
    for name, columns in sorted(tables.items()):
        listed = ', '.join(f'"{column}"' for column in columns)
        digest.update(name.encode() + b'\n')
        for row in db.execute(f'SELECT {listed} FROM {database}."{name}" ORDER BY {listed}'):
            digest.update(repr(row).encode() + b'\n')
    return digest.hexdigest()


def counts(db, tables):
    return {name: db.execute(f'SELECT count(*) FROM "{name}"').fetchone()[0] for name in sorted(tables)}


def inbound_files(state_dir):
    """{relative path: sha256} of the regular files under the inbound tree; a link or special file refuses."""
    top, found = state_dir / INBOUND, {}
    if not os.path.lexists(top):
        return found
    if not stat.S_ISDIR(os.lstat(top).st_mode):
        raise Refusal(f'{top} is not a plain directory')
    for directory, dirs, files in os.walk(top):
        for name in dirs + files:
            path = Path(directory) / name
            mode = os.lstat(path).st_mode
            if stat.S_ISLNK(mode) or not (stat.S_ISDIR(mode) or stat.S_ISREG(mode)):
                raise Refusal(f'{path} is not a plain file or directory')
            if stat.S_ISREG(mode):
                found[path.relative_to(state_dir).as_posix()] = sha256(path)
    return found


def copy_private(source, target):
    target.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
    fd, name = tempfile.mkstemp(prefix='.handover.', dir=target.parent)
    try:
        with os.fdopen(fd, 'wb') as stream, open(os.open(source, os.O_RDONLY | os.O_NOFOLLOW), 'rb') as origin:
            if not stat.S_ISREG(os.fstat(origin.fileno()).st_mode):
                raise Refusal(f'{source} is not a regular file')
            shutil.copyfileobj(origin, stream)
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, target)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def rename_new(source, target):
    """rename(2) source to target, refusing a target that exists, even as an empty directory."""
    libc = ctypes.CDLL(None, use_errno=True)
    if hasattr(libc, 'renameat2'):  # Linux: AT_FDCWD, RENAME_NOREPLACE
        libc.renameat2.argtypes = (ctypes.c_int, ctypes.c_char_p, ctypes.c_int, ctypes.c_char_p, ctypes.c_uint)
        failed = libc.renameat2(-100, os.fsencode(source), -100, os.fsencode(target), 1)
    elif hasattr(libc, 'renamex_np'):  # darwin: RENAME_EXCL
        libc.renamex_np.argtypes = (ctypes.c_char_p, ctypes.c_char_p, ctypes.c_uint)
        failed = libc.renamex_np(os.fsencode(source), os.fsencode(target), 4)
    else:
        raise Refusal('this system has no rename that refuses an existing target; capture cannot run here')
    if failed:
        error = ctypes.get_errno()
        raise OSError(error, os.strerror(error), str(target))


def write_json(path, value):
    fd, name = tempfile.mkstemp(prefix='.handover.', dir=path.parent)
    try:
        with os.fdopen(fd, 'w') as stream:
            json.dump(value, stream, indent=2, sort_keys=True)
            stream.write('\n')
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(name, path)
    finally:
        if os.path.exists(name):
            os.unlink(name)


def now():
    return datetime.datetime.now(datetime.timezone.utc).strftime('%Y-%m-%dT%H:%M:%SZ')


def capture(settings_path, previous_settings, previous_dir, close_pending=None):
    settings = load_settings(settings_path)
    ids = (settings.config['bot_id'], settings.config['user_id'], settings.config['chat_id'])
    if bot_ids(previous_settings) != ids:
        raise Refusal('the previous settings name another bot or paired chat; only the same bot is handed over')
    # The state dir itself is not resolved: a link there, even to nothing, exists and is refused.
    previous_dir = Path(previous_dir).resolve()
    state_dir = settings.state_dir.parent.resolve() / settings.state_dir.name
    if previous_dir == state_dir:
        raise Refusal('the previous and new state directories are the same')
    if owner_running(previous_dir):
        raise Refusal('the previous owner is still running; stop and disable its unit first')
    with bot_lock(settings):
        files, beat = inbound_files(previous_dir), heartbeat(previous_dir)
        source = previous_dir / LEDGER
        digest = sha256(source)
        db = read_only(source)
        # Staged in memory: nothing is written until every check has passed.
        copy = sqlite3.connect(':memory:')
        try:
            tables = schema(db)
            if any(counts(db, set(tables) & NATIVE).values()):
                raise Refusal('the previous ledger holds native card rows; it is not a chat-only bot')
            pending = [row[0] for row in db.execute('SELECT ask_id FROM asks WHERE answered=0 ORDER BY ask_id')]
            if pending and close_pending is None:
                raise Refusal(f'{len(pending)} ask(s) still open at the freeze: answer or close them with the '
                              'previous owner, or pass --close-pending <decision> to archive them as decided')
            copy.execute(f"PRAGMA page_size={db.execute('PRAGMA page_size').fetchone()[0]}")
            db.backup(copy)
            with copy:
                # Archived as a crew close would: decided, nothing relayed, so a late tap runs nothing.
                copy.executemany('UPDATE asks SET answered=1, answer=?, decided=1, relayed=1 '
                                 'WHERE ask_id=? AND answered=0', [(close_pending, ask) for ask in pending])
            offset = copy.execute('SELECT offset FROM cursor WHERE id=1').fetchone()[0]
            captured = counts(copy, tables)
            if sha256(source) != digest:
                raise Refusal('the previous ledger changed during capture; stop its owner')
            if os.path.lexists(state_dir):
                raise Refusal(f'{state_dir} exists: capture runs once, into a state directory not made yet')
            # Built in a sibling of its own and renamed into place last: nothing is written where it did not make.
            made, parent = [], state_dir.parent
            while not os.path.lexists(parent):
                made.append(parent)
                parent = parent.parent
            state_dir.parent.mkdir(parents=True, mode=0o700, exist_ok=True)
            staging = Path(tempfile.mkdtemp(prefix=f'.{state_dir.name}.handover-', dir=state_dir.parent))
            try:
                with contextlib.closing(sqlite3.connect(staging / LEDGER)) as out:
                    out.execute('PRAGMA synchronous=FULL')
                    copy.backup(out)
                os.chmod(staging / LEDGER, 0o600)
                for name in files:
                    copy_private(previous_dir / name, staging / name)
                if beat:
                    copy_private(beat, staging / HEARTBEAT)
                write_json(staging / MANIFEST, dict(
                    version=1, captured_at=now(), source=str(previous_dir), source_sha256=digest, bot_id=ids[0],
                    user_id=ids[1], chat_id=ids[2],
                    schema={name: list(columns) for name, columns in sorted(tables.items())}, counts=captured,
                    offset=offset, closed_pending=pending, files=files))
                try:
                    rename_new(staging, state_dir)
                except FileExistsError:
                    raise Refusal(f'{state_dir} appeared during capture; start the new bridge only after '
                                  'capture reports success') from None
            except BaseException:
                # A run that does not finish takes back only what it made: its staging and parents left empty.
                shutil.rmtree(staging, ignore_errors=True)
                for path in made:
                    with contextlib.suppress(OSError):
                        os.rmdir(path)
                raise
            fd = os.open(state_dir.parent, os.O_RDONLY | os.O_DIRECTORY)
            try:
                os.fsync(fd)
            finally:
                os.close(fd)
        finally:
            copy.close()
            db.close()
    return (f'captured {sum(captured.values())} ledger rows at offset {offset}, {len(files)} inbound file(s), '
            f'{len(pending)} open ask(s) archived; source ledger sha256 {digest}')


def handback(settings_path, previous_dir):
    settings = load_settings(settings_path)
    state_dir, previous_dir = settings.state_dir.resolve(), Path(previous_dir).resolve()
    try:
        manifest = json.loads((state_dir / MANIFEST).read_text())
    except (OSError, ValueError):
        raise Refusal(f'{state_dir / MANIFEST} is missing or unreadable: this state was not captured') from None
    if manifest.get('version') != 1 or manifest.get('source') != str(previous_dir):
        raise Refusal('the manifest names another previous state directory')
    ids = [settings.config[key] for key in ('bot_id', 'user_id', 'chat_id')]
    if [manifest.get(key) for key in ('bot_id', 'user_id', 'chat_id')] != ids:
        raise Refusal('the settings name another bot or paired chat than the one captured')
    record = manifest.get('handback') or {}
    if record.get('state') == 'done':
        raise Refusal('this state was already handed back')
    if owner_running(previous_dir):
        raise Refusal('the previous owner is running; stop it before a handback')
    beat = heartbeat(state_dir)
    with bot_lock(settings):
        target = previous_dir / LEDGER
        new = read_only(state_dir / LEDGER)
        try:
            tables = schema(new)
            if any(counts(new, set(tables) & NATIVE).values()):
                raise Refusal('the new ledger holds native card rows the previous owner cannot keep')
            offset = new.execute('SELECT offset FROM cursor WHERE id=1').fetchone()[0]
        finally:
            new.close()
        handed, files = inbound_files(previous_dir), inbound_files(state_dir)
        if record.get('state') == 'writing':
            # Opening the ledger read-write rolls back a transaction the crash left open.
            with contextlib.closing(sqlite3.connect(uri(target, 'rw'), uri=True, timeout=10)) as db:
                db.execute('SELECT count(*) FROM sqlite_master').fetchone()
        if record and sha256(target) != manifest.get('source_sha256'):
            # The ledger's transaction committed before a crash: only the files are left, unless either owner ran.
            db = read_only(target)
            try:
                old = schema(db)
                if projection(db, old) != record.get('projection'):
                    raise Refusal('the previous ledger changed since the handback began: both owners ran; '
                                  'reconcile by hand')
                db.execute('ATTACH DATABASE ? AS new', (uri(state_dir / LEDGER, 'ro'),))
                if projection(db, old, 'new') != record['projection']:
                    raise Refusal('the new owner ran after the ledger was handed back; reconcile by hand')
            finally:
                db.close()
        else:
            if sha256(target) != manifest.get('source_sha256'):
                raise Refusal('the previous ledger changed since capture: both owners ran; reconcile by hand')
            before = previous_dir / BEFORE_HANDBACK
            # One left by a run that died before its transaction is the same captured ledger.
            if os.path.lexists(before) and not (stat.S_ISREG(os.lstat(before).st_mode)
                                                and sha256(before) == manifest['source_sha256']):
                raise Refusal(f'{before} exists; reconcile by hand')
            if offset < manifest.get('offset', 0):
                raise Refusal('the new cursor is behind the captured one')
            with contextlib.closing(read_only(target)) as db:
                old = schema(db)
                if set(old) - set(tables):
                    raise Refusal('the new ledger lacks a table of the previous one')
                # A run that began wrote down what it hands back; a new ledger rewound since then is not it.
                db.execute('ATTACH DATABASE ? AS new', (uri(state_dir / LEDGER, 'ro'),))
                if record and projection(db, old, 'new') != record.get('projection'):
                    raise Refusal('the new ledger is not the one the handback began with; reconcile by hand')
            copy_private(target, before)
            db = sqlite3.connect(uri(target, 'rw'), uri=True, timeout=10)
            try:
                db.execute('PRAGMA synchronous=FULL')
                old = schema(db)
                db.execute('ATTACH DATABASE ? AS new', (uri(state_dir / LEDGER, 'ro'),))
                record = dict(state='writing', projection=projection(db, old, 'new'), offset=offset, at=now())
                manifest['handback'] = record
                write_json(state_dir / MANIFEST, manifest)
                with db:
                    db.execute('BEGIN IMMEDIATE')
                    for name, columns in old.items():
                        listed = ', '.join(f'"{column}"' for column in columns)
                        db.execute(f'DELETE FROM main."{name}"')
                        db.execute(f'INSERT INTO main."{name}" ({listed}) SELECT {listed} FROM new."{name}"')
                db.execute('DETACH DATABASE new')
            finally:
                db.close()
        record = dict(record, state='ledger', ledger_sha256=sha256(target))
        manifest['handback'] = record
        write_json(state_dir / MANIFEST, manifest)
        for name in files:
            if name.endswith(PENDING_SUFFIX) or name not in handed:
                copy_private(state_dir / name, previous_dir / name)
        # A job the new owner finished is not run twice; one it queued goes back with its audio.
        for name in handed:
            if name.endswith(PENDING_SUFFIX) and name not in files:
                os.unlink(previous_dir / name)
        if beat:
            copy_private(beat, previous_dir / HEARTBEAT)
        manifest['handback'] = dict(record, state='done', files=len(files), done_at=now())
        write_json(state_dir / MANIFEST, manifest)
    return (f"handed back offset {record['offset']} and {len(files)} inbound file(s); "
            f"ledger sha256 {record['ledger_sha256']}")


def main():
    parser = argparse.ArgumentParser(description=__doc__, formatter_class=argparse.RawDescriptionHelpFormatter)
    commands = parser.add_subparsers(dest='command', required=True)
    take = commands.add_parser('capture', help="copy a stopped previous owner's state of this bot")
    take.add_argument('--settings', type=Path, required=True, help="the new instance's settings")
    take.add_argument('--from-settings', type=Path, required=True, help="the previous owner's settings")
    take.add_argument('--from', dest='source', type=Path, required=True, help="the previous owner's state directory")
    take.add_argument('--close-pending', metavar='DECISION', help='archive asks still open as decided with this text')
    give = commands.add_parser('handback', help="write the new state back into the previous owner's ledger")
    give.add_argument('--settings', type=Path, required=True, help="the new instance's settings")
    give.add_argument('--to', type=Path, required=True, help="the previous owner's state directory")
    args = parser.parse_args()
    try:
        if args.command == 'capture':
            print(capture(args.settings, args.from_settings, args.source, args.close_pending))
        else:
            print(handback(args.settings, args.to))
    except (Refusal, SettingsError) as error:
        print('handover refused: ' + str(error), file=sys.stderr)
        return 1
    except (OSError, sqlite3.Error):
        # Filesystem and SQLite messages can carry private paths or row contents.
        print('handover failed part way; check both state directories before running it again', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
