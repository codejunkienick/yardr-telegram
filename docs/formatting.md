# Explicit Markdown over Telegram HTML

Select `markdown` explicitly for ordinary display text:

```sh
yardr reply <seq> --format markdown 'A **bold** answer'
yardr reply <seq> --format markdown - < answer.md
yardr call tgbridge send text="$(cat answer.md)" format=markdown --json
yardr call tgbridge pin slot=waiting text="$(cat waiting.md)" format=markdown --json
<root>/bin/telegram-bridge send --format markdown --body-file answer.md --idempotency-key <key>
```

The bridge CLI also accepts it on `reply-current` and `pin`. API callers pass
`"format":"markdown"` to `/v1/send` or `/v1/pin`; command handlers use it
on `send` and `edit` actions. Routing, reply, silent, keyboard and media flags
keep their existing meanings. An album has 2–10 compatible attachments,
caption only on its first item, and no keyboard. Markdown fetches no images.

Ordinary replies require a core with the explicit reply-format contract
(verified installed core `0035b8c13e27`) and the reviewed source declaration
with `thread.formats: [markdown]`. The thread command receives the original
`body` and `format: markdown`, with the existing thread/parent/key/files.
An old core or undeclared mode refuses before sending. Mail and peer notes
do not accept this flag. Installing the core alone does not activate this
source capability: the operator must install the reviewed bridge and apply
its instance's rendered source pack using [SETUP](../yardr/SETUP.md).

No defaults change. Without `--format`, core keeps its historical trimming,
wire body and key lookup; the bridge thread adapter still selects `auto`.
API/source `auto` chooses only HTML or plain. Literal Markdown fences remain
literal under auto, including its existing HTML/entity detection within a
fence. Direct bridge CLI/publish and command actions still default to plain.
Unknown modes and non-text Markdown sources refuse whole.

## Rendering policy

The pinned CommonMark parser enables only strikethrough in addition to its
CommonMark rules. No plugins, linkify, typographer, table or math extension.
Unmatched delimiters stay literal; an unclosed fence ends at document end.

| Markdown | Telegram display |
| --- | --- |
| Strong, emphasis, strike | `b`, `i`, `s` |
| ATX/Setext headings | Bold line |
| Paragraphs | One blank line between blocks |
| Soft/hard line breaks | LF newline |
| Lists | `• ` or original ordered start plus sequential numbers; continuation lines indented two spaces |
| Rules | Literal `---` |
| Quotes, including nested | Textual `> ` prefix per level on every rendered line; no nested Telegram blockquotes |
| Links/autolinks | `a href`, validated by the existing HTTP/HTTPS/TG policy; relative/mailto/unsafe destinations refuse |
| Images | Plain alt text plus ` (destination)`; no link, download or image entity |
| Inline code | Escaped `code` |
| Indented/fenced code | Escaped `pre`/`code`; first info word becomes a language hint only if it matches `[A-Za-z0-9_+-]{1,32}` |
| Raw HTML | Literal escaped text, even inside fences |

Link titles and additional fence info are not displayed. Table/math delimiters
follow normal CommonMark text rules. List items containing several blocks
retain blank lines; nested list continuations use two spaces per level.
Quote/list prefixes also decorate code lines within those contexts.

Code is a display, not a byte-preserving evidence channel: CRLF and CR become
LF before parsing. Fences retain their parsed spaces, tabs, blank lines and
trailing LF; indentation required by CommonMark is removed. Inline code turns
line endings into spaces and removes one leading/trailing space when both
exist and the content is not all spaces. Neither source nor generated code
is stripped by the renderer. NUL and lone surrogates refuse before parser
normalization. Excessive nesting refuses rather than returning a partial
parse; the parser's internal nesting ceiling exceeds the bounded source.

## Delivery and identity

Original source plus explicit mode is the logical identity. Rendered HTML and
visible text are derived separately; old plain/HTML fingerprints, omitted
plain format and false flags stay unchanged. Changing format under the same
bridge key conflicts. Core gives confirmed answers in another mode their own
identity; an unresolved answer must first be reconciled on its original key
and mode. Never change words, whitespace, mode or key to retry an unknown send.

Generated HTML passes the existing HTML validator and link policy. Both the
Markdown source and generated HTML must fit the existing 16384 UTF-16 source
cap; escaping can reach this before the visible limit. Visible text is at
most 4096 UTF-16 units, or 1024 for captions. Astral characters count two;
ZWJ sequences count all their units. Nothing splits, extracts a file or
bypasses a limit. Ordinary asks must also leave room for the answer suffix;
lettered options are checked on visible text and still need buttons unless
`text_options` is explicitly selected.

Asks store immutable rendered HTML in the existing HTML edit path. Tap/close
appends escaped answer text without parsing Markdown again. History logs
delivered visible text, including after the existing definitive 400 HTML
parse-error fallback. That fallback sends plain visible text once under the
same reservation; timeout/unknown delivery never triggers a formatting retry.

Native commands, approvals, skill files, full diffs, immutable review bundle
bytes and hashes remain on their exact escaped native path. Markdown is no
new authority or approval input. For a copyable terminal command, send one
HTML `pre`-only message where the transport requires it; keep exact evidence
as files/native bundles rather than converting it through Markdown.

## Packaging and operator handoff

[vendor/README.md](../vendor/README.md) records verified wheel hashes,
compatibility, MIT notices and offline reproduction. Each release owns its
unmodified parser copies under vendor/. The existing installer and `python`
setting select the same interpreter for CLI, Linux unit and macOS launcher;
no shared venv, pip or message-time downloads. Upgrading/rolling back one
instance selects its matching code and packages and leaves others alone.

After local review/assembly, the coordinator runs installation from the clean
merged checkout, checks `<root>/bin/telegram-bridge check-install`, follows the
installer's `next:` line, and applies `<root>/pack` while retaining any existing
source alias and credential env-file binding. Check the installed source lists
Markdown before using `yardr reply --format markdown`. Rollback uses the
existing `<root>/current/install.sh --settings <settings> --rollback` and the
matching source-pack apply/restart procedure. Source landed, installed and
live are separate states; this implementation performs no live activation.

Offline tests use fake Telegram plus the real installed CLI/RPC/native store,
and synthetic independent installations without their checkout. macOS service
rendering is tested from source; no actual macOS-host or Telegram-client
validation is claimed. The offline preview shows actual emitted HTML in a
browser, which is evidence of this renderer rather than a Telegram screenshot.
