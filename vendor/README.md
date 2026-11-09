# Pinned Markdown runtime

Unmodified contents of universal `py3-none-any` wheels, verified against
versioned PyPI metadata and downloaded artifact SHA-256 on 2026-11-09:

| Distribution | Wheel SHA-256 | Requires Python | Runtime requirements |
| --- | --- | --- | --- |
| [markdown-it-py 4.2.0](https://pypi.org/pypi/markdown-it-py/4.2.0/json) | `9f7ebbcd14fe59494226453aed97c1070d83f8d24b6fc3a3bcf9a38092641c4a` | >=3.10 | mdurl~=0.1 |
| [mdurl 0.1.2](https://pypi.org/pypi/mdurl/0.1.2/json) | `84008a41e51615a49fc9966191ff91509e3c40b939176e643fd50a5c2196b8f8` | >=3.7 | none |

No extras. Both are MIT; full notices including upstream markdown-it and
Joyent attribution are retained in their `.dist-info` license files.
Python >=3.11 remains the bridge minimum. Wheel METADATA, WHEEL and RECORD
are retained, so extracted bytes can be verified independently.

The three metadata files named in the root `.gitattributes` retain upstream
trailing blank lines. Only Git's `blank-at-eof` check is disabled for those
files; all other whitespace checks remain enabled.

The original verified wheels are retained in `wheels/` as the offline wheelhouse.
To reproduce, run `python3 scripts/vendor_markdown.py vendor/wheels <empty-dir>`.
The script verifies both hashes before extracting anything, uses stdlib only,
and never downloads or installs. Compare that directory to vendor/ excluding
this README and wheels/. This is a maintainer operation, never a message-time action.

The existing installer archives these files with each release and covers
them with its SHA256SUMS. bridge.py puts that release's vendor/ first on its
import path. CLI, Linux unit and macOS LaunchAgent use the same `python`
setting and release; no pip, system/user package changes or shared venv.

Source-add review also ships the unmodified pure-Python `lib/yaml/*.py`
from [PyYAML 6.0.3](https://pypi.org/pypi/PyYAML/6.0.3/json), MIT licensed
(`pyyaml-LICENSE`). The official source archive is retained in `wheels/`:
SHA-256 `d76623373421df22fb4cf8817020cbb7ef15c725b9d5e45f17e189bfc384190f`.
The same reproduction command verifies all three archive hashes before
writing; no C extension is built or shipped. The bridge uses BaseLoader's
syntax tree only, rejects aliases, tags and duplicate keys, and leaves core
responsible for declaration semantics and approval. No package installation
is needed on the operator's machine.

Upstream YAML source whitespace is preserved byte for byte; its scoped Git
attribute permits upstream trailing spaces and trailing blank lines only.
