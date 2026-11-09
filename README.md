# yardr-telegram

A private Telegram bridge for a [yardr](https://github.com/yardr-dev/yardr)
yard: one paired chat with the yard's crew, its topics, tap-button asks,
pins, reactions, typing and on-host voice transcription. It is a fork of
the Gas City telegram pack (YARDR.md says what changed and why). The
bridge runs as a service of its own, and the yard reaches it as the
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
  each its own conversation: `bridge.py topics` creates the topics its
  settings list (`[[topic]]`) once and prints the `yardr subscribe` line
  that routes each to a crew member.
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
| `bridge/bridge.py` | the bridge: `run`, `probe`, `send`, `reply-current`, `ask-close`, `run-ask`, `react`, `pin`, `topics`, `set-commands`, `render-unit`, `check-install`, and the yard API on its loopback port |
| `bridge/OPERATIONS.md` | the operations guide: settings, launch, delivery recovery, transcription |
| `bridge/test_*.py` | `sh scripts/test.sh` (fake Telegram and whisper-cli, an optional isolated real yardr store, and the pack tests; no external network). Set `TEST_PORT_RANGE=LOW-HIGH` to bind test servers inside that range. Pick a range that excludes ports the host's own services use. |
| `settings.example.toml` | every settings key, with example values |
| `install.sh` | installs one yard's bridge as its own release copy of a clean commit under its root, checks its settings and host, mints the two secrets, renders its service and launchers; `--rollback` returns to the previous release |
| `yardr/` | the yardr pack: the declared source `tgbridge` (`tgbridge.yaml`), its `normalize` and `thread` commands, and `SETUP.md`, the steps for a yard |
| `YARDR.md` | the contract between the bridge and a yard: the inbound body, the yard API, the settings |
| `systemd/telegram-bridge.service.in` | the Linux unit template; on macOS the agent is rendered by `bridge.py render-unit` |
| `transcriber/` | `voice_transcribe.py` (whisper.cpp, configured language), `adapter.py` (the bridge's contract), `models.sha256` |
| `skills/` | the shared skills: `telegram-formatting`, `telegram-attachments`, `telegram-voice-transcripts` |

## Prerequisites

- Linux with a systemd user session (`systemctl --user status`). macOS
  with launchd works as it did for the gc pack (`bridge/OPERATIONS.md`,
  "Install and launch"), untested with yardr.
- Python 3.11 or later (`python3 --version`), and curl.
- yardr 0.16 or later with the yard running (`yardr version`), and the
  yard's hook listener on a loopback address (`yardr settings get
  hook_addr`).
- Optional native skill review needs the core captured single-file proposal
  capability (introduced at `a79f6f8162c8`) and the paired operator setup in
  [SETUP.md](yardr/SETUP.md#single-file-skill-snapshots). Full review parts
  precede buttons; Approve requires a direct ask-number reply. Other
  file-bearing proposals remain unsupported.
- Optional native herdr group cards (`group add`, `group set`) need core
  `fc7375cf5ad4` or later and the same setup
  ([SETUP.md](yardr/SETUP.md#herdr-group-add-and-set)).
- For transcription also git, cmake, make or ninja, a C++ compiler, ffmpeg,
  sha256sum, and network access to clone whisper.cpp and download the
  models (`transcriber/setup.sh --settings <file>`; about 600 MB).

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
bridge to its own installation". To move a bot from a Gas City bridge,
stop that bridge's unit first (two pollers on one bot conflict), and point
`state_dir` at a copy of its state directory, so the ledger, the offset,
the pins and the topics carry over.

## Uninstall

1. (shell) `systemctl --user disable --now <unit>`, then
   `rm ~/.config/systemd/user/<unit> && systemctl --user daemon-reload`,
   then `rm -rf <root>` (`~/.local/share/yardr-telegram/<unit stem>` by
   default): its releases, launchers, packs and `service.previous`.
2. (shell) In the yard: `yardr hook rm tgbridge`, then
   `yardr unsubscribe <crew> tgbridge` for each subscribed crew member, then
   `yardr source rm tgbridge`.
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
naming no yard, person or product. A yard links each into its crew's
skill folders.

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
yard = "odyssey"
state_dir = "/home/<user>/.local/state/odyssey-telegram"
port = 8941
[yardr]
hook_url = "http://127.0.0.1:8795/hooks/tgbridge"
hook_secret_file = "~/.config/odyssey-telegram/hook-secret"
[transcribe]
enabled = true
method = "auto"
[[topic]]
name = "homelab"
[[command]]
name = "deploy"
description = "Deploy a branch"
handler = "/home/<user>/bin/telegram-deploy"
```

### Explicit Markdown

Ordinary replies may select `yardr reply <seq> --format markdown <text>`;
source/API sends and pins take `format=markdown`, and the bridge CLI takes
`--format markdown`. Existing auto/plain/HTML defaults stay unchanged.
The pinned parser ships offline in each release through the existing
installer. See [formatting policy and operator handoff](docs/formatting.md).
