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

Every yard runs its own bridge process, with its own bot, from this one
`bridge.py`, configured only by its own settings file. Below, `<repo>` is
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

A settings file of the gc pack does not load as it is. Its keys are refused
by name, each with the key that replaced it:
`settings: city is the gc pack's key; this bridge reads yard (YARDR.md)`,
and so for `agent` (`crew`), `gc_base_url` (`[yardr] hook_url`) and
`gc_api_token_file` (`[yardr] hook_secret_file`); a `[[topic]]` `agent`
refuses as `settings: topic entry <n>: agent is the gc pack's key: name the
crew member with crew`.

| key | required | meaning |
| --- | --- | --- |
| `yard` | yes | the yard's name: in the default unit name, the unit's description and every conversation record in the ledger |
| `state_dir` | yes | ledger, message log, inbound offset, inbound files, heartbeat |
| `port` | yes | the yard API's loopback port, 1024-65535, unique per bridge on a host; the source's `base` names it (`yardr/SETUP.md`, step 5) |
| `token_file`, `callback_secret_file` | yes | owner-only credential files; the callback secret is the yard API's bearer token (`install.sh` creates a missing callback secret: the unit cannot write outside its state and lock directories) |
| `[yardr]` `hook_url` | yes | the yard's hook: `http://<host>:<port>/hooks/<name>`, the host `127.0.0.1`, `::1` or `localhost`, with no user, query or fragment, at most 256 characters |
| `[yardr]` `hook_secret_file` | yes | owner-only file holding the hook's secret, sent as `X-Tgbridge-Secret`; a file other than the token and the callback secret (`install.sh` creates a missing one, as for the callback secret) |
| `[yardr]` `source` | no | the declared source's name in the yard, in the lines `topics` prints; default `tgbridge` |
| `[bot]` `bot_id`, `user_id`, `chat_id`, `account_id` | yes | positive numeric ids (`chat_id` equals `user_id`: a private chat), the bot username as the account |
| `lock_dir` | no | where the per-bot lock lives; default the token file's directory |
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
| `[reactions]` | no | `on_accept`, `seen`, `done`: the `seen` reaction on each accepted message and the emoji `react` sets ("Reactions" below); no reaction ever without `on_accept = true` or a `react` call |
| `[typing]` | no | `seconds` (0-900, default 0): "typing..." in the chat after each accepted message until the next send, at most this long ("Typing" below); no chat action ever with 0 |
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
  naming the settings' port, and the link to the active release's one, which
  the yard applies (`yardr/SETUP.md`, step 5).

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
   `<repo>`, one line per entry, before it writes anything;
2. stages HEAD under `releases/.stage-*`, or reuses `releases/<commit>`
   when its files match the staged sums, and refuses it when they do not;
3. creates the callback secret, the hook secret and the transcription queue
   when they are missing (each owner-only, a secret never printed). These
   are the only writes outside the root before step 4, and are kept
   whatever happens next;
4. runs the staged copy's `check-install` (python, curl, owner-only
   credentials, and with transcription on: ffmpeg, whisper-cli, the models
   and their SHA-256). A failure removes the stage and changes nothing else;
5. activates: `previous` becomes the old `current`, and `current` the
   release. It then writes the service file and the launchers, each only
   when it changed, and moves `pack` last. Every one is written beside its
   place first, and a failure puts back what was already replaced. One
   activation of a root runs at a time (a `flock` on it), and a signal waits
   until the renames or their undo are done. On Linux and WSL 2 the
   service file is the systemd unit `~/.config/systemd/user/<unit>`; on
   macOS it is the LaunchAgent plist `~/Library/LaunchAgents/<label>.plist`,
   where `<label>` is the `unit` name without its `.service` suffix. A
   replaced service file is kept in `service.previous`, and the old and
   new bridge paths are printed.

`install.sh --settings <settings> --rollback` runs from any release (for
example `<root>/current/install.sh`) and needs no git. It verifies
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

`bridge.py --settings <settings> render-unit` prints the service for
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
| `POST /v1/send` | `text`; `key`, else the `Idempotency-Key` header (required); `conversation` (an id) or `topic` (a name), default the paired chat; `format` (`auto`, `plain` or `html`; default `auto`); `silent`; `keyboard` (rows, or rows as a JSON string); `text_options`; `reply_to`; `attachments` (a host path, or a list of them); `attachment_kind` | `{delivered, message_id, conversation}`, with `reason` and `failure_kind` when not delivered |
| `POST /v1/tell` | `title`, `text`, optional `link`, `conversation` or `topic` | as `/v1/send` |
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
(YARDR.md: the yard tells with no key).

The daemon serves each request on its own thread. Its sends queue behind
its `publish_lock`, and they meet the poll loop's and the command line's in
the ledger ("Delivery recovery"). The yard holds each caller to 60 calls of
a source an hour, 10 of them updates, unless the source says otherwise
(YARDR.md, "The yardr pack"); `yardr reply` is not a call.

`yardr reply <seq>` runs the thread command `reply`: `/v1/send` on the
delivery's conversation, with the reply's `key`, `format: auto` and its
files as `attachments`. A `delivered: false` fails it with `the bridge did
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
  `yardr reply`; `/v1/tell`; `send` and `reply-current`; the
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
python3 <repo>/bridge/bridge.py --settings <settings> set-commands
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

- `{"type": "send", "text": ..., "format": "plain"|"html", "keyboard": ...}`
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
empty string or `[]` is no keyboard), and `send` and `reply-current` take
`--keyboard-file <path>` holding the rows as JSON (a JSON `null` refuses:
`keyboard file must hold JSON rows`). The caller chooses the send's key
before it sends, since every button carries an id made from it (below):
`key=` on the yard's call, `--idempotency-key` on the command line.
`yardr reply` sends no keyboard.
An ask sent in a topic records that topic's conversation, and its answer
relays there under the topic's line; any other ask's answer relays on the
paired chat's conversation with no topic line ("Topics").

**Lettered options need buttons.** A send (`/v1/send`, so also `yardr
reply`, and `send` and `reply-current`) whose
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
`send` and `reply-current` with `--keyboard-file` plus `--attachment`. A
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
python3 <repo>/bridge/bridge.py --settings <settings> ask-close --message-id <id> --decision "<text>"
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
ambiguous or not sent), prints the `GC_RECEIPT` line with its reason on
stderr and exits 1. After the write the ask stays closed and the message
says so (`ask closed; Telegram edit not confirmed: <reason>`). A not-sent
edit is sent by running the same command again; a rejected edit (the user
deleted the message, say) stays rejected, and an ambiguous one is reconciled
by hand. `/v1/ask-close` answers `{"delivered": true, "message_id": "<id>"}`,
or `delivered: false` with the same reasons.

(6) The ledger's asks table carries `decided` (0 or 1, default 0) after
`kind`. An existing ledger opens unchanged and gains the column once
when the bridge opens it, so every recorded ask reads decided=0 and keeps its
meaning. No settings key is involved.

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
python3 <repo>/bridge/bridge.py --settings <settings> send --body-file <path> --idempotency-key <stable-key>
```

`reply-current` is the same command as `send`, under the gc pack's name.
`--keyboard-file <path>` attaches an inline keyboard ("Asks" above);
`--silent` notifies with no sound ("Silent messages" below).
`pin` keeps one pinned message per slot ("Pinned messages" below).
Optional `--conversation-id` is the paired chat's id or a configured
topic's `<chat_id>:topic:<thread_id>`, exactly as the delivery names its
conversation, and `--topic <name>` names a configured topic instead: the
message lands in that topic. With neither, a send lands in General
("Topics"). `--session` is accepted and ignored: the gc pack's sending
session, which some add-ons still pass.

Every send is direct: the process that takes it, the daemon for the yard
API and the CLI process for the command line, delivers it through the
shared ledger (`publish()`), one receipt per key, whatever it carries
("Delivery recovery"). Nothing is relayed through the yard, and the yard
keeps no copy of a send; the bridge's message log does, once Telegram
confirms it ("The message log").

The command line prints `Telegram delivery confirmed; message ID <id>`. A
send that is not delivered prints one
`GC_RECEIPT {"delivered": false, "failure_kind": ..., "reason": ...}` line on
stderr and exits 1; the line keeps the gc pack's name because the sysmon
watch and pricewatch add-ons parse it. The yard API answers `delivered`,
`message_id`, `conversation` and, when not delivered, `reason` and
`failure_kind` ("The yard API").

Plain text is limited to 4096 UTF-16 units, with link previews disabled;
split longer updates into separately keyed messages. On the command line
plain text is the default and is sent as is: no Markdown or HTML parsing
occurs. The yard API's default is `format: auto` ("Formatted messages").
The `telegram-formatting` skill is the writer's guide.

### Formatted messages (`html` and `auto`)

`--format html` (on `send` and `reply-current`) and `format: html` (on
`/v1/send` and `/v1/pin`) send the body with Telegram's HTML `parse_mode`.
On the command line it is an explicit opt-in; without it nothing changes for
a caller or its ledger rows. The yard API's `format` defaults to `auto`, and
`yardr reply` always sends `auto`: HTML when the body holds a `<` or `&` and
is valid under the rules below, plain text otherwise, so `auto` never
refuses a body for an HTML rule, and one with a stray `<` goes as the words
it is. `pin --format` takes `plain` or `html` only.

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

### Silent messages (`--silent`)

`--silent` (on `send` and `reply-current`) and `silent: true` (on
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

`--attachment <absolute path>` on `send` and `reply-current` (with optional
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

**Albums.** `--attachment` given 2-10 times (on `send` and `reply-current`),
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

`yardr call tgbridge pin slot=<name> text=<text> [format=auto|plain|html]`
(`/v1/pin`), or on the host
`bridge.py --settings <settings> pin --slot <name> --body-file <path> [--format html]`,
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
0 for a new generation). A failure prints the `GC_RECEIPT` line with its
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

`bridge.py --settings <settings> topics` creates each `[[topic]]` that has
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
It exits 0 only when every `[[topic]]` has a thread id, else 1 after
printing every line. It edits and deletes nothing, and changes nothing in
the yard.

Run each printed `subscribe:` line once in the yard, before anyone writes in
that topic (`yardr/SETUP.md`, step 8); a crew member subscribed to several
topics takes repeated `--where topic=…`. A topic's message reaches only
the crew members whose subscriptions match it; one that none matches is
accepted all the same ("Inbound").

The ledger's `auto_topics` table keeps the topics the paired user created:
the thread id, Telegram's name as last seen and whether it was implicit, the
crew member, when it was first seen, and `bound_at`, which the gc pack set
and nothing reads now. No message
text is stored. A thread a `topics` row holds (a settings topic, or a row
whose name left the settings) is never an auto topic: `bridge.py topics
--record <name> --thread-id <id>` on an auto topic's thread is allowed and
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
`bridge.py --settings <settings> topics --record <name> --thread-id <id>`: it records the
thread id with no Telegram call, and is refused when the name is not a
`[[topic]]`, already has a thread id, or the id is another topic's. If the
topic is not there, run `bridge.py --settings <settings> topics --retry <name>`: it raises the
attempt and creates the topic under the new key. `--retry` is refused unless
the create was ambiguous or rejected; for one not sent it says
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
  `send` and `reply-current` take
  `--conversation-id <chat_id>:topic:<thread_id>` or `--topic <name>`, a
  `[[topic]]`'s exact name, and send there whatever the send carries (HTML,
  silent, a keyboard, attachments). `--conversation-id` also takes a served
  topic the user created; `--topic` names only a `[[topic]]`. A topic with no
  ledger row is refused before anything is sent: on the command line
  `conversation ID is not the paired private chat or a configured topic`, or
  `topic <name> is not configured or not created (run bridge.py topics)`; on
  the yard API a 400 with the first, or with `topic <name> is not
  configured, created or served`. The two flags together, or `--topic` on
  another command, is a usage error (exit 2). With neither, a send lands in
  General.
- An ask with buttons sent in a topic records the topic's conversation id in
  the ledger's asks table (`conversation_id`, its last column, NULL for the
  paired chat; an existing ledger gains it once, and every ask it holds reads
  NULL). A tap's answer relays on that conversation by the inbound rules
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
names it; a row copied from a gc bridge's state keeps the gc agent's name,
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
`send`, `reply-current` and `react` take
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

**Adopting and switching off.** `bridge.py --settings <settings> topics
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
| `seen` | `"👀"` | the emoji for "the yard has the message" (received, not read) |
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
python3 <repo>/bridge/bridge.py --settings <settings> react [--message-id <id>] --reaction seen|done|none|<emoji>
python3 <repo>/bridge/bridge.py --settings <settings> react --conversation-id <id> --reaction done
python3 <repo>/bridge/bridge.py --settings <settings> react --topic <name> --reaction done
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
...`), each with the `GC_RECEIPT` line on stderr (the yard
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

## Typing

The bridge can show "typing..." in the paired chat while the yard works on
a message, so the user knows it arrived: Telegram's `sendChatAction` with
`typing`, which shows for at most 5 seconds and clears when a message from
the bot arrives.

| `[typing]` key | default | meaning |
| --- | --- | --- |
| `seconds` | `0` | the longest "typing..." shows after the latest accepted message, an integer from 0 to 900; 0 never sends a chat action |

A value outside 0-900, a value that is not an integer, and an unknown key in
the table refuse at load naming the key. A file without the table loads
exactly as before.

With `seconds` above 0, each accepted message ("Reactions", the accept
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
and `auto` asks the configured GPU check (below) once per note. A settings
file with `method` may not also set `gpu`; an operator who wants their own
GPU decision writes it as their own `gpu_check` command. There is no
other off switch: no check at all is `method = "gpu"`. In every method
mode a GPU run that fails or runs past its GPU limit (a quarter of the
run limit, at least 30 s) is retried once on the CPU (`cpu: gpu-failed`).
Without `method`, the legacy `gpu = true` behaves as `method = "auto"`
(with the configured GPU check) and `gpu = false` as `method = "cpu"`;
setting both `method` and `gpu` refuses.

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
| `method` | `cpu` | `auto`, `cpu` or `gpu` (above); with it, `gpu` is refused |
| `gpu_check` | `<repo>/transcriber/gpu_check.py` | the GPU check command for `method = "auto"`: an existing executable regular file, writable only by its owner, owned by this user or root, in a directory owned by this user or root and writable by neither group nor others (for a symlink, both the link's and the target's directories) |
| `gpu` | `false` | legacy form without `method`: `true` behaves as `method = "auto"`, `false` as `method = "cpu"` |
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

`voice-transcribe [--model NAME|PATH] [--force] [--quiet] <file|dir>...` is the
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
2. `python3 <repo>/bridge/bridge.py --settings <file> probe` must load the
   settings without an error.
3. `python3 <repo>/bridge/bridge.py --settings <file> check-install --gpu-probe`
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

## Delivery recovery

`<state_dir>/telegram-bridge.sqlite3` stores the inbound offset and outbound
fingerprints/receipts, without message bodies, with two exceptions: the asks
table keeps an ask's question text, ordered options and answer so one tap
answers, edits and relays exactly once, and the message log keeps whole
message texts ("The message log"). Keep it across
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
and the command line prints them in its `GC_RECEIPT` line; only the reason
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

Wire references: `YARDR.md` (the hook's body and the yard API) and
`<repo>/yardr/tgbridge.yaml` (the source the yard reaches the bridge
through).

## Native run cards

`run_asks.py` owns native socket framing, payload checks, rendering and the
SQLite state machine. `bridge.py` supplies its existing ledger-backed
transport, routing, callbacks and ordinary asks. The optional `[yardr] home`
and `operator` settings must be set together. The socket is always resolved
`home/yard.sock`; check-install verifies its owner/type and current operator
mapping. No sandbox write exceptions are installed for native cards.

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
cards keep that limit. A captured single-file `skill add` uses the complete
review bundle below. Secret/setup/identity/depot_push, parameter inputs,
other carried files, pack plans and crew-add prompts still refuse before
sending, with Footplate/cards as the fallback.

Routine Approve calls ApproveAsk with weighed=false after fresh checks.
Weighty Approve only persists a pending confirmation and edits the card
with a direct-reply prompt. The paired human must reply to that bound
message, in the same chat/topic, with the exact ask number within 300
seconds. No sole-pending-card fallback exists. Forwarded/bot/wrong-owner/
wrong-topic/wrong-reply/expired/cancelled/stale/replayed confirmations cannot
approve. Mismatch/expiry clear the confirmation. Cancel restores the open
card. Decline needs the same ownership checks and no weight confirmation.
Only actual bound confirmation replies are consumed; other chat continues.
No account or device passwords are requested.

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


### Captured single-file skill review

A native `skill add <absolute-source> [--name <name>]` can add or refresh one
regular `SKILL.md`. Both source and standing destination must contain only
that file, each at most 32768 UTF-8 bytes. Extra files, directories, symlinks,
deletions and multi-file source/pack/crew proposals remain unsupported.
The source is provenance: the server installs the captured bytes even after
that source changes or disappears. No assignment, grant or account changes.

This requires the core captured-snapshot contract documented in its
`docs/api.md`, “Skill-add proposal contract”, introduced by core revision
`a79f6f8162c8`. Version labels alone are insufficient: every call checks the
actual canonical argv/command/description, complete carried manifest,
metadata, old bytes/inventory/mode/size, hashes, replacement diff, complete
quoted Does sections, truthful undo text and exact strong code weight.
Older or unfamiliar shapes refuse before any interactive send. The bridge's
metadata reader accepts name/description frontmatter using plain, JSON-style
double-quoted, YAML single-quoted or simple block strings; other YAML forms
or additional metadata keys require Footplate/cards. It adds no dependency
or permission layer. Core remains the authority for live target/holder and
operator checks under its apply lock.

The bundle shows every canonical Does line: metadata/holders, old/new
identity, complete proposed and standing bytes and the replacement diff.
Quoted strings preserve CRLF, tabs, trailing newlines, Unicode and literal
weight marks. Content is HTML-escaped inside preformatted blocks and labelled
untrusted in every envelope. It is never stripped, normalized or shortened.
A long line splits between Unicode codepoints; the *whole* envelope stays
within 3500 visible UTF16 units. An envelope/card that cannot fit refuses
before sending any bundle part. No file attachment or partial preview is
used as approval evidence.

SQLite's existing native row stores the binding/proposal digest, ordered
bundle and part hashes, stable send keys, conversation and confirmed message
IDs. Every part replies to its predecessor; the final card names the bundle
digest/count and replies to its last part. Open that quoted reply, then
follow the part reply chain to part 1. ALL parts must be confirmed before a
live Approve button is sent. A definitely unsent part retries under its
original key; confirmed parts stay sent. Ambiguous delivery stays blocked
under the existing ledger's manual reconciliation semantics, even after a
restart or a different caller key. There is no automatic duplicate-send
repair or recovery of a deleted Telegram message. Preserve the ledger.

Skill bundle digests also bind the native home directory device/inode, so
replacing a home at the same path cannot reuse an identical proposal.
Native ownership/proposal/state is reread before each part, transport retry
and final card, as well as before every decision. Changes invalidate the
binding; old/new bundles cannot mix. Approve first opens the existing
five-minute direct-reply ask-number confirmation; the tap does not install.
Decline/cancel never install. Approval recorded/pending carry, verified
installation, failure and uncertain transport remain distinct canonical
states. Polling/restart and ask_close reconcile the same native answer;
caller-supplied decision text cannot declare success. Filesystem writes and
recovery belong entirely to the core server, never the bridge.

## Ordinary native ask cards

Authenticated `POST /v1/ask-card`, CLI `ask-card` and source update `ask_card`
accept only bead/positive ask, routing, silent and a stable key (body key or
Idempotency-Key). They use the existing run_asks module and SQLite tables,
lock, paired-human checks and configured home's socket. Old rows without a
kind remain run cards. There is no caller-selected yard, operator or payload.
Unknown canonical shapes and operation-bearing/weighted asks refuse whole.
The current ordinary weight gate belongs to unsupported identity mappings;
run cards keep their bound number confirmation. Read-only 503s during core
replacement get at most three attempts with bounded delay; writes are never
automatically retried. Error bodies and HTTP errors after writes remain
ambiguous until a canonical Bead reread establishes the result.

The full question/context/options/consequences/about/recommendation/why must
fit the existing 3500-unit card bound and 100-button limit (98 options plus
Later/Decline). Buttons use the displayed stored option
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
ask, turn) for its whole life, bound only from a confirmed send receipt.
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
sends stay unbound and require reconciliation, never an automatic resend.
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
Run/skill rows retain their existing ledger upgrade and edit behavior.

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

With native `[yardr] home` and `operator` set, `/decide` is the bridge's own
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
questions, run and single-file skill proposals get a button. Secret, setup,
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
