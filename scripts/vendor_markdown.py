#!/usr/bin/env python3
"""Reproduce vendor/ from verified local archives, without native extensions."""
import argparse
import hashlib
from pathlib import Path
import zipfile
import tarfile

PINS = {
    'markdown_it_py-4.2.0-py3-none-any.whl': '9f7ebbcd14fe59494226453aed97c1070d83f8d24b6fc3a3bcf9a38092641c4a',
    'mdurl-0.1.2-py3-none-any.whl': '84008a41e51615a49fc9966191ff91509e3c40b939176e643fd50a5c2196b8f8',
    'pyyaml-6.0.3.tar.gz': 'd76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f',
}


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument('wheelhouse', type=Path)
    parser.add_argument('destination', type=Path)
    args = parser.parse_args()
    wheels = []
    for name, digest in PINS.items():
        path = args.wheelhouse / name
        if hashlib.sha256(path.read_bytes()).hexdigest() != digest:
            parser.error('wheel hash mismatch: ' + name)
        wheels.append(path)
    for path in wheels:
        if path.suffix == '.gz':
            with tarfile.open(path) as archive:
                for member in archive.getmembers():
                    name = member.name.removeprefix('pyyaml-6.0.3/')
                    if name.startswith('lib/yaml/') and name.endswith('.py'):
                        target = args.destination / name.removeprefix('lib/')
                    elif name == 'LICENSE':
                        target = args.destination / 'pyyaml-LICENSE'
                    else:
                        continue
                    if not member.isfile() or '..' in Path(name).parts:
                        parser.error('unexpected source member in ' + path.name)
                    target.parent.mkdir(parents=True, exist_ok=True)
                    target.write_bytes(archive.extractfile(member).read())
            continue
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.startswith('/') or '..' in Path(name).parts or not name.endswith(('.py', '.typed', '/port.yaml', '/METADATA', '/WHEEL', '/LICENSE', '/LICENSE.markdown-it', '/RECORD', '/entry_points.txt')):
                    parser.error('unexpected wheel member in ' + path.name)
            archive.extractall(args.destination)


if __name__ == '__main__':
    main()
