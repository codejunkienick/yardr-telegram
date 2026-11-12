# yardr-telegram

**Public source, not an installable release (2026-11-10).** This source
is published alongside the thread-formats change proposed for
[yardr-dev/yardr](https://github.com/yardr-dev/yardr). It requires a core
with `thread.formats` declaration support. Released cores today refuse
this pack at `yardr pack check yardr/` with
`thread.formats: unknown key`. An installable release must wait for
the change to merge, a compatible public core release and the
[compatibility re-check](docs/public-readiness.md#public-core-release-gate).

A private Telegram bridge for a [yardr](https://github.com/yardr-dev/yardr)
yard: one paired chat with the yard's crew, its topics, tap-button asks,
pins, reactions, typing and on-host voice transcription. The bridge runs
as a service of its own, and the yard reaches it as the
declared source `tgbridge` (`yardr/`).

(Terms such as the paired chat and the ledger: `bridge/OPERATIONS.md`, "Terms".)

Release history: `CHANGELOG.md`.

## What it does

- The paired user gets a private chat with the yard's crew (the
  yardmaster by default) for text in both directions, with Telegram HTML formatting on outbound text. A message
  the user forwards reaches the yard marked as forwarded, its content
  quoted as material rather than the user's instruction.
- The agent gets inbound photos, documents, videos, voice and audio files
  saved on the host, and can send photos, videos, documents, voice notes
  and round video notes to the chat.
- The agent sends several screenshots as one album (`--attachment` given
  2-10 times) under one key, captioned by the body.
- When transcription is enabled, the yard gets each voice and audio note
  transcribed on the host with whisper.cpp in the configured language, with
  no cloud service. With `method = "auto"`, it uses the GPU when one is free.
  An English-only (`.en`) model requires `language = "en"`.
- The yard gets a delivery ledger that stops a retried send from posting
  twice and reports an unknown outcome instead of resending it, and a
  message log that `yardr thread show` reads whole.
- A crew member sends an update that needs no action silently: it
  still notifies, but with no sound, so only asks and problems ring.
- A yard keeps one pinned message per slot at the top of the chat,
  edited in place (`yardr call tgbridge pin`), such as a list of what
  waits on the user.
- A yard gives the paired chat topics, one per subject beside General,
  each its own conversation: `<root>/bin/telegram-bridge topics` creates
  the topics its settings list (`[[topic]]`) once and prints the
  `yardr subscribe` line that routes each to a crew member.
- The paired user gets a slash command menu, and a yard adds commands
  through its own handler programs that answer with text and inline
  buttons and need no enable flag.
- The paired user sees a reaction on a message once the yard has it
  (`seen`, when the settings turn it on) and another when the work it
  asked for is done (`yardr call tgbridge react reaction=done`).
- The paired user sees "typing..." after a message the yard has, until
  the bridge's next send, when the settings turn it on (`[typing]`).
- The paired user answers an ask on a text message or on one photo
  message (the caption is the question) by tapping one of its inline
  buttons instead of typing: one tap reaches the yard once as one
  actionable message, and the ask message then shows the choice. When the
  user answers some other way, the crew member closes the ask with the
  decision made (`yardr call tgbridge ask_close`): its buttons go and it
  shows the decision.
- Each yard gets its own bot, settings file, bridge process, delivery
  ledger and installed copy of the code on one host, and the bridges share
  one whisper.cpp build, its models and the transcription queue.
- The operator gets setup checks from `install.sh` and `check-install`
  before the bridge runs, and a service for each yard: a systemd user
  unit on Linux and WSL 2, a launchd agent on macOS.
- The agent gets ready-made skills for Telegram formatting, attachments
  and voice transcripts.

| Path | What |
| --- | --- |
| `bridge/bridge.py` | the bridge: `run`, `probe`, `send`, `reply-current`, `ask-close`, `run-ask`, `ask-card`, `siding-card`, `react`, `pin`, `topics`, `set-commands`, `render-unit`, `check-install`, and the yard API on its loopback port |
| `bridge/OPERATIONS.md` | the operations guide: settings, launch, delivery recovery, transcription |
| `bridge/test_*.py` | `sh scripts/test.sh` (fake Telegram and whisper-cli, an optional isolated real yardr store, and the pack tests; no external network). Set `TEST_PORT_RANGE=LOW-HIGH` to bind test servers inside that range. Pick a range that excludes ports the host's own services use. |
| `settings.example.toml` | every settings key, with example values |
| `install.sh` | installs one yard's bridge as its own release copy of a clean commit under its root, checks its settings and host, mints the two secrets, renders its service and launchers; `--rollback` returns to the previous release |
| `yardr/` | the yardr pack: the declared source `tgbridge` (`tgbridge.yaml`), its `normalize` and `thread` commands, and `SETUP.md`, the steps for a yard |
| `YARDR.md` | the contract between the bridge and a yard: the inbound body, the yard API, the settings |
| `systemd/telegram-bridge.service.in` | the Linux unit template; on macOS the agent is rendered by `<root>/bin/telegram-bridge render-unit` |
| `transcriber/` | `voice_transcribe.py` (whisper.cpp, configured language), `adapter.py` (the bridge's contract), `models.sha256` |
| `skills/` | the shared skills: `telegram-formatting`, `telegram-attachments`, `telegram-voice-transcripts`; `telegram-decisions` for coordinators with the relevant call grants |

## Native cards and review bundles

Native run cards bind the proposed command to its ask and bead. For a
weighty change, tap Approve, then Confirm on the same card within 60
seconds. The confirmation names the ask, bead, command and time window.

[View the synthetic Approve and Confirm card sequence](docs/images/native-card-confirmation.png).

Synthetic offline HTML capture: unarmed Approve, armed Confirm, expired
and cancelled cards. These are test fixtures, not a live Telegram chat.

A review bundle arrives as one document with the complete proposal,
files and diffs, followed by one compact approval card bound to it.

[View the synthetic review document and compact approval card](docs/images/review-bundle.png).

Synthetic offline HTML capture: one review document and its approval
card, using fictional data. Open the image at full size to read the
details; this is not a live Telegram chat.

## Prerequisites

This is public source for review. The setup steps below require a
compatible public core release; released cores currently reject the
shipped declaration, including for hook/chat-only setup. See the
[public-core gate and capability table](docs/public-readiness.md#public-core-release-gate)
for the outstanding contracts and required passing check.

- Linux with a systemd user session (`systemctl --user status`). macOS
  has a launchd renderer (`bridge/OPERATIONS.md`,
  "Install and launch"), untested with yardr.
- Python 3.11 or later (`python3 --version`), and curl.
- yardr with source packs, hook verification, thread commands, explicit
  `thread.formats` declarations and the yard
  running (`yardr --version`); configure a loopback hook listener
  (`yardr settings get hook_addr`). Native cards and Markdown need the
  additional capabilities listed in [the compatibility table](docs/public-readiness.md#capabilities-and-grants).
  Hook-only settings still require a core that accepts the whole pack.
  A version number alone does not establish those capabilities.
- The [yard-local skill setup](yardr/SETUP.md#the-crews-skills-shell) needs
  `yardr skill add` and crew/group `skills=` (local core baseline `a66722bb`,
  not a public install target), under the same public-core release gate.
  From core `58e93b7f`, crew members and yard users may add directly;
  assignment stays the operator's. Earlier cores require the operator to add.
- Optional native skill review needs the core captured single-file proposal
  capability (local baseline `a79f6f8162c8`, not a public install target)
  and the paired operator setup in
  [SETUP.md](yardr/SETUP.md#single-file-skill-snapshots). Full review parts
  precede buttons; tap Approve, then Confirm on the same card within 60
  seconds. Only one regular `SKILL.md` with supported metadata can use skill review.
  From core `58e93b7f`, new proposals under the core policy refuse because
  the proposer may add directly; cards still serve stored asks and yards
  whose policy keeps adding the operator's.
- Optional native group cards (`group add`, `group set`) read upstream
  yardr's group rows (`model=` on the aligned build alone) and need the
  same setup ([SETUP.md](yardr/SETUP.md#group-add-and-set)).
- Optional native source-add review (`source add <file>`) needs the carried
  source-add contract (local baseline `546e3fdd2127`, not a public install
  target). No compatible public core is available yet. It uses the same
  `run_ask` grant and paired operator setup, and reviews the declaration,
  command files and replacement diffs
  ([SETUP.md](yardr/SETUP.md#source-declarations-and-command-files)).
- Optional native source-kind pack review uses the same `run_ask` grant
  and paired operator setup. Directory
  `pack apply <absolute-directory> --sha256 <digest>` needs the directory
  source-pack proposal contract (local baseline `ced24b0e849c`, not a public
  install target). Complete review parts and a snapshot document precede
  controls; Approve then requires a direct card reply with the exact ask
  number. Embedded `pack apply <name>` uses complete carried-source text
  parts and core weight rules, without a snapshot document. No compatible
  public core is available yet. See [pack upgrade setup](yardr/SETUP.md#the-source-pack-crew-then-you-in-telegram-shell-fallback)
  and [source pack bundles](bridge/OPERATIONS.md#source-pack-bundles).
- For transcription also git, cmake, make or ninja, a C++ compiler, ffmpeg,
  sha256sum, and network access to clone whisper.cpp and download the
  models (`transcriber/setup.sh --settings <file>`; about 600 MB).

Native run cards exclude depot-kind/summary pack plans, `crew add`,
`depot push`, multi-file skills, secret/setup/identity operations and
parameter input; use the full native client for those.

## Adopt in a yard

`yardr/SETUP.md` walks one yard through it: the bot, the settings, the
install, the yard's hook listener, the pack and its credential, the hook,
who writes, who answers, the start, and telling.

To read the ids for the settings before the bridge's first start, without
the token in argv or history:

```sh
python3 - ~/.config/<yard>-telegram/telegram-bot-token <<'EOF'
import json, sys, urllib.request
token = open(sys.argv[1]).read().strip()
for method in ('getMe', 'getUpdates'):
    with urllib.request.urlopen(f'https://api.telegram.org/bot{token}/{method}', timeout=20) as answer:
        print(method, json.dumps(json.load(answer)['result'], indent=1))
EOF
```

getMe's `id` goes to `[bot] bot_id`, its `username` to `[bot] account_id`.
In the `/start` update, `message.from.id` goes to `[bot] user_id`,
`message.chat.id` to `[bot] chat_id` (equal to user_id), and `update_id`
to `pairing_update_id`.

Each yard's bridge is its own instance: `install.sh --settings <file>`
copies the checkout's committed HEAD (it refuses tracked changes) to
`<root>/releases/<commit>` and points `<root>/current` at it, with the
root `~/.local/share/yardr-telegram/<unit stem>` unless `--root` names
another. The unit runs `<root>/current/bridge/bridge.py`, and
`<root>/bin/telegram-bridge` and `<root>/bin/voice-transcribe` run the same
copy with the settings' python, so the checkout can change, move or go
without touching a running yard, and updating, rolling back
(`install.sh --rollback`) or removing one instance changes no other. A
release's own Python dependencies, when it has any, are committed under
`vendor/`. Why and how: `docs/design/independent-instances.md`; moving a
bridge whose unit still runs a checkout: `yardr/SETUP.md`, "Move an existing
bridge to its own installation". To move a bot from an existing bridge,
disable and stop the old instance first, so it cannot return at login or
reboot (two pollers on one bot conflict):

- Linux and WSL 2: `systemctl --user disable --now <old unit>`.
- macOS: `launchctl bootout gui/$(id -u)/<old label>`, then move
  `~/Library/LaunchAgents/<old label>.plist` out of `~/Library/LaunchAgents`
  and keep it with the retained old installation.

Keep the old settings and installation for rollback. Point `state_dir` at
a copy of its state directory, so the ledger, the offset, the pins and the
topics carry over. A new settings path needs a fresh `unit` and root;
install it, then run the printed `next:` line to enable and start only the
new instance. To roll back, disable and stop the new instance the same way
(on macOS, also retain its plist outside `~/Library/LaunchAgents`), then
run `systemctl --user enable --now <old unit>`, or restore the old plist to
`~/Library/LaunchAgents` and run
`launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<old label>.plist`.
The [settings-move steps](yardr/SETUP.md#move-a-settings-file-shell) spell out
both directions.

## Uninstall

1. (shell) `systemctl --user disable --now <unit>`, then
   `rm ~/.config/systemd/user/<unit> && systemctl --user daemon-reload`,
   then `rm -rf <root>` (`~/.local/share/yardr-telegram/<unit stem>` by
   default): its releases, launchers, packs and `service.previous`.
2. (shell) Select this instance's yard home explicitly:
   `yardr --home <this-yard-home> hook rm tgbridge`, then
   `yardr --home <this-yard-home> unsubscribe <crew> tgbridge` for each
   subscribed crew member, then
   `yardr --home <this-yard-home> source rm tgbridge`.
3. (shell, optional) Delete the settings file, the token, both secrets and
   the state directory. That directory holds the ledger and the message log,
   so keep a private copy if the bridge may return.
4. (shell) Keep the host's shared pieces while another bridge runs: the
   queue `~/.local/state/voice-transcribe` and the whisper.cpp build and
   models. `~/.local/bin/voice-transcribe` is yours: an install never
   creates, changes or removes it.
5. (you, optional) Delete the bot with BotFather.

## Skills

The skills are the shared base set, written for any yard and any bot,
naming no yard, person or product. With core `58e93b7f` or later, a crew
member (the yardmaster), a yard user or the operator copies them from its
installed release with `yardr skill add` into that yard's home; earlier
cores require the operator. Giving the names to crew or groups stays the
operator's. See [the crew's skills](yardr/SETUP.md#the-crews-skills-shell)
for setup and refresh after an install or rollback. Harness skill folders
such as `~/.claude/skills` are shared by all yards on the host; keep
instance-specific links out of them.

- **A yard adds its own skill** in its own pack, never by editing these: for example its menu commands' meaning,
  who reads the chat, or its message house style. Its skill may point at these
  ones by name.
- **Promoting a skill into the shared set**: when a yard's skill turns out to be
  true for every yard (a Telegram rule, not a habit of one yard or user),
  rewrite it without any yard, person, product or path of that yard, move it
  to `skills/<name>/SKILL.md` here with a citations table of the pack files it
  relies on in `citations.md` beside it (never in SKILL.md), and delete the yard's
  copy in the same change. Nothing here names a yard, person or company.

## Add-ons

An add-on is one `[[command]]` table in a yard's bridge settings, with no
enable flag. Its handler lives outside this repository: a program of your
own, or one you install. Add one: install the handler at an absolute path (owner-writable
only, executable, owned by this user or root, in a directory owned by
this user or root and writable by neither group nor others), add the
table (and a `handler_write_paths` entry if it writes), then run
`install.sh` again and restart the service: on Linux and WSL 2
`systemctl --user daemon-reload` and restart the unit, on macOS
`launchctl bootout gui/$(id -u)/<label> 2>/dev/null; launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist`, and run
`set-commands`. Remove one: delete the table, delete the handler program if nothing else uses it, drop its `handler_write_paths` entry if no other handler writes there, run `install.sh` again, restart the service the same way, then run `set-commands`. The menu loses the command only then.
The handler protocol is in `bridge/OPERATIONS.md`, "Commands".
With native `[yardr] home` and `operator` set, `/decide` and the `decide:`
prefix are the bridge's own (open decisions as native cards, YARDR.md), so no
add-on may use them.
Three ready add-ons are
[telegram-quota](https://github.com/codejunkienick/telegram-quota), which adds
a `/quota` command that shows how much of each AI subscription's usage
windows is spent, with the time left until each reset,
[telegram-secret-drop](https://github.com/codejunkienick/telegram-secret-drop),
which adds a one-time form on the local network for handing a secret to
an agent without it passing through the chat, and
[telegram-waiting](https://github.com/codejunkienick/telegram-waiting),
which adds a `/waiting` command that shows the list of what waits on the
paired user.

## Example

A partial file, with only the keys that differ from settings.example.toml:

```toml
yard = "example"
state_dir = "~/.local/state/example-telegram"
port = 8941
[yardr]
hook_url = "http://127.0.0.1:8795/hooks/tgbridge"
hook_secret_file = "~/.config/example-telegram/hook-secret"
[transcribe]
enabled = true
method = "auto"
[[topic]]
name = "homelab"
[[command]]
name = "deploy"
description = "Deploy a branch"
handler = "/absolute/path/to/telegram-deploy"
```

### Explicit Markdown

Ordinary replies may select `yardr reply <seq> --format markdown <text>`;
source/API sends and pins take `format=markdown`, and the bridge CLI takes
`--format markdown`. Existing auto/plain/HTML defaults stay unchanged.
The pinned parser ships offline in each release through the existing
installer. See [formatting policy and operator handoff](docs/formatting.md).

## Local public-readiness review

[Public-readiness checks and release handoff](docs/public-readiness.md) covers
portable setup, compatibility limits, a location-only committed-tree scan and
a local export/manifest for review. It does not publish or rewrite history.
Optional transcription downloads have separate [third-party notices](THIRD_PARTY_NOTICES.md);
the project's MIT [LICENSE](LICENSE) is unchanged.
