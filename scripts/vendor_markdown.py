#!/usr/bin/env python3
"""Reproduce vendor/ from two verified universal wheels in a local wheelhouse."""
import argparse
import hashlib
from pathlib import Path
import zipfile

PINS = {
    'markdown_it_py-4.2.0-py3-none-any.whl': '9f7ebbcd14fe59494226453aed97c1070d83f8d24b6fc3a3bcf9a38092641c4a',
    'mdurl-0.1.2-py3-none-any.whl': '84008a41e51615a49fc9966191ff91509e3c40b939176e643fd50a5c2196b8f8',
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
        with zipfile.ZipFile(path) as archive:
            for name in archive.namelist():
                if name.startswith('/') or '..' in Path(name).parts or not name.endswith(('.py', '.typed', '/port.yaml', '/METADATA', '/WHEEL', '/LICENSE', '/LICENSE.markdown-it', '/RECORD', '/entry_points.txt')):
                    parser.error('unexpected wheel member in ' + path.name)
            archive.extractall(args.destination)


if __name__ == '__main__':
    main()
