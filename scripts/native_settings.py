#!/usr/bin/env python3
"""Check or add exactly yardr.home/operator without rewriting other settings."""
import argparse
import fcntl
import json
import os
from pathlib import Path
import re
import stat
import sys
import tempfile
import tomllib


def private_file(path, flags):
    fd = os.open(path, flags | os.O_NOFOLLOW | os.O_NONBLOCK, 0o600)
    info = os.fstat(fd)
    if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
        os.close(fd)
        raise ValueError('settings and lock must be owner-only regular files')
    return fd


def configure(path, home, operator, apply=False):
    path, home = Path(path), Path(home).expanduser().resolve()
    info = home.stat()
    if not stat.S_ISDIR(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o022:
        raise ValueError('native home must be an owner-held directory without group/world writes')
    if not re.fullmatch(r'[A-Za-z0-9][A-Za-z0-9._-]*', operator):
        raise ValueError('invalid native operator name')
    lock = private_file(str(path) + '.native.lock', os.O_RDWR | os.O_CREAT) if apply else None
    try:
        if lock is not None:
            fcntl.flock(lock, fcntl.LOCK_EX)
        with os.fdopen(private_file(path, os.O_RDONLY), 'rb') as stream:
            before = stream.read()
            identity = os.fstat(stream.fileno())
        try:
            source = before.decode('utf-8')
            parsed = tomllib.loads(source)
        except (ValueError, UnicodeError):
            raise ValueError('invalid settings TOML') from None
        yard = parsed.get('yardr')
        if not isinstance(yard, dict):
            raise ValueError('settings require a yardr table')
        expected = dict(home=str(home), operator=operator)
        missing = {key: value for key, value in expected.items() if key not in yard}
        for key, value in expected.items():
            present = yard.get(key)
            if key == 'home' and isinstance(present, str):
                present = str(Path(present).expanduser().resolve())
            if key in yard and present != value:
                raise ValueError('conflicting yardr.' + key + '; no settings changed')
        if not missing:
            return 'native settings match'
        # Refuse inline or unfamiliar table spellings rather than reserialize secret-bearing TOML.
        matches = list(re.finditer(r'^\[yardr\][ \t]*(?:#[^\r\n]*)?\r?$', source, re.MULTILINE))
        if len(matches) != 1:
            raise ValueError('native settings need a literal [yardr] table')
        newline = '\r\n' if '\r\n' in source else '\n'
        insert = ''.join(newline + key + ' = ' + json.dumps(value, ensure_ascii=True) for key, value in missing.items())
        at = matches[0].end()
        if source[at - 1:at] == '\r':
            at -= 1
        after = (source[:at] + insert + source[at:]).encode('utf-8')
        changed = tomllib.loads(after.decode())
        if changed != dict(parsed, yardr=dict(yard, **missing)):
            raise ValueError('native settings edit could not preserve the document')
        if not apply:
            return 'would add ' + ', '.join('yardr.' + key for key in missing)
        current = path.lstat()
        if (current.st_dev, current.st_ino, current.st_size, current.st_mtime_ns) != (
                identity.st_dev, identity.st_ino, identity.st_size, identity.st_mtime_ns):
            raise ValueError('settings changed during check; retry')
        backup = path.with_name(path.name + '.before-native')
        with os.fdopen(os.open(backup, os.O_WRONLY | os.O_CREAT | os.O_EXCL | os.O_NOFOLLOW, 0o600), 'wb') as stream:
            stream.write(before)
            stream.flush()
            os.fsync(stream.fileno())
        fd, name = tempfile.mkstemp(prefix='.' + path.name + '.', dir=path.parent)
        try:
            with os.fdopen(fd, 'wb') as stream:
                stream.write(after)
                stream.flush()
                os.fsync(stream.fileno())
            os.replace(name, path)
        finally:
            if os.path.exists(name):
                os.unlink(name)
        return 'added ' + ', '.join('yardr.' + key for key in missing) + '; owner-only backup created'
    finally:
        if lock is not None:
            os.close(lock)


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--settings', type=Path, required=True)
    parser.add_argument('--home', required=True)
    parser.add_argument('--operator', required=True)
    parser.add_argument('--apply', action='store_true', help='write missing keys after validation; default only checks')
    args = parser.parse_args()
    try:
        print(configure(args.settings, args.home, args.operator, args.apply))
    except (OSError, ValueError):
        # TOML and filesystem exceptions can contain secrets or raw input.
        print('native settings refused; check ownership, TOML, home/operator conflicts and backup existence', file=sys.stderr)
        return 1
    return 0


if __name__ == '__main__':
    sys.exit(main())
