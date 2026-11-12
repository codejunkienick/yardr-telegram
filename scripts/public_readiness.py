#!/usr/bin/env python3
"""Scan committed files and optionally export that exact tree for local review."""
import argparse
import hashlib
import io
import json
from pathlib import Path
import re
import subprocess
import sys
import tarfile


# Deliberately narrow: generic homes and author/third-party attribution are valid.
PATTERNS = {
    'bot-token': rb'(?<![0-9])[0-9]{6,12}:[A-Za-z0-9_-]{35}(?![A-Za-z0-9_-])',
    'private-key': rb'-----BEGIN (?:RSA |EC |OPENSSH |DSA |ENCRYPTED )?PRIVATE KEY-----',
    'operator-path': rb'/(?:home|Users)/ni' + rb'ck(?:/|\b)',
}
RUNTIME_NAMES = {'telegram-bot-token', 'callback-secret', 'hook-secret', '.env'}
# Exact compatibility tokens do not exempt the rest of their line or file.
RELATIONSHIP = re.compile(
    rb'gas[\s_-]*city|\bextms[g]\b|\b19973[c]a\b|'
    rb'(?<!\w)\.?g[c](?:[_.]\w*)?\b|\b\w+_g[c]_\w+\b', re.IGNORECASE)
COMPATIBILITY_NAMES = {b'GC_RECEIPT', b'_print_gc_receipt', b'GC_SETTINGS',
                       b'gc_base_url', b'gc_api_token_file'}
LOOKUP_FIXTURES = {b'.g' + b'c', b'G' + b'C_CITY', b'G' + b'C_CITY_PATH', b'G' + b'C_'}
GARBAGE_COLLECTION = {b'import g' + b'c', b'g' + b'c.collect()'}
# Reviewed binaries, each accepted only at its path with its exact SHA-256: the offline
# wheelhouse and the two synthetic README captures. Any other binary, or changed bytes, refuse.
REVIEWED_BINARIES = {
    'vendor/wheels/pyyaml-6.0.3.tar.gz': 'd76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f',
    'vendor/wheels/markdown_it_py-4.2.0-py3-none-any.whl': '9f7ebbcd14fe59494226453aed97c1070d83f8d24b6fc3a3bcf9a38092641c4a',
    'vendor/wheels/mdurl-0.1.2-py3-none-any.whl': '84008a41e51615a49fc9966191ff91509e3c40b939176e643fd50a5c2196b8f8',
    'docs/images/native-card-confirmation.png': '57ba182adcaf3b324587763823e2d6c597b0dcba3db52c19b291a9698ea127e8',
    'docs/images/review-bundle.png': '55fae6e155178a8af861ff5b877017fcdcddea44c078d1bfdac0683aa98dfec4',
}


class Refusal(Exception):
    pass


def git(root, *args, data=None):
    result = subprocess.run(['git', '-C', str(root), *args], input=data, capture_output=True)
    if result.returncode:
        raise Refusal('cannot read committed tree')
    return result.stdout


def committed_files(root):
    commit = git(root, 'rev-parse', '--verify', 'HEAD^{commit}').decode().strip()
    tree = git(root, 'rev-parse', 'HEAD^{tree}').decode().strip()
    entries = git(root, 'ls-tree', '-rz', '--full-tree', commit).split(b'\0')
    files = []
    for entry in filter(None, entries):
        metadata, name = entry.split(b'\t', 1)
        mode, kind, oid = metadata.decode().split()
        if kind != 'blob' or mode not in ('100644', '100755'):
            raise Refusal('tree contains an unsupported entry type; review before export')
        path = name.decode('utf-8')
        content = git(root, 'cat-file', 'blob', oid)
        files.append((path, mode, oid, content))
    return commit, tree, files


def scan(files):
    findings = []
    for name, mode, oid, content in files:
        path = Path(name)
        for category, pattern in PATTERNS.items():
            # Tests may carry former operator paths as explicit migration fixtures.
            if category == 'operator-path' and (path.name.startswith('test_') or 'fixtures' in path.parts):
                continue
            for number, line in enumerate(content.splitlines(), 1):
                count = len(re.findall(pattern, line))
                if count:
                    findings.append(dict(path=name, line=number, category=category, count=count))
        if b'\0' not in content:
            counts = {}
            for match in RELATIONSHIP.finditer(content):
                token = match.group()
                if token in COMPATIBILITY_NAMES:
                    continue
                if name == 'bridge/test_settings.py' and token in LOOKUP_FIXTURES:
                    continue
                start = content.rfind(b'\n', 0, match.start()) + 1
                end = content.find(b'\n', match.end())
                line = content[start:end if end != -1 else len(content)].strip()
                if name == 'bridge/test_bridge.py' and line in GARBAGE_COLLECTION:
                    continue
                number = content.count(b'\n', 0, match.start()) + 1
                counts[number] = counts.get(number, 0) + 1
            for number, count in sorted(counts.items()):
                findings.append(dict(path=name, line=number, category='relationship-reference', count=count))
        if path.name in RUNTIME_NAMES or path.suffix in ('.db', '.sqlite', '.sqlite3', '.env'):
            findings.append(dict(path=name, line=0, category='runtime-file', count=1))
        if b'\0' in content and REVIEWED_BINARIES.get(name) != hashlib.sha256(content).hexdigest():
            findings.append(dict(path=name, line=0, category='binary-needs-review', count=1))
    return findings


def export(root, destination, commit, tree, files):
    destination = destination.resolve()
    if destination.is_relative_to(root.resolve()):
        raise Refusal('export directory must be outside the worktree')
    archive = git(root, 'archive', '--format=tar', commit)
    expected = {name: (mode, content) for name, mode, oid, content in files}
    seen = set()
    with tarfile.open(fileobj=io.BytesIO(archive)) as tar:
        for member in tar:
            if member.isdir():
                continue
            if not member.isfile() or member.name not in expected or member.name in seen:
                raise Refusal('archive differs from committed tree')
            mode, content = expected[member.name]
            if tar.extractfile(member).read() != content or bool(member.mode & 0o111) != (mode == '100755'):
                raise Refusal('archive differs from committed tree')
            seen.add(member.name)
    if seen != set(expected):
        raise Refusal('archive omits committed files; review export attributes')
    manifest = dict(schema=1, commit=commit, tree=tree, archive='source.tar',
                    archive_sha256=hashlib.sha256(archive).hexdigest(),
                    files=[dict(path=name, mode=mode, blob=oid, size=len(content),
                                sha256=hashlib.sha256(content).hexdigest())
                           for name, mode, oid, content in files])
    # Refuse an existing destination; never scrub, overwrite or extract human files.
    destination.mkdir(mode=0o700)
    (destination / 'source.tar').write_bytes(archive)
    (destination / 'manifest.json').write_text(json.dumps(manifest, indent=2, ensure_ascii=True) + '\n')


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('--export', type=Path, help='create a new directory outside this worktree')
    args = parser.parse_args()
    root = Path.cwd()
    try:
        root = Path(git(root, 'rev-parse', '--show-toplevel').decode().strip())
        if git(root, 'status', '--porcelain', '--untracked-files=normal'):
            raise Refusal('worktree has changes; review and commit before scanning')
        commit, tree, files = committed_files(root)
        findings = scan(files)
        for finding in findings:
            print(json.dumps(finding, ensure_ascii=True))
        print(json.dumps(dict(commit=commit, tree=tree, files=len(files), findings=sum(f['count'] for f in findings))))
        if findings:
            return 1
        if args.export:
            export(root, args.export, commit, tree, files)
        return 0
    except Refusal as exc:
        print('public-readiness refused: ' + str(exc), file=sys.stderr)
        return 1
    except (OSError, ValueError, UnicodeError, tarfile.TarError):
        # Underlying errors may contain private filenames or input; never echo them.
        print('public-readiness refused: check git tree, clean status, entry types and new external export directory', file=sys.stderr)
        return 1


if __name__ == '__main__':
    sys.exit(main())
