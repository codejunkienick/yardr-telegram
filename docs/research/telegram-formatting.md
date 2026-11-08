# Markdown and HTML at the Telegram formatting boundary

Checked 2026-11-08. Repository observations refer to commit
`aad8776fa7b7d758cc9a20e2e6b8d2fa5e37cb9c`; relative links below identify that
snapshot's source. This is a research recommendation, not an approved runtime
change. Upstream metadata and release artifacts were read without installing or
executing them. No Telegram requests or account inspection were performed.

## Recommendation

Support explicit `format=markdown` alongside the existing formats. Keep every
current default: API/source `auto` continues to choose HTML or plain; direct
publishing and CLI calls remain plain. Parse Markdown with a pinned OSS parser,
then render a small, documented subset into the bridge's existing Telegram HTML
path. Recommended candidate: `markdown-it-py==4.2.0` with
`mdurl==0.1.2`, no extras, raw HTML disabled, and a narrow token renderer. This
uses an existing delivery seam while outsourcing Markdown syntax; it does not
require a bot SDK or a new transport. The parser exposes tokens and configurable
rendering, but its default HTML renderer produces ordinary web HTML, so it is
not itself a Telegram converter. [Parser API][mdit-main],
[renderer][mdit-renderer], [current formatting/publishing][publish].

Prefer this over telegramify's general conversion pipeline for this bridge:
the released converter already handles Telegram entities, but adopting that
output would touch every text/caption/edit path, and its built-in math and HTML
policies do not meet the proposed literal-content policy without further work.
This is an integration-cost judgment, not a claim that a parser is universally
better than a converter. [Released converter artifact][tg-wheel],
[delivery paths][deliver-text], [caption edits][caption-edit].

## What Telegram supports, and what the bridge does

Classic `sendMessage` accepts either `parse_mode` or explicit `entities`.
Telegram's classic HTML is a restricted tag vocabulary; MarkdownV2 is a separate
Telegram dialect, including `*bold*`, contextual backslash escaping, and fenced
code. Ordinary CommonMark such as `**bold**` is therefore not interchangeable
with MarkdownV2. Legacy `Markdown` has fewer features and disallows nesting.
Entity offsets and lengths count UTF-16 code units. [sendMessage][api-send],
[classic formatting][api-format], [MessageEntity][api-entity].

There is also a genuinely separate Rich Message API: `sendRichMessage` takes an
`InputRichMessage` with exactly one of `html`, `markdown`, or `blocks`. Its Rich
Markdown supports headings, tables, lists, formulas and media, and can contain
HTML. Those are not features enabled by switching classic `parse_mode`.
The checked API page identifies 10.3, dated 2026-08-24; Rich Message support first
appeared in 10.1 and gained explicit blocks/media in 10.2. Do not silently move
this bridge to that API to solve fenced text. [Rich Message method][api-rich],
[input object][api-rich-input], [rich formatting][api-rich-format],
[official change history][api-changes].

The present bridge's behavior is fully explained by its source:

| Entry point | Current behavior | Later Markdown integration |
| --- | --- | --- |
| `auto_format` | Only attempts HTML when `<` or `&` occurs; valid subset selects HTML, otherwise plain | Preserve the function's decisions |
| `api_send`, `api_pin` | Default `auto`, resolve to plain/HTML | Accept explicit `markdown` without changing default |
| `_publish`, CLI `--format`, `pin`, command send/edit actions | Plain default; only plain/HTML supported | Add explicit mode through the same validators, where exposed |
| `yardr/tgbridge/thread.reply` | Unconditionally supplies `format: auto` | Needs an explicit format transport before ordinary thread replies can request Markdown |
| `yardr/tgbridge.yaml` send/pin | Enum currently `auto`, `plain`, `html` | Extend enum with the explicit mode |

Sources: [`auto_format`][auto], [send API][api-send-local],
[pin API][api-pin-local], [publishing][publish], [CLI][cli],
[pin][pin], [command validation][commands], [thread adapter][thread],
[source contract][source-contract].

Consequently, a Markdown fence with no `<` or `&` goes out as plain text and its
backticks remain visible. Even a fence containing valid `<b>x</b>` can select
HTML: the tags format, while the fence markers remain. Auto is not a mixed
Markdown/HTML parser. Adding fence detection changes meanings for existing
callers containing examples, shell operators, identifiers or literal markup;
there is no reliable intent detector in the current contract. [Auto selection][auto].

The local HTML subset is deliberately narrower than Telegram's: it allows
`b/strong/i/em/u/s/code/pre/a/blockquote/tg-spoiler`, limits link schemes to
HTTP/HTTPS/TG, and permits a code language class only inside `pre`. The HTML
validator rejects unbalanced/unknown markup and invalid numeric entities;
it bounds source at 16384 UTF-16 units. Reuse it rather than introducing a
second, broader HTML policy. [HTML rules and validator][html-rules].

## Released options

These are checked candidates, not installed capabilities. Release dates support
the maintenance assessment; they do not prove correctness on this bridge.

| Candidate and checked release | Actual role and footprint | Fit here |
| --- | --- | --- |
| **telegramify-markdown 1.4.2**, PyPI 2026-10-30 | MIT, Python >=3.10, universal Python wheel; mandatory `pyromark>=0.7.0`; `convert` emits text/entities; optional Mermaid extra adds Pillow/aiohttp | Most Telegram-specific support, but new entity plumbing and policy/compiled-dependency costs |
| **markdown-it-py 4.2.0**, 2026-05-07, plus mdurl 0.1.2 | MIT parser; Python >=3.10; universal wheels; only mdurl required without extras | Recommended parser plus small bridge-owned Telegram renderer |
| **Mistune 3.4.0**, 2026-10-19 | BSD-3-Clause parser, Python >=3.10; universal wheel; no required runtime dependencies on this project's Python >=3.11 | A genuinely smaller dependency graph; still needs the same Telegram renderer and policy |
| **python-telegram-bot 22.8**, 2026-06-12 | LGPL-3.0-only package metadata; Python >=3.10; SDK with HTTPX and conditional HTTPcore dependency | Its escaping helper does not convert CommonMark; transport migration brings no demonstrated formatting gain |

Evidence: versioned [telegramify metadata][tg-meta] and [artifact][tg-wheel];
[markdown-it release][mdit-release], [metadata][mdit-meta],
[license][mdit-license], [mdurl metadata][mdurl-meta] and [license][mdurl-license];
[Mistune release][mistune-release], [metadata][mistune-meta],
[license][mistune-license]; [PTB metadata][ptb-meta] and [helper source][ptb-helpers].

### Telegramify: verify the release, not the main README

The checked GitHub latest release was **v1.4.1**, commit
`70394c114cbe3b1d155032260d42e6b3c5e72806`; PyPI had the newer **1.4.2**,
tagged **pypi_1.4.2** at commit
`3535c87ecc41e7861bc5f4ffa400871bc93e9075`. The findings here
use that tagged source and the actual PyPI 1.4.2 wheel, SHA-256
`590001fe4da043a7d3c4907541279e4e389bf9412762ac5d996145ce1fbdda1a`,
not an assumed correspondence with current main. [GitHub latest release][tg-release],
[release tag][tg-tag], [version metadata with hashes][tg-meta], [wheel][tg-wheel].

In that artifact, `convert(markdown, *, latex_escape=True, config=None)` returns
`(str, list[MessageEntity])`; entities have `to_dict()`. Compatibility wrappers
`markdownify`/`standardize` still return MarkdownV2 strings. `split_entities`
exists, but the higher `telegramify()` pipeline can split output, extract code
files and render Mermaid; `richify()` is a parallel Rich Message backend.
Those pipelines must not be adopted incidentally. [1.4.2 public API][tg-api],
[entities][tg-entities], [pipeline][tg-pipeline], [Rich backend][tg-rich].

Important policy mismatch found by source inspection: `STANDARD_OPTIONS` enables
math, and `_on_inline_math`/`_on_display_math` can translate LaTeX symbols even
when `latex_escape=False`; that flag disables a preprocessing step, not the
math event handlers. Block HTML is ignored and most inline HTML tags are
ignored. Thus neither faithful arbitrary mixed HTML nor a complete no-math-
transformation policy follows from a single advertised switch. This was not
executed as a runtime reproduction. The synchronous conversion call graph has
no network operation in the inspected core, but the optional Mermaid renderer
downloads from an external rendering service. Restricting use to core
conversion would avoid that pipeline; it would not resolve the policy mismatch.
[1.4.2 converter][tg-converter], [Mermaid renderer][tg-mermaid].

The currently released **pyromark 0.10.4** requires Python >=3.11 and
`typing-extensions>=4.4.0`; its Rust extension uses pulldown-cmark, PyO3 and
syntax-highlighting crates. Release files include CPython 3.11/3.13 Linux
x86_64/aarch64 manylinux and musllinux wheels, and macOS x86_64/arm64 wheels.
That covers the common CI/desktop combinations by file availability, not by a
tested installation here; different interpreters/architectures need a matching
wheel or a Rust source build. Pinning only telegramify leaves pyromark open to
change. [Versioned metadata/file list][pyromark-meta],
[Rust manifest][pyromark-cargo], [build configuration][pyromark-build],
[MIT license][pyromark-license].

### Parser and SDK distinctions

`MarkdownIt()` defaults to CommonMark with raw HTML enabled. For this adapter,
use `MarkdownIt("commonmark", {"html": False, "linkify": False,
"typographer": False})`, enable only selected rules such as strikethrough, and
write an exhaustive token renderer. Disable plugins, math, highlighting and
image fetching. HTML-disabled input becomes escaped literal text; supported
Markdown is translated to local allowed tags and newlines, never passed
through generic web HTML. Unknown renderer tokens must fail with a stable
reason, not be dropped or rendered by a generic fallback. [Security guidance][mdit-security],
[4.2.0 preset][mdit-preset], [renderer fallback behavior][mdit-renderer].

Mistune's `create_markdown(escape=True, plugins=[])` and AST/custom renderer
interfaces make it a plausible smaller alternative. Its convenience `html`
singleton explicitly uses `escape=False` and extra plugins; that shortcut is
not the desired policy. Recommend markdown-it-py's explicit token/configuration
boundary for the first implementation, while recognizing that Mistune saves
one dependency and deserves a fixture-based comparison if packaging size
becomes decisive. No claim of measured performance or superior conformance is
made here. [Mistune 3.4.0 API source][mistune-api].

PTB's `escape_markdown` escapes a string's Telegram markup characters; escaping
an entire CommonMark document would make its formatting literal. Passing a
`parse_mode` to an SDK also leaves Telegram interpreting that dialect. PTB does
not supply the missing general Markdown-to-Telegram conversion through this
helper, so replacing curl/polling with its framework would leave the central
problem unsolved. [22.8 helper implementation][ptb-helpers],
[Telegram parse-mode contract][api-send].

## Proposed interface and invariants for later implementation

Proposed internal seam:

```python
render_markdown(source: str) -> tuple[str, str]  # Telegram HTML, visible text
```

It is a pure display conversion: bounded input, no I/O, subprocesses, dynamic
plugins, telemetry or grants. Validate the generated HTML with
`html_visible_text` and require its visible result to match the returned text.
Do not add more formats, automatic fallbacks or configuration flags merely to
accommodate a library. Expose the single explicit `markdown` mode at existing
API/CLI/command/source boundaries that need it; unknown modes fail. Existing
`auto` remains unchanged. A future source/thread caller must actually carry
the explicit mode; changing only `api_send` would not fix normal thread replies.
[Current boundaries][api-send-local], [thread adapter][thread],
[source declaration][source-contract], [validator][html-rules].

Proposed rendering policy: strong/emphasis/strike become supported tags;
paragraphs and line breaks become newlines; headings become bold lines; lists
become indented text markers; rules become a literal separator. Code is escaped
once inside `code`/`pre`; fence language is allowlisted against the existing
class rule. Links require the existing scheme policy and escaped attributes.
Images become visible alt text plus a literal destination without fetching.
Leave table/math extensions disabled: their delimiters follow ordinary
CommonMark text rules, without table or formula semantics. Nested quotes
must flatten to an explicitly documented text representation because Telegram
forbids nested block quotations. Raw HTML in Markdown is literal; existing
native HTML bypasses Markdown conversion. These are recommended policies, not
claims of existing support. [Existing HTML policy][html-rules],
[classic entity nesting restrictions][api-format].

Use CommonMark's incomplete-input semantics: unmatched inline delimiters stay
literal and an unclosed fence ends at the document boundary. Reject NUL/lone
surrogates and unknown input or token types before any ledger reservation;
do not let parser replacement behavior silently alter them. [Delimiter rules][commonmark-emphasis],
[fence parser][mdit-fence], [normalization][mdit-normalize].

Keep the original source and explicit mode in the logical request identity;
derive a separate immutable display value. Never overwrite canonical command,
skill, diff or file bytes or their hashes. Native run approvals already escape
their canonical command into `pre` and hash the underlying run proposal: keep
that path outside conversion and outside any new approval decision. Ordinary
Markdown parsers normalize content: markdown-it-py normalizes CRLF/CR to LF and
NUL to a replacement character, and inline-code syntax has its own whitespace
rules. Byte-exact code/file displays must use the existing escaped native/plain
path; fence tests must prove the chosen payload policy, including any use of
original source slices, rather than assume parser tokens preserve bytes.
[Run-card rendering][run-render], [proposal identity][run-proposal],
[parser normalization][mdit-normalize], [CommonMark code spans][commonmark-code].

Preserve old plain/HTML fingerprints exactly, including the omission of plain
format and false flags; only Markdown requests gain the new identity input.
Store the rendered HTML/visible text for asks so tap/close edits append an
escaped answer to the same rendering instead of reparsing Markdown later.
Feed that visible text to `_log_sent`, whose current special handling covers
only HTML, so message history matches the delivered rendering. [Message log][sent-log].
Keep all reservation, ledger, ambiguity, reply/thread routing and button logic
in place. Existing definitive HTML parse-error 400 fallback can send visible
plain text once under the same reservation; a timeout/unknown outcome must
never trigger another formatting attempt/send. [Identity/reservation][reserve],
[plain compatibility][publish], [ask edits][ask-edits], [delivery fallback][deliver-text],
[pinned fingerprint regression][fingerprint-test].

Current checks use visible UTF-16 lengths: 4096 for text, 1024 for captions;
caption overflow is refused because splitting would violate one logical
delivery's receipt contract. Albums have 2–10 items, caption only the first,
and no keyboard. Keep that behavior, silent delivery, reply parameters,
captionless media rules, option detection on visible text, and the answer-suffix
budget for text/photo asks. No converter's automatic splitting or file
extraction belongs in this seam. A future split feature needs its own delivery
identity/reconciliation design. [Length/caption policy][caption-policy],
[publishing/options][publish], [album delivery][deliver-album],
[attachment delivery][deliver-attachment].

## Portable packaging and later acceptance checks

Today the installer uses `python3` and curl and does not install Python packages;
the supported Python minimum is 3.11. CI covers Ubuntu/macOS with 3.11 and 3.13;
the README still calls macOS with this host integration untested. A new import
would therefore not be drop-in unless installation and service invocation also
provide it. [Installer][installer], [contribution requirements][contributing],
[CI][ci], [platform caveat][readme].

For a later implementation, ship a hash-pinned, private environment per instance
(or an equivalent explicit packaged runtime) containing markdown-it-py/mdurl and make
the CLI and both Linux/macOS service launch paths use the same interpreter.
Prebuild/download their universal wheels into a wheelhouse for offline
installation; do not modify system/user Python or download at message time.
Retain both MIT license notices with any redistributed copies. Telegramify
would additionally require exact pyromark/typing-extensions pins, platform
wheel coverage and native bundled-license review; wheel availability must not
be confused with supported operation. These are deployment work to approve,
not changes made by this report. [Parser and mdurl files][mdit-meta],
[mdurl files][mdurl-meta], [licenses][mdit-license], [mdurl notices][mdurl-license],
[pyromark third-party notices][pyromark-third-party].

The deployment requirement is independent installation of the same yard-agnostic
code: each instance owns a pinned runtime copy, its code/assets and dependency
environment, with its own launcher selecting that interpreter. Duplicate small
code/assets and wheel bundles are acceptable; do not require a shared mutable
checkout, venv or site-packages, yard-specific code branches, a remote formatter,
or coordination with other instances to install, upgrade, roll back or remove
one. The parser, converter and SDK candidates can be packaged separately this
way; the native converter adds per-platform wheel handling, while the SDK adds
dependencies without solving conversion. This is a proposed installation
contract, not a claim about the current installer. [Current launch-path binding][installer],
[candidate dependencies/files][tg-meta], [native wheels][pyromark-meta],
[parser files][mdit-meta], [SDK dependencies][ptb-meta].

Keep the formatting module unaware of instance identity and operational state;
its input/output remains text. Each distributed runtime copy must retain its
dependency pins and required license notices. A separate deployment change must
provide the instance runtime/interpreter boundary; this report specifies what
the adapter needs from it and does not prescribe or implement a competing
installer layout. No formatter service, grants or new coordination mechanism
is needed for this recommendation.

Later acceptance checks should extend the existing fake-transport paths, not
just test a converter helper:

1. In `test_yard_api.py`, `test_bridge.py` and `yardr/tests/test_pack.py`, send
   explicit Markdown through the actual source/API/CLI boundaries; inspect the
   wire payload, receipt and visible log. Replay the same key, change only the
   format, and retain fixed old plain/HTML fingerprint fixtures. Prove unchanged
   auto behavior for fences, raw HTML, ampersands, underscores and shell text.
2. Cover punctuation and nested formatting, link parentheses/backslashes and
   forbidden schemes, literal raw HTML, multiline/indented/longer fences,
   backticks/operators and language hints. Check incomplete markup under the
   chosen CommonMark policy, and refuse unknown renderer tokens/input errors
   whole with reasons that name the input field, never its raw content.
3. Exercise emoji/astral characters, combining text, ZWJ sequences and lone
   surrogates; verify exact visible lengths at 4096/4097 and 1024/1025 UTF-16
   units, source expansion at the existing cap, and no split within a surrogate
   pair. If entities are chosen instead, additionally prove every UTF-16 entity
   offset, bound and nesting rule; HTML avoids adding that local accounting.
4. In `test_text_options.py`, `test_ask_close.py`, `test_commands.py`,
   `test_pin.py` and `test_run_asks.py`, cover visible option detection,
   reply/thread fields, buttons, send/edit and text/photo close/tap edits,
   suffix budget and exact native proposal/command bytes and hashes.
5. In `test_albums.py`, `test_media_kinds.py`, `test_silent.py` and
   `test_send_retry.py`, cover caption limits, first-item album caption,
   silent/reply fields, one permitted parse fallback and no retry after an
   ambiguous result. Assert no network/image fetching, subprocess, new server
   or implicit attachment from formatting.
6. Check a clean Linux/macOS install using the packaged interpreter and pinned
   wheelhouse, including Python 3.11/3.13. Install two isolated instances and
   upgrade, roll back and remove one while proving the other's code, dependency
   versions, launcher and formatting output remain unchanged. Prove neither
   imports from a shared mutable runtime or needs the other's checkout or a
   remote formatting service. Then run `sh scripts/test.sh` under
   the project's isolation rules. These are future checks, not experiments
   claimed here. [Existing test inventory][tests], [pack tests][pack-tests],
   [repository test contract][contributing].

Adding explicit format transport to thread replies is compatible with unchanged
auto. The material operator choice is whether to go further and change the
default or introduce a mixed grammar; that needs an intentional compatibility
contract. Full Rich Messages,
automatic splitting and entity-wide delivery are separate design decisions.
This report recommends the explicit mode and leaves the runtime dependency and
packaging change for the implementation review.

[api-send]: https://core.telegram.org/bots/api/methods/sendmessage.md
[api-format]: https://core.telegram.org/bots/api/formatting.md
[api-entity]: https://core.telegram.org/bots/api/types/messageentity.md
[api-rich]: https://core.telegram.org/bots/api/methods/sendrichmessage.md
[api-rich-input]: https://core.telegram.org/bots/api/types/inputrichmessage.md
[api-rich-format]: https://core.telegram.org/bots/api/rich-messages.md
[api-changes]: https://core.telegram.org/bots/api-changelog
[tg-meta]: https://pypi.org/pypi/telegramify-markdown/1.4.2/json
[tg-wheel]: https://files.pythonhosted.org/packages/5e/6a/fdec8e4f5a7cd8640de7c61f3a502a66a7ac76cbf745e609eeebe7eb30db/telegramify_markdown-1.4.2-py3-none-any.whl
[tg-release]: https://github.com/sudoskys/telegramify-markdown/releases/tag/v1.4.1
[tg-tag]: https://github.com/sudoskys/telegramify-markdown/tree/pypi_1.4.2
[tg-api]: https://github.com/sudoskys/telegramify-markdown/blob/pypi_1.4.2/src/telegramify_markdown/__init__.py
[tg-entities]: https://github.com/sudoskys/telegramify-markdown/blob/pypi_1.4.2/src/telegramify_markdown/entity.py
[tg-pipeline]: https://github.com/sudoskys/telegramify-markdown/blob/pypi_1.4.2/src/telegramify_markdown/pipeline.py
[tg-rich]: https://github.com/sudoskys/telegramify-markdown/blob/pypi_1.4.2/src/telegramify_markdown/rich.py
[tg-converter]: https://github.com/sudoskys/telegramify-markdown/blob/pypi_1.4.2/src/telegramify_markdown/converter.py
[tg-mermaid]: https://github.com/sudoskys/telegramify-markdown/blob/pypi_1.4.2/src/telegramify_markdown/mermaid.py
[mdit-release]: https://github.com/executablebooks/markdown-it-py/releases/tag/v4.2.0
[mdit-meta]: https://pypi.org/pypi/markdown-it-py/4.2.0/json
[mdit-license]: https://github.com/executablebooks/markdown-it-py/blob/v4.2.0/LICENSE
[mdit-main]: https://github.com/executablebooks/markdown-it-py/blob/v4.2.0/markdown_it/main.py
[mdit-preset]: https://github.com/executablebooks/markdown-it-py/blob/v4.2.0/markdown_it/presets/commonmark.py
[mdit-renderer]: https://github.com/executablebooks/markdown-it-py/blob/v4.2.0/markdown_it/renderer.py
[mdit-security]: https://markdown-it-py.readthedocs.io/en/latest/security.html
[mdit-normalize]: https://github.com/executablebooks/markdown-it-py/blob/v4.2.0/markdown_it/rules_core/normalize.py
[mdit-fence]: https://github.com/executablebooks/markdown-it-py/blob/v4.2.0/markdown_it/rules_block/fence.py
[mdurl-meta]: https://pypi.org/pypi/mdurl/0.1.2/json
[mdurl-license]: https://github.com/executablebooks/mdurl/blob/0.1.2/LICENSE
[mistune-release]: https://github.com/lepture/mistune/releases/tag/v3.4.0
[mistune-meta]: https://pypi.org/pypi/mistune/3.4.0/json
[mistune-license]: https://github.com/lepture/mistune/blob/v3.4.0/LICENSE
[mistune-api]: https://github.com/lepture/mistune/blob/v3.4.0/src/mistune/__init__.py
[ptb-meta]: https://pypi.org/pypi/python-telegram-bot/22.8/json
[ptb-helpers]: https://github.com/python-telegram-bot/python-telegram-bot/blob/v22.8/src/telegram/helpers.py
[pyromark-meta]: https://pypi.org/pypi/pyromark/0.10.4/json
[pyromark-cargo]: https://github.com/monosans/pyromark/blob/v0.10.4/Cargo.toml
[pyromark-build]: https://github.com/monosans/pyromark/blob/v0.10.4/pyproject.toml
[pyromark-license]: https://github.com/monosans/pyromark/blob/v0.10.4/LICENSE
[pyromark-third-party]: https://files.pythonhosted.org/packages/a0/3a/8d2fe687fd7534bc104738db9b04d406a51d7ba7678db3e9fd28cb723427/pyromark-0.10.4-cp311-cp311-manylinux2014_x86_64.manylinux_2_17_x86_64.whl
[commonmark-code]: https://spec.commonmark.org/0.31.2/#code-spans
[commonmark-emphasis]: https://spec.commonmark.org/0.31.2/#emphasis-and-strong-emphasis
[auto]: ../../bridge/bridge.py#L404
[html-rules]: ../../bridge/bridge.py#L367
[publish]: ../../bridge/bridge.py#L3608
[sent-log]: ../../bridge/bridge.py#L3348
[api-send-local]: ../../bridge/bridge.py#L3407
[api-pin-local]: ../../bridge/bridge.py#L3534
[cli]: ../../bridge/bridge.py#L4519
[pin]: ../../bridge/bridge.py#L3799
[commands]: ../../bridge/bridge.py#L2550
[thread]: ../../yardr/tgbridge/thread#L66
[source-contract]: ../../yardr/tgbridge.yaml
[deliver-text]: ../../bridge/bridge.py#L3155
[deliver-attachment]: ../../bridge/bridge.py#L3193
[deliver-album]: ../../bridge/bridge.py#L3273
[caption-edit]: ../../bridge/bridge.py#L3042
[ask-edits]: ../../bridge/bridge.py#L2939
[caption-policy]: ../../bridge/bridge.py#L3132
[reserve]: ../../bridge/bridge.py#L2686
[fingerprint-test]: ../../bridge/test_bridge.py#L1376
[run-render]: ../../bridge/run_asks.py#L203
[run-proposal]: ../../bridge/run_asks.py#L304
[installer]: ../../install.sh
[contributing]: ../../CONTRIBUTING.md
[ci]: ../../.github/workflows/test.yml
[readme]: ../../README.md#prerequisites
[tests]: ../../bridge
[pack-tests]: ../../yardr/tests/test_pack.py
