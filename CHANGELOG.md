# Changelog

All notable changes to this pack are recorded here, newest first. The
format follows Keep a Changelog, and the wording is what an operator
sees and does, not how the code is organised.

## [Unreleased]

### Added

- Native source-kind `pack apply` review through the existing `run_ask`
  grant. Directory proposals deliver complete review parts and a document
  containing every captured file before weighted approval; embedded source
  packs use complete text bundles. Depot packs and parameter inputs refuse.
- Structured native decision and run cards with bold questions, spaced
  context, numbered options and separated outcomes. Quoted evidence stays
  literal, and existing delivery receipts and approval gates keep working.
- Public capability and setup documentation for native `source add <file>`
  review, including declarations, command files and replacement diffs.
- A committed-tree public-readiness scan with location/count diagnostics and
  a local source archive plus exact commit/tree/file hash manifest. It does
  not publish, rewrite history or remove attachments.
- Separate notices for the pinned Markdown runtime, bundled PyYAML 6.0.3
  reader and optional Whisper and VAD downloads, with upstream provenance
  and retained MIT notices for the vendored code.

- Native herdr `group add` and `group set` cards (core `fc7375cf5ad4`): the
  whole card and every role, quoted, arrive before the weighted card;
  approval writes the group once. A set for a group that names no prompt
  is reviewed too, once core's group confirms it has none, and so is a set
  for a paused group (limit 0).
- Explicit Markdown for ordinary replies, API/source sends and pins, CLI and
  command send/edit actions through the existing Telegram HTML path. The
  pinned MIT parser ships offline inside each instance release; defaults
  and native exact evidence/approval paths remain unchanged.

- Native ordinary option/word and manual-stage cards via `ask_card`, with
  paired-human replies, Later/Decline and canonical close/reopen status.
- Operator-only check/apply helper for missing native home/operator keys.
- `check-install` in an installed release says whether its files match
  its `SHA256SUMS`.
- A release's pinned pure-Python dependencies have one place, `vendor/`,
  which the bridge puts first on its import path.
- `/decide` lists the operator's open decisions and opens a supported one as
  its native card; closed asks report their canonical outcome.

### Changed

- Weighty native run cards now take Approve, then Confirm on the same card
  within 60 seconds. Cancel occupies Approve's former position; Confirm
  has its own row. Typed numbers no longer approve, and legacy pending
  confirmations clear on the first poll after updating.

- **BREAKING CHANGE:** Install, dry-run and rollback refuse a unit or root
  owned by another settings path, or whose ownership cannot be read. Moving
  a settings file requires a fresh unit and root: stop the old service,
  retain its settings and installation for rollback, then verify the new
  instance before starting it. There is no takeover flag.
- Public installation requires core changes that are not yet available in
  a compatible public release, even for hook/chat alone: the shipped pack
  requires `thread.formats`. Local capability hashes are not public install
  targets; publish and accept the required core first.
- Operator commands use this instance's launchers and explicit yard home.
  Skills are copied into that yard with `yardr skill add` and assigned with
  crew/group `skills=`; refresh the copies after installation or rollback
  instead of linking shared harness folders to an instance's release.
- Installation/examples use fictional yards/operators and home-relative paths.
  Existing source aliases and credential-host bindings remain supported;
  the readiness guide records custom-base and callback migration limits.

- Ordinary native cards require core `7fa0d4c75ff6` or compatible guarded ask
  methods; stale Telegram answers cannot replace another client's decision.
- `askcard:` is reserved for native decisions; overlapping custom callback
  prefixes must be renamed before upgrading.
- Each yard's bridge is installed as its own release copy. `install.sh
  --settings <file>` copies the checkout's committed HEAD to
  `~/.local/share/yardr-telegram/<unit stem>/releases/<commit>` (`--root`
  names another root), checks it there, and points `current` at it. It
  refuses a checkout with tracked changes. The service runs `<root>/current/bridge/bridge.py`,
  so changing, moving or deleting the checkout no longer reaches a
  running yard, and updating, rolling back or removing one yard changes
  no other. `install.sh --rollback` returns to the previous release. The
  first install keeps the service file it replaced in
  `<root>/service.previous`.
- Commands go through the instance's launchers: `<root>/bin/telegram-bridge`
  (with the settings built in) and `<root>/bin/voice-transcribe`. The
  `next:` line uses them, and restarts a running service.
- `install.sh` no longer creates or repoints `~/.local/bin/voice-transcribe`;
  an existing link is left as it is.
- `install.sh` refuses, before it writes anything, a root, settings file or
  any path the settings name (`python`, the secrets, state, lock, health
  file, `yardr.home`, handlers and their write paths, a configured
  `gpu_check`, whisper-cli and models) that depends on the checkout, links
  followed at every component, so `<checkout>/.venv/bin/python` is refused.
  It prints one line per entry with its remedy, and never rewrites the
  settings. An installed release's `check-install` checks the same against
  the source its `RELEASE` records, and a reinstall that reuses a release
  of the same commit checks the settings against the source that release
  records too, whichever checkout runs it.
- With native keys, `/decide` and the `decide:` prefix belong to the bridge:
  a `[[command]]` or `[[menu]]` named `decide` or an overlapping prefix
  refuses to load.

### Fixed

- An unconfirmed ordinary decision card can be reconciled by the paired
  operator's verified tap on the original message, without resending or
  replacing its ambiguous send receipt. Stale cards retire using canonical
  state; missing message proof gives manual recovery instructions.
- Same-key siding retries resume definitely unsent cards while retaining
  confirmed siblings and ambiguous delivery blocks. Status-card retries
  recheck the operator's current role and source identity before sending.
- The native-settings helper validates the complete candidate with the
  bridge loader before creating a backup or applying changes. Reserved
  command, callback and menu conflicts refuse without changing the file.
- The operations guide now states the per-topic inbound rule where it
  states the settings-agent rule: a message in a topic is accepted
  only when the yard routes it on that topic's conversation to that topic's
  crew member, which may differ from the settings crew member.

## [0.2.0] - 2026-10-28

### Added

- Silent sends: `send` and `reply-current` accept `--silent`. The
  message still appears in notifications but makes no sound, so only
  asks and problems ring.
- Albums: giving `--attachment` 2 to 10 times sends the files as one
  album under one key, with the body as the caption of the first item.
  Photos and videos may mix; documents group only with documents. One
  attachment sends exactly as before.
- Reactions: with reactions turned on, the paired user sees the `seen`
  reaction on a message once it has been accepted, and the agent can
  set the `done` reaction (or another allowed emoji, or clear it) with
  the `react` command.
- Pinned slots: the `pin` command keeps one pinned message per named
  slot at the top of the chat, edited in place when its text changes,
  and removes it with `--unpin`. No setting is needed.
- Typing: with typing turned on, the chat shows "typing..." after a
  message that has been accepted, until the configured limit runs out
  or the bridge next sends or changes anything in the chat (a send,
  edit, ask, pin, album or reaction), whichever comes first.
- Topics set up in settings: one `[[topic]]` table per subject gives
  the paired chat topics beside General, each on its own conversation.
  The `topics` command creates each listed topic once and prints the
  line that binds it. A message written in a topic reaches the agent
  on that topic's conversation, and the answer lands in the same
  topic. Reactions and typing follow the topic the message was
  written in.
- Topics the paired user creates: with automatic topics turned on,
  each topic the paired user creates is recorded, bound and served on
  its own conversation, with no setup step per topic. With it off,
  such topics are recorded but not served.
- Closing a button ask with the decision: when the paired user answers
  an open ask some other way, the agent closes it with the
  `ask-close` command. The buttons go and the ask message shows the
  decision.
- The list of ready add-ons now names telegram-waiting, which adds a
  command that shows what waits on the paired user.

### Fixed

- Lettered options need tap buttons: a `send` or `reply-current` whose
  text lists lettered options (for example two or more consecutive
  `A:`, `B:` lines) without tap buttons is refused before anything is
  sent or recorded. Send it again with buttons, or use the deliberate
  text-only override when a plain list is really meant. A send already
  confirmed under the same key keeps its saved receipt.
- The quota-exhausted notice follows the agent of the conversation it
  is posted in: in a topic set up in settings it watches that topic's
  agent, names it, and is keyed by it, so one agent's episode neither
  posts in another agent's topic nor is hidden by it. In the paired
  chat it watches the settings' agent, as before.

### Upgrade notes

- Nothing new runs until it is turned on. Each new settings table
  ships off, and a settings file without it loads exactly as before:
  - Reactions: add `[reactions]` with `on_accept = true` to show the
    `seen` reaction on accepted messages. The `seen` and `done` emoji
    may be set in the same table; the `react` command works without
    the table.
  - Typing: add `[typing]` with `seconds` above 0 (up to 900) to show
    "typing..." after an accepted message. The default 0 never shows it.
  - Configured topics: add one `[[topic]]` table per topic, run the
    `topics` command, and run each bind line it prints, before anyone
    writes in a topic.
  - Topics the user creates: add `[auto_topics]` with
    `enabled = true`. Without it, only the `[[topic]]` tables are
    served.
- Before using topics, set the bot's prerequisites in BotFather: turn
  topic mode on. Leave users creating topics off when only the
  configured topics should exist, and leave it on when automatic
  topics are enabled, since creating topics is what the paired user
  does.
- Restart the bridge service after changing the settings file, so the
  bridge loads the new tables.
- Silent sends, albums, pinned slots, `ask-close` and the lettered
  options refusal need no setting: they are available as soon as the
  new code runs.

## [0.1.0] - 2026-10-22

The first public version.

### Added

- A private chat between the paired user and the agent, for text in
  both directions, with HTML formatting on outbound text. A message
  the user forwards reaches the agent marked as forwarded, its
  content quoted as material rather than as the user's instruction.
- Inbound photos, documents, videos, voice and audio files saved on
  the host, and outbound photos, videos, documents, voice notes and
  round video notes.
- Voice and audio transcription on the host in the configured
  language, with no cloud service. When set to automatic, it uses the
  graphics processor when one is free; an English-only model requires
  the language to be English.
- A delivery ledger that stops a retried send from posting twice and
  reports an unknown outcome instead of sending again.
- A slash command menu, and extra commands answered by the operator's
  own handler programs, with text and inline buttons and no enable
  flag.
- One bot, settings file, bridge process and delivery ledger per
  installation on a host, sharing one copy of the code and one
  transcription build.
- Setup checks before the bridge runs, and one service per
  installation: a systemd user unit on Linux and WSL 2, a launchd
  agent on macOS, with macOS and WSL 2 setup documented.
- Ready-made skills for formatting, attachments and voice
  transcripts.

### Later in 0.1.0

Published on 2026-10-23 and 2026-10-25, still as 0.1.0:

- Tap-to-answer buttons on asks: the paired user taps one inline
  button instead of typing. One tap reaches the agent once as one
  actionable message, and the ask message then shows the choice.
- Ask buttons on a photo message, where the caption is the question,
  answered by a tap exactly like a text ask.
- An answered ask is edited to show the choice, and that edit still
  works after the bridge has restarted.
- The formatting skill documents tap-to-answer asks.
- The add-on list links the quota and secret-drop add-ons, and test
  runs can be confined to a chosen range of local ports.
