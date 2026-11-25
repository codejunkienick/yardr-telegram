# Telegram bridge operations

This guide describes the bridge between one yardr yard and one Telegram
bot. It adds no LLM, task poller, approval authority, or automatic blocker
detection. The yard's crew sends purposeful notifications and replies
through the yard (`yardr reply`, `yardr call tgbridge …`), and a process on
the host through the bridge's command line. It accepts private text and
media authored by the paired numeric user only; groups, other senders, bot
messages, and `/start` are skipped. A message the paired user forwards
reaches the yard marked and quoted as material, never as the user's
instruction ("Forwarded messages").

README.md covers adopting the bridge, `yardr/SETUP.md` setting it up in one
yard, `YARDR.md` the contract between the bridge and a yard, this guide
running it, and the skills are an agent's writing guides. Where README.md or
a skill differs from this guide, this guide is right and the other is the
one to fix; where this guide differs from `bridge/bridge.py`, the code is.

Every bot runs its own bridge process from this one `bridge.py`, configured
only by its own settings file. Several bots may share a yard, with distinct
sources and state directories. Every instance has the same message states;
`[yardr] cards` names the one native-card owner independently of reads and nudges.
See [the second-bot procedure](../yardr/SETUP.md#add-a-second-crew-members-bot).
Below, `<repo>` is
this repository's directory and `<settings>` that bridge's settings file.

## Terms

- **The yard**: the yardr yard this bridge serves (`yard`). The bridge
  reaches it through a hook ("Inbound"), and the yard reaches the bridge as
  the declared source `tgbridge` (`<repo>/yardr/tgbridge.yaml`, "The yard
  API").
- **The crew member**: the yard's crew member this bridge names (`crew`,
  default `yardmaster`). It routes nothing: the yard's subscriptions decide
  who gets each message ("Topics").
- **The paired user** and **the paired chat**: the one Telegram account
  (`user_id`) and its private chat with the bot (`chat_id`, equal to
  `user_id`).
- **A conversation**: the paired chat, `<chat_id>` (General), or one of its
  topics, `<chat_id>:topic:<thread_id>`. A thread of the yard is a
  conversation.
- **The ledger**: `<state_dir>/telegram-bridge.sqlite3`, the inbound offset,
  one receipt per outbound key ("Delivery recovery") and the message log
  ("The message log").
- **The accept point**: the yard's 2xx answer with a positive `seq` to a
  relayed message ("Inbound").
- **The account**: the bot's username (`account_id`), in the ledger's
  conversation records, which tells two bots apart.
- **The callback secret**: the random value in `callback_secret_file` that
  every request to the yard API carries as a bearer token; the yard holds it
  as the source's credential `TGBRIDGE_SECRET`.
- **The hook secret**: the random value in `[yardr] hook_secret_file` that
  the bridge sends to the yard's hook, which was added with it.

## Settings

Prerequisites: Python 3.11+, curl, and a yardr yard set up for the bridge
(`yardr/SETUP.md`): its hook listener on a loopback address (`hook_addr`),
the pack in `<repo>/yardr` applied with the callback secret as the source's
credential, a hook added with the hook secret, and the paired user's
Telegram id as their identity in the source. Do not apply yardr's own
`telegram` pack to this bot: its webhook stops `getUpdates` polling. The bot
must have no webhook or other poller. `probe` checks identity and webhook
without consuming messages; it checks nothing in the yard.

The settings file is TOML, owner-only (mode 0600), outside any repository;
`<repo>/settings.example.toml` lists every key. Without `--settings` the
bridge reads `$TELEGRAM_BRIDGE_SETTINGS`; with neither, every command refuses
with `no settings file: pass --settings or set TELEGRAM_BRIDGE_SETTINGS`. A
missing file, a file others can read, an unknown key or a bad value refuses
every command with one line naming the problem; nothing falls back to a
default yard.

| key | required | meaning |
| --- | --- | --- |
| `yard` | yes | the yard's name: in the default unit name, the unit's description and every conversation record in the ledger |
| `state_dir` | yes | ledger, message log, inbound offset, inbound files, heartbeat |
| `port` | yes | the yard API's loopback port, 1024-65535, unique per bridge on a host; the source's `base` names it (`yardr/SETUP.md`, step 5) |
| `token_file`, `callback_secret_file` | yes | owner-only credential files; the callback secret is the yard API's bearer token (`install.sh` creates a missing callback secret: the unit cannot write outside its state and lock directories) |
| `[yardr]` `hook_url` | yes | the yard's hook: `http://<host>:<port>/hooks/<name>`, the host `127.0.0.1`, `::1` or `localhost`, with no user, query or fragment, at most 256 characters |
| `[yardr]` `hook_secret_file` | yes | owner-only file holding the hook's secret, sent as `X-Tgbridge-Secret`; a file other than the token and the callback secret (`install.sh` creates a missing one, as for the callback secret) |
| `[yardr]` `source` | no | the declared source's name in the yard, in the lines `topics` prints; default `tgbridge` |
| `[yardr]` `home` | no | existing private yard home for message-state reads on any instance; socket is `home/yard.sock` |
| `[yardr]` `operator` | no | current operator mapped to the paired Telegram user on this source; requires home, enables verified nudges and configured operator commands |
| `[yardr]` `cards` | no | native-card ownership: true on exactly one instance, false on all others; true needs home+operator. Required with home and operator |
| `[bot]` `bot_id`, `user_id`, `chat_id`, `account_id` | yes | positive numeric ids (`chat_id` equals `user_id`: a private chat), the bot username as the account |
| `lock_dir` | no | shared directory for per-bot and per-yard native-card locks; default the token file's directory |
| `unit` | no | service name: the systemd unit name on Linux and WSL 2, and on macOS the source of the launchd label (this name without its `.service` suffix); default `<yard>-telegram.service` |
| `python` | no | interpreter for the unit and the transcriber; default `/usr/bin/python3` |
| `user_display_name` | no | the name of the paired user on relayed messages (`from.name`) and in the message log; default `User` |
| `timezone` | no | times in notices and the time zone passed to handlers (`TZ`); default `UTC` |
| `health_file` | no | a health report the bridge only reads; enables the quota-exhausted notice |
| `pairing_update_id` | no | the `update_id` of the `/start` update; the bridge starts after it. Read only for a new ledger (README.md, "Adopt in a yard") |
| `crew` | no | the crew member this bridge names; default `yardmaster`. It is the crew member in the `subscribe:` line of each `[[topic]]` that names none of its own and of each topic the user creates ("Topics"), the one in the unit's description, and the session the quota-exhausted notice watches (on a recorded `[[topic]]`'s conversation, that topic's `crew`). It routes nothing |
| `[[menu]]` `command`, `description` | no | the chat's "/" menu, in order |
| `[[command]]` | no | the yard's own commands and buttons, run by its own handlers ("Commands" below) |
| `handler_write_paths` | no | directories the unit lets command handlers write, besides the bridge's own |
| `[transcribe]` | no | voice transcription (below); off by default |
| `[reactions]` | no | `on_accept`, `seen`, `done`, `read`, `nudge_after` (plus ignored legacy `working`, `working_after`): the `seen` reaction on each accepted message, optional yard-backed `read`/`done` tracking and unread Nudge offer ("Reactions" below) and the emoji `react` sets; no reaction ever without `on_accept = true`, `read` or a `react` call |
| `[typing]` | no | `seconds` (0-900, default 0): with a live event stream, "typing..." follows real crew activity in the newest open row's thread, capped by `seconds` as a safety cap; without one, "typing..." shows after each accepted message until the next send, at most this long ("Typing" below); no chat action ever with 0 |
| `[[topic]]` `name`, `crew` | no | topics of the paired chat, each its own conversation, created once by `topics` ("Topics" below); at most 50 |
| `[auto_topics]` | no | `enabled` (default false): serve the topics the paired user creates in the chat ("Topics the user creates" below); without it only the `[[topic]]` tables are served |

The health report at `health_file` is a JSON object with `observed_at`
(ISO-8601 with zone, at most 10 minutes old) and `sessions`, a list of
objects with `template` (the crew member's name), `status` (`quota_wait`
triggers the quota-exhausted notice), `episode` (a non-empty string) and
`until` (epoch milliseconds, in the future). The bridge never writes it.

Paths may start with `~`; after that they must be absolute, normalized, and
made of letters, digits and `._@+-` only, so they are safe in a unit file.
No credential goes in the settings file, command arguments or Git. curl
runs with `-q`, so no user curl configuration applies to the bridge's
requests.

## Install and launch

`<repo>/install.sh --settings <settings> [--root <root>]` installs one
bridge instance as its own release copy. `<repo>` must be a git checkout
with no tracked changes; its HEAD is the release. The root defaults to
`~/.local/share/yardr-telegram/<unit stem>` (the `unit` name without
`.service`) and holds:

- `releases/<commit>/`: a `git archive` of the commit, with `RELEASE`
  (commit, source path, UTC time) and `SHA256SUMS`;
- `current` and `previous`: links to releases;
- `bin/telegram-bridge` (`<python> <root>/current/bridge/bridge.py
  --settings <settings>`) and `bin/voice-transcribe`;
- `service.previous`: the service file the install last replaced;
- `packs/<digest>/` and `pack`: a release's `yardr/` with its `base` line
  naming the settings' port and its pack/source names taken from `[yardr]
  source`, and the link to the active release's one, which
  the yard applies (`yardr/SETUP.md`, step 5).

Use `<root>/bin/telegram-bridge` for the operator commands below; it supplies
this instance's settings and Python interpreter.

An installed instance runs, reads and writes nothing inside the source it
was installed from: not its root, not the settings file, and not `python`, a
secret, `state_dir`, `lock_dir`, `health_file`, `yardr.home`, a handler, a
handler write path, a configured `transcribe.gpu_check`, `whisper_cli` or
`models_dir`. A value counts as inside when resolving it, with every link
followed at every component, passes through `<repo>`, so a
`<repo>/.venv/bin/python` that links to the host's python counts. The
refusal names the entry and its remedy: copy a program or model out and
point the setting at the copy; stop the service, move a file or directory
out and point the setting at it; point `python` at an interpreter outside
(such as `/usr/bin/python3` or a venv made elsewhere). The settings are
never rewritten, and an installed release's `check-install` checks the same
against the `source` its `RELEASE` records.

In order, it:

1. checks python and the settings, and refuses anything that depends on
   `<repo>`, one line per entry, before it writes anything. The existing
   service file and `<root>/bin/telegram-bridge` must name this same
   settings path; another path or unreadable settings ownership refuses
   before any write, in install, dry-run and rollback alike. A collision
   names the conflicting file and both settings paths. Moving a settings
   file deliberately needs a fresh unit and root, retaining the old instance
   for rollback (`yardr/SETUP.md`, "Move an existing bridge to its own installation");
2. stages HEAD under `releases/.stage-*`, or reuses `releases/<commit>`
   when its files match the staged sums, and refuses it when they do not;
3. creates the callback secret, the hook secret and the transcription queue
   when they are missing (each owner-only, a secret never printed). These
   are the only writes outside the root before step 4, and are kept
   whatever happens next;
4. runs the staged copy's `check-install` (python, curl, owner-only
   credentials, and with transcription on: ffmpeg, whisper-cli, the models
   and their SHA-256). A failure removes the stage and changes nothing else;
5. activates under exclusive `flock`s on the root, then the service
   directory; one lock covers both when they name the same directory,
   including through a symlink. It rechecks the service and launcher
   settings ownership under these locks, so different roots cannot claim
   the same service concurrently. Then `previous` becomes the old `current`,
   and `current` the release. It writes the service file and the launchers,
   each only when it changed, and moves `pack` last. Every one is written
   beside its place first, and a failure puts back what was already
   replaced. A signal waits until the renames or their undo are done.
   On Linux and WSL 2 the service file is the systemd unit
   `~/.config/systemd/user/<unit>`; on
   macOS it is the LaunchAgent plist `~/Library/LaunchAgents/<label>.plist`,
   where `<label>` is the `unit` name without its `.service` suffix. A
   replaced service file is kept in `service.previous`, and the replacement
   diagnostic prints its old and new settings paths as well as bridge paths.

`install.sh --settings <settings> --rollback` runs from any release (for
example `<root>/current/install.sh`) and needs no git. When the upgrade changed
settings compatibility, restore the pre-edit settings file and run
`<root>/previous/install.sh` instead: the upgraded `current` script refuses the
older settings before swapping. It verifies
`previous` against its `SHA256SUMS`, runs its `check-install`, and swaps
`current` and `previous`; `pack` returns to that release's own. After the
first install there is no previous release: restoring `service.previous`
over the service file is that rollback. Releases are never pruned: a running bridge keeps the release
its `current` resolved to when it started. Removing an instance is
disabling its service, removing its service file and `rm -rf <root>`.

A release's pinned pure-Python dependencies, when it has any, are committed
under `vendor/`, which `bridge.py` puts first on `sys.path`. Nothing is
installed with pip or fetched from the network. `install.sh` is idempotent, never starts or
stops a service, never touches `~/.local/bin/voice-transcribe` and never
contacts Telegram or the yard; `--dry-run` only reports and writes nothing.
The yard's side, from its hook listener to the subscriptions, is
`yardr/SETUP.md`, steps 4 to 8. Then, in the user's shell (an agent asks
the user, or runs it only where it may manage the user's services), on
Linux and WSL 2:

```sh
<root>/bin/telegram-bridge probe
systemctl --user daemon-reload
systemctl --user enable <unit>
systemctl --user restart <unit>      # starts it, or restarts it on the new release
systemctl --user status <unit>
<root>/bin/telegram-bridge set-commands
```

On macOS (in a shell whose `python3` on PATH is 3.11 or later, e.g.
`/opt/homebrew/bin` first on PATH: `install.sh` refuses an older
`python3` on PATH; the launcher and the agent run the settings' `python`):

```sh
<root>/bin/telegram-bridge probe
launchctl bootout gui/$(id -u)/<label> 2>/dev/null
launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<label>.plist
launchctl print gui/$(id -u)/<label>
<root>/bin/telegram-bridge set-commands
```

`probe` prints `Telegram identity and webhook checks passed` and
`set-commands` prints `commands set: <count> (/<command> ...) for chat
<chat_id>` (`commands set: 0 () for chat <chat_id>` when the menu is empty).
Once the service runs, `yardr source check tgbridge` checks the yard's side
(`GET /v1/health`, "The yard API").

`<root>/bin/telegram-bridge render-unit` prints the service for
this host: the systemd unit text on Linux and WSL 2, the launchd
LaunchAgent plist on macOS, and `check-install` runs the checks alone
(`--gpu-probe` adds the GPU probe, below; without it no transcription
runs). In an installed release `check-install` also says whether its files
match its `SHA256SUMS`; a checkout prints no such line. On Linux and WSL 2
the unit runs
`<python> <root>/current/bridge/bridge.py --settings <settings> run` with the state
directory and the lock directory as its only writable paths, plus the
transcription queue with transcription on and every `handler_write_paths`
entry. The settings file, token, callback secret and hook secret are
read-only inside the unit (`ReadOnlyPaths`), even inside the lock
directory, and the unit sets RestrictSUIDSGID, LockPersonality,
RestrictRealtime, RestrictNamespaces, SystemCallArchitectures=native and
RestrictAddressFamilies=AF_UNIX AF_INET AF_INET6 AF_NETLINK, all of which
the GPU probe carries too. On macOS the plist runs
`<python> <root>/current/bridge/bridge.py --settings <settings> run --conflict-exit-status 0`
with the state directory as its working directory, loads at login, restarts after 15 s, allows 45 s to stop, sets `PATH` to `/opt/homebrew/bin:/usr/local/bin:/usr/bin:/bin:/usr/sbin:/sbin`, and writes its output to `~/Library/Logs/<label>.log`. `run` accepts `--conflict-exit-status <0-255>` (default 2, a `run` flag only): on Linux and WSL 2 a polling conflict exits 2 and the unit does not restart it, while the macOS agent passes 0 (below).

`run` serves the yard API on `127.0.0.1:<port>` only ("The yard API") and
polls Telegram; it registers nothing with the yard, which reaches it at the
source's `base`. Only `run` reads the hook secret. At startup the
Telegram probe is retried with backoff for up to 10 minutes, so a short network
outage does not exit the process; an identity mismatch or an existing webhook
still exits at once. Other failures exit and the service restarts it
every 15 s without a start limit (systemd on Linux and WSL 2; on macOS
launchd restarts the agent after an unsuccessful exit, throttled to
15 s), so an outage can never leave it permanently failed. While the
service is down nothing from the chat reaches the yard and the yard API
does not answer: `yardr reply` fails with `the bridge did not answer
/v1/send (<error>): is its unit running?` (`<repo>/yardr/tgbridge/thread`),
and `yardr source check tgbridge` fails. The command line keeps working:
every command but `run` delivers directly through the ledger ("Send and
receive"). Restart the service with `systemctl --user restart <unit>` on
Linux and WSL 2; on macOS with
`launchctl bootstrap gui/<uid> ~/Library/LaunchAgents/<label>.plist`
when the agent is not loaded, otherwise
`launchctl kickstart -k gui/<uid>/<label>`. A bot polling
conflict on Linux and WSL 2 exits with status 2 and disables automatic
restart; stop the other poller/webhook, then start the unit explicitly.
On macOS the agent passes `--conflict-exit-status 0`, so a polling
conflict exits successfully and launchd does not restart it; stop the
other poller/webhook, then start it again with
`launchctl kickstart gui/$(id -u)/<label>`.

`check-install` adds host lines per OS. A `WARN` line is not a
failure: `check-install` still exits 0, so a successful install can
show `WARN` instead of `ok` for the warnings below. On WSL 2 it passes
`WSL 2 with systemd`, fails WSL 1 and a distribution without systemd
running, fails any checked path under `/mnt/`, and warns without
linger. On macOS it warns for a checked path in `~/Desktop`,
`~/Documents`, `~/Downloads` or iCloud Drive, fails `transcribe.cpus`
(below), marks each handler `(runs with your user's full rights: no sandbox on macOS)`, and warns that each `handler_write_paths` entry `is not enforced on macOS (no sandbox)`; `curl` and `ffmpeg` are looked up on the launchd `PATH` above.
The hook secret is checked like the callback secret: owner-only, or `is
created by install.sh` when it is missing.

After each successful `getUpdates` the bridge records
`<state_dir>/telegram-bridge-heartbeat.json`
(`{"last_poll_ok_at": <ISO-8601 UTC>, "pid": <int>}`), atomically and at most
once per 60 s; a failed write prints `poll heartbeat not saved` and polling
continues. `GET /v1/health`, the source's check, answers its
`last_poll_ok_at` as `last_poll_ok`. A health check can judge the bridge by
its age, but the
age can grow by up to the longest configured `[[command]]` timeout (up to
3600 s) while a healthy bridge runs a handler in its poll loop, so a
health check must allow for that before flagging or restarting it.

The yard API is protected by the loopback bind and the callback secret.
The yard holds that secret as the source's credential, and so does each
crew member that replies, in its env file (`yardr/SETUP.md`, steps 5 and
8). Whoever holds it can send to the chat as the bot, read the message log,
set reactions and pins, close asks, and have the bridge send any file its
user can read, except the bridge's credentials and ledger (`attachments`
are host paths). The bridge assumes a host whose other local accounts are
trusted: loopback ports are reachable by every local user, so an account
that binds the yard's hook port or a handler's port while that service is
down receives what the bridge sends there (the hook secret and the message
text, or the handler JSON).

Removing a yard's bridge: README.md, "Uninstall".

## Inbound

The bridge relays each message of the paired user to the yard as one POST
to the yard's hook, after its own handling: a command goes to its handler
instead ("Commands"), a button press is an ask answer or a handler's
("Asks"), a file is downloaded first ("Inbound media relay"), and a voice
or audio note is relayed at once and its transcript later ("Voice-note
transcription"). The request goes through curl with the URL, the headers
and the body on curl's stdin, never in argv:

```text
POST <[yardr] hook_url>
X-Tgbridge-Secret: <the contents of [yardr] hook_secret_file>
Content-Type: application/json
```

The body is one JSON object (`YARDR.md`, "Inbound: bridge → yard"):

| field | value |
| --- | --- |
| `id` | `<bot_id>:<conversation>:<message_id>`, which the yard dedupes on |
| `kind` | `message`; `transcript` for a voice or audio note's transcript; `answer` for a tapped ask |
| `chat` | the paired chat's id |
| `conversation` | the conversation it is relayed on ("Topics") |
| `topic` | `general`, or the topic's name: a `[[topic]]`'s name, or the display name of a topic the user created ("Topics") |
| `thread_id` | the conversation's thread id, or `null` on the paired chat's conversation |
| `message_id` | the provider message id: Telegram's message id, `<id>-transcript` or `ask-<ask-id>` |
| `reply_to` | the id of the message it replies to, or `null` |
| `from` | `{"id": "<user_id>", "name": "<user_display_name>"}` |
| `text` | the whole relay text: the topic line, a forward's header, a file's note, the text or caption, a transcript |
| `forwarded` | `true` for a forwarded message and its transcript, else `false` |
| `received_at` | when the bridge relayed it, ISO 8601 UTC |

The yard keeps at most 500 characters of the text; the message log keeps
it whole for `yardr thread show <seq>` ("The message log").

**The accept point.** The bridge waits up to 20 s for the yard's answer. A
message is accepted only when the yard answers 2xx with a JSON object whose
`seq` is a positive integer: the yard has recorded it, new or as a
duplicate of an earlier delivery of the same `id` (`{"duplicate": true,
"seq": <n>}`). Only then does the bridge go on: it adds the text to the
message log; for a message of the paired user (not a transcript or an ask
answer) it records the message as the last accepted one ("Reactions"),
sets `seen` and opens typing when the settings say so, and may post the
quota-exhausted notice; then the inbound cursor moves past the update.
The accept point is the yard's record, not its routing and not an answer:
a message that no subscription matches is accepted all the same.
Initially the cursor begins after the pairing message.

Everything else keeps the cursor: an answer outside 2xx (a wrong secret,
an unknown hook), `{"dropped": true}` (the yard verified the secret and
set the delivery aside, as it does for a sender it does not hear:
`yardr/SETUP.md`, steps 6 and 7), a 2xx without a positive `seq`, no answer
within 20 s, and a request that never left the host (the yard's hook
listener is down). The
message is not lost: the poll loop logs `Transport unavailable; inbound
cursor retained`, waits 2 s times the failures in a row (at most 10 s),
and reads the same update again, and every later update waits behind it.
After 10 failures in a row the process exits 1 with `transport failed
repeatedly; restarting preserves cursor`, and the service restarts it
("Install and launch"). The bridge's log carries no reason for a refusal;
the yard's side does (`yardr hook show tgbridge`, `yardr events`). A
retried relay carries the same `id`, so the yard records it once. Diagnose
repeated failures instead of erasing state.

A transcript is relayed by the transcription worker, not the poll loop,
with retries of its own ("Durable transcript queue").

## The yard API

`run` serves the yard on `http://127.0.0.1:<port>`, the source's `base`.
The yard's calls (`<repo>/yardr/tgbridge.yaml`, `calls` and `notify`) and
its thread commands (`<repo>/yardr/tgbridge/thread`) use it; so may any
process on the host that holds the callback secret. Every request carries
`Authorization: Bearer <the callback secret>`, and every answer is one JSON
object.

| route | body | answer |
| --- | --- | --- |
| `POST /v1/send` | `text`; `key`, else the `Idempotency-Key` header (required); `conversation` (an id) or `topic` (a name), default the paired chat; `format` (`auto`, `plain`, `html` or `markdown`; default `auto`); `silent`; `keyboard` (rows, or rows as a JSON string); `text_options`; `reply_to`; `attachments` (a host path, or a list of them); `attachment_kind` | `{delivered, message_id, conversation}`, with `reason` and `failure_kind` when not delivered |
| `POST /v1/tell` | `title`, `text`, optional `link`, `conversation` or `topic` | as `/v1/send`; 503 when whole mail is unfinished ("Whole mail") |
| `POST /v1/posted` | `key`, else the `Idempotency-Key` header | `{"message_id": "<id>"}` when the ledger holds a confirmed send under the key, else `{}` |
| `POST /v1/read` | `conversation` or `topic`; optional `limit`, 1-200, default 50 | `{"messages": [{id, direction, from, text, at}, ...]}`, oldest first ("The message log") |
| `POST /v1/react` | `reaction`; optional `message_id`, or `topic` or `conversation`; optional `key` | `{delivered, message_id}`, with `reason` and `failure_kind` when not delivered ("Reactions") |
| `POST /v1/pin` | `slot`; `text` and optional `format` (default `auto`), or `unpin: true` | `{delivered, message_id}`, with `changed` for a pin, as above when not delivered ("Pinned messages") |
| `POST /v1/ask-close` | `decision`, and one of `message_id` and `ask_key` | `{delivered, message_id}`, as above when not delivered ("Asks", A6) |
| `GET /v1/health` | | `{"ok": true, "bot": "<account_id>", "last_poll_ok": <ISO-8601 or null>}` |

- **200** carries the answer above, also for a send that is refused or not
  confirmed: `delivered: false` with the receipt's `reason` and
  `failure_kind` ("Delivery recovery"), so a caller can tell a replayed key
  from a failure.
- **400** `{"error": "<what is wrong>"}`, before anything is sent: a body
  that is not one JSON object of at most 256 KiB, a missing or malformed
  field, a `topic` or `conversation` this bridge does not serve (`topic
  <name> is not configured, created or served`, `conversation ID is not the
  paired private chat or a configured topic`), and a reaction with no
  accepted message to react to.
- **403** `{"error": "forbidden"}` for a missing or wrong secret, on every
  path; **404** `{"error": "not found"}` for any other path.
- **500** `{"error": "delivery unconfirmed"}` for anything unexpected: treat
  the send as ambiguous and never resend it blind ("Delivery recovery").
- **503** `{"error": "tell unfinished; retry"}`, from `/v1/tell` only, with
  `[yardr.mail]`: a part of a mail was not sent, the time to start one
  had passed, a mail could not be rendered, the yard did not answer the
  mail read, or a sender's mail is not yet read back to the window's start.
  The yard tries the tell again and the ledger resumes it ("Whole mail"). An answer the caller is no longer there to read logs
  `yard api answer not written`; what was sent stands in the ledger.

`silent`, `text_options` and `unpin` read `true`, `"true"`, `"1"` or `1` as
true and `false`, `"false"`, `"0"`, `0`, an empty string or no value as
false; anything else is a 400. In `/v1/send`, `/v1/react`, `/v1/pin` and
`/v1/ask-close` an empty string reads as an absent optional field. `topic`
names a `[[topic]]` with a ledger row, or a served topic the user created
by its display name, and wins over `conversation`; `topic` `general` reads
as no topic, so `conversation`, else the paired chat, applies.

`/v1/tell` is the yard's `notify`: plain text, the title on its own first
line, then the text, then the link, each stripped, at most 250, 3500 and
512 characters. Its key is the request's `key`, else its `Idempotency-Key`
header, else `tell:` and 32 hex characters of the SHA-256 of the
conversation, the text and the minute, so the same tell retried within the
minute is sent once and the same words a minute later are a new message
(YARDR.md: the yard tells with no key). A key starting `yardmail:` is the
bridge's own ("Whole mail"): `/v1/send`, `/v1/tell` and `send
--idempotency-key` refuse it with a 400 or a usage error.

### Whole mail

The yard's notify tells the first 200 characters of one mail, and of
several only who wrote (`3 new in your yardr inbox` / `mail from a (2),
mail from b`). With `[yardr.mail]` in the settings (no keys; it needs
`[yardr] home` and `operator`), each `/v1/tell` first sends the operator's
unread mail whole:

1. Read, within 3 s, over the native socket: the operator is checked as for
   native cards, and their `notify` must be this source (else the tell is
   sent as it came). `UserInbox` names who wrote unread mail;
   `Conversation` with each, from the newest end, 200 letters a page, gives
   the unread mail to the operator of the window: the last 6 hours (the yard
   tells nothing older) and not before the first start with `[yardr.mail]`
   (no backfill). Each sender is paged to the window's start within the
   3 s, and what was read is kept (the ledger table `yard_mail_scan`, once
   its mail is sent): a later tell reads only below it and what is newer,
   never the same pages again, and a sender read through the inbox's newest
   unread mail not at all. A sender not read that far, or not reached in
   the 3 s, has the tell answer 503 after sending what it found; the yard's
   retry reads the senders least lately read first, and is answered 200
   once every sender is read through the newest unread. A sender's older
   mail may so come after newer mail it already sent. The yard unavailable or not
   answering is a 503 too, with nothing sent. A read the yard refuses or
   answers in a form not understood, or an operator not verified, sends the
   tell as before with one line `(whole mail not read: <reason>)`.
2. Each mail, by seq, is its own message or messages to the tell's
   conversation (today the chat itself), headed `mail from <sender> ·
   HH:MM` in the settings' timezone (`timer from` for a timer's), with
   `· i/n` when it takes several parts and, on part 1, `files: <names> (in
   the yard)` for a mail with files (the files stay in the yard). The text
   is Markdown with links; else with links as `text (destination)` (a
   relative link refuses the first); else the text escaped, no markup. A
   fenced block is `<pre>`, its content exact, in a list or a quote too.
   Telegram's parse 400 sends the plain form once, as for every HTML send.
3. Parts: whole blocks, a blank line between, up to 4096 visible units each
   with room kept for the longest header, so a part never changes size
   between tries. A block that alone does not fit is cut: a list or quote
   with code around its code blocks, each still one exact `<pre>`; a code
   block at lines into consecutive exact `<pre>` slices; any other at lines
   of its source as plain text, resolving links to `text (destination)` so
   reference definitions outside the block are preserved. Nothing is dropped
   or reordered. A mail that cannot be rendered (over 99 parts, or a header
   Telegram would refuse) is a 503.
4. Each part goes once under the ledger key `yardmail:<seq>:<i>`; a part
   already sent is never sent again (nor logged again). A part not sent
   (offline, or Telegram's 429, which only mail treats as not sent) stops
   the tell with a 503; the yard's retry resumes at that part. A part whose
   send is unconfirmed is never resent; the next part says `· an earlier
   part is unconfirmed`. A part Telegram refuses ends that mail. No part
   starts after 7 s from the tell's start (the yard waits 10 s), checked
   again once the part holds the send lock: the rest goes on the retry, as
   not sent. One expansion runs at a time.
5. The tell's own notice is left out only when it tells of mail alone, in
   the yard's exact words, and every unread window mail of each sender it
   names, read by this tell or an earlier one (the ledger table
   `yard_mail_found`), went whole, at least as many as it counts; otherwise
   it follows the mails (an ask among them, a failed or unconfirmed part).
   The worst case is one line too many, never a mail lost. A
   tell answered 503 sends no notice: the bridge cannot know the yard's
   last try, and a mail still unread in the window goes whole with the
   next tell that reads it.

Mail enters the conversation's message log (`/v1/read`) as every send
does, and leaves the machine whole. Mail stays unread in the yard: reading
it in Telegram acknowledges nothing. Without `[yardr.mail]`, `/v1/tell` is
as described above.

To turn it on, on an instance with home and operator: add an empty `[yardr.mail]` table to
the settings, run `<root>/bin/telegram-bridge --settings <settings> probe`
(the first start with it: mail before is never sent), then restart the
unit. To turn it off, remove the table and restart; the ledger and the
start mark stay. Mail goes to the user's `TGBRIDGE_CONVERSATION`: a
`<chat id>:topic:<thread id>` address stops working when the chat's
threaded mode is turned off, so give the bare chat id again with `yardr
user key <operator> tgbridge`.

The daemon serves each request on its own thread. Its sends queue behind
its `publish_lock`, and they meet the poll loop's and the command line's in
the ledger ("Delivery recovery"). The yard holds each caller to 60 calls of
a source an hour, 10 of them updates, unless the source says otherwise
(YARDR.md, "The yardr pack"); `yardr reply` is not a call.

`yardr reply <seq>` runs the thread command `reply`: `/v1/send` on the
delivery's conversation, with the reply's `key`, `format: auto` and its
files as `attachments`. With `--format markdown`, the declared thread carries
the original source and explicit mode through the same send path (requires
a core with explicit reply-format support). A `delivered: false` fails it with `the bridge did
not deliver the reply: <reason> (<failure_kind>)`. The thread command
`posted` (`/v1/posted`) lets the yard find a reply whose answer was lost,
and `read` (`/v1/read`, `limit` 50) is what `yardr thread show <seq>` shows.

## The message log

The ledger's `messages` table keeps the newest 500 messages of each
conversation, whole, for `/v1/read` and so for `yardr thread show`. A row
is one of:

- **in**: a relay the yard accepted ("Inbound"), written after the accept
  point, never before: its whole text under its provider message id
  (`<id>`, `<id>-transcript`, `ask-<ask-id>`), from `user_display_name`, at
  its `received_at`. A message relayed on the paired chat's conversation
  from a topic the bridge does not serve is logged there.
- **out**: a send Telegram confirmed through `publish()` (`/v1/send`, so
  `yardr reply`; `/v1/tell`; `send`; the
  quota-exhausted notice), under its message id (an album's first), from
  the account, with its visible text (tags removed for HTML) and, when it
  carried files, a first line `[<file name>, ...]`.

Handler sends and edits, ask edits, pins, reactions and chat actions are
not logged. A row is added once per conversation, direction and message id,
so a replayed relay or send adds nothing. A row that cannot be written logs
`inbound message not logged (msg <id>)` or `sent message not logged` and
changes nothing else: the yard already has the message, or Telegram has
already shown it.

## Command menu

The paired private chat shows the settings' `[[menu]]` entries, then the
`[[command]]` entries with `menu = true`, as its "/" menu, in file order.
Register it once after a change, and again at any time with the same result:

```sh
<root>/bin/telegram-bridge set-commands
```

It calls `setMyCommands` with scope `{"type": "chat", "chat_id": <chat_id>}`,
reads the list back with `getMyCommands` for that scope, and prints
`commands set: <count> (/<command> ...) for chat <chat_id>` only when the
read-back equals the list exactly. A Telegram rejection or a different
read-back exits 1 with a message that carries no token or response body. It
reads the token and the settings only: no SQLite store, callback secret, bot
lock, webhook or `getUpdates` call, so it is safe while the unit runs and
needs no restart. Other chats and the bot's default menu are not set. With
nothing to show it sends an empty list, clearing the paired chat's menu, and
verifies the read-back is empty.

The quota-exhausted notice goes out as Telegram HTML (bold headers) through
the bridge's own `publish()`. A command a `[[command]]` registers goes to
its handler (below); every other command reaches the yard as an ordinary
message.

## Commands

A yard brings its own slash commands and inline-keyboard buttons through the
bridge's settings and its own handler programs, without editing this
repository. Each `[[command]]` table registers one:

| key | default | meaning |
| --- | --- | --- |
| `name` | required | the command, `/<name>`: 1-32 of `a-z`, `0-9`, `_` |
| `description` | required | the menu text, 1-256 characters |
| `handler` | one of the two | absolute path of an existing regular file this user may execute, writable by nobody but its owner, owned by this user or root, in a directory only they can write (for a symlink, both the link's and the target's directories) |
| `url` | one of the two | `http://127.0.0.1:<port>/<path>` or `http://[::1]:<port>/<path>`, nothing else |
| `menu` | `true` | show it in the chat's "/" menu (`set-commands`) |
| `callback_prefix` | `"<name>:"` | button presses whose `callback_data` starts with it go to this handler; 1-64 printable ASCII characters, no space |
| `timeout` | `10` | seconds the handler may take, 1-3600 |

The settings refuse to load, naming the entry, on an unknown key, a repeated
name, a name equal to `start` or a `[[menu]]` command, a missing or
second handler, a handler that is not an owner-writable-only executable file
owned by this user or root in a directory only they can write (for a
symlink, both the link's and the target's directories),
a URL off loopback, a `callback_prefix` that equals or starts another one
(a press would have two owners), or more than 100 menu entries in all.
`start` stays reserved always.

Adding or removing an add-on follows the procedure in `README.md`,
"Add-ons".

**Dispatch.** A message from the paired user whose text is `/<name>` or
`/<name>@<bot username>`, alone or followed by a space and arguments, goes to
that command's handler and never to the yard. The bridge polls for `message`
and `callback_query` updates. A button press from the paired user (`from.id`
is `user_id`, not a bot, in the paired chat) whose `callback_data` starts
with a registered prefix goes to that prefix's handler. A press under the
bridge-owned `ask:` prefix is an ask answer ("Asks" below), never a
command. Any other press is answered
empty, logged as `callback dropped (update <id>): <reason>` and dropped; it
never reaches the yard. With no `[[command]]` every non-ask press is dropped so.
Handlers run one at a time in the poll loop: a slow handler holds the next
update for up to its `timeout`. A `timeout` above the unit's
`TimeoutStopSec` (45 s) can outlive a stop or restart: the bridge is killed
before it advances the offset past that update, so after the restart the
same `update_id` is delivered again. A URL handler runs outside the unit,
so a stop cannot end its wait either. Handlers, URL handlers above all,
must therefore dedupe on the `update_id` in their input and treat a repeat
of an update they already handled as already done.

**Handler input** is one JSON object; a field that does not apply is `null`:

```json
{"kind": "command", "command": "deploy", "args": "feature/x", "update_id": 40,
 "message_id": 1040, "callback_data": null, "callback_message_id": null,
 "received_at": "2026-10-09T08:00:00+00:00", "user_display_name": "Alex"}
```

`args` is the text after the first space (`""` when none). For a press,
`kind` is `"callback"`, `args` and `message_id` are null, `callback_data` is the
button's data and `callback_message_id` the message carrying the button. No
token, secret, raw update or yard data is passed. A handler runs as the
bridge's user and can read what that user can, the bot token included:
install only handlers you trust. An executable handler is run
as `argv = [handler]` (no shell) with the JSON on stdin, its own directory as
working directory, and only `PATH=/usr/local/bin:/usr/bin:/bin`, `HOME`,
`LANG`, `TZ` (the settings' `timezone`) and `TELEGRAM_BRIDGE_COMMAND=<name>`
in its environment; at its `timeout` its whole process group is killed. A
URL handler gets the JSON as an `application/json` POST, with no proxy and no
redirect followed, the same timeout over the whole exchange, and a 200 answer
read up to 64 KiB.

**Handler output** is exactly one JSON object `{"actions": [...]}`, at most
64 KiB and 32 actions, each exactly one of:

- `{"type": "send", "text": ..., "format": "plain"|"html"|"markdown", "keyboard": ...}`
- `{"type": "edit", "message_id": <int>, "text": ..., "format": ..., "keyboard": ...}`
  (edits a message the bot sent; without `keyboard` the edit removes the buttons)
- `{"type": "answer_callback", "text": ..., "alert": <bool>}` (a press only, at
  most once; `text` up to 200 characters, both optional)

A command typed in a topic, or a press on a message in a topic, answers in
that thread: each `send` and the failure notice carry its
`message_thread_id`; edits and press answers do not ("Topics").

`format` defaults to `plain`; `html` follows "Formatted messages" below and
the text limit is 4096 UTF-16 units of visible text. A `keyboard` is rows of
buttons `{"text": ..., "callback_data": ...}`, at most 8 buttons per row and
at most 100 buttons in total;
`callback_data` is 1-64
bytes and starts with one of the settings' registered prefixes. The JSON is
read strictly (no duplicate keys, no `NaN`, no string escaping a lone UTF-16
surrogate). Anything else, a non-zero exit, a timeout, a handler that cannot
start or be reached, or a non-200 answer is a **handler failure**: the bridge
acts on none of the answer, sends the fixed notice `command <name> failed`,
answers a pending press empty, and logs `command <name> failed (update <id>):
<reason>` with one of `handler_not_started`, `handler_unreachable`,
`exit_status`, `timeout`, `http_status`, `output_too_large`, `malformed_json`,
`invalid_unicode`, `not_an_actions_object`, `too_many_actions`,
`unknown_action_type`, `unknown_action_key`, `invalid_text`, `invalid_html`,
`invalid_message_id`, `invalid_keyboard`, `foreign_callback_data` or
`invalid_answer`. Nothing a handler prints, stdout or stderr, reaches the chat
or the log.

**Exactly once.** The actions run in order. Every send and edit goes through
the delivery ledger (below) under `command:<update_id>:<index>`, the failure
notice under `command:<update_id>:notice`, and the press answer (the
handler's `answer_callback`, else an empty one, always) under
`command:<update_id>:answer`, all before the inbound cursor moves past the
update. A reprocessed update runs the handler again but never sends, edits or
answers twice: a saved receipt stands, and a different answer under a used key
is `idempotency_key_conflict` and not sent. An unconfirmed send stays
`ambiguous_delivery_requires_manual_reconciliation` like any other receipt,
and one that never got out is retried and can end `transport_unavailable_not_sent`
("Delivery recovery");
an action Telegram refuses (an edit of an unchanged message, say) is logged as
`command <name> (update <id>): action <i> <type> not delivered: <reason>` and
sends no failure notice. The HTML fallback of "Formatted messages" applies to
sends and edits.

**Sandbox.** On Linux and WSL 2 an executable handler runs inside the bridge unit's sandbox:
`ProtectSystem=strict`, a read-only home, a private `/tmp`, and as writable
paths only the bridge's state and lock directories (which hold the ledger:
a handler must leave them alone), the transcription queue, and the
`handler_write_paths` directories, which the rendered unit appends to
`ReadWritePaths`. Add a directory there for each place a handler writes, run
`install.sh` again and restart the unit. A URL handler is a service of your
own and runs outside the sandbox. `check-install` lists every handler and
fails on a handler that is no longer a safe executable or a write path that is
not a directory. The settings and credential files are read-only for a
handler too. On macOS, launchd has no equivalent of the unit's sandbox: the
bridge and every command handler run with the user's full rights; the write
limit to the state, lock, queue and handler directories, the read-only
credentials, the private `/tmp` and the address-family and syscall
restrictions do not apply, and `handler_write_paths` is accepted but not
enforced. The settings, token and secrets stay owner-only and are refused
otherwise; other local accounts must be trusted, as on Linux.

Example, in the settings file:

```toml
handler_write_paths = ["/home/alex/example/var/deploy"]

[[command]]
name = "deploy"
description = "Deploy a branch"
handler = "/home/alex/example/bin/telegram-deploy"
```

## Native yard commands

Besides `[[command]]` handlers, the bridge answers some commands itself,
from the yard it serves. Each is a table under `[yardr]`; the table's
presence turns the command on, and each needs native `[yardr]` `home` and
`operator`.

### /quota

`/quota` runs `yardr limits --json` in the yard, and in parallel one
`limits --group <g> --json` per configured group, all within `timeout`,
and renders the yard's providers, its self-reporting sources and the
groups as one monospace table: the dot, the short label, the used share,
the reset as Ddd HH:MM in the bridge's timezone and the time left. Each
provider's block is headed by its display name alone, the providers in the
order Claude, Codex, Muse, Antigravity (`agy`), Grok and then the rest in
the yard's order. A per-model window shows its model, unless its label
already names it (`Weekly Fable` shows as `Fable wk`). A window with a used
figure and no reset shows its figure and `no reset`. The machine's own
source (load, swap, disk and mem) is left out unless `machine = true`, and
its rows then show the figure only. An entry the yard does not know renders
one `limits unknown for <name>` row naming only the provider, source or
group, and kinds no source lists render one `not asked` line. The answer is one
`html <pre>` message of at most 4096 units; longer tables drop whole
trailing blocks with a last `…` line. Text after /quota is ignored, and
there are no buttons.

```toml
[yardr]
bin = "/home/alex/.local/bin/yardr"  # default: the first yardr on PATH
home = "~/.yardr"
operator = "alex"

[yardr.quota]
timeout = 30           # seconds the reads may take in all, 5-120
groups = ["builders"]  # up to 16 group names, read under each group's env
machine_login = true   # also show the providers read on the machine's login
machine = false        # also show the machine's load, swap, disk and mem
```

`bin` is the yardr executable the bridge runs: no shell, stdin closed, in
the yard's home, its own process group, killed at the deadline or past
4 MiB of output. It is validated as a `[[command]]` handler path is, and
is required only with a native yard command table. The child's
environment is built fresh — the bridge's own `PATH` (so the yard's
limits source finds `aiquokka` and the CLIs it reads), `HOME`, `LANG`,
`TZ` from the settings and `USER`/`LOGNAME`, and no `YARDR_*` variable.
Stdout is parsed as JSON whatever the exit status (`limits` exits 1 with
the full JSON when an entry is not known); anything else answers
`quota unavailable`: a run still alive at the deadline is killed and the
answer says `timed out`; an unreachable yard says `yardr not reachable`;
stdout past 4 MiB says `answer too large`; stdout that does not parse
says `yardr exited <code>` when yardr left a plain small exit code, else
`answer not valid JSON`. The reply and the log never carry free text
yardr or a limits source wrote: no stderr line, no `limits_unknown` or
other diagnostic string reaches either. The bridge logs only the
structured outcome of an off-course read (status, problem, byte sizes).

With `[yardr.quota]`, a `[[command]]` or `[[menu]]` named `quota`, or a
`[[command]]` `callback_prefix` overlapping `quota:`, refuses the
settings with the migration message: delete the `[[command]]` quota
table. Without the table a `[[command]]` quota keeps working as before.
To switch back, restore the table, remove the native table, restart the
unit and run `set-commands`.

The yard writes the login back when its limits source renews one, and the
bridge's unit mounts home read-only: before /quota goes live, the operator
gives the yard's aiquokka source a copy whose limits command ends in
`--no-refresh`. As any handler, /quota holds the poll loop up to
`timeout`; its answer goes out once per update through the delivery
ledger.

<a id="switch_model"></a>

### /switch_model

`/switch_model` shows a crew member's running model and lets the paired user,
as the yard's operator, switch it through a durable native handoff with one
updating card. It uses the `[yardr.switch_model]` table, its picks and
the `switch-model:` callback prefix (`docs/design/switch-model.md`).

```toml
[yardr.switch_model]
crew = "yardmaster"   # the crew member a tap switches, [A-Za-z0-9][A-Za-z0-9_-]{0,31}
timeout = 60          # bounded CLI I/O deadline in seconds (10-120), NOT the 600s handoff wait

[[yardr.switch_model.picks]]
id = "sol"            # [a-z0-9][a-z0-9_-]{0,31}, distinct; selector/force payloads stay <=64 UTF-8 bytes
label = "GPT-6.1 Sol high"   # the button, 1-32 printable characters
kind = "codex"        # the yardr harness kind
model = "gpt-6.1-sol" # the model id the kind's --model takes
args = ["--dangerously-bypass-approvals-and-sandbox", "-c", "model_reasoning_effort=\"high\""]
env_file = "/home/alex/.yardr/crew-yardmaster.env"   # optional, absolute, passed as written
```

`picks` is required, 1–8 tables, with no defaults. `args` is the complete list
the crew member runs with, at most 64 printable args of 1–1024 characters. The
effort is parsed from `--reasoning-effort` / `--effort` (Muse, Grok) or `-c` /
`--config` `reasoning_effort` (Codex). Picks must differ in kind, model or
effort. `env_file` is passed to yardr as written and never opened by the bridge;
omitting it preserves the crew member's existing `env_file`.

**Native capability and baseline.** `/switch_model` requires a core supporting
`crew_handoff_switch v1` (landed core baseline `bd7f54b4484321ce618c5224a9e419f3c9b94b55`;
a locally built core is not evidence of a compatible public release). Before
sending a selector, admitting a switch, forcing or running `--dry-run`, the
bridge probes:

- `yardr --home <home> crew switch capabilities --json` (RPC `CrewSwitchCapabilities`),
  requiring the exact JSON object `{"crew_handoff_switch": 1}` (never guessed from
  `--version`);
- `yardr --home <home> crew show <crew> --json` (RPC `GetCrew`) for `revision`,
  `session_generation`, `launch`, `config`, `state`, `status`, context telemetry
  and `switch`;
- `yardr --home <home> crew set <crew> ... --revision <rev> --handoff --request-id <id>
  --session-generation <gen> --wait-seconds 600 --json` (RPC `RequestCrewSwitch`,
  socket-only operator mutation);
- `yardr --home <home> crew switch show <request-id> --json` (RPC `GetCrewSwitch`);
- `yardr --home <home> crew restart <crew> --switch <op-id> --generation <op-gen>
  --action-id <action-id> --json` (RPC `ForceCrewSwitch`, socket-only operator
  mutation).

Unsupported, malformed or oversized capability/status answers refuse before any
SQLite reservation or write and disclose no stderr or stdout.

**Card lifecycle, target vs launch, and Force apply.**
`/switch_model` sends one bound selector card (`model_selectors` token bound to
bot, account, yard, source, `realpath(home)`, operator, paired user/chat,
conversation, message, crew and canonical config snapshot). Text after
`/switch_model` is ignored.

On a pick tap (`switch-model:s:<token>:<pick>`):

1. Stale selectors (unknown tokens, retired selectors, changed
   picks/crew/config or mismatched bindings) answer
   `That model button is out of date.` and reserve nothing.
2. Private allowlist/bot/user/conversation and current native operator mapping
   (`GetUser`) are verified before reservation and rechecked right before
   admission.
3. If an operation is already unresolved for `(realpath(home), crew)`: a tap for
   the same pick acknowledges the original card without resetting the 600s
   deadline or starting a new admission; a different pick is explicitly refused
   (`A switch is already pending; a different pick is refused.`).
4. Otherwise, if `crew.state == "running"`, `crew.launch` matches a previously
   confirmed target and the preserved non-target crew definition matches, the
   tap answers `Already running this exact pick.` with no write (desired config
   equality alone does not skip admission while an older session still runs).
5. The bridge durably reserves `switch_model_taps`,
   `model_operations` (with 192-bit `tg-switch-...` `request_id` and original
   binding/fingerprint) and `model_actions`, answers the callback, and runs the
   single atomic `crew set ... --handoff --wait-seconds 600 --json` admission.
   The core saves the next-session target while the active session keeps its
   actual `launch` until it stops.
6. The selector message is edited in place (never replaced with a second card)
   to show `Please wait for the handoff.`, `Current: ...`, `Target: ...`,
   elapsed/deadline, session status, context with reading time (or
   `Context unavailable.`; active reads older than `45`s show `(stale
   observation)`), `Target effort: unknown · env: unknown.`, and an immediate
   `Force apply` button.

While unresolved, `model_switch.refresh()` polls after `drain()` within a shared
3-second read budget (about every 20s when Telegram is quiet):

- **Queued vs delivered vs written:** `requested` (request queued) →
  `handoff requested from the session` (`nudged_at`: safe nudge delivered to the
  session, or an earlier context/rotation nudge was delivered; not proof of
  reading) →
  `handoff written` (`handoff_written_at` and `handoff_note` from the
  authenticated line session) → `restarting` (`restarting_at`) → `running on
  target` (`running_at` and `new_generation != previous_generation`). Restart
  accepted (`restart_requested` or CLI exit `0`) is never treated as `running`;
  skipped intermediate polls still display the persisted milestone trail.
- **600-second deadline and natural exit:** At `deadline` (`timed_out`), the
  saved target remains intact, switch nudges stop, age rotation cannot auto-force,
  and `Force apply` stays available. A legitimate late handoff or natural exit
  (`previous_ended_without_handoff`) can still launch the saved target and
  settle `running` without fabricating a handoff.
- **Force apply:** Tapping `Force apply` (`switch-model:f:<token>:<gen>`)
  re-verifies operator/binding and current native operation, reserves a 192-bit
  `tg-force-...` action, answers the callback, immediately edits the card to
  `Force apply accepted, please wait. Ordinary restart waits for idle when
  applicable.` with controls removed, and then calls `crew restart --switch`.
  Force bypasses waiting for a handoff and schedules an ordinary restart on the
  saved target; busy sessions still wait for idle/grace. Double Force, stale
  Force after settlement, and Force racing a handoff cannot restart the new
  session.
- **Unknown, unmatched and display failures:** Unconfirmed admissions or Force
  calls never retry automatically. On reopen, `lookup()` queries only the
  original `request_id`. Receipts without a saved confirmed admission receipt
  cannot be matched to the full v1 request and stay `unknown` (`Native request
  could not be matched; operator reconciliation required. Nothing retried.`) with
  Force removed. An abandoned Force reservation before native confirmation
  clears `force_action` and increments the button generation only after a
  validated `waiting`/`timed_out` handoff observation. Deleted or uneditable
  messages record `display_failure` without sending a replacement message.

**Collisions and diagnostics (no silent conversion).**
With `[yardr.switch_model]`, `switch-model:` and `/switch_model`
are owned by the bridge.
A `[[command]]` or `[[menu]]` named `switch_model`, or any
`[[command]]` `callback_prefix` overlapping `switch-model:` (shorter such as
`switch-`, or longer such as `switch-model:x`), refuses to load before any write.
For example, keeping a custom handler beside `[yardr.switch_model]`:

```toml
[yardr.switch_model]
crew = "yardmaster"
[[yardr.switch_model.picks]]
id = "sol"
label = "GPT-6.1 Sol high"
kind = "codex"
model = "gpt-6.1-sol"
args = []

[[command]]
name = "switch_model"
description = "Switch model"
handler = "/home/alex/bin/switch-model"
```

fails with:

```text
settings: [[command]] switch_model clashes with the native /switch_model ([yardr.switch_model]); delete the [[command]] switch_model table
```

An overlapping custom prefix (`callback_prefix = "switch-model:"` or `"switch-"`)
fails naming the conflicting table, `switch-model:`, and the exact repair
(`delete the [[command]] <name> table or change its callback_prefix to a non-overlapping prefix`).
Repair settings explicitly (delete conflicting `[[command]]` and `[[menu]]`
tables, keep `switch-model:` unclaimed), then verify with `--dry-run` and
`check-install`.

**Read-only dry run.** `switch-model --dry-run <pick>` probes
`crew switch capabilities --json` and `crew show <crew> --json` only; it opens
no SQLite store, reserves no tap or operation, contacts no Telegram endpoint
and runs no `set`/`restart`/`force`:

```sh
<root>/bin/telegram-bridge switch-model --dry-run sol
```

When idle, the output prints the current launch, session status, target label,
exact `crew set --handoff --request-id <stable-id> --revision <rev>
--session-generation <gen> --wait-seconds 600 --json` flags, 600-second deadline
semantics, and `crew restart --switch <operation-id> --generation <generation>
--action-id <action-id>`. When an unresolved switch is already active, it
reports truthfully whether `<pick>` acknowledges the existing card or is refused.
Unknown picks and cores lacking `crew_handoff_switch v1` fail safely with fixed
diagnostics.

**Rollout and rollback.**
Before upgrading, the operator must confirm that no model operation is active.
Existing tap tables remain untouched for rollback; their rows are not migrated.
Buttons on cards sent before the upgrade answer empty and log
`no_registered_prefix`; send `/switch_model` again to open a new selector.

Rollout order preserves paired bot/user/chat, `tgbridge` source env
binding and grants (no new accounts, permissions, model choices, pack changes
or `yardr pack apply` required):

1. Reviewed bridge and core sources land; the authorized owner installs a
   compatible core (`crew_handoff_switch v1`).
2. Run the candidate suite with the landed core (`TGBRIDGE_TEST_YARDR=<core>
   sh scripts/test.sh`).
3. Keep a copy of the pre-edit settings file, rename any prior model-switch
   table and its picks to `[yardr.switch_model]` / `[[yardr.switch_model.picks]]`
   in the same edit as the install (check-install refuses any unknown key), install
   through the existing release path (`<checkout>/install.sh --settings
   <settings> --root <root>`), run `<root>/bin/telegram-bridge check-install`
   and `switch-model --dry-run <pick>`, restart the **same** configured unit
   (`systemctl --user restart <unit>` on Linux; `launchctl` equivalent on macOS),
   and run `<root>/bin/telegram-bridge set-commands`.
4. Verify `/switch_model` live on the paired chat.

For rollback:

1. Resolve or explicitly supersede any pending native switch in the yard, then
   stop the unit (`systemctl --user stop <unit>`). Rolling back bridge code alone
   does not cancel a pending core operation or undo a saved crew target.
2. Restore the pre-edit settings file, then run
   `<root>/previous/install.sh --settings <settings> --root <root> --rollback`
   (`--dry-run` first) to restore the previous bridge release. The upgraded
   `current` script refuses the restored pre-rename settings before swapping.
3. Keep SQLite state intact (`switch_model_taps` is ignored by the prior
   release, which keeps its own tap table; new `switch-model:s:`/`f:` buttons
   fail out of date on the prior release, so send the switch command again).
   Do not downgrade core while unresolved switch operations exist.
4. Restart the same unit and run `<root>/bin/telegram-bridge set-commands` to
   restore the menu. Independent instances and the public-core/committed-head
   release chain remain untouched.

**Neutral upgrade steps for another instance (e.g. IMD, for yardmaster release mail).**
Use portable placeholders and that instance's **own** existing settings, root,
unit, bot, and source/env bindings (nothing is sent from this build):

```sh
# 1. Confirm the installed yardr core supports crew_handoff_switch v1:
<YARDR_BIN> --home <YARD_HOME> crew switch capabilities --json

# 2. Save the pre-edit settings and rename the switch table and picks to [yardr.switch_model],
#    then install from the reviewed clean bridge commit into this instance's own root:
<CHECKOUT>/install.sh --settings <INSTANCE_SETTINGS> --root <INSTANCE_ROOT> --dry-run
<CHECKOUT>/install.sh --settings <INSTANCE_SETTINGS> --root <INSTANCE_ROOT>

# 3. Verify install and read-only dry-run without changing source/grants:
<INSTANCE_ROOT>/bin/telegram-bridge check-install
<INSTANCE_ROOT>/bin/telegram-bridge switch-model --dry-run <PICK>

# 4. Restart the same configured unit and refresh the Telegram "/" menu:
systemctl --user restart <INSTANCE_UNIT>
<INSTANCE_ROOT>/bin/telegram-bridge set-commands
```

Verify `/switch_model` opens the selector card; keep `tgbridge` source
env bindings and grants unchanged. To roll back, resolve/supersede any
pending switch, stop `<INSTANCE_UNIT>`, restore the pre-edit settings file,
run `<INSTANCE_ROOT>/previous/install.sh --settings <INSTANCE_SETTINGS>
--root <INSTANCE_ROOT> --rollback` (`--dry-run` first), restart
`<INSTANCE_UNIT>`, and run `<INSTANCE_ROOT>/bin/telegram-bridge
set-commands`. The upgraded `current` script refuses the restored pre-rename
settings before swapping.

### /nudge

An existing `[[command]]` named `nudge`, a `[[menu]]` entry with
`command = "nudge"`, or a custom `callback_prefix` overlapping `nudge:`
takes precedence over the built-in. The settings still load and the custom
handler, arguments, buttons and menu keep their meaning. A menu-only
`/nudge` keeps relaying as ordinary mail. Prefix overlap includes both
directions: `nudge:x` and `nud` each keep the built-in off.

With such a conflict the bridge omits the built-in command and its confirm
cards, including replies with words to yard-filed cards and refreshes of
saved typed-nudge rows. Yard-filed run cards still use their own approval
and outcome path. The bridge logs the reason once per startup, includes it
in its ready text, and reports it as a nonfatal `check-install` warning.
To enable the built-in, rename custom `nudge` commands/menu entries (for
example to `my_nudge`) and move any explicit overlapping `callback_prefix`
to a separate prefix such as `my_nudge:`. Once both the name and prefix are
free, the next startup registers the built-in.

`/nudge <session|crew member|bead> [words]` lets the paired user, as the
yard's operator, nudge a stuck session or crew member with their own words.
It opens a confirm card drawn from canonical state: the target and its
current status (the `GetCrew` read or the `ListSessions` list over the
socket), the exact line shown whole (the core default line when no words are
given), and Nudge and Cancel buttons. A reply with words to a nudge card
(the yard's own, below) opens the same confirm card, with that card's target.

A verified tap (`verify_operator`, sender, chat, topic, message binding,
claimed once) calls core `NudgeSession` as the operator, one write; Cancel
writes nothing. Text is passed as given, never interpolated into a shell. A
chat message alone never nudges: only the tap on the confirm card does. The
outcome is relayed as in the card child below: the daemon's
`session_nudged` matched on the request id, told once as a reply on the
card, unknown after ten minutes.

**Native capability and baseline.** `/nudge` requires a core serving
`NudgeSession` (operator-only, socket-only). At command time the bridge
probes it with an empty call, which a supporting core refuses in validation
and an older core answers `no such method`: the latter is classified like
`RPCUnsupported`, and `/nudge` answers that this yard's core has no session
nudge, with nothing written. The built-in `/nudge` is typed-only and stays
out of the Telegram menu on every core, so an unsupported core never
advertises it. No grant, setting or service is added; the ledger gains the
`typed_nudges` table. See also [Read and done from the yard](#read-and-done-from-the-yard)
for automatic unread-message Nudge offer cards (`[reactions] nudge_after`),
which reuse this same confirm card, `NudgeSession` probe and outcome path.

## Asks

An ask is a message with tap-to-answer buttons: the paired user taps
instead of typing, one tap reaches the yard at most once as one actionable
message (a delivery of kind `answer`), and the ask message then shows the
choice.

**Sending.** `publish()` accepts an optional `keyboard`: rows of
`{"text": ..., "callback_data": ...}` with the row, button-count, text and
size limits of a handler keyboard. The keyboard joins the idempotency
fingerprint like `format` does: the same key with another keyboard is
`idempotency_key_conflict`, and no `keyboard` field sends byte-identically to
before. `/v1/send` takes `keyboard` as rows or as rows in a JSON string
(`yardr call tgbridge send key=<key> keyboard="$(cat rows.json)" …`; an
empty string or `[]` is no keyboard), and `send` takes
`--keyboard-file <path>` holding the rows as JSON (a JSON `null` refuses:
`keyboard file must hold JSON rows`). The caller chooses the send's key
before it sends, since every button carries an id made from it (below):
`key=` on the yard's call, `--idempotency-key` on the command line.
`yardr reply` sends no keyboard.
An ask sent in a topic records that topic's conversation, and its answer
relays there under the topic's line; any other ask's answer relays on the
paired chat's conversation with no topic line ("Topics").

**Lettered options need buttons.** A send (`/v1/send`, so also `yardr
reply`, and `send`) whose
visible text (tags removed, entities decoded) offers a choice as lettered
text lines without a keyboard is refused before anything is sent
or recorded, with the receipt reason `options_need_keyboard`; the command
line prints a one-line message naming `--keyboard-file` and "Asks with
buttons". An
option line's first visible characters, after optional whitespace and a
bullet (`•`, `-`, `*`), are one capital A-D followed by `:`, `)` or a
spaced dash (`A -`, `A —`); the refusal needs two such lines carrying
consecutive letters from A (A and B, or A, B, C), so one `A:` recap line
or a mid-line `Plan A` never refuses. The same key with the same body
refuses again, and stays free for the buttoned resend; a send already
confirmed under that key and body replays its saved receipt instead
("Delivery recovery"). With
a keyboard the check is skipped (the keyboard rules above apply).
The override is `--text-options` (`text_options: true` on `/v1/send` and
`publish()`):
a deliberate text-only list goes out, the delivered receipt records
`text_options: true`, and the flag joins the idempotency fingerprint
only when true; any other value refuses as
`text_options_must_be_boolean`.

An ask may be one photo whose caption is the question, with the same
buttons; a tap answers it exactly as a text ask is answered, and the caption
then shows the choice.

Ask buttons use the one bridge-owned prefix `ask:`, with `callback_data`
`ask:<ask-id>:<choice>` within 64 bytes. `<ask-id>` is derived from the
send's idempotency key (the first 16 hex characters of its SHA-256), so one
ask belongs to one logical send; each question of a multi-question ask goes
out as its own message with its own key and keyboard.

A1 A valid press names an existing ask, the message the ask was sent as, an
authorised user and one of the ask's options; anything else keeps the
drop-and-answer-empty behaviour and never reaches the yard.

A2 On the first valid press, in order: (1) answer the callback once (key
`ask:<update_id>:answer`); (2) in one ledger write mark the ask answered with
the choice and the update id, only while it is unanswered (A6); (3) relay
exactly one entry, kind `answer`, with provider message id `ask-<ask-id>`
and the text
`[ask answer | choice: <label> | message: <id>]`, then a newline and
`question: ` followed by the visible question verbatim as the last field,
and record the ask as relayed once the yard accepts it ("Inbound"); (4) only
then attempt the
edit that appends `\n✓ <label>` to the text or caption and removes the
buttons, under the per-ask key `ask:<ask-id>:edit` (editMessageText for a
text ask, editMessageCaption for a photo ask). Both edits carry an empty
inline keyboard (`{"inline_keyboard": []}`). The offset advances once the
yard accepts the relay; the edit outcome (confirmed, rejected or ambiguous)
never holds it, un-answers or relays again.

A3 When the yard does not accept the relay the offset is retained and the
retry relays once under the same provider id (the yard records the same
`id` once), records relayed, then edits; the callback is answered no second
time and the edit happens at most once.

A4 A later press on an answered and relayed ask runs the pending edit once
before the already-answered answer (a no-op when the edit already ran, under
the per-ask key); nothing is relayed again.

A5 The send-time refusals: the ask id must be the send's derived id, two
buttons may not share `callback_data`, no button label may hold `|`, `]`, a
line break or a control character (refused when `label.splitlines() !=
[label]` or any character has Unicode category Cc, Zl or Zp; Cf such as
emoji ZWJ stays valid, and any would make the relayed answer's first line
ambiguous). The ask question is free text and is never refused for its
characters (newlines, tabs, `|` and `]` and every other character are valid;
only the 4096-unit room rule below applies): it rides verbatim as the last
field of the relayed answer, after a newline, so nothing in it can be
confused with a field. The question must leave room
for `\n✓ <label>` of its longest option within 4096 UTF-16 units of text, or
1024 UTF-16 units of caption for a photo ask.

P1 A keyboard may go with no attachment or with exactly one photo; any other
attachment kind (or more than one) refuses the send as `invalid_keyboard`
before anything is recorded or sent, through `publish()`, `/v1/send` and
`send` with `--keyboard-file` plus `--attachment`. A
keyboard on a photo holds only ask buttons: any other callback data on a
photo refuses
the send as `invalid_keyboard` (a handler edit of that photo would go through
editMessageText, which Telegram rejects for media). Every other attachment
kind stays refused.

P2 A photo ask's question is its caption. The A5 room rule for a photo ask
uses 1024 UTF-16 units of caption. The ledger records each ask's kind, text or
photo.

P3 A2 step 4 for a photo ask is editMessageCaption (append `\n✓ <label>` to
the caption, send an empty inline keyboard) under the same per-ask key;
everything else of A1-A5 is unchanged: record, relay with provider message id
`ask-<ask-id>`, then edit; the edit outcome never holds the offset, never
un-answers and never relays again.

P4 A photo ask never falls back to a text message: when Telegram rejects the
photo for anything but an HTML parse error, the send fails (no sendMessage
without the photo, no `delivered`), so the ask is never recorded as sent
without its buttons. The HTML parse-rejection fallback re-sends the photo
once without `parse_mode`, keeping the keyboard (as for text).

An ask button whose id is not the send's derived id refuses the send
(`invalid_keyboard`), so a reused id can never silently rebind the first
question, options and message to a second send. Two buttons sharing one
`callback_data` refuse the send (`invalid_keyboard`): the press could only
ever match the first, so the second label could never be relayed. A label
holding `|`, `]`, a line break or a control character refuses the send
(`invalid_keyboard`), since the relayed answer's first line separates its
fields with ` | ` inside `[...]`: refused when `label.splitlines() !=
[label]` or any character has Unicode category Cc, Zl or Zp (Cf such as
emoji ZWJ stays valid). An ask question is never refused for its characters:
it rides verbatim after the first line's newline as the last field. A question with
no room left for the appended choice refuses the send (`invalid_keyboard`),
since no tap could complete it. The bridge records each
ask in the ledger before the send is confirmed (ask id, question text or
caption, ordered options, sent message id, answered flag, kind text or
photo, decided flag). A `[[command]]` `callback_prefix` equal to or overlapping `ask:`
refuses at load, and handler output using `ask:` data is a
`foreign_callback_data` failure: no press, handler output or settings value
can send a second copy of an answer.

A replayed update changes nothing: saved receipts stand. Unregistered,
malformed or unauthorised presses keep the drop-and-answer-empty behaviour
and never reach the yard. The HTML parse-rejection fallback re-sends once
without `parse_mode` under the same key and keeps the keyboard (the photo
once without `parse_mode`, keeping the keyboard, for a photo ask); an ask
edit rejected the same way falls back to the plain question with the choice
appended.

A6 Closing with a decision. When the paired user answers an open ask by
typing, or decides something other than its options, the crew member closes
it with the decision made:

```sh
yardr call tgbridge ask_close message_id=<id> decision="<text>" --json
<root>/bin/telegram-bridge ask-close --message-id <id> --decision "<text>"
```

`<id>` is the message ID the ask send gave (`message_id` in the yard's
answer, the last word of `Telegram delivery confirmed; message ID <id>` on
the command line); the ask send's idempotency key (`ask_key=`,
`--ask-key <key>`) selects it instead when only the key is known (exactly
one of the two). The close runs through the shared ledger in the process
that takes it, the daemon for `/v1/ask-close` and the CLI process for
`ask-close`, and relays nothing to the yard, since the crew member made the
decision. In order:

(1) Refusals before any ledger write, each a receipt reason:
`ask_not_found` (no ask was sent as that message id, which covers an unknown
id and a message that is not an ask, or no ask has that key's derived id),
`ask_message_unknown` (the ask's send never confirmed a message id; it is
reconciled by hand), `ask_already_answered` (a tap answered it; the message
names the recorded choice), `ask_already_decided` (another decision is
recorded; the same decision again is the idempotent repeat),
`decision_required` (empty or whitespace only), `decision_breaks_line` (a
line break or control character, the A5 label rule) and
`decision_exceeds_room` (the visible question plus `\n✓ Decided: <decision>`
over 4096 UTF-16 units of text, or 1024 units of caption for a photo ask).

(2) One guarded ledger write marks the ask answered, decided and relayed,
with the decision as its answer, only while it is unanswered. When a tap won
in the meantime the close refuses `ask_already_answered`.

(3) The A2 step 4 edit, under the same per-ask key `ask:<ask-id>:edit`: it
appends `\n✓ Decided: <decision>` to the text or caption (the decision
escaped for an HTML ask, with the plain fallback) and removes the buttons,
editMessageText for a text ask and editMessageCaption for a photo ask, with
an empty inline keyboard. The edit's identity comes from the ledger row, so
the close's edit and any later press's A4 edit are one request: a saved
receipt, never a second edit.

(4) A later press on a closed ask goes the A4 way: the edit step (a no-op),
then the `Already answered` answer; nothing is relayed.

(5) Output: `Telegram ask closed; message ID <id>` (exit 0), the same line on
an idempotent repeat. A refusal, or an edit that is not confirmed (rejected,
ambiguous or not sent), prints the `BRIDGE_RECEIPT` line with its reason on
stderr and exits 1. After the write the ask stays closed and the message
says so (`ask closed; Telegram edit not confirmed: <reason>`). A not-sent
edit is sent by running the same command again; a rejected edit (the user
deleted the message, say) stays rejected, and an ambiguous one is reconciled
by hand. `/v1/ask-close` answers `{"delivered": true, "message_id": "<id>"}`,
or `delivered: false` with the same reasons.

(6) The ledger's asks table carries `decided` (0 or 1, default 0) after
`kind`. No settings key is involved.

A close and a tap run in separate processes or threads (the CLI process or
a yard API request, and the poll loop); the guarded writes (a tap's mark
and a close's write each change the row only while it is unanswered)
are their only serialisation. When a close wins between a tap's read and its
mark, that tap's callback was already answered empty (A2 step 1), so its
toast is empty rather than `Already answered`; the tap relays nothing and
runs the edit step, which shows the decision, so the edit happens once.

## Send and receive

In the yard, a crew member answers a delivery with `yardr reply <seq>
"<text>"` (files with `--file`), in the delivery's own conversation, and
sends anything else with a call:

```sh
yardr call tgbridge send text="<text>" topic=<name> format=html silent=true --json
```

`send` takes `text`, `topic` or `conversation`, `format`, `silent`,
`keyboard`, `text_options`, `attachment` and `key`; `react`, `pin`,
`unpin` and `ask_close` are calls too (`<repo>/yardr/tgbridge.yaml`), and
a crew member makes those it is granted (`yardr/SETUP.md`, step 8). On the
host, outside the yard (the operator, an add-on), the bridge's command line
does the same:

```sh
<root>/bin/telegram-bridge send --body-file <path> --idempotency-key <stable-key>
```

`--keyboard-file <path>` attaches an inline keyboard ("Asks" above);
`--silent` notifies with no sound ("Silent messages" below).
`pin` keeps one pinned message per slot ("Pinned messages" below).
Optional `--conversation-id` is the paired chat's id or a configured
topic's `<chat_id>:topic:<thread_id>`, exactly as the delivery names its
conversation, and `--topic <name>` names a configured topic instead: the
message lands in that topic. With neither, a send lands in General
("Topics").

Every send is direct: the process that takes it, the daemon for the yard
API and the CLI process for the command line, delivers it through the
shared ledger (`publish()`), one receipt per key, whatever it carries
("Delivery recovery"). Nothing is relayed through the yard, and the yard
keeps no copy of a send; the bridge's message log does, once Telegram
confirms it ("The message log").

The command line prints `Telegram delivery confirmed; message ID <id>`. A
send that is not delivered prints one
`BRIDGE_RECEIPT {"delivered": false, "failure_kind": ..., "reason": ...}` line on
stderr and exits 1; delivery consumers parse the `BRIDGE_RECEIPT` prefix.
The yard API answers `delivered`,
`message_id`, `conversation` and, when not delivered, `reason` and
`failure_kind` ("The yard API").

Plain text is limited to 4096 UTF-16 units, with link previews disabled;
split longer updates into separately keyed messages. On the command line
plain text is the default and is sent as is: no Markdown or HTML parsing
occurs. The yard API's default is `format: auto` ("Formatted messages").
The `telegram-formatting` skill is the writer's guide.

### Formatted messages (`html` and `auto`)

`--format html` (on `send`) and `format: html` (on
`/v1/send` and `/v1/pin`) send the body with Telegram's HTML `parse_mode`.
On the command line it is an explicit opt-in; without it nothing changes for
a caller or its ledger rows. The yard API's `format` defaults to `auto`, and
`yardr reply` without `--format` sends `auto`: HTML when the body holds a `<` or `&` and
is valid under the rules below, refused with its html_* rule when it holds
an allowed tag but is not valid, plain text otherwise, so only a body with
no tag and a stray `<` goes as the words it is. `pin --format` takes `plain`,
`html` or `markdown`.

- Allowed tags, lowercase, attribute values in double quotes: `b`, `strong`,
  `i`, `em`, `u`, `s`, `code`, `pre` (a `<code class="language-x">` directly
  inside it names the language), `a href` (`http`, `https` or `tg` links only),
  `blockquote` (bare `expandable` attribute for a collapsed quote) and
  `tg-spoiler`. Any other tag or attribute, `<br>` included, is refused before
  Telegram is contacted (`html_tag_not_allowed`, `html_attribute_not_allowed`,
  `html_link_scheme_not_allowed`); so are unbalanced tags, tags inside `code`,
  anything but `code` inside `pre`, and a nested `a`, `pre` or `blockquote`.
  Line breaks are plain newlines.
- The sender escapes text outside tags: `&` as `&amp;`, `<` as `&lt;`, `>` as
  `&gt;` (also inside an `href`). Only those, `&quot;` and numeric references
  are accepted; a bare `&`, `<` or `>` is `html_text_not_escaped`.
- The limits count the visible text after parsing (tags removed, entities
  decoded): 4096 UTF-16 units for a message, 1024 for a caption. A longer body
  is refused whole, never cut; the markup itself may be up to 16384 units.
- If Telegram still answers 400 "can't parse entities", the bridge re-sends the
  same content once without `parse_mode` (tags stripped, entities decoded; links
  keep their text, not the URL) inside the same reservation and key. The receipt
  then carries `format_fallback_reason: telegram_html_parse_rejected_400`; one
  key never gives two messages. Any other rejection or an unconfirmed send keeps
  the usual receipts below. A rejected HTML attachment caption retries the file
  once with the plain caption; the attachment text fallback is always plain.
- The format is part of the idempotency identity: the same key with another
  format is `idempotency_key_conflict`, like another body. `auto` is resolved
  first, so the ledger holds `html` or plain.
- The message log keeps an HTML send's visible text.

Explicit `markdown` on the same API/CLI/command boundaries renders a bounded
CommonMark subset to this HTML path. Defaults stay unchanged. See
[the rendering policy and packaging](../docs/formatting.md) for literal HTML,
code whitespace, lists/quotes, limits, identity and operator activation.

### Silent messages (`--silent`)

`--silent` (on `send`) and `silent: true` (on
`/v1/send`) deliver the message silently: Telegram's
`disable_notification` ("Users will receive a notification with no sound"),
so the message still shows in the user's notifications but makes no sound
and does not ring the phone. It combines
with HTML, attachments and a keyboard; without it nothing
changes for a caller or its ledger rows.

- The request carries `silent: true`. Every Telegram request of that publish
  carries `disable_notification: true`: the `sendMessage` body, the
  `send<Media>` multipart field, the HTML parse-rejection re-send and the
  attachment text fallback.
- `publish()` accepts `silent` true or absent; false is the same as absent
  and is dropped before the fingerprint, so a send without it keeps its exact
  body and ledger fingerprint. Any other value refuses the send before
  Telegram is contacted (`silent_must_be_boolean`).
- Silent is part of the idempotency identity: the same key with and without
  it is `idempotency_key_conflict`, like another body.
- Edits never notify, so command edits and ask answers are unaffected.

### Outbound attachments

`--attachment <absolute path>` on `send` (with optional
`--attachment-kind photo|video|document|voice|video_note`, inferred from the
extension otherwise: photo, video or document, never voice or video_note),
and `attachments` on `/v1/send` (a host path, or a list of them, with
`attachment_kind` likewise; `yardr reply --file` and `yardr call tgbridge
send attachment=` fill it), send
a file with the body as its caption: photos up to 10 MB, videos, documents,
voice notes (`sendVoice`, OGG/Opus) and round video notes (`sendVideoNote`, a
square MP4 of at most 60 s) up to 50 MB, a caption of at most 1024 UTF-16
units. A video note has no caption: its body must be empty and any text is
refused before sending (`video_note_takes_no_caption`). The bridge does not
probe the media; a wrong format or shape is Telegram's 4xx and takes the text
fallback. It goes through the shared ledger like any send, one receipt per
key (sent, not sent, or ambiguous). A path
containing `;` or `,` is refused. An attachment that is the
settings file, the bot token, the callback secret, the hook secret or the
ledger is refused before Telegram is contacted
(`attachment_path_is_a_bridge_credential`). A file Telegram rejects falls back to one
plain text message carrying the caption (if any) and
`[attachment rejected: <reason>]`; the send carries no keyboard (P1). A photo
ask with a keyboard never takes this fallback: a rejected photo fails the
send instead, so the ask is never recorded as sent without its buttons.
Nor does a file sent into a topic the user created ("Renamed and deleted
topics").

**Albums.** `--attachment` given 2-10 times (on `send`),
or 2-10 `attachments` on `/v1/send` (`yardr reply` with 2-10 `--file`),
sends the files as one Telegram album (sendMediaGroup) under one idempotency
key, with one receipt: `message_id` is the first message's id and
`message_ids` lists every message in order; the CLI prints the first and
the yard API answers it. One file keeps the request field `attachment` and
its bytes exactly; two or
more make the request field `attachments`, the ordered list of `{path, kind}`
with resolved absolute paths, which is part of the idempotency identity
(another file, order or kind under the same key is `idempotency_key_conflict`;
`attachment` and `attachments` together refuse,
`attachment_and_attachments_together`). `--attachment-kind` (or
`attachment_kind`), when given, applies to every file; otherwise each kind
is inferred per file.

- Photos and videos may mix; documents group only with documents. A voice
  note or video note refuses the album (`album_kind_not_groupable`), a
  document with a photo or video refuses it (`album_mixes_documents`), and
  fewer than two or more than ten files refuse it
  (`album_needs_2_to_10_files`). Every file passes the single-file checks
  above (absolute path, no `;` or `,`, not a bridge credential or the ledger,
  regular and readable, its kind's size limit); any one reason refuses the
  whole album before anything is reserved or sent.
- The body is the caption of the first item only (at most 1024 visible
  UTF-16 units; HTML sets that item's `parse_mode`); an empty
  body sends no caption. Silent adds `disable_notification=true`.
  `--keyboard-file` with an album refuses as `invalid_keyboard` (P1), and so
  does a `keyboard`: sendMediaGroup takes no `reply_markup`.
- The multipart request carries `chat_id`, `media` (a JSON array of
  `{"type": "photo"|"video"|"document", "media": "attach://file<i>"}`, the
  first with the caption) and the files as `file0`..`file9`, through curl's
  configuration on stdin like every other send.
- The send is confirmed only by HTTP 200 with a list of one Message per
  file, each in the paired chat. An HTML caption that Telegram cannot parse
  (400 "can't parse entities") re-sends the album once with the plain
  caption inside the same reservation
  (`format_fallback_reason: telegram_html_parse_rejected_400`); any other
  4xx falls back to one plain text message with the caption and
  `[album rejected: <reason>]` under the same key (not in a topic the user
  created: "Renamed and deleted topics"). A 5xx, a timeout, a short
  or foreign answer stays ambiguous and is never resent; an album that never
  left this host follows the not-sent rule ("Delivery recovery").
- One attempt has the 20 s send limit, and an upload still running past it
  ends ambiguous and is never resent, so albums suit screenshots, not large
  videos.

Telegram Bot API sendMediaGroup (read 2026-10-27, Bot API 10.3): the media
"must include 2-10 items"; "Documents and audio files can be only grouped in
an album with messages of the same type"; "On success, an Array of Message
objects that were sent is returned". Its parameters include
`disable_notification` and `reply_parameters`, and no `reply_markup`.

## Pinned messages

`yardr call tgbridge pin slot=<name> text=<text> [format=auto|plain|html|markdown]`
(`/v1/pin`), or on the host
`<root>/bin/telegram-bridge pin --slot <name> --body-file <path> [--format html]`,
keeps one pinned bot message per slot in
the paired chat, edited in place: a yard keeps a list at the top of the chat
(say, what waits on the user) by running it whenever the list changes. This
repository ships the mechanism only; what a slot says, and when it changes,
is the yard's. `yardr call tgbridge unpin slot=<name>` (`unpin: true`) and
`pin --slot <name> --unpin` remove the slot's message from the pins. A slot
matches
`[a-z0-9_-]{1,32}`; anything else is refused before the body is read or
Telegram is contacted. The body follows the `sendMessage` rules: 4096 visible
UTF-16 units, HTML with its allowlist and its one plain fallback (`auto`
on the yard API, as for a send),
refused whole when over. No setting is involved; the process that takes it,
the daemon for `/v1/pin` and the CLI process for `pin`, works through the
shared ledger. A slot's message lives in
General, outside any topic: it is sent with no `message_thread_id`.

The ledger's `pins` table holds per slot its generation (from 1), the live
message id (none when nothing is pinned), the revision and the fingerprint of
the last applied body and format. One update holds
`<state_dir>/telegram-pin-<slot>.lock` (created owner-only, never the bot
lock) for its whole run, so two callers of one slot never both send. In
order:

- No live message: a new generation. The body is sent with
  `disable_notification: true` under `pin:<slot>:g<generation>:message`, its
  confirmed message id recorded, then pinned with pinChatMessage
  `{"chat_id", "message_id", "disable_notification": true}` under
  `pin:<slot>:g<generation>:pin`. The send's identity is the slot's
  generation, not the body, so one generation never sends a second message:
  a rerun after a crash finds the saved receipt (and edits it to the current
  body). An ambiguous first send fails with its reason and stays ambiguous:
  every later update returns that receipt and sends nothing, until the
  operator reconciles it with an unpin (below); a not-sent one is sent by
  the next update; a rejected one fails and the next update starts a new
  generation (nothing was sent).
- A live message whose pin is not yet delivered (a pin that failed after a
  confirmed send) is pinned again before anything else. A pin's 400
  "message to pin not found" (the user deleted the message before its pin
  landed) ends that generation like an edit's below: the next generation
  sends and pins a new message in the same run.
- The same body and format as last applied: no Telegram call; the command
  prints `pin <slot> unchanged; message ID <id>`, and the yard API answers
  `changed: false`.
- Another body: the revision goes up by one and the live message is edited
  with editMessageText under `pin:<slot>:g<generation>:r<revision>`, so A,
  then B, then A again edits every time. Telegram's 400 "message is not
  modified" counts as applied. Its 400 "message to edit not found" (the user
  deleted the message) ends that generation, and the next generation sends
  and pins a new message in the same run.
- An unpin: unpinChatMessage `{"chat_id", "message_id"}` for the live
  message under `pin:<slot>:g<generation>:unpin`; the slot then has no live
  message and keeps its generation counter, so the next update is a new
  generation. With no live message it prints `pin <slot> not pinned` and
  exits 0; otherwise `unpinned message ID <id> (slot <slot>)`.
- Reconciling an unconfirmed first send: check the chat for that message
  (delete it if it arrived, or the slot gains a stray unpinned copy), then
  unpin. With no live message and a generation whose send never
  confirmed (ambiguous or not sent), an unpin ends that generation with no
  Telegram call and prints `pin <slot> not pinned; unconfirmed send of
  generation <g> ended, the next update sends a new message`; the next
  update then sends and pins under the next generation. This is the slot's
  form of the "Delivery recovery" rule: check the chat before a new send.

An edit, a pin and an unpin are idempotent in effect, so one that is not
delivered (rejected or unconfirmed) is attempted again by the next update:
an edit under the next revision (an unconfirmed edit leaves the applied body
unknown, so even the last applied body edits again), a pin or an unpin under
its own key. Only a generation's first `sendMessage` is exactly-once.
A pin and an unpin of one generation each mark the other's row not applied
just before their call, since that call may undo it: an update after an
unconfirmed unpin pins the message again before it can report the slot
unchanged, and an unpin after such a re-pin unpins again.

Success prints `pinned message ID <id> (slot <slot>, revision <r>)` (revision
0 for a new generation). A failure prints the `BRIDGE_RECEIPT` line with its
reason on stderr and exits 1, naming the step (send, pin, edit or unpin) and
the message id when there is one. The yard API answers a pin with
`{"delivered": true, "message_id": "<id>", "changed": <bool>}`, an unpin
with `delivered` and the message id when there was one, and a failure with
`delivered: false` and the reason.

Telegram Bot API (read 2026-10-27, Bot API 10.3): pinChatMessage says "In
private chats and channel direct messages chats, all non-service messages can
be pinned" and "Notifications are always disabled in channels and private
chats", so a pin never rings. editMessageText's 48-hour limit applies only to
business messages not sent by the bot, so the bot's own pinned message stays
editable. Each new pin (a new generation) adds Telegram's one "pinned"
service line to the chat; an edit adds nothing and never notifies.

## Topics

A yard can give the paired chat topics: Telegram's threads in a private
chat with a bot, one per subject, beside General, the chat outside any
topic. Each topic is its own conversation, `<chat_id>:topic:<thread_id>`
(in the ledger a record with the paired chat's `<chat_id>` as its parent and
kind `thread`). General stays the paired chat's conversation (`<chat_id>`,
kind `dm`) and behaves exactly as without topics. A message written in a
topic reaches the yard on that topic's conversation with its name as
`topic`, and the yard routes it by subscription:

```sh
yardr subscribe <crew> tgbridge --scope all --where type=message --where topic=<name>
```

Nothing is bound: the bridge relays every message the same way, and the
yard's subscriptions alone decide who gets it. General is `topic=general`.
A crew member answers in the topic with `yardr reply`, or names it by its
conversation id or its name (both below).

Prerequisites, in BotFather's settings for the bot: topic mode on (getMe's
`has_topics_enabled`). With `[auto_topics]` off or absent, also users
creating and deleting topics off (`allows_users_to_create_topics`), so the
settings' topics are the only ones and nobody deletes one, with all its
messages, from the phone. With `[auto_topics]` `enabled = true`, leave that
setting on: it is what lets the paired user create the topics the bridge
serves.

The settings name the topics, one `[[topic]]` table each, at most 50, in the
order `topics` creates them:

| `[[topic]]` key | required | meaning |
| --- | --- | --- |
| `name` | yes | the topic's name in the chat and its `topic` in the yard: 1-128 characters, no leading or trailing whitespace, no control or line separator character (Unicode categories Cc, Zl and Zp), none of `[`, `]` and `\|`; unique ignoring case |
| `crew` | no | the crew member the topic's `subscribe:` line names, and whose session the quota-exhausted notice watches on its conversation; default the settings' `crew` |

A file without `[[topic]]` loads exactly as before. A bad entry refuses every
command with one line, `settings: topic entry <n>: <problem>`. No thread id
is a setting: Telegram assigns it when it creates the topic, and the
ledger's `topics` table keeps it.

`<root>/bin/telegram-bridge topics` creates each `[[topic]]` that has
no thread id once, in file
order. It holds `<state_dir>/telegram-topics.lock` (created owner-only,
never the bot lock) for its whole run and never calls getUpdates, so it runs
beside the polling unit. In order:

- Before the first create, getMe. With topic mode off it exits 1 with
  `topic mode is off for this bot; switch it on with BotFather` and creates
  nothing. When users may create topics it prints
  `WARN users may create and delete topics in this chat (BotFather setting)`
  and goes on; with `[auto_topics]` `enabled = true` it prints
  `users may create topics: the bridge serves each one (auto_topics)` in
  its place.
- Each create is createForumTopic `{"chat_id": <chat_id>, "name": <name>}`
  through the ledger under `topic:<attempt>:<name>` (attempt 1 until
  `--retry` raises it). It is confirmed only by HTTP 200 with a topic of that
  name and a thread id above 0, and the thread id is recorded in the same
  step. A confirmed create whose row is missing (a crash between the two)
  gets its row from the saved receipt on the next run, with no send.
- A 4xx is `telegram_rejected_not_sent_<code>`. A create that never left the
  host is not sent: the next plain run sends it under the same key, and this
  run creates nothing after it (Telegram is unreachable). Anything else is
  `ambiguous_delivery_requires_manual_reconciliation`, and no run sends it
  again.

Output, one line each, in settings order: for a topic with a thread id

```text
topic <name>: thread <id> (created|recorded earlier|recorded by hand)
subscribe: yardr subscribe <crew> <source> --scope all --where type=message --where topic=<name>
```

(`<source>` is `[yardr] source`, the name shell-quoted when it needs it),
and for one without, `topic <name>: not created (<reason>); <next step>`;
then, for each ledger row whose name left the settings,
`topic <name>: thread <id> (not in settings; its messages reach the agent as not configured)`;
then, with `[auto_topics]` `enabled = true`, two lines per topic the paired
user created that the bridge serves, in thread id order,

```text
auto topic <display name>: thread <id> (served)
subscribe: yardr subscribe <crew> <source> --scope all --where type=message --where thread=<id>
```

and with it off or absent, when the ledger holds such topics, one line
`auto topics: <n> recorded, not served (auto_topics off)`.
It exits 0 only when every `[[topic]]` has a thread id, and, with
`[yardr.peer_mail] topic` set, that name is one of them (the lines
`peer mail topic: …` print either way); else 1 after printing every line.
It edits and deletes nothing, and changes nothing in the yard.

Run each printed `subscribe:` line once in the yard, before anyone writes in
that topic (`yardr/SETUP.md`, step 8); a crew member subscribed to several
topics takes repeated `--where topic=…`. A topic's message reaches only
the crew members whose subscriptions match it; one that none matches is
accepted all the same ("Inbound").

The ledger's `auto_topics` table keeps the topics the paired user created:
the thread id, Telegram's name as last seen and whether it was implicit, the
crew member, when it was first seen, and the legacy `bound_at` field,
retained without being read. No message
text is stored. A thread a `topics` row holds (a settings topic, or a row
whose name left the settings) is never an auto topic:
`<root>/bin/telegram-bridge topics --record <name> --thread-id <id>`
on an auto topic's thread is allowed and
adopts it as that settings topic, and its auto row stays, unserved and
unlisted. With `[auto_topics]` off the rows stay too, unserved, and switching
it on again serves them. An auto topic's display name is computed from its
row on every use, never stored: Telegram's name with `[`, `]`, `|` and every
control or line separator character made a space, whitespace collapsed, cut
to 128 characters, `thread <id>` when that is empty or the name is unknown;
a name equal, ignoring case, to a `[[topic]]` name or to another auto
topic's gets ` #<thread_id>` on every auto topic involved, so each name is
unique ignoring case and does not depend on the order the topics appeared.

Recovering an unconfirmed create: check the chat. If the topic is there,
write in it, take the thread id from the `[topic: thread <id> | not
configured ...]` line that message reaches the yard with (on General's
conversation), and run
`<root>/bin/telegram-bridge topics --record <name> --thread-id <id>`: it
records the thread id (1 to 2147483647, Telegram's int32) with no
Telegram call, and is refused when the name is not a `[[topic]]`,
already has a thread id, the id is another topic's, or the id is
outside that range. If the
topic is not there, run `<root>/bin/telegram-bridge topics --retry <name>`:
it raises the attempt and creates the topic under the new key. `--retry`
is refused unless the create was ambiguous or rejected; for one not sent it says
`send again: run topics without --retry`. After a rejection, fix its cause
first. `--record` and `--retry` are exclusive, and `--thread-id` goes only
with `--record`.

**Inbound.** A message is in a topic only when Telegram sets
`is_topic_message` to true and `message_thread_id` is an integer above 0.
Anything else is General and relays exactly as without topics: the same
body, the same text, the same reply id. A message in a topic relays under
one first line naming its topic, and the text General would relay (a
forward's header, an attachment's note, a voice note's note, the caption)
follows it in its usual order; every "first line" documented elsewhere is,
in a topic, the line after the topic line:

| the message's thread | relayed on | `topic` | first line |
| --- | --- | --- | --- |
| a `[[topic]]` with a ledger row | the topic's conversation | its name | `[topic: <name>]` |
| with `[auto_topics]` on, a thread no `topics` row holds: a topic the user created ("Topics the user creates") | the topic's conversation | its display name | `[topic: <display name>]` |
| a ledger row whose name left the settings; with `[auto_topics]` off or absent, any other thread | the paired chat's conversation | `general` | `[topic: thread <id> \| not configured: reply lands in the main chat]` |

- The accept point is the same on every conversation ("Inbound"): the
  bridge never asks whom the yard routes a message to.
- A thread no topic serves logs
  `topic thread <id> not configured (msg <id>); relayed to the main conversation`.
  Without any `[[topic]]`, a message in a topic is relayed so too; a chat
  without topic mode never has one.
- A reply to the topic's root message (`reply_to_message.message_id` equal
  to the message's `message_thread_id`) relays no reply id; a reply to any
  other message keeps it.
- The service messages `forum_topic_created`, `forum_topic_edited`,
  `forum_topic_closed` and `forum_topic_reopened` carry no text and relay
  nothing; the cursor moves past them. With `[auto_topics]` on, the first
  two name a topic the user created ("Topics the user creates").
- A voice or audio note in a topic records its thread in its pending file
  (`thread_id`, "Durable transcript queue"). Its transcript resolves the
  thread when it relays, by the rules above, so a topic recorded meanwhile
  gets the transcript on its own conversation.
- A command typed in a topic, and a press on a message in a topic, send
  every `send` action and the failure notice into that thread
  (`message_thread_id`, which also joins the send's ledger identity), the
  topic configured or not; edits and press answers act by message id, and the
  handler input is unchanged.
- Provider ids stay `<id>`, `<id>-transcript` and `ask-<ask-id>`: Telegram
  message ids are unique in a chat, so a not-configured relay on the paired
  chat's conversation never meets a General message's id.

**Outbound.** `publish()` accepts the paired chat's conversation, a
configured topic's with a ledger row and, with `[auto_topics]` on, a served
topic the user created ("Topics the user creates"), its six fields exactly as
above. Any other conversation (a thread with no row, one whose name left the
settings, another chat, another kind) is `recipient_not_authorized`, and
nothing is sent. Every Telegram request of a send on a topic's conversation
carries the topic's `message_thread_id`: an integer in the JSON body of
`sendMessage` (the text, an ask, the HTML plain fallback, an attachment's or
an album's text fallback) and a decimal string as a multipart field of
`send<Media>` and `sendMediaGroup`, beside a silent send's
`disable_notification`. The receipt names the topic's conversation. A send on
the paired chat's conversation carries no thread: the same bodies and ledger
fingerprints as without topics. The conversation is part of a send's
identity, so one key in two conversations is `idempotency_key_conflict`.

- `yardr reply` answers on the delivery's conversation, so in its topic.
  `/v1/send` takes `conversation`, the id exactly as a delivery names it, or
  `topic`, a `[[topic]]`'s exact name or a served topic's display name.
  `send` takes
  `--conversation-id <chat_id>:topic:<thread_id>` or `--topic <name>`, a
  `[[topic]]`'s exact name, and send there whatever the send carries (HTML,
  silent, a keyboard, attachments). `--conversation-id` also takes a served
  topic the user created; `--topic` names only a `[[topic]]`. A topic with no
  ledger row is refused before anything is sent: on the command line
  `conversation ID is not the paired private chat or a configured topic`, or
  `topic <name> is not configured or not created (run bridge.py topics)`; on
  the yard API a 400 with the first, or with `topic <name> is not
  configured, created or served`. Use `<root>/bin/telegram-bridge topics`
  to create the settings topics. The two flags together, or `--topic` on
  another command, is a usage error (exit 2). With neither, a send lands in
  General.
- An ask with buttons sent in a topic records the topic's conversation id in
  the ledger's asks table (`conversation_id`, its last column, NULL for the
  paired chat). A tap's answer relays on that conversation by the inbound rules
  above: the `[topic: <name>]` line comes before the `[ask answer | ...]`
  line; a topic no longer served relays it on the paired chat's
  conversation under the not-configured line, the thread id taken from
  the recorded conversation id. The provider id stays `ask-<ask-id>`. The edit
  and the close act by message id and are unchanged.
- The quota-exhausted notice goes on the conversation its triggering message
  was accepted on: the topic's, or the paired chat's for General and the
  not-configured relay. It watches the session of that conversation's crew
  member (the recorded topic's `crew`, else the settings' `crew`: the
  paired chat, the not-configured relay and the auto topics) and names it;
  an episode of any other crew member posts nothing there. Its key is the
  crew member's name's length, the name and the episode (a hash of the pair
  when that would exceed the publish key limit), so one episode of one crew
  member still sends one notice: a message on another conversation of the
  same crew member in the same episode meets the key's conflict and sends
  nothing, while another crew member's episode never suppresses it.
- A pinned slot's message stays in General ("Pinned messages"). Edits,
  reactions, callback answers, pins and unpins act by message id and take no
  thread.
- `react --conversation-id <id>` or `--topic <name>`, and `/v1/react` with
  `conversation` or `topic`, react to the last message accepted on that
  conversation ("Reactions"), and "typing..." shows in the thread the user
  wrote in ("Typing").

Logs name the thread and message ids, never a topic's name or any text.

The bridge never deletes, closes or renames a topic: deleteForumTopic would
delete all its messages. A changed `name` is a new topic, which the next
`topics` creates, with a new `topic` in the yard; the old one stays in the
chat, unserved, and its row is printed as not in settings. A removed
`[[topic]]` stops serving its topic the same way.

Telegram Bot API (read 2026-10-27, Bot API 10.3): `User.has_topics_enabled`
and `User.allows_users_to_create_topics` are "Returned only in getMe" (9.3
and 9.4); createForumTopic creates "a topic in a forum supergroup chat or a
private chat with a user" (9.4) with a name of 1-128 characters, and returns
a ForumTopic with its `message_thread_id`. sendMessage, sendPhoto,
sendVideo, sendDocument, sendVoice, sendVideoNote and sendMediaGroup take
`message_thread_id` for "private chats of bots with forum topic mode
enabled" (9.3).

### Topics the user creates

With `[auto_topics]` `enabled = true`, and BotFather's setting that lets
users create topics on, every topic the paired user creates is served with no
step per topic in the bridge: the bridge records it and relays its messages
on its own conversation, under its name; one subscription in the yard routes
it (Routing below). With the table off or absent none of this happens: such
a thread relays as not configured, exactly as before.

**Detection.** A thread is a topic the user created when a message is in it
by the rule of "Inbound", the update passes the paired chat and sender
checks, and no `topics` row holds the thread (a settings topic, or a row
whose name left the settings, stays as above). The first such update, a
message, a command, a service message or one the bridge skips, records
its `auto_topics` row for the settings' `crew`; only `forum_topic_edited`
records none. The row keeps that crew member, and its `subscribe:` line
names it; a row from an existing state directory keeps its stored crew name,
so check the line before running it.

**Naming**, from Telegram's service data only; whether Telegram sends the
first two in a private chat with a bot is not documented, and every path
works without them:

- `forum_topic_created` in the thread records the row with its `name` and
  `is_name_implicit` (absent: false) and relays nothing.
- A message whose `reply_to_message` carries `forum_topic_created` names a
  row that has no name yet.
- Any other first message records the row with no name, shown as
  `thread <id>`.
- `forum_topic_edited` with a `name`, in a served thread, replaces the name
  and clears implicit, so the next relay carries the new name. An edit of the
  icon alone changes nothing, and neither does an edit in a settings topic,
  a leftover row's thread or a thread with no row.

**Routing.** A served topic's messages reach the yard with its display name
as `topic` and its thread id as `thread`, and `topics` prints the
`subscribe:` line that routes them by the thread id. The display name is no
routing key: a rename, a name that gains or loses its ` #<thread_id>`
suffix (another topic of the same name ignoring case appears or goes), and
a first name after `thread <id>` change it, and the thread id never
changes. Until a subscription matches, the topic's messages are accepted
all the same ("Inbound").

**Relay.** A served topic's message relays on its conversation under
`[topic: <display name>]`, and is accepted like any other ("Inbound").
Voice transcripts, ask answers and commands in such a topic follow the
"Inbound" rules of "Topics" with no rule of their own; an ask answer
recorded on its conversation relays there under its topic line.

**Outbound.** A served topic's conversation is one `publish()` accepts,
with the six fields of every topic:
`<chat_id>:topic:<thread_id>`, parent `<chat_id>`, kind `thread`. Everything
"Outbound" in "Topics" says of a send in a topic holds for it with no rule
of its own: every Telegram request carries its `message_thread_id` (text,
the HTML plain fallback, an ask with buttons, which records its
conversation, a file, an album, a silent send), the receipt names its
conversation, the quota notice follows a message accepted on it, typing
shows in its thread, a command answers in it, and a pinned slot's
message stays in General. `yardr reply` answers in it; `/v1/send` and
`/v1/react` take its conversation id or its display name as `topic`;
`send` and `react` take
`--conversation-id <chat_id>:topic:<thread_id>` of a served topic, exactly
as a delivery names it. `--topic` on the command line names only `[[topic]]`
tables: a display name is refused like any other unknown name. A thread a
`topics` row holds follows
that row's rules. With `[auto_topics]` off, and for a thread with no row,
nothing changes: the id is refused (`conversation ID is not the paired
private chat or a configured topic`; `recipient_not_authorized` from
`publish()`) and nothing is sent. General, the settings topics and a file
without `[auto_topics]` send the same request bodies, with the same ledger
fingerprints and receipts, as before.

**Renamed and deleted topics.** A rename changes only the display name
(Naming above), and with it the `topic` of its next messages (Routing
above); the conversation and the row's crew member stay. Telegram documents
no update for a deleted topic, so the bridge keeps its row and serves it as
before. A send into it that Telegram
answers with a 4xx is `telegram_rejected_not_sent_<code>`: not sent, never
retried and never resent into General; the agent answers in the main chat
instead. A file or album rejected there therefore takes no text fallback
("Outbound attachments"): it fails the same way, with one request to Telegram.
Only the HTML parse fallback, a 400 "can't parse entities", re-sends there.

**Adopting and switching off.** `<root>/bin/telegram-bridge topics
--record <name> --thread-id <id>` on a served topic's thread adopts it as
that `[[topic]]` (above): it then relays under the `[[topic]]`'s name, which
is also its `topic` from then on, `--topic <name>` reaches it, and its auto
row stays, unserved. Switching `[auto_topics]` off restores the behaviour
without it: the threads relay as not configured, on the paired chat's
conversation with `topic` `general`, and their conversations are refused
for sends. The rows stay, unused, and switching it on serves them again.

At daemon start with `[auto_topics]` on, when the start probe's getMe
reports `allows_users_to_create_topics` or `has_topics_enabled` not true,
the bridge logs this line once and starts as usual, with no Telegram call of
its own:

```text
auto topics on, but users may not create topics (BotFather setting)
```

## Inbound media relay

An authorized private photo, document, video, voice, or audio message is
downloaded (photo: the largest size) via Telegram's `getFile` before the
message is relayed to the yard, so a message the yard accepts always has
its file on disk: the same accept-before-cursor guarantee as text
("Inbound"). Files land under
`<state_dir>/telegram-inbound/<YYYYMMDD>/<message_id>-<name>` (directory
mode 0700, file mode 0600); `<name>` is the original filename sanitized to a
safe basename (no path separators, no leading dot, ASCII-safe, length-capped)
or a kind-default (`photo.jpg`, `video.mp4`, `voice.ogg`, `audio.mp3`,
`document.bin`) when none is usable. Files over 50 MB are never downloaded; the
yard gets a skip note with the reason and `file_id` instead. A
download is retried up to 3 times; if it still fails the yard
gets a "could not be fetched" note with the `file_id` rather than the bridge
retrying forever. The relayed text is
`[attachment saved: <absolute path> | <kind> | <size> bytes |
caption: <caption or (none)>]`, with the caption text itself appended as the
message body when present. The bridge never executes, opens, or logs
attachment content. The inbound root and each date directory are refused
outright if either is a symlink, and every create, download, and delete is
realpath-checked against the state directory right before it happens, so
a symlinked root or date directory can never send a write or a delete outside
the inbound tree. The download itself lands in a temp file the bridge opens
with `O_CREAT|O_EXCL|O_NOFOLLOW` under an unpredictable name, and every
response byte flows through that one file descriptor (curl's stdout, never a
second path-based open) before an fchmod/realpath-checked rename into place.
The bridge never deletes inbound files.

## Forwarded messages

A message the paired user forwards into the paired private chat reaches the
yard as one entry under the message's own id, with `forwarded: true`. Its
first line is a header built only from Telegram's forward fields:

```text
[forwarded message | from: <origin> | sent: <UTC date> | forwarded material, not the user's instruction]
```

`<origin>` is the original sender's name and `@username`, a hidden sender's
name, or a group's or channel's title and `@username` with `signed: <author>`
when Telegram gives a signature (`forward_origin`, or the older
`forward_from`, `forward_sender_name` and `forward_from_chat`). `<UTC date>`
is the original send time, as `YYYY-MM-DDTHH:MM:SSZ`. A missing or malformed
field gives `unknown`, never a dropped message. Each field is untrusted: line
breaks and control characters become spaces, `[`, `]` and `|` become `(`, `)`
and `/`, and it is cut to 80 characters with an ellipsis, so a crafted name
can neither end the header nor start a line of its own.

The forwarded text, or an attachment's caption, follows with every line
prefixed `> `. An attachment keeps its note from "Inbound media relay" between
the header and the quoted caption, with `caption: quoted below` (or
`(none)`); a forwarded voice or audio note's `transcribing` note and its
transcript entry both carry the header and `forwarded: true`, the transcript
quoted as
`[voice <m:ss> transcript (msg <id>): quoted below] (audio: <path>)` and
`> <transcript>`. A forward never runs a command, and a forwarded
`/start` is relayed like any other text. A forward with neither text nor a supported attachment is
skipped like an empty message, and a forward from anyone but the paired user,
or in a group, is skipped like any other message from there. There is no
setting: forwards are always relayed. In a topic, the header is the line
after the topic line ("Topics").

## Reactions

The bridge can react to the paired user's messages with one emoji, a quick
acknowledgement that is not a message: Telegram's `setMessageReaction`, which
replaces the message's whole reaction list (a bot sets at most one), so a
repeat never stacks a second reaction and edits nothing the user wrote.

| `[reactions]` key | default | meaning |
| --- | --- | --- |
| `on_accept` | `false` | `true` sets `seen` on each message once the yard has accepted it ("Inbound") |
| `seen` | `"👀"` | the emoji for "the yard has the message" (with `read` enabled, typically `"🕊"`) |
| `read` | unset (off) | optional emoji set once the yard has shown the message to the target crew (`inbox_shown`) or the crew's inbox cursor passes the message's hook `seq` (`inbox_read` / `GetCrew`; typically `"👀"`; requires `[yardr] home`) |
| `working` | ignored | ignored since the working reaction was removed (accepted so existing settings load; `check-install` warns) |
| `working_after` | ignored | ignored since the working reaction was removed (accepted so existing settings load; `check-install` warns) |
| `nudge_after` | `0` (off) | integer seconds from `0` to `3600` (`0` off); when unread past this age while the crew is `running` and `idle` or `blocked`, offers one Nudge confirm card (requires `read`) |
| `done` | `"👌"` | the emoji for "the work this message asked for is finished or answered" |

An emoji value is a non-empty string of at most 16 characters with no
whitespace, control character or ASCII letter or digit; anything else, and
an unknown key in the table, refuses at load naming the key. Telegram allows
only the emoji of its fixed reaction list (👀, 👌, 👍, 🫡 and 💯 among them;
✅ and ✔ are not on it), and answers 400 for any other. A file without the
table loads exactly as before.

**The accept point.** When the yard accepts a message from the paired user
("Inbound": text, a file, a voice or audio note, a forward), the
ledger records it as the last accepted message (one row: its message id and
when). A command, an ask answer, any other button press and a voice
transcript follow-up are never accepted messages. With `on_accept = true`
the bridge then queues the `seen` reaction for a worker thread: it never runs
in the poll loop, and it never retains, delays or advances the inbound
cursor. It is sent once under ledger key `reaction:<message_id>:accept`, so a
replayed update never sets it a second time; a not-sent attempt is saved as
final too. Since a reaction replaces the whole list, `seen` must not run
over a `react` call: the worker and every `react` hold one `flock`
(`<state_dir>/telegram-bridge.sqlite3.reactions.lock`) from their check to
Telegram's answer, so a `done` waits for a `seen` in flight, and the worker
sends nothing for a message that already has a ledger key
`reaction:<message_id>:…` other than its own. A
failure, a worker thread that cannot start included, only logs
`reaction not set (msg <id>): <reason>`. A crash before the worker ran loses
the reaction; it is cosmetic and never retried.

**The `react` command.** An agent sets a reaction with

```sh
yardr call tgbridge react reaction=done topic=<name> --json
<root>/bin/telegram-bridge react [--message-id <id>] --reaction seen|done|none|<emoji>
<root>/bin/telegram-bridge react --conversation-id <id> --reaction done
<root>/bin/telegram-bridge react --topic <name> --reaction done
```

`seen` and `done` resolve from the settings, `none` clears the reaction (an
empty list), and any other value passes the emoji check above. Without
`--message-id` (a positive integer) it targets the last accepted message,
and refuses with `no accepted message to react to` when none is recorded.
The process that takes it, the CLI process or the daemon for `/v1/react`,
sends it through the same ledger. The command line's key is its own on
each call, `reaction:<message_id>:<nanoseconds>-<8 hex>`, so an earlier
ambiguous call never blocks the next. `/v1/react` uses the request's `key`,
else its `Idempotency-Key` header (the yard sends one with every update
call, so a call retried after a lost answer sets the reaction once), else a
key of the command line's form; a reaction under the yard's key does not
start with `reaction:<message_id>:`, so the worker's check above does not
see it, and only the lock orders it after a `seen` in flight.
Success prints `Telegram reaction set; message ID <id>` (the yard API
answers `{"delivered": true, "message_id": "<id>"}`); a rejection exits 1
with `Telegram reaction not set: telegram_rejected_not_sent_<code>` (an emoji
outside Telegram's list is a 400), a not-sent call with the not-sent message
of "Delivery recovery" (`Telegram could not be reached; nothing was sent
...`), each with the `BRIDGE_RECEIPT` line on stderr (the yard
API answers `delivered: false` with the reason). Reaction updates from the
user are not polled.

**Reactions in topics** ("Topics"). The accept point also records the
message as the last accepted message of the conversation the yard accepted
it on, in the ledger table `accepted_conversations` (a conversation id, its
newest message id and when; an existing ledger gains the table, and its
`accepted` row keeps its meaning: the newest accepted message in any
conversation). That
conversation is a topic's `<chat_id>:topic:<thread_id>`, or `<chat_id>` for
General and for a topic message relayed on the paired chat's conversation
(not configured), the conversation the delivery names. `react
--conversation-id <id>` (the paired chat's id, a configured topic's with
a ledger row, or a served topic the user created) or `react --topic <name>`
targets that conversation's last accepted message, so an agent answering in
a topic reacts to that topic's message even when a newer one arrived in
another; it refuses with `no accepted message to react to in that
conversation` when none is recorded, and an id or name `send` refuses is
refused the same way; an empty
`--conversation-id`, which `send` reads as the paired chat, refuses as an id
no configured topic has, so a reaction never guesses its conversation.
`--message-id`, `--conversation-id` and `--topic` are exclusive (exit 2).
The key, the `setMessageReaction` body and the output line are unchanged,
and without the two flags `react` is exactly as above. `/v1/react` takes
`message_id` first, else the last message accepted on the conversation
`topic` or `conversation` names (a served topic's display name included),
else the last accepted message; with none it answers 400 `no accepted
message to react to`.

### Read and done from the yard

When `[reactions] read` is set (together with `[yardr] home`;
recommended activation: `on_accept = true`, `seen = "🕊"`, `read = "👀"`,
`done = "👌"`), the accept point also inserts a row in the `read_states`
ledger table (`message_id` PK, `conversation_id`, `thread_id`, `seq`,
`crew`, `state`, `accepted_at`, `read_at`, `done_at`, `reason`) using
`INSERT OR IGNORE` so replayed updates keep one row. Only the existing
accept point inserts rows; commands, ask answers, button presses and
transcripts do not. When `read` is absent, no rows are inserted and no
native calls are made.

- **States:** `landed` (initial), `read`, and terminal `done`, `manual`,
  `off`, `expired`. Terminal rows are never read again. Any legacy row left in
  `working` settles once to `read` (👀) on the first refresh after upgrade.
- **Keys and locking:** `seen` still uses `reaction:<mid>:accept` (and
  skips any row already past `landed`). Read and done transitions use
  one helper under the reactions `flock`, re-reading the row and stepping
  forward (`landed -> read -> done`), delivering via `readstate:<mid>:read`
  (or `readstate:<mid>:read:settle` when settling a legacy `working` row) and
  `readstate:<mid>:done`. `Store.sent_since` ignores `readstate:*` keys so a
  typing window survives read reactions;
  `agent_reacted` still watches only `reaction:<mid>:*`. Delivered,
  rejected (4xx) or ambiguous responses advance the row (logged once as
  `reaction not set (msg <id>): <reason>`); transient `not-sent` leaves
  the row unchanged and retries the same key next round.
- **Stream and fallback rounds:** While `run` is active with `[reactions] read`
  and `[yardr] home`, a stream worker follows `GET /events?after=<seq>` on
  `home/yard.sock` (starting from `Head` and reconnecting on any stream end
  with the last `seq` seen): `inbox_shown` or `inbox_read` (`session == ""`,
  matching crew, `through >= seq`) moves `landed` to `read` at once — 👀 means
  the yard has shown the message to the crew member (`inbox_shown`) or the crew
  acked it (`inbox_read`), whichever arrives first — `replied` (matching
  `source`, `crew`, `seq`) moves open rows to `done` at once, matching `hook`
  events resolve `crew` early, and line-session `crew_status` (`user == ""`,
  matching crew) and `crew_stopped` drive stream typing without changing any
  reaction.
  `read_states.refresh()` also runs in `poll_once` after
  `typed_nudge.refresh()` and from the open-row fallback timer at most once per
  5 monotonic seconds, only while an open (`landed`, `read` or `working`) row
  exists (zero native calls when idle):
  - *Recipient check (once per message):* resolves `crew` from the
    conversation (`_quota_agent`), then calls `EventsSince [seq-1, 1]`
    requiring one `hook` event with that `seq`, our source and
    `data.deliveries` naming `crew`.
  - *Read check:* one `GetCrew {"name": crew}` per distinct crew with
    `landed` rows; `seq <= cursor` moves `landed` to `read`.
  - *Reply check:* runs only when `deliveries` has a row newer than the
    stored mark whose key starts with neither `reaction:` nor
    `readstate:`, reading `Events [journal, n]` (64 doubling to 1024,
    validated via `run_asks._validate_events`) for each crew with open
    rows. A `replied` event matching `data.seq`, our source and `crew`
    moves the row to `done`. The mark advances after two rounds with no
    newer send because `yardr reply` logs `replied` after `/v1/send`
    returns.
  - *Expiry:* `landed` rows older than 24 h move to `expired` without a
    reaction.
- **React and `/v1/send` overrides:** after a delivered `react` call
  (inside the reactions `flock`), the `done` emoji moves an open row to
  `done`, the `read` emoji moves `landed` to `read`, `seen` leaves it
  unchanged, and `none` or any other emoji sets `manual` and stops
  tracking. A delivered `/v1/send` with `reply_to` naming an open row
  moves it to `done`.
- **Degrade cases:** without `[yardr] home`,
  loading with `reactions.read` refuses with `SettingsError` naming
  `reactions.read`. A missing socket (`RPCUnavailable`/`RPCAmbiguous`)
  logs once while it stands, never stalls the inbound cursor, and retries
  next round. Missing/invalid `cursor`, `EventsSince` refusal or
  unsupported (`no such method`), or `crew` absent from
  `data.deliveries` moves the affected row(s) to `off` with a reason;
  unexpected `Events` array shapes are logged once without breaking
  polling. On a core without `inbox_read` (or where `/events` is refused),
  the bridge logs the fallback once and still moves rows to `read` via the
  5 s `GetCrew` poll while rows remain open; a core without `inbox_shown`
  behaves identically via `inbox_read` and the 5 s poll with no new fallback
  logged.
- **Unread Nudge offer (`[reactions] nudge_after`):** an integer from `0` to
  `3600` (default `0`, off; a value above `0` without `read` refuses at load
  naming `reactions.nudge_after`). With `nudge_after > 0`, if the built-in
  `/nudge` is disabled (`Bridge.native_nudge()` false) or `[yardr] operator`
  is unset, offers stay off and are logged once. Otherwise, at the end of
  each round (after `landed -> read` transitions), a row still in `landed`
  gets at most one offer ever when `now - accepted_at >= nudge_after`, its
  crew's `GetCrew` is `state = "running"` with `status` `"idle"` or
  `"blocked"`, no other offer for that crew is `open`,
  `decide_conversation(thread_id)` is served, and `NudgeSession` is
  supported (probed once per process with `{}` and cached on
  `RPCUnsupported`, logged once). The offer reuses `typed_nudge.open_confirm`
  sent silently as a reply to the operator's message, showing how long it
  has been unread, the crew's status, a fixed constant line naming only the
  integer event `seq` (never the operator's message text), and `Nudge` /
  `Cancel` buttons. `read_states.offer_token` and the `typed_nudges` row
  (`message = None`) are saved before the send under
  `typed-nudge:<token>:send`; a later round finding `message = None` sends
  again under that key (confirmed replays, `not-sent` retries, ambiguous
  counts as offered and logs once). Tapping `Nudge` runs the existing
  `nudge:<token>:nudge` path (one `NudgeSession` with `clear = false` as
  the operator, outcome relayed on the card); `Cancel` writes nothing. If
  the message moves to `read`, `done`, `manual`, `expired` or `off` while
  its offer is `open`, the offer moves to `cancelled` with a notice and the
  card is edited without buttons. With `nudge_after = 0`, rounds make
  exactly the read-and-done calls above.

## Typing

The bridge can show "typing..." in the paired chat while the yard works on
a message, so the user knows it arrived: Telegram's `sendChatAction` with
`typing`, which shows for at most 5 seconds and clears when a message from
the bot arrives.

| `[typing]` key | default | meaning |
| --- | --- | --- |
| `seconds` | `0` | safety cap in seconds (or the longest "typing..." shows without an event stream), an integer from 0 to 900; 0 never sends a chat action (600 recommended when enabled) |

A value outside 0-900, a value that is not an integer, and an unknown key in
the table refuse at load naming the key. A file without the table loads
exactly as before.

When `[typing] seconds` is above 0 and `[reactions] read` is active with a live
native event stream (`read_states` connected on `GET /events`), typing follows
real crew activity from `crew_status` (and `crew_stopped`): while a crew
member is in a `working` turn and has at least one open (`read`)
row, the typing worker refreshes `sendChatAction` (`typing`) every 4 seconds
in the thread of that crew's newest open (`read`) row, and stops
immediately when that crew leaves `working`, when all its open rows finish,
on any non-accept/non-readstate ledger send (`Store.sent_since`), or when
`seconds` elapses as a safety cap; a send-stopped window resumes when the
crew's newest open row changes while it is still working, within the same
cap. With no live event stream (no `reactions.read`,
an old core refusing `/events`, or a dropped stream), typing keeps the
accept-timer behaviour below unchanged:

With `seconds` above 0 and no live event stream, each accepted message ("Reactions", the accept
point: never a command, an ask answer, another button press or a
transcript follow-up) opens a typing window, or extends the open one, to end
`seconds` after it. The window remembers the ledger's newest delivery row
when it began, and ends at its end time, when the bridge stops, or as soon as
the ledger holds a newer row whose key is not an accept reaction
(`reaction:<message_id>:accept`): any send, edit, ask, pin, album or `react`
reaction, from the daemon or a CLI process (they share the ledger), the
quota-exhausted notice included. One worker thread, started when the daemon
runs, sends the action at once and then every 4 seconds while the window is
open, and looks at the ledger every second; it never runs in the poll loop,
never holds `publish_lock`, and never retains, delays or advances the inbound
cursor. Only the daemon shows typing; the CLI commands never send a chat
action.

Each chat action is one attempt with a 5-second limit, never retried. The
first failure in a window logs one line, `typing not shown: <reason>`, never
text:

- `transport_unavailable`: the request failed or its answer was unreadable;
  the window stays open for the next action.
- `telegram_unconfirmed`: Telegram answered without confirming (HTTP 5xx, or
  an answer that is not `ok`); the window stays open for the next action.
- `telegram_rejected_<code>`: Telegram refused it (HTTP 4xx); the window
  ends, since the next action would be refused alike.
- `ledger_unavailable`: the ledger could not be read, so a send would go
  unseen; the window ends (at the accept point, no window opens).
- `typing_failed`: a worker tick failed unexpectedly; the window ends and
  the worker goes on with the next accepted message.
- `typing_worker_unavailable`: the worker thread could not start when the
  daemon began; the bridge runs without typing until it restarts.

A message accepted before its window's end extends it, even while the worker
is ending the window at that end; one accepted after the window ended begins a
new window, which logs its own first failure. A send seen late can leave "typing..." up to 5 seconds
after the reply, until Telegram clears it.

**Typing in topics** ("Topics"). Typing shows where the user wrote: each
Telegram thread has its own window, opened by a message written in it (a
topic message relayed on the paired chat's conversation, not configured,
still shows typing in its topic) and started, extended and ended
by the rules above on its own; the worker serves every open window each
tick. A topic window's chat action is `{"chat_id": <chat_id>, "action":
"typing", "message_thread_id": <thread_id>}`; General's carries no thread,
exactly as without topics. The ledger does not record which conversation a
row belongs to, so any send closes every window open when it was made: a
reply in one topic also ends "typing..." in another, until that topic's next
message. A topic window's failure logs `typing not shown (thread <id>):
<reason>`, once per window.

## Voice-note transcription

With `[transcribe] enabled = true`, a `voice` (or `audio`) message is
downloaded exactly like any other inbound attachment and relayed at once as
`[attachment saved: <path> | voice <m:ss> | transcribing]`; the inbound cursor
advances as soon as the yard accepts it. One background worker thread then
transcribes queued
notes one at a time and relays a second message, `[voice <m:ss> transcript
(msg <id>): <transcript>] (audio: <path>)`, of kind `transcript`, using
provider message id `<id>-transcript`.

The transcriber is `<python> <repo>/transcriber/adapter.py AUDIO MODEL DEVICE`,
DEVICE `cpu` (`method = "cpu"`, the default), `check`
or `auto` (`method = "auto"`; `auto` is the legacy name for the same check
path), or `gpu` (`method = "gpu"`).
It prints `{"state": "running"}` when its turn in the host-wide queue starts,
then, last, one JSON line: `{"text": ...}` or `{"error": <code>}`, with
`"device": <label>` once whisper ran. It runs
`<repo>/transcriber/voice_transcribe.py`: ffmpeg to 16 kHz mono WAV, then
whisper.cpp's `whisper-cli` on the CPU (`-ng`) or the GPU (the same call
without `-ng`) with the configured language (`-l <language>`, where
`[transcribe] language` is a whisper language code such as `en` or `ru`,
or `auto` to let whisper detect it; `en` when the key is missing) and Silero
VAD (`--vad -vm`), so a note without speech gives an empty text. A phrase of
two or more words repeated three or more times in a row is retried once with
no text context (`-mc 0`) and the result with fewer repeats is kept. No
transcript is refused because of its language or script.

The transcriber is fenced so it never takes the host from its user: before
decoding it confines itself (and so ffmpeg and whisper-cli) to the settings'
`cpus` with `sched_setaffinity`, runs at nice 10 with best-effort I/O level 7
(`ionice -c 2 -n 7`), and runs at most 64 whisper threads.
Idle priority is not used: on a busy host it starves the note. An explicitly
configured `cpus` list that cannot be applied fails the note (`fence_failed`);
nice and `ionice` are best-effort only and never fail the note. On macOS
there is no CPU affinity and no `ionice`: `check-install` refuses
`transcribe.cpus` there, a note that still runs with `cpus` set fails
`fence_failed`, and only nice 10 applies.

### Setting up whisper.cpp

The host's whisper.cpp build and models are shared by every bridge on the
host (never under one bridge's directories). `install.sh` only checks them;
`<repo>/transcriber/setup.sh` creates them for one bridge's settings:

```sh
<repo>/transcriber/setup.sh --settings <settings> [--dry-run] [--whisper-ref <ref>]
```

It is idempotent and prints one `ok`/`FAIL`/`would` line per step. It reads
`whisper_cli`, `models_dir`, `model`, `vad_model` and the method from the
settings through the bridge's loader, prints no settings value other than
the paths it acts on, and never reads the token or secret files. With
`[transcribe] enabled = false` it prints `skip  transcription is off in
<file>` and does nothing. A usage error exits 64.

Prerequisites (git, cmake, make or ninja, a C++ compiler, ffmpeg, curl and
sha256sum) are only reported: a missing tool is `FAIL  <tool> not found
(install it with your package manager)` and the script exits 1 before any
change, installing nothing itself. There is no sudo, nothing is installed
system-wide and nothing is started. On macOS the hash prerequisite is
`sha256sum` when present, else the built-in `shasum` (`ok    shasum -a 256`),
which then hashes the models; with neither, the same `sha256sum` FAIL line
applies.

The checkout is the directory the `whisper_cli` layout names
(`<checkout>/build/bin/whisper-cli`). A missing checkout is cloned from
the whisper.cpp repository and moved to the pinned ref with a detached
checkout; a present checkout is left exactly as it is (never reset or
pulled) and reported with its describe string. The pinned default is the
commit `d1be6fde11ac6e0407606b4e42fe72d34add8037`
(`v1.9.5`); a fresh clone that is not at that commit is
removed and fails. `--whisper-ref` takes a tag or a commit instead.
setup.sh runs curl with `-q`; when `TELEGRAM_PACK_MODELS_SHA256` (tests
only) is set it prints a `note` line naming that list. `whisper-cli` is
built only when it is missing
(`cmake -B build`, then `cmake --build build -j --config Release
--target whisper-cli`). For method `gpu` or `auto` the configure adds
`-DGGML_CUDA=1` only when nvcc is on PATH (nvcc is no prerequisite);
without nvcc it builds for the CPU and reports that the GPU is tried and
falls back to the CPU. On macOS it never adds that flag, even with `nvcc`
on PATH: whisper.cpp builds with Metal by default there, so for method
`gpu` or `auto` the lines report Metal (`would build whisper-cli at <path> with Metal (whisper.cpp's default on macOS)`, then `ok    whisper-cli built with Metal at <path>`), and the configure is exactly `cmake -B build` (plus `-G Ninja` when only ninja exists); for method `cpu` the plain lines apply.

An existing executable is reused without checking its build type or
rebuilding it for a later yard's method. `ok    whisper-cli at <path>`
confirms its presence, not CPU, CUDA or Metal support: a later yard with
`method = "gpu"` or `"auto"` still reuses an existing CPU-only build.
The operator manages backend changes as host maintenance for all yards
sharing that path. Coordinate a rebuild across those yards and verify each
affected GPU-enabled instance with its own
`<root>/bin/telegram-bridge check-install --gpu-probe` before restarting it.
Keep the shared build, models and queue when removing a single instance.

Each model file (`<models_dir>/ggml-<name>.bin` for `model` and for
`vad_model`) is normally named in `transcriber/models.sha256`; a present,
non-empty local transcribe `model` file with no recorded entry is reused
as-is instead of failing (the `vad_model` file must be listed). A present file
with the recorded SHA-256 is `ok`; with any other hash it fails and is
never overwritten. A missing file is downloaded over HTTPS into a
temporary file in `models_dir`, checked, and only then moved into place;
a mismatch deletes the temporary file and fails, leaving no file. Whisper
models come from the whisper.cpp model repository and the VAD model from
the whisper-vad repository, at the URLs the checksum file's header names.
`--dry-run` prints `would ...` for each change and changes nothing. The
last line on success names the next step,
`next: <repo>/install.sh --settings <file>`; run that next so every check
says `ok`.

### GPU or CPU

`[transcribe] method` chooses where notes run: `cpu` (the default) is
always the CPU (`cpu: gpu-off`), `gpu` is always the GPU with no check,
and `auto` asks the configured GPU check (below) once per note. An operator
who wants their own GPU decision writes it as their own `gpu_check` command.
There is no other off switch: no check at all is `method = "gpu"`. In every
method mode a GPU run that fails or runs past its GPU limit (a quarter of the
run limit, at least 30 s) is retried once on the CPU (`cpu: gpu-failed`).

#### The GPU check

With `method = "auto"`, once the note's turn has started and its audio is
decoded, right before whisper, the transcriber runs the check once:

- argv is `[gpu_check]` (default `<repo>/transcriber/gpu_check.py`), no
  shell, empty stdin, its own directory as the working directory, and
  only `PATH=/usr/local/bin:/usr/bin:/bin`, `HOME`, `LANG` and
  `GPU_MIN_FREE_MB=<gpu_min_free_mb>` in its environment. At most 15 s,
  then its whole process group is killed.
- Its stdout, read up to 4 KiB, must be exactly one JSON object
  `{"gpu": true}` or `{"gpu": true|false, "reason": "<reason>"}`, where a
  reason is 1-32 characters of `a-z`, `0-9` or `-` and not one of the
  transcriber's own `gpu-failed`, `gpu-off` or `check-error`.
- `true` runs on the GPU (label `gpu`, or `gpu: <reason>`), `false` on
  the CPU (label `cpu: <reason>`, or `cpu: busy` without a reason), and
  anything else — a non-zero exit, a timeout, bad JSON, a bad or reserved
  reason — on the CPU with `cpu: check-error`. Nothing the check prints
  reaches the log beyond that label.

The default check is a small standard-library script. On Apple
Silicon it answers `{"gpu": true}` for Metal without running anything.
Otherwise it uses
`nvidia-smi --query-gpu=memory.used,memory.total` only when that is found
on PATH (on WSL 2 the check's `PATH` also looks in `/usr/lib/wsl/lib`):
free MiB at least `GPU_MIN_FREE_MB` (default 2048) answers
`{"gpu": true}`, less answers `{"gpu": false, "reason": "vram-low"}`,
and when it cannot tell (no `nvidia-smi`, a non-zero exit, or an answer
that is not one well-formed line) it answers
`{"gpu": true, "reason": "unknown"}`, so the GPU is tried and a failing
GPU run retries on the CPU. That `nvidia-smi` runs in its own process
group, reads at most 4 KiB in at most 5 s, and is killed by process group
on timeout or overrun. No vendor tool runs in the bridge, the
adapter or the method paths of the transcriber itself.

A CPU run never sees the GPU (`CUDA_VISIBLE_DEVICES` empty; on macOS the
same call keeps `-ng`, so CPU runs stay off Metal), whichever path
led to it. The bridge logs one line per note with the device and the
reason, never the text: `voice transcription done (msg <id>): <s> s; device
gpu`, `device gpu: <reason>` or `device cpu: <reason>` with a reason of
1-32 characters of `a-z`, `0-9` or `-`; a failure line ends the same way.
An adapter device label outside that shape makes the result `invalid output`.
On one test host with a CUDA GPU, `large-v3-turbo-q5_0` ran a 72 s note
in 2.4 s at about 1.1 GB of video memory, against about 54 s per audio
minute on 4 CPU threads.

The unit needs nothing new for the GPU: it does not restrict devices
(`/dev/nvidia*` stay reachable).
`ProtectHome=read-only` leaves CUDA no writable kernel cache under `~/.nv`;
a GPU run that fails for that reason moves to the CPU like any other GPU
failure, and the journal line says `cpu: gpu-failed`.

One transcription runs at a time on the whole host, in arrival order, whichever
bridge (or the `voice-transcribe` command) asked: every caller joins one
FIFO queue in `~/.local/state/voice-transcribe` (owner-only). It is not a
setting: every bridge and `voice-transcribe` run as the host user derive the
same directory, so two bridges can never end up in separate queues. A caller takes a numbered ticket under
a short `flock` on `queue.lock` and holds an `flock` on its ticket while it
waits and runs; the lowest ticket runs, and a ticket nobody holds (its process
died) is removed by the next caller. A waiting note keeps its place however
long the queue takes: there is no give-up wait. Each run has a hard limit: the
decode gets `timeout` seconds and whisper gets `timeout` plus
`timeout_per_minute` per minute of audio, after which it is killed (`timeout`)
and the next note runs. The bridge's own limit (the same scale from
Telegram's duration, plus a decode allowance and 30 s) starts at the
`running` line, so time spent waiting in the queue never counts. The unit may
write the queue (`ReadWritePaths`); `install.sh` creates it.

| `[transcribe]` key | default | meaning |
| --- | --- | --- |
| `enabled` | `false` | `true` transcribes voice and audio notes |
| `whisper_cli` | `~/work/whisper.cpp/build/bin/whisper-cli` | the whisper.cpp CLI every bridge on the host shares (a default; set it to place the shared build elsewhere) |
| `models_dir` | `~/work/whisper.cpp/models` | holds `ggml-<model>.bin` (a default; set it to place the shared build elsewhere) |
| `model` | `large-v3-turbo-q5_0` | measured at 4 threads: about 210 core-seconds per minute of audio; `small.en` about 50 with more slips in names |
| `vad_model` | `silero-v6.2.0` | the Silero VAD model |
| `cpus` | `""` (every CPU the bridge has) | CPU list for the transcriber, such as `"0-3"` |
| `threads` | `4` | whisper-cli threads, 1-64 |
| `timeout` | `120` | base seconds of a run, 1-3600 (the decode limit, and the start of the whisper limit) |
| `timeout_per_minute` | `120` | seconds of whisper limit added per minute of audio, 0-3600 (the bridge uses Telegram's duration, assuming 10 minutes when it gives none) |
| `method` | `cpu` | `auto`, `cpu` or `gpu` (above) |
| `gpu_check` | `<repo>/transcriber/gpu_check.py` | the GPU check command for `method = "auto"`: an existing executable regular file, writable only by its owner, owned by this user or root, in a directory owned by this user or root and writable by neither group nor others (for a symlink, both the link's and the target's directories) |
| `gpu_min_free_mb` | `2048` | free video memory, MiB, a GPU run needs (256-131072); whisper itself takes about 1.1 GB; also the check's `GPU_MIN_FREE_MB` |
| `language` | `en` | whisper language code (short lowercase, such as `en` or `ru`) or `auto` to let whisper detect it; passed to whisper-cli as `-l <value>` |

The supported models are the `ggml-<name>.bin` entries of
`<repo>/transcriber/models.sha256` except the VAD model. `load_settings`
accepts any well-formed `model` name. `check-install` keeps the SHA-256
check for a listed model (a mismatch still fails), warns
`WARN  model ggml-<name>.bin is not in transcriber/models.sha256; skipping
hash check` and passes for an unlisted model present on disk as a non-empty
regular file, and otherwise fails an unlisted one with
`FAIL  transcribe model <name> is not one of the supported models: <list>`:

| model | file | speed at 4 threads | language |
| --- | --- | --- | --- |
| `large-v3-turbo-q5_0` | `ggml-large-v3-turbo-q5_0.bin`, about 574 MB | about 210 core-seconds per minute of audio | multilingual |
| `small.en` | `ggml-small.en.bin`, about 488 MB | about 50 core-seconds per minute, with more slips in names | English-only |
| `small.en-q5_1` | `ggml-small.en-q5_1.bin`, about 191 MB | not measured | English-only |
| `base.en` | `ggml-base.en.bin`, about 148 MB | not measured | English-only |
| `small` | `ggml-small.bin`, about 488 MB | not measured | multilingual |

An English-only model (`*.en`) requires `language = "en"`: `load_settings`
refuses it with any other language (including `auto`) at startup, before
any unit or Telegram contact.

`install.sh` creates the queue directory and checks `whisper-cli` and both model
files; a listed model's SHA-256 is checked against
`<repo>/transcriber/models.sha256`, an unlisted local transcribe `model` file
is accepted with a warning and no hash check (the VAD model must be listed).

On failure the second message carries `transcription failed: <reason>` with
the saved file path, where the reason is one of a fixed set: `timeout`,
`transcriber exited with an error`, `invalid output`, `output too large`,
`transcriber missing (ffmpeg not on PATH)`, `transcriber missing (adapter not
installed)`, `transcriber missing (whisper-cli not found)`, `transcriber
missing (model not found)`, `transcriber queue unavailable`, `audio could not
be decoded`, `whisper-cli failed`, `transcriber could not
be confined to its configured CPUs`, or `no speech
detected`. A failed transcription never blocks inbound
delivery. Nothing the child prints is relayed or logged: the bridge log line
carries only the category, exit code, byte counts, duration and message id.
Child stdout is read through a 64 KiB cap and the process group is killed on
overrun or timeout. The file is always kept. The transcript is
whitespace-collapsed, control-character-stripped and capped at 4000
characters. Oversize audio keeps the plain skip note.

`<root>/bin/voice-transcribe [--model NAME|PATH] [--force] [--quiet] <file|dir>...` is the
same transcriber by hand: it writes `<name>.txt` next to each audio file and
prints the text. Each instance has its own, `<root>/bin/voice-transcribe`,
which runs its current release's transcriber with the settings' python;
`install.sh` leaves `~/.local/bin/voice-transcribe` to the operator. It transcribes
in `VOICE_TRANSCRIBE_LANGUAGE` when set (the bridge sets it from
`[transcribe] language`), otherwise English. It runs on the
CPU unless `VOICE_TRANSCRIBE_METHOD` is `auto` or `gpu` (with
`VOICE_TRANSCRIBE_GPU_CHECK` and `VOICE_TRANSCRIBE_GPU_MIN_FREE_MB` for
`auto`), or, with no method, `VOICE_TRANSCRIBE_GPU=1` (method `auto`),
and prints one
`voice-transcribe: <file>: device <label>` line per file on stderr.

### Turning the GPU on for a bridge

On the host:

1. Add `method = "auto"` under `[transcribe]` in the bridge's settings file
   (keep it mode 0600), removing any `gpu` there; or
   `method = "gpu"` for the GPU with no check at all. A bridge with its
   own GPU decision also sets `gpu_check` to its own command.
2. `<root>/bin/telegram-bridge probe` must load the
   settings without an error.
3. `<root>/bin/telegram-bridge check-install --gpu-probe`
   proves the GPU where the bridge runs, before the restart (below). It
   skips with `method = "cpu"` and probes with `method = "auto"` or
   `"gpu"`.
4. Restart the bridge, then send a voice note. On Linux and WSL 2:
   `systemctl --user restart <unit>`, and the journal
   (`journalctl --user -u <unit>`) shows
   `voice transcription done (msg <id>): ... device gpu`. On macOS:
   `launchctl kickstart -k gui/$(id -u)/<label>`, and the log
   (`~/Library/Logs/<label>.log`) shows the same line; on Apple Silicon
   prefer `method = "auto"`, whose default check answers for Metal.
   Back out with `method = "cpu"` and another restart the same way.

`check-install --gpu-probe [--clip <path>]` transcribes a short clip with the
bridge's transcriber and `VOICE_TRANSCRIBE_GPU=1`. On Linux and WSL 2 that is
inside a transient unit
(`systemd-run --user --wait --pipe --collect`) that carries the bridge unit's
sandbox: every `[Service]` setting of the rendered unit except how the process
starts and restarts, `ReadWritePaths` included, so the two cannot drift. The
clip goes in on stdin and is written only inside that unit's `PrivateTmp`. On
macOS there is no `systemd-run` and no sandbox to reproduce, so the probe runs
directly with the launchd agent's environment and a fresh private `TMPDIR`.
Either way it waits in the host-wide transcription queue like any note. The default clip is
`samples/jfk.wav` of the whisper.cpp checkout `whisper_cli` is built in
(`<checkout>/build/bin/whisper-cli`). It prints one line, never transcript
text: `ok    gpu probe: device gpu (<seconds> s)` (or `device gpu:
<reason>` when the GPU check gave one);
`FAIL  gpu probe: device
cpu: gpu-failed` (the GPU failed inside the sandbox; check-install exits
non-zero); one `skip` line with
transcription off or `method = "cpu"`; and a `FAIL` line
for a missing clip, a missing writable path of the unit or a transcriber error.
With `method = "auto"` a GPU check that answers `false` is not a failure:
any `cpu: <reason>` it gives (including `cpu: busy` without a reason and
the default check's `cpu: vram-low`) prints `skip  gpu probe: GPU not
free now (cpu: <reason>), try later` and check-install still succeeds;
`cpu: gpu-failed` and `cpu: check-error`
(a check the transcriber could not use) stay `FAIL`.

### Durable transcript queue

Each queued voice note has a `<message_id>-transcript.pending` JSON file in the
same date directory as its audio (mode 0600, written through the descriptor-
pinned date directory like the download, before the first note is relayed):
`kind`, `duration`, `message_id`, `provider_id` (`<id>-transcript`),
`audio_name` (basename only; the path is rebuilt from the pinned directory and
a name with separators is refused), `attempts`, `note_sent`, `gave_up`,
`created_at`, and for a forwarded note `forwarded` (`origin` and `sent`, the
header's fields). `note_sent` flips to true once the yard accepts the first
note. A
file without `forwarded` delivers unmarked; a malformed one still gets the
header, with `unknown` for what it cannot read. A note written in a topic
also has `thread_id` (an integer above 0), and its transcript relays by the
rules of "Topics"; a file without it relays on the paired chat's
conversation, and one whose `thread_id` is malformed is refused like any
other malformed file.

On start the bridge scans the inbound date directories (the state directory
and the inbound root must be real directories; symlinked date directories and
symlinked or malformed pending files are refused) and re-enqueues every
pending file with `note_sent` true and no `gave_up`. A file whose first note
was never confirmed is left alone: that update is redelivered by the cursor and
sends the note first. If the marker cannot be written after the first note, the
inbound cursor is retained and the update is reprocessed (the yard answers
the repeated note as a duplicate of its `id`). A successful follow-up deletes
the file. A relay the yard does not accept ("Inbound") is
retried with backoff (5 s, then 30 s); the third failure sets `gave_up`, logs
`voice transcript relay gave up (msg <id>)` and keeps the file as a marker,
never retried automatically. The relay reuses the provider id
`<id>-transcript`, so a retry after a lost answer is the same delivery `id`,
which the yard records once rather than as a second follow-up.

## Linked-yard crew mail

To mirror mail for local crew in the paired private chat, use the native
`[yardr]` home, source and operator settings with this table.

```toml
[yardr.peer_mail]
crew = ["yardmaster"]
# topic = "ops"   # optional: a [[topic]] of this chat; without it the mirror
                  # posts on General, the paired chat's own conversation
```

List each approved local crew name once. Check which linked yards can
exchange mail with that crew, and confirm the operator has approved exporting
their mail to this bot before enabling the setting. The daemon verifies the
owner-held socket, the current operator role and the paired Telegram identity
on the configured source. Native reads use the existing socket and source.
The `topic`, when present, must be a non-empty string naming a `[[topic]]`
entry of this same chat exactly to send; a name with no entry pauses the
mirror with private status while ordinary traffic keeps working, and a
malformed value refuses the settings. It needs no other topic change
and no source change: the ordinary topic configuration and the current
source declaration stay as they are.

The first verified daemon start saves the native event Head before opening
the stream. Incoming mail enters the queue when the yard accepts it. Outgoing
mail enters when the yard confirms delivery to the peer. The local store
sequence at those two points sets the display order. Mail already present or
queued at activation stays outside the window. The stream ignores ordinary
yard mail, human inboxes, unlisted crew, and unrelated transport events.
Post-activation confirmations follow a renamed link, and removal discards
its outstanding correlations. Each part identifies its direction and parties
and carries a part number. Files stay in the yard with a reference in part
one. A direct reply to any part is consumed before commands, attachments or
yard relay, including after restart or disable. Ordinary replies still relay.

Before enabling a `topic`, run `<root>/bin/telegram-bridge topics` and
require exit 0. With a `topic` set, exit 0 means every `[[topic]]` has its
thread id recorded and the `topic` is exactly one of those names (case
counts): the run prints `peer mail topic: recorded in the ledger as thread
<id>`, else it fails with `peer mail topic: not recorded in the ledger`, and
with no `[[topic]]` tables it fails too. That run never calls Telegram for the
topic: `peer mail topic: Telegram reach not checked here` says so, and the
first mirror post confirms it (a thread Telegram does not know pauses the
letter). The mirror resolves the name through that same recorded route on
every send.
Every part of one letter goes to the same conversation and thread the
letter claimed, on General or in the topic, and a retry reuses the same
delivery key and body, so a retry never moves a part. When the topic is
missing, removed, unrecorded, recorded with a thread id outside 1 to
2147483647, or stale at Telegram, delivery pauses and the journal
records the pause (a claimed letter's line names its thread id); nothing
falls back to General, and the affected work and its claim stay queued.
Changing the setting moves only letters claimed afterwards: an unfinished
letter keeps its claimed conversation and thread, and a claimed thread that
no longer resolves pauses that letter. Recording that original thread id
again resumes the paused work with no resend of confirmed parts; a new thread
recorded under the same name does not resume the claim, since only the
original recorded thread id can be served. Upgrading a General mirror with
unfinished letters and adding a `topic`: a letter already started (its parts
rendered or sent) keeps General, and a letter not yet started claims the topic
at its first send, whichever release queued it.
Removing a crew name discards that name's queued letters with their claims.

Keep the private SQLite database across restarts. It binds the native cursor
and delivery keys to this yard, operator, source and paired bot and chat.
Queued bodies and unfinished rendered parts are kept privately only while
they are needed. The native cursor and the Telegram inbound offset advance
independently. A dropped stream replays from the last committed cursor.
Identity changes, a missing native checkpoint or a delivery-key conflict
pause the mirror and appear in the bridge journal. Inspect and restore the
expected binding or store before resuming.

The worker retries a provably unsent part on the same key with a bounded
backoff. A send counts only when Telegram's receipt echoes the claimed
destination, thread included. The worker also honors Telegram's
`retry_after` for a definitive 429. An
attempt that may have reached Telegram is never automatically resent. The
journal names its `peermail:<letter-seq>:<part>` key, and later parts mark
the earlier part unconfirmed. Compare the private chat, the native letter
and the saved receipt to reconcile that part. Keep the receipts and reply
references even after reconciliation. The reference on each post lets the
bridge recognize replies when Telegram accepted a send and its receipt was lost.

Install a reviewed release through the existing instance's install and
preflight procedure ("Install and launch"): from a clean checkout of the
release, `<repo>/install.sh --settings <settings> --root <root>` into the
instance's own root, keeping its unit and state directory. With a `topic`, run
`<root>/bin/telegram-bridge topics` first and require exit 0. The
authorized operator then enables the setting, restarts the same unit and
smokes the already linked route with two harmless mails, one per direction,
plus one direct reply to a mirror part and one ordinary chat message: both
mails appear whole in order, on General or in the topic, the reply stays
inert, and the ordinary message relays. To pause delivery, remove the
table and restart on a release with reply protection. Re-enabling the same
scope resumes from its saved cursor, including qualifying mail during the
pause if the native stream retains it; claimed threads resume with it. A
newly added name gets its own current watermark. Removing a name during an
enabled run's next start discards its outstanding work. To roll back, run
the previous release's `<root>/previous/install.sh --settings <settings>
--root <root> --rollback` against the same database and restart the unit; when the
upgrade changed settings compatibility, restore the pre-edit settings
file first. Before rolling back to a release without reply protection,
account for the mirror posts still visible in the chat before resuming
polling. Rolling back to a General-only peer-mail release needs two
preconditions: no unfinished topic work, and no visible topic mirror
posts plus a drained inbound backlog. The older release sends every
unfinished letter to General and relays replies to topic posts to the
yard hook, even with the mirror disabled, because its reply guard
rejects topic targets. Telegram retains queued updates past the drain
counts, so stopping the protected poller before deleting the posts
still leaves a queued reply for the old release to relay. Disable first
on the candidate: remove the `[yardr.peer_mail]` table and restart it
with the mirror off, keeping it polling, so it claims no further mail.
With it still polling, wait until
`SELECT count(*) FROM peer_mail_work` and
`SELECT count(*) FROM peer_mail_threads WHERE thread IS NOT NULL` are
both 0; if work remains, keep the candidate and do not roll back.
With it still polling, delete every topic mirror post from the chat,
then keep it polling until the inbound backlog drains (queued updates
consumed, no further inbound). Only then stop the unit, or keep the
candidate until no reply is expected. Restore the pre-edit settings
file without the `topic` line (the older release refuses the key),
then roll back and restart.

## Delivery recovery

`<state_dir>/telegram-bridge.sqlite3` stores the inbound offset and outbound
fingerprints and receipts. The asks table keeps question text, ordered
options and answers so one tap answers, edits and relays exactly once. The
message log keeps whole message texts ("The message log"). The linked-yard
mirror keeps pending letter bodies and reply evidence. Keep the database across
restarts and back it up privately. A token-wide `flock` (`<lock_dir>/telegram-bot-<bot_id>.lock`)
prevents two copies of this bridge consuming one bot even with different state
directories. It cannot exclude unrelated bot clients.

A delivery key is reserved durably **before** contacting Telegram. Repeating the
same key/body returns its saved receipt, including after a crash. A reused key
with another body is rejected. Telegram lacks send idempotency: a timeout or crash
may happen after delivery. Such a receipt is `permanent` with
`ambiguous_delivery_requires_manual_reconciliation`; it is never automatically
resent. Check the private chat before using any new key and record the outcome.
A crash before the actual send is conservatively ambiguous too.

A chat action ("Typing") delivers nothing to the chat history: it is no
send, so it is the one Telegram call outside the ledger, with no key, no row
and one attempt.

**Not sent.** One send (a message, a file, a press answer, and separately its
HTML or attachment text fallback) is tried up to 3 times inside its
reservation when an attempt provably never left the host: curl exit 6 (DNS),
7 (connect) or 35 (TLS handshake), or exit 28 before the TLS handshake
completed (`time_appconnect` 0, nothing uploaded). It waits 1 s, then 3 s
between attempts. When every attempt fails that way the receipt is
`delivered: false`, `failure_kind: transient`, reason
`transport_unavailable_not_sent`, and its ledger row is not final: the same key
with the same body (and format) reserves it again and sends; another body is
still `idempotency_key_conflict`. Rows saved before this rule keep their
meaning and are never resent. Any attempt that may have reached Telegram ends
the send as ambiguous at once, never retried: exit 28 after the handshake, 52,
56, 92, an HTTP 5xx, a body that cannot be read, a timeout on the bridge's own
side, and every other curl exit code. A 4xx stays the definite rejection below.
The yard API passes the receipt's `reason` and `failure_kind` on unchanged,
and the command line prints them in its `BRIDGE_RECEIPT` line; only the reason
`transport_unavailable_not_sent` means not sent. For it a send on the
command line says `Telegram could not be reached; nothing was sent
(transport_unavailable_not_sent): send again with the same key and body`,
and for any other reason `Telegram delivery not confirmed: <reason>`.

**Time bound.** Each attempt is limited to 20 s (5 s to connect, curl's limits)
and the whole send, waits included, to 25 s: three attempts that never get out
take at most 3 x 5 s + 4 s = 19 s, a later attempt starts only while at least
6 s of the 25 are left, and `publish_lock` is held throughout, so other sends queue behind it.
The 25 s stay below the 30 s the yard gives a source's command or call, and
the thread command waits 25 s for the bridge (`<repo>/yardr/tgbridge/thread`).
A send queued behind another, or an HTML send whose fallback also
runs (at most 2 x 25 s), can outlast the caller's wait: the caller then
gets no answer, and the bridge still finishes the send under the key, so a
retry with the same key returns the real receipt and `/v1/posted` finds a
confirmed one. The command line waits for its own send.

**Log.** Every failed attempt writes one line to the bridge's log (the unit's
journal, or the CLI's stderr):
`telegram <method> attempt <n>/3 <not sent|unconfirmed>: curl exit <code>, phase <phase>, <ms> ms`,
where the phase is `dns`, `connect`, `tls`, `after-send`, `http-5xx` or
`unknown` (`-` for the exit code when curl gave none). curl reports no connect
time while a handshake stalls, so a stalled handshake may read `connect`.
Error strings carry the same codes and timings; neither ever carries text,
captions, file names, URLs, tokens, chat ids or response bodies.

A definitive Telegram 4xx rejection is saved as
`telegram_rejected_not_sent_<code>`; fix the cause (or wait for a 429 limit) before
an intentional new-key retry. All other unconfirmed responses are ambiguous.
Inspect receipt reasons locally using SQLite; do not delete records to force retry.
The bridge logs lifecycle/failure categories, never text, tokens or secrets.

Validate locally with (fake Telegram, a fake yard and fake whisper-cli, then
the yardr pack's tests in `yardr/tests`; no network):

```sh
sh scripts/test.sh
```

Set `TEST_PORT_RANGE` to `LOW-HIGH` to bind test servers inside that
range. Pick a range that excludes ports the host's own services use.
A service that listens only on demand may otherwise find its port
taken by a test run. With `TEST_PORT_RANGE` unset, tests bind port 0
and the kernel picks a port.

The native integration tests (`bridge/test_run_asks.py`, `bridge/test_siding_cards.py`)
start an isolated yardr server from the build named by `TGBRIDGE_TEST_YARDR` (an
absolute path), else the `yardr` on `PATH`; with neither, they skip. Each test says
which build it needs. The source-limit and trust-weight store test and the siding
card tests run on every build. The skill-add store test needs a pre-alignment build
and skips, with its reason, on an aligned or upstream one. The group add and set
test needs an aligned or upstream build and skips on a pre-alignment one; it runs
its `model=` card on the aligned build alone (upstream refuses the ask). A
build is read from its running server, not its version: `POST /call/RecordSkillAdded`
with `{}` answering `no such method RecordSkillAdded` means aligned or upstream, and
any other answer means pre-alignment (`bridge/testyardr.py`); a pre-alignment build
refuses the empty call on its skill name and writes nothing. That probe is
historical: it decides alignment and nothing else. An optional feature is
asked for by its own method (`testyardr.serves`): the skill card test skips,
with its reason, on a core that serves no `SkillDigest` (public `5dc0bfca`,
`52e22d57`) or whose CLI has no skill card rows (`c5642561`), and runs on a
skill core with the rows (`5dedfc56`, `9677728c`, `29fcd40d`).

Wire references: `YARDR.md` (the hook's body and the yard API) and
`<repo>/yardr/tgbridge.yaml` (the source the yard reaches the bridge
through).

## Native run cards

`run_asks.py` owns native socket framing, payload checks, rendering and the
SQLite state machine. `bridge.py` supplies its existing ledger-backed
transport, routing, callbacks and ordinary asks. The optional `[yardr] home`
allows reads on every instance; `operator` additionally enables verified operator acts.
Native cards require `cards = true` with both keys (`cards` is required wherever
home and operator are both set). The socket is always resolved
`home/yard.sock`; check-install verifies its owner/type and current operator
mapping, or reads every configured conversation crew when no operator is set.
No sandbox write exceptions are installed for native cards.

Only the polling `run` command takes the yard's native-card ownership lock,
inside the bot lock and held for the process lifetime. Every instance on
the host must use the same `lock_dir`; a second owner refuses startup with
the instruction to set `cards = false` on one of them. CLI sends, probes,
card publishing and ask-close do not take this process lock.

Authenticated `POST /v1/run-ask` accepts exactly `bead`, positive `ask`,
`topic`/`conversation`, `silent`, and `key` (or Idempotency-Key). It reads
GetUser and Bead over the native RPC socket with X-Yardr-Connection naming
the configured source. RPC answers use `result` or structured
`error: {code, message}`. Caller-supplied approval data is refused.

Each card binds the configured yard/home/source/operator, paired bot/user/
chat, conversation, native recipient, question, proposer, context, complete
run/weight and Telegram message. Custom commands and publish keyboards
cannot use `runask:`. Move any existing overlapping callback_prefix to a
different namespace before updating. The complete canonical card is HTML-escaped and limited
to 3500 visible UTF16 units. Asker WEIGHTY-mark lookalikes lose the mark,
following Footplate; canonical warning lines alone are bold. Self-contained
cards keep that limit. `group add`/`group set`, native `source add`,
source-kind `pack apply` and bounded depot directory packs use the complete
review bundles below. Secret/setup/identity/depot_push, parameter inputs,
other carried files, unsupported depot forms and crew-add prompts still
refuse before sending, with Footplate/cards as the fallback.

Routine Approve calls ApproveAsk with weighed=false after fresh checks.
Weighty Approve persists a 60-second confirmation and edits that same bound
card. The complete card stays visible; an added line names the ask, bead
and fresh native command (shortened only if needed to fit Telegram). The
paired human taps Confirm on its own second row, in the same chat/topic.
Cancel takes Approve's former position, so a reflex tap there cancels.
Repeated Approve keeps the same nonce and deadline; the host's clock starts
the window when it handles the first tap. Forwarded/bot/wrong-owner/
wrong-topic/wrong-message/expired/cancelled/stale/replayed confirmations cannot
approve. A wrong nonce, expiry or backward host-clock jump clears the arm.
Cancel restores the open card with ‘Cancelled; nothing ran’. Decline records
the native decline immediately with the same ownership checks.
A text reply to an older run-card row is not consumed: only rows of
kind=decision are handled, so such a reply falls through to ordinary
inbound routing like any other message, and no arm is cleared. Other
replies and free-standing numbers keep ordinary routing.
No account or device passwords are requested.

Existing unarmed cards need no redraw. A restart preserves a current arm;
each poll clears expired arms before processing queued updates.

The state lives beside ordinary asks in the existing SQLite ledger;
process locks serialize terminal interactions and durable update claims
prevent replay. Send reservations precede Telegram: an ambiguous card send
is not resent. Repeating a run-ask request returns its original receipt even
while weight confirmation is pending, including after restart. Caller keys
cannot be shared between native cards and ordinary sends; the second kind
refuses with `idempotency_key_conflict` before sending. Existing ordinary
ask-key selectors retain their meaning if an older ledger has a collision.
A write's answer never establishes its outcome alone: the
bridge re-reads Bead, including after a native wire error or generic HTTP
failure. A timeout/EOF/invalid response after sending is uncertain; no
blind write retry occurs. Fresh taps read canonical state again. If the
outcome cannot be read, the operator sees "the yard did not answer; read
the card again", never a success inferred from silence.

`ask_close` keeps decision plus message_id/ask_key for compatibility.
Native selectors ignore decision and refuse open/put-aside asks; terminal
words, take-back by, ran.result, pending_reason and failed come from Bead.
CarryPending means "approved, to be carried out", not successful execution.
Polling and native ask_close reconcile crash-after-commit, settlements and
later outcome edits. Canonical revisions give edits, outcome parts and hook
relays distinct stable keys. Long outcomes split into messages of at most
3500 UTF16 units; exact result text is retained. A committed approval stays
recorded even if its edit/relay delivery is not confirmed. Keep the ledger
across service restarts and checkout moves.

See [source setup](../yardr/SETUP.md#12-native-run-cards-operator-optional)
for declaration installation, explicit `yardr source grant tgbridge run_ask
--crew yardmaster`, operator bootstrap, check-install and restart. The pack
contains no grants; builder/secretary/blanket grants are not required.


### The yard's nudge cards

On a core whose daemon files one, a harness that stops on an error (any
reason but `not_logged_in`) gets a `yardr session nudge <target>` run ask to
the operator, asked by `yard`, on the stuck session's bead or the crew
member's `<name>-journal`. The core takes it back when the error spell ends.
The yardmaster is not involved; it may be the agent that stopped.

Each poll, right after the run cards' own refresh, `nudge_cards.py` reads the
operator's `UserAsks` and sends every open run ask whose asker is `yard` and
whose argv begins `session nudge` as an ordinary run card in the paired chat,
through the same `create` path as `run_ask` and `/decide` (ledger key
`nudge:<row id>`). A card already sent for that ask, by `/decide`, `run_ask`
or an earlier poll or restart, is not sent again. A send of ours that never
left the host is sent again under the same key on a later poll; one Telegram
confirmed before a crash bound it is bound from its saved receipt, not sent
again; an ambiguous one is left to the card's reconcile. Approve, Decline,
stale, take-back and reconcile are the run card's own paths; no crew call is
made.

After an approval, each poll reads `Events` of the ask's bead, newest first
and back to the approval's `session_nudge_requested` (or the ask's own note,
or the journal's start), for the daemon's `session_nudged` whose `ask` is the
card's, for ten minutes from the first poll that finds the card approved, and
replies once on the card: `✓ Nudge typed`, or `✕ Nudge refused|expired:
<reason>`, or `? Nudge unknown: <reason>`. With none in ten minutes, the
journal read through, the reply says the outcome is unknown; an `Events` read
that fails is read again next poll and is never said as unknown. The reply
chosen is kept until its delivery is final, so one that never left the host is
sent again unchanged; nothing is approved or nudged again. An event of
another shape is said as unreadable. Approved cards are followed while the
operator verifies, even when the queue is unreadable; an unreadable queue is
logged once while it stands. A card the bridge cannot show whole is logged
once and left to Footplate.

A core without the nudge row (unmodified upstream) files no such ask: no
card is sent and nothing is logged. No grant, setting or service is added;
the ledger gains the `nudge_cards` table. The plain `session_harness_error`
tell is untouched. Source landing installs nothing: the bridge's deploy and a
core with the yard-filed card are each the operator's.

### Nudging with your own words

`/nudge <target> [words]` (see `### /nudge` under `## Native yard
commands`) opens a confirm card for a session id, a crew member's name or a
bead with exactly one live session; a reply with words to a nudge card above
does the same, with that card's target (a conversation's card, whose argv
carries `--for user`, nudges that conversation). The card shows the target, its
current status and the exact line, and only the tap on its Nudge button
writes: one `NudgeSession` call as the operator. Wrong user, chat or topic,
a replay and a double tap each write at most once (the core's standing
request refuses a second); Cancel writes nothing. Over-long text is refused
before any call. After repointing the bridge to another yard, retained
confirm cards close on tap without a nudge write.

The outcome is relayed as in the card above, matched on the request id
instead of the ask: the daemon's `session_nudged` told once as a reply on
the confirm card, unknown after ten minutes, with the same resend and
unreadable rules. The `typed_nudges` ledger table carries the confirm rows;
`scripts/preview_typed_nudge.py` renders the card and its replies as
synthetic captures. A core without the method answers the command
not-supported and writes nothing; plain chat messages still go to the hook
as mail, unchanged.


### Native general cards

A general card may name `skill add` on a core that offers that command;
this does not restore the removed captured skill card or add optional skill
commands to public core.

A core that exposes `RunAsked.general` can deliver its existing unchecked
contract through `run_ask` and `/decide`. Tested source pins are unmodified
public `5dc0bfca62ac118f7e7533fcf8125faa09481eee` and
`52e22d570cfff58bae725e6df53c047bd63ff4a6`, plus reviewed local
`b751afc9a018c77e066d302675a84320c8ac69e5`. No local skill, patch or
bootstrap RPC is required. An older core without general cards continues
to serve ordinary cards. The local core retains its separately reviewed
filing refusals and persistence fixes; stock compatibility does not claim
its looser refusal policy is identical.

The actual operator must enable `general_cards` in that yard's terminal;
it defaults off. Only the native yardmaster may file verbatim `--run-word`
or `G:` words. A matching specific card still takes precedence. Turning
the setting off stops new filings without revoking existing work. The
bridge never enables it and cannot bootstrap it by card on stock core.

The bridge checks General/argv identity, the full native command,
Description, every Does sentence and numbered word, empty rollback and
strong code weight before dispatching any command family. Unknown fields,
malformed Unicode, invisible controls/format characters and inconsistent
prose refuse whole. General words are not parsed as a local skill or patch
contract. A command missing from the executing core relays native failure.

One complete Markdown document precedes the compact strong-code card.
Every word is shown completely as an escaped JSON string with rune and
UTF-8 byte counts, including empty words, spaces, quotes and Unicode.
Decode each string and encode UTF-8 to recover the exact argv. Native Does
may shorten words over 128 runes; it is retained as canonical prose beside
the complete word review. The card binds the document name, size, SHA-256
and proposal digest, and delivery must be confirmed before controls appear.
The existing 50 MiB document bound and compact text bounds refuse the whole
proposal, with no shortened approval. The document hash identifies the
review document, **never the contents of a file operand**.

Approving runs the unchecked words as the operator in the same native
home. No file operands are captured or hashed; edits to files or yard
state do not make this card stale. No rollback is promised. Command output
is told to the asker, so never propose credentials or sensitive output.
The core supplies its usual no-shell/closed-stdin, two-minute execution
bound and 64 KiB stdout cap. Python never executes these commands.

Approve arms the existing nonce-bound Confirm for 60 seconds. Its footer
names the complete argv review document instead of shortening a command.
The native one-hour expiry is independent: public Bead/UserAsks exposes
only `It is good until HH:MM:SS.` in the core's local time, with no public
Seen/deadline date or timezone. The bridge invents none. It re-fetches
before approval and reconciles a native expired refusal by disabling
approval while explicitly leaving the native ask open for decline.

Canonical pending, exact success output and validation/timeout failure
are relayed. An older boot's claimed general operation settles failed with
unknown completion after restart, without being rerun. The bridge adds an
UNKNOWN explanation while preserving that native result. A consumed
confirmation and durable uncertain intent also survive a bridge restart:
a lost response is reconciled from native state, and a still-open uncertain
ask cannot be approved again through that card. Independently inspect
command effects before requesting a fresh human decision on a fresh ask.
No callback replay, new delivery key or restart automatically files a new
ask or retries command execution; exactly-once completion is not promised.

Keep the SQLite ledger, review documents and native carry records across
updates or rollback. A downgrade must understand those records; unknown
work must never be replayed. Source landing is not installation or live
activation. The instance operator owns reviewed installation, pairing,
per-call grants, terminal opt-in and a harmless `version` acceptance card
in the configured private topic. This source change performs none of those
live actions. Offline presentation: `python3 -B scripts/preview_run_asks.py --general`.

### Native skill administration cards

Fork-only and capability-qualified: these cards exist only on a core with the
optional skill topic and its card rows. The covered cores are yardr local
`5dedfc56`, whose removal count reads "all N entries of its tree", and yardr
local `9677728c` through `29fcd40d`, whose removal count reads "all N regular
files of its tree" (the skill card code is unchanged across that range). The
bridge accepts exactly those two sentences. A later core that changes a skill
sentence needs its own fixture before it is named here. None is installed or
active until the operator installs it. Unmodified public
`5dc0bfca`/`52e22d57` and the installed `c5642561` refuse to file them, so
the bridge never sees one there; ordinary, general, source, pack and group
cards are unaffected.

History, plainly: on November 12/13 the single-file `skill add` install card
was dropped (`c809c2d`) and adding skills became the operator's alone in a
terminal. The skill core since restores self-service: the operator, users
and crew add or replace a whole skill directly with `yardr skill add <dir>`
(`c5642561`; bead, exec and starting panes are refused). That needs no
source, grant or card, so the old add card stays absent, and a stored one
still refuses before any send.

What stays the operator's is removal and assignment. A user or crew member
proposes one with `yardr bead ask … --run 'skill rm <name> [--force]'` or
`--run 'group|crew set <name> skills=[…]'` / `--unset skills`; the bridge
delivers it through the existing `run_ask` grant and `/decide`:

- The card must be exactly the core's contract: argv, command, description,
  every Does sentence, holder lists (after = before without the skill;
  holders only with `--force`), tree digests, sessions lines, take-back and
  the `code` weight for newly given skills. Anything else, including an older
  fork's skill card, refuses whole with the fallback and lists terminal-only.
- Before sending, the bridge reads `SkillDigest` for every skill the card
  names and requires the tree the card shows. A core without the method
  refuses as `run_ask_skill_cards_unavailable`; a moved tree refuses as
  `run_ask_skill_tree_moved` (ask again). It never falls back to files.
- One review document (manifest, every native sentence quoted, untrusted
  `SKILL.md` descriptions fenced) precedes the compact card: command, tree,
  every holder's list before -> after with its sessions, or the target's list
  before -> after with each named tree. Every skill card arms Approve, then
  Confirm within 60 seconds, even a routine one: a removal has no way back on
  a card (a removed tree is added again only with `yardr skill add <dir>`).
- Only the paired operator's taps count; the core holds the approval to the
  tree, holders and the target's definition (`Defined`) again. Its stale
  refusal retires the card ("Stale skill proposal; nothing ran"); the ask
  stays open for a decline and a fresh ask.
- A removal is carried out by the server under operation `card-…`: the card
  shows the canonical pending, then the receipt naming the operation, tree
  and each holder taken off, or the failure. An assignment shows the core's
  `saved` lines. Lost replies, replays and restarts reconcile from `Bead`.
- A general card naming `skill rm` or a skills change in any form refuses
  (`run_ask_skill_general_refused`): the specific card is the only door.

Audit lives in the core: `skill_removed` names the card as `proposal`
(`<bead>#<ask>`) with the before tree and every holder change; `group_set`
and `crew_defined` carry `skills` before/after and `skills_audit` with the
proposal and each named tree. Rollback: a removal is reversed by adding the
tree again; an assignment by its shown take-back, the operator's. Keep the
ledger and core recovery records across bridge or core rollback. Offline
presentation: `python3 -B scripts/preview_run_asks.py --skill`.

### Complete review bundles

The bridge refuses the removed specific `skill add` card (see above): skills
are added directly with `yardr skill add`. A stored specific `skill add`
ask refuses before any send with a terminal-only `/decide` line; use
Footplate or the yard cards.

Every new source, source-pack, depot-pack and group bundle arrives as **one
UTF-8 Markdown review document (.md), then one compact confirmation card**.
The document contains the full question, context, command, weight and undo,
followed by the complete canonical review text previously carried in numbered parts.
That review text is byte-identical: metadata/holders, manifests, every
quoted file, inventory, old/new hashes and replacement diffs. Quoted strings
preserve CRLF, tabs, trailing newlines, Unicode and literal weight marks.
Quoted content remains untrusted file content. It is never normalized or
shortened and cannot inject markup or controls into the card.

The document names the bundle digest and frames the canonical review with
its UTF-8 byte count. It is atomically written, mode 0600, as
`<state_dir>/run-ask-documents/review-<bundle-digest>.md`. The existing native
SQLite row retains the immutable bytes, document SHA-256, size, stable send
key and confirmed message ID. The proposal digest still binds native
approval; the document hash is delivery evidence in the bridge, requiring
no new core field or authority.

The caller-key reservation and immutable native review row commit in one
SQLite transaction. Before presenting or acting on an open card, the bridge
checks its saved document text, filename, size and hash against the private
file and confirmed delivery receipt. A missing or modified snapshot retires
the controls; it cannot be repaired by regenerating a different review.

The card shows each create/change/remove and its path hashes, kept source/group
grants, size, one-document/zero-text-message
counts, the equivalent old text-part count, bundle digest, filename,
document SHA-256, canonical weight and undo. The full proposal and review
stay in the document. A complete compact card that exceeds 3500 visible
UTF-16 units refuses before any send; plan identities and kept grants are
never silently dropped to make it fit.

The whole document, including framing and any saved legacy snapshot, must fit
**52428800 bytes (50 MiB)**, the bridge's Telegram document ceiling. There
is no 128-text-part cap on new document bundles. An oversized document
refuses before delivery or buttons with
`run_ask_review_document_too_large`, naming an **explicit part-based review
fallback** the operator can request through Footplate or the yard cards.
There is no automatic split, truncation or text fallback.

The document has no keyboard. Only a confirmed Telegram document receipt
with an integer size matching the captured bytes permits the card, which
replies directly to it. Missing, boolean or mismatched sizes remain
unconfirmed. A summary, partial document, reply to the document or unknown
receipt cannot approve. Definitely unsent delivery retries under its
original key; ambiguous delivery stays blocked under the existing ledger's
manual reconciliation rules across restarts and caller keys. Reconciliation
must confirm the document size as well as its message ID. A crash after a
ledger receipt resumes without resending the document or card.

Already captured legacy bundles retain their original text parts, optional
snapshot document, filenames, hashes, keys and receipts; a restart never
converts that generation or resends confirmed parts. Preserve the ledger
and review documents. There is no automatic recovery of a deleted Telegram
message. Saved `.txt` review generations keep their original names, bytes,
hashes and receipts, including across restart and manual reconciliation.

Rollback uses the existing release rollback. The previous runtime still
reads `.md` rows and keeps already bound cards' Approve then Confirm gate,
with no document resends. Its delivery API rejects `.md` filenames with
`native_review_document_name_unsafe`, including on a previously confirmed
generation; it cannot finish an interrupted `.md` document/card delivery.
Return to a Markdown-capable release to resume those deliveries with their
saved keys and receipts. Rollback never renames an already sent document.

Bundle digests also bind the native home directory device/inode, so
replacing a home at the same path cannot reuse an identical proposal.
Native ownership/proposal/state is reread before the document, each
transport retry and final card, as well as before every decision. Changes invalidate the
binding; old/new bundles cannot mix. Approve first opens the existing
60-second Confirm step on the same bound card; the first tap does not install.
Decline/cancel never install. Approval recorded/pending carry, verified
installation, failure and uncertain transport remain distinct canonical
states. Polling/restart and ask_close reconcile the same native answer;
caller-supplied decision text cannot declare success. Filesystem writes and
recovery belong entirely to the core server, never the bridge.

### Source-add declaration and command review

`source add <file>` uses the same native `run_ask` call, pairing, receipt
ledger, document delivery and approval gate as group bundles. This adds no
RPC, grant or setting. Checked against isolated aligned and plain upstream
yardr builds; the carried contract is checked on every read.
The bridge never reads the declaration's provenance path or
writes source files itself.

Before any send, the bridge checks exactly one carried source; known
source/file/diff/inventory/removed envelope fields; unique relative program
paths matching the declaration; UTF-8 byte counts and SHA-256 identities;
replacement diffs against their proposed bytes and standing digest; and
inventory/removal consistency. Absolute programs, unknown envelope or
top-level declaration fields, duplicate YAML keys, aliases/tags, inputs,
pack plans, malformed hashes or oversized texts refuse whole. Declaration
paths containing whitespace require the native full client. The pinned
pure-Python YAML reader ships in the release; no pip step is required.
Core owns declaration semantics, native weights, authority and stale-state
checks under its apply/carry locks.

Limits match upstream: declaration 8192 bytes, each command file 8192 bytes,
32768 bytes for those texts together, 65536 bytes for command texts, diffs
and removed texts, and at most 40 native Does lines. Complete declaration,
each command file, each replacement diff, inventory, removed records/text
and every native Does line arrive in the quoted untrusted review document. Each
section names its exact UTF-8 hash and size; diff sections also retain the
old/new file identities. No whitespace or line ending is normalized.
The compact card names the immutable bundle digest/count and document hash
and replies to that document; its matching size must be confirmed before
controls appear. Canonical weight lines precede the plan.

New/changed code and every replacement use core's weight gate: the first
tap opens the Confirm step on the same bound card for 60 seconds. Commands-free new sources can be routine and apply on the first
verified Approve tap, as core specifies. Cancellation and decline write no
source. A changed standing file is refused by core and shown as a stale
source proposal with no controls; decline it in the yard and ask again.
Approval pending carry, settled success and failed carry use the existing
canonical result/restart reconciliation. A confirmed or ambiguous document/card
is never resent under another key. Preserve the ledger.

Offline evidence: `python3 -B scripts/preview_run_asks.py --source` drives
the bridge over a real-core replacement fixture with fake Telegram. It is
synthetic presentation evidence, not a live Telegram capture. Isolated
native tests prove new, replacing and commands-free installation, exact
stored declaration/files, stale refusal, cancellation, decline and replay.

### Declaration notes

The comments from `tgbridge.yaml` are kept here so a loose source replacement
fits upstream's 8192-byte declaration bound. Every call, parameter and YAML
value is preserved, including the siding call's description.

The bridge polls Telegram, transcribes voice notes, runs chat commands and
keeps the delivery ledger. Both the yard's hook listener and the bridge's
callback service use loopback. Each bot belongs to one yard on its own port;
`install.sh` renders the source name and port into `<root>/pack`.
`credential.env: TGBRIDGE_SECRET` names the callback secret without storing
its value in the declaration. Every update carries the yard's idempotency
key, and a retried call finds its first send in the bridge's ledger.

`send.attachment` is a local path the bridge reads, like CLI `--attachment`.
It is a string parameter because the bridge accepts JSON, rather than a
multipart file upload. `send.key` overrides the yard's key. Ask buttons carry
`ask:<first 16 hex of sha256(key)>:<choice>`, so callers choose the key before
sending; without an explicit key the yard's key applies.

The `notify` call tells the operator what reached their inbox, in the
conversation named by `TGBRIDGE_CONVERSATION`: the paired chat id or
`<chat id>:topic:<thread id>`. Register it with `yardr user key <user> tgbridge`.
The bridge itself sends the hook secret in `X-Tgbridge-Secret`; it stays
on this machine. Each hook entry identifies its bot, conversation and message,
and `delivery.body: id` lets a retry find its prior relay.
Hook keys are `chat` (paired chat id), `topic` (general or topic name), `kind`
(message, transcript or answer), `sender` (operator Telegram id) and `thread`
(the topic's thread id). The bridge drops other chats. Register
`yardr user identity <user> --source tgbridge --id <Telegram user id>`;
`hear.by: identity` then maps deliveries to `speaker=<user>`.

A thread is the paired chat id or `<chat id>:topic:<thread id>`. Reply uses
the yard's key; posted can find an answer lost after sending. Read returns
whole messages from the bridge log; the yard's delivery keeps 500 characters.
The command protocols are:

| Command | Input | Output |
|---|---|---|
| `tgbridge/thread reply` | `{thread, parent, body, key, files?}` | `{id}` |
| `tgbridge/thread posted` | `{thread, key}` | `{id}` or `{}` |
| `tgbridge/thread read` | `{thread}` | `{messages: [...]}` |

### Source pack bundles

`pack apply <absolute-directory> [--sha256 <digest>]` and embedded
`pack apply <name>` use the existing `run_ask` grant, operator pairing,
ledger and native approval gate. A directory must be inside a registered
depot's main checkout; a worktree or an installed release's `<root>/pack`
cannot be proposed. Use a copy of `yardr/` in that checkout for a card,
or apply the installed pack in a terminal. The native server reads the pack;
the bridge never opens its provenance path.

This source-kind form supports packs without inputs. Before sending, the
bridge checks the upstream pack name/kind/label/digest envelope, command and
optional pin, first Does line, every carried declaration and program hash,
replacement diffs, inventory and removals. Unknown fields, the old
`pack.directory` snapshot, `pack.yes`, binding flags, inputs, URLs and
summary plans refuse whole with the terminal fallback. Depot plans use the
separate bounded contract below. Upstream's
source-kind cards refuse `--yes`: that flag is for overwriting hand-edited
files of depot plans; overwrite approval is unsupported here too.

One review document contains every native Does line, each carried declaration
and command file, complete replacement diffs, old/new identities, inventory
and removals, plus the native pack label and directory digest. Upstream carries
no directory snapshot: `pack.yaml`, setup notes and other directory files
are not supplied or invented. The digest identifies the whole native reading;
the bridge cannot recompute it from the carried sources alone.
Declarations are bounded to 262144 bytes together, command texts/diffs/removed
texts to 65536 bytes and Does to 40 lines. The shared 50 MiB document ceiling
includes framing and refuses before any send or control.

For source add and pack apply, an aligned replacement sentence ends with
`grants kept for source <name>: ...` or `no grant of source <name> stands.`
The full sentence appears in the document and its grants suffix on the compact
card. Plain upstream omits that disclosure: replacing cards refuse before
any send, while new-source cards work. A legacy card in an unsupported shape
loses its controls on refresh and directs the operator to the yard.

The compact card follows only after the document's delivery and size are
confirmed. It replies to the document and names the create/change/remove paths
and hashes, kept grants, bundle/document digests and counts, with native
weight lines first. New or changed programs require Approve, then Confirm on
the same card within 60 seconds. A pack replacement keeping its programs
can be routine and approve on the first tap; a `source add` replacement
always needs confirmation. Typed replies cannot approve weighted cards.

The native server re-reads a directory at approval. A moved digest or changed
standing declaration, command copy or grant makes the proposal stale, with
nothing run. Ask again from the current state; the bridge never substitutes
a new directory reading for the one reviewed. Only the server records approval
and carries out the operation; pending/ran/failed are read back canonically.
Decline, Cancel and repeated taps do not apply again.

Definitely unsent deliveries resume under the same key. Ambiguous sends need
the existing ledger reconciliation, across restarts and caller keys.
Keep the state directory, including review documents. A saved supported
legacy generation retains its parts and keys; unsupported old shapes retire
without breaking refresh.

`python3 -B scripts/preview_run_asks.py --source` and `--pack` produce labelled
synthetic captures from real native fixtures with fake Telegram. The isolated
source/pack integration runs on aligned and upstream builds, compares complete
wire fixtures and resulting source/copy bytes, and covers approval once,
routine and weighted packs, decline/cancel, stale state and replay.
Source landed, release installed and live activation are separate states:
the operator owns release installation, pack apply and restart. For rollback,
use `install.sh --rollback` and apply a reviewed earlier pack in a terminal.
After activation, check `source show` and `source check`, then propose a
synthetic card and verify its canonical result before relying on it.

### Depot pack bundles

The supported depot form is a complete native directory plan:
`pack apply <absolute-directory> --sha256 <digest> --depot <depot>` with
canonical native flow, crew, group, bind, members, open_threads and calls
mappings where supplied. The directory must be in that registered depot's
main checkout. Native `git`, `dir` and omitted-kind envelopes are accepted
by their depot/plan fields. The bridge reconstructs argv from structured
fields in core's fixed order and requires the POSIX-parsed command words to
match `yardr` plus that argv. Argument words are compared with the same
ask's structured depot and mappings, subject to generic text and size checks;
the bridge adds no per-flag word grammar. The native command text is shown
verbatim; paths remain subject to the core's proposal grammar.
Embedded depots, bare records, URLs, summary/files/yes payloads, `--yes`,
unresolved inputs, hand-edited overwrites and nonempty missing/asks/refused/
nontext/edited blockers refuse whole before sending. Unknown nested fields,
object kinds/actions, duplicate mappings or objects, invalid paths, partial
old/new changes and invalid file hashes/diffs also refuse.

The one Markdown document contains all native Does lines verbatim, name,
kind, build label, depot, digest and mappings; every plan object/action,
old/new field, runs/env/keeps/shadows and flow/governs consequence; full
created/changed file text and diffs with old/new SHA-256; carried source
declarations, programs, inventory, removals and standing grants. Dropped
objects retain the native explanation of what stays standing. The compact
card names the depot, operation, plan counts, kept replacement grants, native
weight, bundle/document/pack-digest identities and undo limits. A review receipt
proves delivery only; core approval and canonical execution outcome remain separate.

Native Does prose is server-owned disclosure, not a second executable
grammar in the bridge. Unknown or reworded prose remains visible and bound
to the proposal. The narrow source-section/grants framing check remains:
typed source create/change/unchanged actions must match carried sources,
and replacement disclosure must account for every standing grant. Aligned
core supplies that disclosure; public-main replacements omitting it refuse
as `run_ask_invalid_depot_pack_use_terminal` before send and list as
terminal-only in `/decide`. No manifest/setup snapshot is invented, no
provenance path is read by the bridge and the whole-pack digest is trusted to
core's capture/stale check. Individual supplied file hashes and complete
inverse diffs are validated.

Bounds remain 40 Does lines, 256 KiB carried declarations and 64 KiB aggregate
carried texts/diffs/removals plus depot file texts/diffs, with existing source
per-file bounds and the shared 50 MiB framed-document ceiling. Empty text or
diff omitted by native `omitempty` is valid only when the supplied hashes
and action describe an empty file. Nothing is truncated to fit. Document
delivery and matching size must be confirmed before the bounded card replies
to it. Ambiguous sends retain their original ledger keys across restart and
caller-key changes. Native routine proposals use one tap; native weighted
proposals use Approve then nonce-bound Confirm on the same card within
60 seconds. Refresh clears expired controls before queued updates drain.

Enabling depot review requires an explicit prospective native event watermark,
recorded while the old bridge is stopped. It binds the current native
home/device/inode, operator, source, bot and paired routing to native `Head`,
the latest global event sequence. The existing delivery ledger stores this
activation metadata, never an invented historical refusal. Each publication
and approval reads the bead's native `Events` and requires a `noted` creation
event bound to that bead, ask number and asker, strictly after the watermark.
Ask numbers are never compared with the watermark. Missing, malformed or
ambiguous creation evidence refuses whole; there is no local-clock fallback.
Missing or invalid activation metadata also refuses depot review. Older
ask-number markers do not establish this event boundary: explicitly recapture
during authorized activation; their original ledger rows remain untouched.
Earlier asks remain blocked through original/new keys and `/decide`; withdraw
them in the yard and create a fresh proposal after activation. Existing
definitive refusals remain truthful and cannot be revived. See the
[setup boundary](../yardr/SETUP.md#depot-pack-imports-and-activation).

During that authorized stopped-instance activation, with native yardr
running, use the reviewed release's configured interpreter/root/settings:

```sh
<instance-python> -B - <root> <settings> <<'PY'
import runpy
import sys
from pathlib import Path

module = runpy.run_path(str(Path(sys.argv[1]) / 'current/bridge/bridge.py'))
settings = module['load_settings'](sys.argv[2])
store = module['Store'](settings.state_dir / 'telegram-bridge.sqlite3',
                        settings.pairing_update_id + 1)
bridge = module['Bridge'](settings.config, store, '', None,
                          port=settings.port, state_dir=settings.state_dir)
print(bridge.run_asks.record_depot_cutover())
PY
```

It prints only the global event floor and makes no Telegram call. Repeating
this helper retains or advances that floor: it belongs to an intentional
activation, never ordinary startup or check-install. See the
[depot-pack approval design](../docs/design/depot-pack-approvals.md) for
the full contract, bounds, routine vs. Confirm rules and rollback guarantees;
the downstream instance handoff owns the exact landed refs,
installation/restart/identity-check packet and rollback. No installer, CLI or
settings change automatically records the marker.

Create the fresh native proposal after capture, using the pin from native
`pack check` and retaining the same proposed mappings:

```sh
yardr bead ask <bead> "Import this complete depot pack?" --to <operator> \
  --context "Read the complete native plan, files and diffs." \
  --run "pack apply /absolute/main/checkout/pack --sha256 <digest> --depot <depot> --crew reviewer=reviewer"
yardr call tgbridge run_ask bead=<bead> ask=<fresh-ask> topic=general key=<fresh-key>
```

Core rechecks the pack reading, standing files, depot/mapped configuration,
sources and grants before execution. Stale proposals lose their controls.
Pending, success, failure, decline and withdrawal are read back from the yard;
replay never treats a delivery receipt as permission to run again. Retain the
state directory, review documents and watermark during rollback. Source
reviewed/landed, release installed and service live are separate states;
only the instance operator installs or activates this feature. No new
settings, source grant or runtime permission is required.

`python3 -B scripts/preview_run_asks.py --depot` produces a labelled synthetic
preview from genuine native fixtures, including a complete large diff, a
routine card, weighted Confirm and pending/success/failure/refusal states.
The checked-in [preview](../docs/images/native-depot-pack.html) uses the same
renderer/state machine with fake Telegram. Real isolated native tests on the
aligned and pinned public-main cores cover exact carry, stale state, actual
SQLite/poll ordering, crashes, source/API publication and replay. These are
local source capabilities, not a compatible public-release promise.

### Native group add and set review

The yard's group rows (`yardr group add <name> --runner herdr|exec
[--limit <n>] [--set cmd=|dir=|kind=|args=|prompt=|worktrees=...]` and
`yardr group set <name>` with any of `prompt=<file>`, `cmd=<command>`,
`preset=<label>`, `model=<model>` and `effort=<level>|default`) reach the
bridge as ordinary run asks: argv in the yard's stored order, command,
description, the yard's `does` sentences, back/back_by and the weight.
`model=` exists on yardr's aligned build alone, beside another setting;
plain upstream refuses it when the ask is written. Bead serves no `role`
field: the prompt an approval writes is the lines after the sentence "On
approval the yard writes the text below to the file
`<home>/roles/<group>.<sha256>.md`", and the digest in that file name
decides its exact bytes, a final line ending included. A `role` the yard
may serve beside the card is accepted on these two rows alone and must be
that text.

`run_asks.parse_group` reads the card sentence by sentence in the yard's
order (`runask_groupadd.go`, `runask_groupset.go`, `runask_strong.go`).
Fixed sentences match exactly; variable ones match their pattern and are tied
to the argv: name, runner, limit, kind, args, command, model, effort level,
preset label, the written file's group name, back and back_by, and the exact
weight. An add without `--limit` says limit 1, the herdr and exec default;
an exec add's directory and worktrees are the argv's words, so one that
resolves through a symlink to another path refuses. A set's back is the one
the yard writes from what the card says stands now (`strongBack`): each
changed setting put back to its value, or unset where the card says none
(no flags of their own may be `--unset args`, `'args=[]'` or
`'args=null'`); a value that reads as JSON refuses. An add's settings must be ones its card shows for the runner (herdr:
kind, args, prompt, worktrees; exec: cmd, dir, prompt, worktrees). A set's
standing prompt, when the card shows it, is the lines before the written
file's sentence; the card reads whole only where exactly one such split
leaves a new prompt that hashes to its file name and the rest of the card
in order. Refused whole before any send, with the fallback: a pre-alignment
card (`runner=`/`arg=` words, `roles/sha256/` files), any other argv word or
order, a manual or peer runner, a relative or unclean prompt, directory or
worktrees path, a value
outside plain words, an unknown, missing or extra sentence, a prompt over
8192 bytes or holding a control, bidi or line-separator character or a
mark the yard draws itself, carried files, `seen`, or a different weight,
back or description. The bridge reads no group: what stands is the card's to
say, and the yard holds the approval to it.

Weight follows the yard (`strongChanged`): an add, and a set carrying a
prompt, command, model or preset, are `code`; a set of `effort=` alone,
or `effort=default` alone, carries none and approves with one tap.

The review bundle uses the shared one-document/one-card delivery above,
with the same stable ledger keys, confirmation and rechecks. Its complete
review contains every sentence of the card, the new prompt as quoted JSON
strings per line with its line ending, and a set's standing prompt the same
way, as its lines show on the card (whether that file ends in a line ending
is not on the card). A manifest names the written file, its SHA-256 and
bytes. The compact card names the operation, runner and written file and
repeats every sentence of the yard's but the prompt lines; prompt content
never appears as card text even when it reads like a sentence of the yard's.

Approval is the gate above: Approve then Confirm on a weighted card, one
tap on a routine one; ApproveAsk is called once. The yard writes the group
(and a prompt's read-only `roles/<group>.<sha256>.md`) in that write; the
card settles from the canonical `ran`. A group changed since the card, or a
prompt file that no longer holds the text, refuses in the yard: nothing is
written, the ask stays open and the card says the yard refused. Decline and
Cancel write nothing. A card sent before the yard moved to these shapes no
longer reads whole: its next refresh or tap marks it "Proposal changed; use
Footplate or the yard cards" and drops its buttons, and the operator decides
it in the yard. There is no pending carry for these rows and no automatic
undo beyond the card's Back.


### Native repository edit proposals

Repository-edit proposals (`yardr depot edit <depot>`, `run.repo_edit`) are an
optional fork-only capability probed via `RepoEditCapabilities` before any ask
is sent or opened from `/decide`:

- Cores without `RepoEditCapabilities` return `RPCUnsupported` and keep all
  existing native cards untouched; the bridge refuses any `repo_edit` proposal
  locally with `FALLBACK` and `/decide` lists it as `terminal-only run`.
- Known capability contracts are version `1` (`materialize: false`, capture-only)
  and version `2` (`materialize: true`, full materialized carry, review and
  `merge-local` landing). Version or schema mismatches refuse cleanly before
  sending any document or message.
- [`bridge/repo_edit.py`](repo_edit.py) validates every `run.repo_edit` field,
  `code` weight, normalized `.md`/`.txt` relative paths (1–16 paths, `<= 64 KiB`
  text, `<= 8 KiB` context), modes (`100644`), SHA-256 hashes, unified diffs,
  canonical `does` lines, `yardr native proposal <native-proposal@yardr.invalid>`
  authorship/committer, and Go-compatible canonical `repo_edit_digest`.
- The bridge sends one `0600` UTF-8 Markdown review document (`<= 50 MiB`)
  carrying exact quoted old/new bytes, line-ending/control offsets, diffs,
  depot, base commit, subject, authorship, test command, review/assembly route
  and inverse rollback, then one compact card bound to that document and to
  `yardr_home` (`home_identity`).
- Approval requires tapping `Approve` then `Confirm` within 60 seconds, passing
  `repo_edit_version` and `repo_edit_digest` in `ApproveAsk`. Stale core
  refusals retire the card (`nothing ran`). On `answered` v2 asks, the bridge
  extracts the reserved patch bead ID and queries `NativeEditStatus` across
  `carrying`, `prepared (not landed or installed)`, `reviewed`, `held`, `failed`,
  `withdrawn`, and `landed (source landed; not installed or live)`, stopping
  status polls once terminal.
- Preview locally with `python3 -B scripts/preview_run_asks.py --repo-edit` and
  see [docs/design/repo-edit-approvals.md](../docs/design/repo-edit-approvals.md).


## Ordinary native ask cards

Native decision and run cards render questions as bold headlines, with blank
lines between context paragraphs and Markdown lists and line breaks rendered
through the send renderer. Decision options are numbered bold blocks with
their consequences indented below; recommendation/why, reply instructions
and recorded outcomes have separate spacing. Labels, consequences, provenance,
recommendations and outcomes remain escaped literal text. Narrative Markdown
links and mentions display their label and destination as text, never HTML
links; raw HTML stays literal and unsupported Markdown uses the send subset.
Link reference definitions, used or unused, refuse the whole native card and
direct the operator to Footplate or the yard cards.
Only canonical weight lines may retain the native warning marker. Commands
and quoted file/review bytes remain literal.

Bundle headings and section labels use bold HTML, narrative lists keep their
original lines, and quoted lines stay preformatted. Presentation does not
change review text, UTF-16 counts, chunk boundaries, hashes, part counts or
the final approval/reply-chain binding. Cards are bounded by their complete
rendered visible text, including spacing; overflow refuses the complete card
through the existing fallback, without truncation or live controls. Existing
ledger rows retain their saved HTML and delivery receipts; this change does
not resend them. New cards store their footer spacing with the rendered
HTML, so existing edit reservations keep their exact payloads. No core,
setting, ledger migration or grant changes.

`python3 -B scripts/preview_run_asks.py --dense` shows a synthetic copy of the
four-option transcription-provider decision and its recorded outcome through
the actual offline send/edit path. These previews use disposable native
socket/ledger fixtures and fake Telegram, with no live account or store.
They prove source rendering only. Installation/activation belongs to the
operator through the normal release process; rollback selects the previous
release. Preview review checks the headline, paragraph spacing, option blocks,
literal quoted bytes, separated outcome and reply navigation.

Authenticated `POST /v1/ask-card`, CLI `ask-card` and source update `ask_card`
accept only bead/positive ask, routing, silent and a stable key (body key or
Idempotency-Key). They use the existing run_asks module and SQLite tables,
lock, paired-human checks and configured home's socket. Old rows without a
kind remain run cards. There is no caller-selected yard, operator or payload.
Unknown canonical shapes and operation-bearing/weighted asks refuse whole.
The current ordinary weight gate belongs to unsupported identity mappings;
run cards keep their bound Approve, then Confirm gate. Read-only 503s during core
replacement get at most three attempts with bounded delay; writes are never
automatically retried. Error bodies and HTTP errors after writes remain
ambiguous until a canonical Bead reread establishes the result.

The full question/context/options/consequences/about/recommendation/why must
fit the existing 3500-unit card bound and 100-button limit (97 options plus
Later/Decline/Report). Buttons use the displayed stored option
number; they do not synthesize You decide. Direct replies take `N optional
words`, or the whole answer with option zero on a no-options card. The message,
conversation/topic, bot and paired human must match; forwards, stale proposals,
replays and changed role/identity/home bindings cannot answer. Replies remain
in chat. Later/Decline use native primitives. Put-aside cards lose controls;
due/reopened canonical asks get a new message for their new open turn. Answered,
declined and taken-back states have no live controls. Long canonical words use
the existing bounded, keyed outcome-message path. Native ask_close ignores
caller decision text and derives status from Bead; chat-only close is unchanged.

Ordinary cards require canonical `turn` and use only `AnswerAskExpected`
{bead, ask, turn, option, words, actor, weighed}, `DeferAskExpected`
{bead, ask, turn, actor} and `DeclineAskExpected`
{bead, ask, turn, words, actor} on this instance's socket. Unsupported weighted
asks refuse before this path; ordinary answers send weighed=false. The minimum
verified core is `7fa0d4c75ff6`. Missing turn or method refuses as an older core,
without falling back to legacy writes. A concurrent mutation conflicts
atomically; every write result still reconciles through Bead, including errors
after a committed write. The turn is kept separately from immutable proposal
content. Each Telegram message presents exactly one open generation (bead,
ask, turn) for its whole life, bound from a confirmed send receipt or the
verified callback reconciliation below.
Callbacks must carry that message's turn; direct replies take the turn from
the replied-to message's binding. Both must still match canonical state; the
core guard closes the final read/write race.

A changed open turn sends a new complete card, honoring the original silent
setting. Previous cards are retired by footer edits without controls; their
inputs refuse even when retirement edits fail or remain uncertain. Replies
to any known generation are consumed as native input, including stale replies;
unknown message ids keep ordinary chat behavior. Native ask_close accepts any
generation's message id. Each generation has a stable delivery key: the first
keeps `runask:<id>:send`, later turns use `runask:<id>:send:<turn>`. Confirmed
sends never repeat; definitely unsent sends retry on later refreshes; ambiguous
sends stay unbound until verified callback reconciliation, never an automatic resend.
Restart preserves these receipts and bindings. Recovery binds only a confirmed
receipt with a positive integer message id whose stored fingerprint matches
the card's exact send request (complete HTML, turn-bound keyboard, silent
setting and topic). A key already used by another send leaves that turn unsent
and unbound with an idempotency conflict, including after restart, repost or
close. The unrelated message keeps ordinary chat behavior and cannot answer
the native ask. If no replacement card is bound, older cards lose controls
and direct the operator to the yard, without claiming a newer card exists.
Ordinary rows lacking generation bindings refuse as unconfirmed; these
unreleased rows have no upgrade path.
Run rows retain their existing ledger upgrade and edit behavior.

When an ordinary decision card's proposal digest changes (such as a bead title,
a referenced `about` bead's stage or title, or home identity) while the native
ask remains open, the bridge marks the stale card refused
(`run_ask_stale_proposal`), posts at most one replacement card per
`(ask, proposal digest)` with the current content and turn (honoring the
original conversation and silent setting), and edits the stale card without
controls to `"Proposal changed; replaced by a newer card"` (or directs to
Footplate or the yard cards if the replacement cannot be shown or confirmed).
Taps and replies on the stale card remain refused, and the fresh card answers
through the same `seq` and `turn` guards. The replacement is issued per
staleness event: a digest value that recurs (such as a title reverted to an
earlier value) posts one further fresh card, scoped by the row it replaces,
so retries and restarts converge on one card and replacement chains stay
acyclic; a retired row is never re-linked or re-presented. Canonical state is
re-read before posting, so if the ask is already answered, declined, taken
back, or put aside before the replacement is issued (including across a
restart between marking stale and posting), no replacement card is posted.
Run cards keep `"Proposal changed; use Footplate or the yard cards"`
without automatic replacement.

An ordinary decision delivered by Telegram can lose its send response. Its
saved receipt remains `ambiguous_delivery_requires_manual_reconciliation`;
repeating `ask_card`, including after recovery, keeps that receipt for the
same turn. Neither a new caller key nor a restart resends the card. Recovery
binds the existing message without changing the send ledger or claiming the
send response was confirmed:

1. Check the paired chat for the original card. If it is there, the paired
   operator can tap the action they intend to take. That tap both reconciles
   the message and performs the selected action if the canonical ask is still
   open at that turn. A reply alone cannot recover an unknown message id.
2. Recovery requires this bot's non-forwarded message in the exact paired
   chat/topic, a positive message id, the complete original text and keyboard
   naming this row and turn, and the matching saved ambiguous-send fingerprint.
   Core's current operator identity, home, proposal and turn checks still apply.
   Missing proof, a message already bound elsewhere, another row's callback,
   or a definitely refused send cannot acquire a binding. A second message
   cannot bind the same turn. Replays cannot answer twice. A recovered older
   turn or an ask already decided in the yard is retired without another
   decision; its controls are removed using canonical state.
3. Before recovery, `ask_close ask_key=<original-key> decision=refresh`
   returns `ask_card_delivery_unconfirmed` with an instruction to tap the
   original card or decide in the yard and delete the card manually. It
   cannot edit a message whose id Telegram never returned. After recovery,
   `ask_close` can reach that message by the original key or recovered message
   id and shows canonical state; caller decision text remains ignored.
4. If the genuine tap lacks enough proof, it says: “Card delivery unconfirmed;
   decide in Footplate or the yard and delete this Telegram card manually”.
   Make the decision there, then remove the original Telegram card manually.
   If no card is visible, use the yard directly. Do not delete ledger rows or
   publish under another key to work around ambiguity.

This recovery needs no settings, grants or ledger migration. The operator
installs and activates the reviewed release through the existing setup steps;
source landing alone does not change a running bridge. Retain the ledger on
release rollback: recovered messages use the existing generation bindings,
and the original ambiguous receipt remains saved.

Manual stage cards use exactly the same path and stored options. AnswerAskExpected
checks actual stage membership/current stay and records the words and movement
atomically; canonical sibling take-backs reconcile on polling. The bridge
requires to=the configured operator even if another person has a sibling ask.
An externally changed option answer can update the canonical outcome; the
bridge displays it on refresh, while a settled card cannot initiate revisions.

The only newly reserved callback namespace is `askcard:`. Settings and custom
publish keyboards diagnose collisions explicitly. Rename an overlapping
add-on prefix and its generated callbacks before adopting; existing conflicting
buttons need operator reconciliation. This is a compatibility exception, not
a claim that every old custom-prefix configuration loads unchanged. No ledger
migration or deletion is needed. The setup guide's ordinary-decision section
covers the minimum grant, private home/operator check/apply helper, verified
core baseline and pending independent-runtime deployment prerequisite.

## /decide: the operator's open decisions

With `[yardr] cards = true` (home and operator required), `/decide` is the bridge's own
command. It is never relayed and runs no add-on. Without those keys a
`[[command]]` named `decide` and its `decide:` prefix keep their handler, as
before.

`/decide` verifies the paired user, chat and bot as every command does, then
the operator's role and Telegram identity through `GetUser`, then reads the
core's public `UserAsks` {name, viewer} for that operator on this instance's
socket. The core filters the recipient and states: answered, declined, taken
back and put-aside asks are not listed; a put-aside ask returns when the core
returns it. Nothing is cached between taps: every page, refresh and selection
reads the queue again. An unknown field or shape in the queue refuses the
whole list.

The list is plain text, six entries a page, oldest first: the question (cut
to 100 UTF-16 units, never inside a combining or joined character), then the
kind, the ID, the bead's title and the asker. An ID is `<bead>#<ask>`: the
bead is unique in this yard and the ask is that bead's note seq; each bridge
serves one yard, so no ID is looked up anywhere else. Decisions, words
questions, run, source-add bundles and group add/set proposals get a
button (a group ask is listed as `run`; one with a field the card does not
support is listed without a button, as below). Secret, setup,
identity, depot-push, weighted, terminal-only run (file-carrying or
input-wanting) and linked-yard (`bead@peer`) asks are listed with
"Not complete in Telegram" and no button; selecting one says so and sends no
card or generic approval. Prev/Next/Refresh edit the list message in place;
a page past the end shows the last page.

A button (`decide:o:<id>`, the native row id) or `/decide <bead>#<ask>` opens
the card through the same `create` path as `ask_card`/`run_ask`: same row id,
render, delivery key, ledger and callbacks, so an open card is never sent
twice. If the card is already delivered, the bridge says so, replying to the
card when it is in the same conversation. A card keeps the conversation and
silent setting it was first sent with; when the bridge no longer serves that
conversation, `/decide` refuses rather than relocate it. `/decide <bead>` opens
the bead's only open ask, or lists its IDs when there are several. A selection
of an ask no longer open reports its canonical outcome (answered with option
and words, a run's approved, failed or to be carried out with its result, put
aside until, declined, taken back by) and sends no card. An ambiguous card
send is reported as unconfirmed and never retried by `/decide`.

The command answers in the thread it came from when that is the paired chat
or a configured or served topic; any other thread gets a refusal there and
nothing is read. A callback must come from the paired user on an unforwarded
message of this bot in the paired chat and a served thread. An ID from another
binding or a tampered button is "out of date". A yard that does not answer
gets "The yard <yard> is unavailable"; nothing is listed or opened.

With native keys, settings refuse a `[[command]]` or `[[menu]]` named
`decide` and a `callback_prefix` overlapping `decide:`, and publish keyboards
refuse `decide:` buttons. `set-commands` adds `/decide` to the menu (it
counts toward Telegram's 100). The core needs `UserAsks` (public Calls
contract) beside the ordinary-card baseline; no new grant, setting, table
migration or service. The bridge adds one SQLite table, `decide_ids`, mapping
a button id to its bead and ask.

## Decision report on tap (`📄 Report`)

Every open native decision and run card (from `ask_card`, `run_ask`, or
`/decide`) carries a final `📄 Report` row (`askcard:<id>:report:<turn>` or
`runask:<id>:report`). Settled cards drop all controls as before.

Tapping `📄 Report` is strictly read-only toward the yard and leaves the card's
text, buttons, generation bindings and state untouched (`ensure` and
`reconcile_callback` do not run, and no yard write RPC is called). The bridge
verifies the paired user, paired private chat, bot-authored card message,
matching topic thread, current binding and `GetUser` operator identity, then
reads `Bead`, `Notes` and `Events` on the configured home's socket at tap time.

The bridge renders and sends one Markdown document named `<bead>-ask-<n>.md` in
reply to the card's message (`text_fallback=False`), using the existing send
ledger key `runask:<id>:report:<message_id>:<update_id>` so a retried update
never sends the document twice. A yard or Telegram failure leaves the card
untouched and sends one short text reply (under `:error`) saying why.

Report selection and formatting:

- **Header:** bead id and title, stage, ask number, question, the newest
  commit SHA named in the notes (`VERDICT:`, `STATUS:`, `head:`, `commit:`,
  `on`, `fixed`, `blocked,`, or `@`) if present, and a one-line
  `How to read:` key (`finding -> answer -> reviewer status -> recommendation`)
  naming the note seqs present.
- **Finding being answered:** before the notes since stage entry, the bridge
  includes the most recent substantive review report at or before the cutoff
  (a `kind == "note"` starting `VERDICT: FINDINGS`, or a prior report note
  named by a repair/status note) under `## Finding being answered`, even when
  it is older than the stage entry.
- **Note range:** if the ask note's JSON carries an `entry` event seq, the
  bridge walks `Events` (doubling from 64 up to 8192) to find `noted` events at
  or before `entry`; otherwise it uses notes with `seq <= ask`. Starting from
  the last ordinary (`kind == "note"`) note at or before that cutoff whose
  author is not `yard`, `yardmaster`, or the configured operator (falling back
  to the last `kind == "note"` at or before the cutoff, else the ask note or
  earliest note), it includes every note through the newest note in ascending
  `seq` order (without duplicating the finding note if it is in that range).
- **Note rendering:** each note prints `seq`, `author`, and timestamp converted
  to `+05` (`YYYY-MM-DD HH:MM:SS +05`). `ask` and `answer` notes are rendered
  from their structured fields (question, context, options, pick/words/status,
  or run command/result), never as raw JSON.
- **200 KB cap:** the document is capped at 204,800 UTF-8 bytes (`200 * 1024`).
  Over the cap, older notes are dropped first to keep the newest notes and a
  notice names the omitted seq range and byte count (if a single newest note
  alone exceeds the cap, it is truncated on a UTF-8 boundary with the cut byte
  count noted). The "Finding being answered" section is never omitted: when
  it and the newest note do not fit together, their text is truncated (each with
  a visible "truncated, N bytes cut, full text in note <seq>" line) before
  anything else is dropped. No new grant, setting, or ledger migration is
  required.

## Siding status and the plain fold

Authenticated `POST /v1/siding-card`, CLI `siding-card` and the source update
`siding_card` accept `depot`, optional `flow`, `stage`, `group`, routing
(`topic` or `conversation`), `silent`, and a stable `key`. The HTTP
`Idempotency-Key` header can supply the key. They accept no actor, command,
member list, destination or purported result from the caller.

```sh
<root>/bin/telegram-bridge siding-card \
  --depot <depot> --idempotency-key <stable-key> \
  --flow <flow> --stage <stage> --group <group>
yardr call tgbridge siding_card depot=<depot> key=<stable-key>
```

The card is built from yardr's ordinary calls, the same on the aligned and
on plain upstream yardr; no siding guard in core is needed:

- **Discovery.** `GetDepot`, `Groups`, `Routes` and the `operator` of
  `Settings` find each stage whose route sends the depot's beads to a manual
  group whose config states `fold`, `fold_after` or `fold_max` (upstream
  `store.SidingOf`). An unstated `fold_max` is 6.
- **Members.** `ListBeads [{"Depot": d, "Stage": s}]` lists the stage's open
  beads, and each is a member of the siding whose group its own route names,
  ranked and matched as yardr's `store.MatchRoute` does: a bead a more
  specific route sends to another group is not on this card. `SidingBeads` gives each its points and why a fold would leave it
  (`held`, `claimed`, `peer`, `train`, `blocked`, `asked`); `NextStages`
  gives each its flow and the carrier's stage, the first stage the flow
  leads to, as `yardr bead fold` picks it. Beads of different flows, or of
  one flow name from different sources, make separate cards, as yardr keys
  a siding.
- **Proposal.** The eligible beads, oldest entered first, with their ids,
  titles, points, the carrier's stage and `fold_max`. Over `fold_max`,
  fewer than two eligible, or no single next stage: status only, no button.

Every card lists every open bead of the siding. A reply `fold <id> <id> …`
asks for a **new card** for exactly those beads, in that order, and never
folds; the old card is withdrawn. A hand fold has no points maximum, but the
reply refuses a bead that is not open in the siding or that a fold would
leave; a held or asked bead, which `FoldBeads` itself would take, is folded
with `yardr bead fold` in a terminal. A reply `release <id>` asks for a
**release card** for that one bead, which is sent and moves nothing. The
bead must be one the fold would take (not held, claimed, blocked, asked,
peer or train) and have a next stage; the card names it, its title, the
stage it leaves and the stage it enters, and its button is `Release <id> →
<stage>`. The siding card is left as it is: once the bead moves, that card
refuses its own tap. `Leave waiting` withdraws the card. Cards expire after
15 minutes. Callback prefix `siding:` is reserved; generic sends cannot
mint it.

A tap reads the shown beads again: each must still be open, in the siding's
stage, routed to its group, not stuck, and bound for the shown carrier stage; any difference
refuses with the beads' current state on the card and nothing written. Then
the bridge marks the row attempted, calls `FoldBeads [[ids], {"To": stage,
"Title": ""}, operator]` once with exactly the shown ids and stage, and,
whatever it answered, reads the beads (`GetBead`):

- every bead closed `folded into C`, one C: settled; the card names C, its
  stage, the beads and the undo `yardr bead unfold C <member> --reason <why>`;
- every bead still open in the siding: nothing folded (a fold is all or
  nothing); the mark clears and the card stays open;
- anything else: the current state, no button.

A release card's tap re-reads its bead: it must still be open in the siding's
stage, routed to its group, not stuck, bound for the card's stage, and at
the `revision` the card read (`GetBead`). Any difference refuses with the
bead's current state on the card and nothing written. Otherwise the bridge
marks the card attempted and calls `Advance [id, <stage>, {"stage": <from>,
"revision": <revision>}, operator]` once. The store refuses a stale stage or
revision itself, and checks the operator against the siding group's members
as it does for any move out of that stage. The move ends the siding's manual
claim on the bead. Whatever the advance answered, the bridge reads the bead
(`GetBead`): in the stage the card enters, the card settles with the footer
`Released <id> from <from> into <stage>.`; still open in the stage it leaves
at the same revision, nothing moved and the card stays open; anything else,
the current state and no button. A repeated tap answers `Already released
into <stage>` with no write. A release never folds, and it offers no undo:
a move back is a person's move with `yardr bead advance`, where the flow
allows it.

A repeated tap cannot fold twice: the fold refuses closed beads, and a tap
on a settled card answers `Already folded into C` with no write. A crash
between the fold and the ledger write is settled the same way by the next
refresh. Reads are retried when yardr answers 503 while it replaces itself;
neither the fold nor the release advance is. A yard home that stops
validating while a reply or tap arrives is logged and nothing is written;
the poll goes on.

Siding rows live in `siding_fold_cards` and share the native-card
ledger with ordinary and run cards: update receipts, lock, send
fingerprints, delivery recovery, display edits and idempotency keys. Every
card is bound to this instance's yard, home directory identity, operator,
source identity and Telegram human, bot, chat and topic. `ask-close` closes
settled, stale, withdrawn or expired cards after confirmed delivery of their
control-free presentation, and status cards from their publish receipt;
open cards await the human.

`python3 -B scripts/preview_siding_cards.py` renders offline HTML of an
open, a status-only, an explicit-subset, a settled and a refused card from a
disposable yardr store and fake Telegram. It proves the rendered states, not
a Telegram runtime or live activation. The integration tests
(`bridge/test_siding_cards.py`) run on every yardr build and cover source
calls, authenticated HTTP, installed-copy CLI and imports, and the production
getUpdates → refresh → drain order.
