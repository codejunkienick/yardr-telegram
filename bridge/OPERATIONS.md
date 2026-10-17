# Private mayor Telegram transport

This adapter connects a city's existing mayor to Telegram using GC 1.4.1
external messaging. It adds no LLM, task poller, approval authority, or
automatic blocker detection. The mayor sends purposeful notifications and
replies through the city. It accepts private text and media authored by the
paired numeric user only; groups, other senders, bot messages, forwarded
messages, and `/start` are skipped.

Every city runs its own bridge process from this one `bridge.py`, configured
only by its own settings file. Below, `<pack>` is this pack's directory and
`<settings>` that city's settings file.

## Settings

Prerequisites: Python 3.11+, curl, the city's GC API on loopback, a confirmed
private bot pairing, and one native **agent-name** binding to the configured
`agent` (default `mayor`) for the configured conversation. Do not add a
wildcard default route. The bot must have no webhook or other poller. `probe`
checks identity and webhook without consuming messages.

The settings file is TOML, owner-only (mode 0600), outside any repository;
`<pack>/settings.example.toml` lists every key. Without `--settings` the bridge
reads `$TELEGRAM_BRIDGE_SETTINGS`, else
`~/.config/<workspace_name>/telegram-bridge.toml`, where `workspace_name` is
gc's name for the city in `<city>/.gc/site.toml` and `<city>` is `--city-root`
(default `$GC_CITY_PATH`, `$GC_CITY`, else the working directory). A missing
file, a file others can read, an unknown key or a bad value refuses every
command with one line naming the problem; nothing falls back to a default
city.

| key | required | meaning |
| --- | --- | --- |
| `city` | yes | gc's city name, the extmsg scope |
| `state_dir` | yes | ledger, inbound offset, inbound files, heartbeat |
| `port` | yes | loopback callback port, unique per city on a host |
| `token_file`, `callback_secret_file` | yes | owner-only credential files (`install.sh` creates a missing callback secret: the unit cannot write outside its state and lock directories) |
| `[bot]` `bot_id`, `user_id`, `chat_id`, `account_id` | yes | positive numeric ids (`chat_id` equals `user_id`: a private chat), the bot username as the adapter account |
| `lock_dir` | no | where the per-bot lock lives; default the token file's directory |
| `unit` | no | systemd unit name; default `<city>-telegram.service` |
| `python` | no | interpreter for the unit and the transcriber; default `/usr/bin/python3` |
| `user_display_name` | no | the actor name on relayed messages; default `User` |
| `timezone` | no | times in `/quota` and notices; default `UTC` |
| `gc_base_url`, `gc_api_token_file` | no | loopback GC API (default `http://127.0.0.1:8372`) and its bearer token file |
| `health_file` | no | the city's health report; enables the quota-exhausted notice |
| `pairing_update_id` | no | the first offset is this plus one, only for a new ledger |
| `agent` | no | the gc agent this bridge serves; default `mayor`. Inbound is accepted only when GC routes it to this agent, the quota-exhausted notice watches this agent's session, and `send` without `--session` or `$GC_SESSION_ID` sends as it. "The mayor" in this guide means this agent; a second bridge for a second agent is in `<pack>/README.md`, "A second bridge for a second agent" |
| `[[menu]]` `command`, `description` | no | the chat's "/" menu, in order |
| `[[command]]` | no | the city's own commands and buttons, run by its own handlers ("City commands" below) |
| `handler_write_paths` | no | directories the unit lets command handlers write, besides the bridge's own |
| `[quota]` `enabled`, `gate_file`, `deepseek_key_file` | no | the built-in `/quota` (being retired); while it is off, `/quota` may be a `[[command]]` instead (below) |
| `[transcribe]` | no | voice transcription (below); off by default |

Paths may start with `~`; after that they must be absolute, normalized, and
made of letters, digits and `._@+-` only, so they are safe in a unit file.
No credential goes in the settings file, command arguments or Git.

## Install and launch

`<pack>/install.sh --settings <settings>` checks the settings and the host
(python, curl, owner-only credentials, and with transcription on: ffmpeg,
whisper-cli, the models and their SHA-256), creates the callback secret and
the transcription queue when they are missing, links
`~/.local/bin/voice-transcribe`, and renders the city's unit into
`~/.config/systemd/user/<unit>`. It is idempotent, never starts or stops a unit
and never contacts Telegram; `--dry-run` only reports. Then:

```sh
python3 <pack>/bridge/bridge.py --settings <settings> probe
systemctl --user daemon-reload
systemctl --user enable --now <unit>
systemctl --user status <unit>
```

`bridge.py --settings <settings> render-unit` prints the unit and
`check-install` runs the checks alone (`--gpu-probe` adds the GPU probe,
below; without it no transcription runs). The unit runs
`<python> <pack>/bridge/bridge.py --settings <settings> run` with the state
directory and the lock directory as its only writable paths, plus the
transcription queue with transcription on and every `handler_write_paths`
entry.

The adapter binds only `127.0.0.1:<port>`, registers its GC callback, and
refreshes the in-memory registration every ~30 seconds. At startup the
Telegram probe is retried with backoff for up to 10 minutes, so a short network
outage does not exit the process; an identity mismatch or an existing webhook
still exits at once. Other failures exit and systemd restarts the unit every
15 s without a start limit, so an outage can never leave it permanently
failed. When the unit is down, `reply-current` says so and names the restart
command. A bot polling conflict exits with status 2 and disables automatic
restart; stop the other poller/webhook, then start the unit explicitly.

After each successful `getUpdates` the bridge records
`<state_dir>/telegram-bridge-heartbeat.json`
(`{"last_poll_ok_at": <ISO-8601 UTC>, "pid": <int>}`), atomically and at most
once per 60 s; a failed write prints `poll heartbeat not saved` and polling
continues. A city's health check can judge the bridge by its age.

GC 1.4.1 cannot send a bearer header to adapter callbacks. A random capability
path in the callback secret file protects the callback along with the loopback
bind and `X-GC-Request` check. The capability is exposed through local GC
adapter introspection, so the existing local GC API/user account is the trust
boundary. Do not publish that API or adapter-registration output publicly.

## Command menu

The paired private chat shows the settings' `[[menu]]` entries, then the
`[[command]]` entries with `menu = true`, as its "/" menu, in file order.
Register it once after a change, and again at any time with the same result:

```sh
python3 <pack>/bridge/bridge.py --settings <settings> set-commands
```

It calls `setMyCommands` with scope `{"type": "chat", "chat_id": <chat_id>}`,
reads the list back with `getMyCommands` for that scope, and prints
`Telegram command menu set for the paired chat: /<command> ...` only when the
read-back equals the list exactly. A Telegram rejection or a different
read-back exits 1 with a message that carries no token or response body. It
reads the token and the settings only: no SQLite store, callback secret, bot
lock, webhook or `getUpdates` call, so it is safe while the unit runs and
needs no restart. Other chats and the bot's default menu are not set. With
nothing to show it refuses.

With `[quota] enabled = true` the bridge answers `/quota` itself, from
`aiquokka <provider> --json` per provider (DeepSeek's key from
`deepseek_key_file`) plus, when `gate_file` names the city's quota gate report,
the company account's readings; it never reaches the mayor, so it works while
the mayor is out of quota. The `/quota` reply and the quota-exhausted notice go
out as Telegram HTML (bold headers) through the bridge's own `publish()`. This
built-in is being retired: with `enabled = false` or no `[quota]` table, a city
may instead register `/quota` as an ordinary `[[command]]` (below) and its
handler answers; the built-in then never runs. A `[[command]] name = "quota"`
together with `enabled = true` refuses to load, naming both. A city's own
commands go to their handlers (below); every other command reaches the mayor as
an ordinary message.

## City commands

A city brings its own slash commands and inline-keyboard buttons through its
settings and its own handler programs, without editing this pack. Each
`[[command]]` table registers one:

| key | default | meaning |
| --- | --- | --- |
| `name` | required | the command, `/<name>`: 1-32 of `a-z`, `0-9`, `_` |
| `description` | required | the menu text, 1-256 characters |
| `handler` | one of the two | absolute path of an existing regular file this user may execute, writable by nobody but its owner |
| `url` | one of the two | `http://127.0.0.1:<port>/<path>` or `http://[::1]:<port>/<path>`, nothing else |
| `menu` | `true` | show it in the chat's "/" menu (`set-commands`) |
| `callback_prefix` | `"<name>:"` | button presses whose `callback_data` starts with it go to this handler; 1-64 printable ASCII characters, no space |
| `timeout` | `10` | seconds the handler may take, 1-30 |

The settings refuse to load, naming the entry, on an unknown key, a repeated
name, a name equal to `start` or a `[[menu]]` command, `quota` while
`[quota] enabled = true` (that refusal names the setting), a missing or
second handler, a handler that is not an owner-writable-only executable file,
a URL off loopback, a `callback_prefix` that equals or starts another one
(a press would have two owners), or more than 100 menu entries in all.
`start` stays reserved always; `quota` is reserved only while the built-in
is on.

An add-on is one `[[command]]` table, with no enable flag of its own. Add one
by adding the table (and a `handler_write_paths` entry if it writes, then
`install.sh` again), restarting the unit and running `set-commands`; remove
one by deleting the table and doing the same steps.

**Dispatch.** A message from the paired user whose text is `/<name>` or
`/<name>@<bot username>`, alone or followed by a space and arguments, goes to
that command's handler and never to GC. The bridge polls for `message` and
`callback_query` updates. A button press from the paired user (`from.id` is
`user_id`, not a bot, in the paired chat) whose `callback_data` starts with a
registered prefix goes to that prefix's handler. Any other press is answered
empty, logged as `callback dropped (update <id>): <reason>` and dropped; it
never reaches the mayor. With no `[[command]]` every press is dropped so.
Handlers run one at a time in the poll loop: a slow handler holds the next
update for up to its `timeout`.

**Handler input** is one JSON object; a field that does not apply is `null`:

```json
{"kind": "command", "command": "deploy", "args": "feature/x", "update_id": 40,
 "message_id": 1040, "callback_data": null, "callback_message_id": null,
 "received_at": "2026-10-09T08:00:00+00:00", "user_display_name": "Alex"}
```

`args` is the text after the first space (`""` when none). For a press,
`kind` is `"callback"`, `args` and `message_id` are null, `callback_data` is the
button's data and `callback_message_id` the message carrying the button. No
token, secret, raw update or GC data is passed. An executable handler is run
as `argv = [handler]` (no shell) with the JSON on stdin, its own directory as
working directory, and only `PATH=/usr/local/bin:/usr/bin:/bin`, `HOME`,
`LANG`, `TZ` (the settings' `timezone`) and `TELEGRAM_BRIDGE_COMMAND=<name>`
in its environment; at its `timeout` its whole process group is killed. A
URL handler gets the JSON as an `application/json` POST, with no proxy and no
redirect followed, the same timeout over the whole exchange, and a 200 answer
read up to 64 KiB.

**Handler output** is exactly one JSON object `{"actions": [...]}`, at most
64 KiB and five actions, each exactly one of:

- `{"type": "send", "text": ..., "format": "plain"|"html", "keyboard": ...}`
- `{"type": "edit", "message_id": <int>, "text": ..., "format": ..., "keyboard": ...}`
  (edits a message the bot sent; without `keyboard` the edit removes the buttons)
- `{"type": "answer_callback", "text": ..., "alert": <bool>}` (a press only, at
  most once; `text` up to 200 characters, both optional)

`format` defaults to `plain`; `html` follows "Formatted messages" below and
the text limit is 4096 UTF-16 units of visible text. A `keyboard` is 1-8 rows of
1-8 buttons `{"text": ..., "callback_data": ...}`; `callback_data` is 1-64
bytes and starts with one of the city's registered prefixes. The JSON is read
strictly (no duplicate keys, no `NaN`, no string escaping a lone UTF-16
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

**Sandbox.** An executable handler runs inside the bridge unit's sandbox:
`ProtectSystem=strict`, a read-only home, a private `/tmp`, and as writable
paths only the bridge's state and lock directories (which hold the ledger:
a handler must leave them alone), the transcription queue, and the
`handler_write_paths` directories, which the rendered unit appends to
`ReadWritePaths`. Add a directory there for each place a handler writes, run
`install.sh` again and restart the unit. A URL handler is the city's own
service and runs outside the sandbox. `check-install` lists every handler and
fails on a handler that is no longer a safe executable or a write path that is
not a directory.

Example, in the settings file:

```toml
handler_write_paths = ["/home/alex/example-city/var/deploy"]

[[command]]
name = "deploy"
description = "Deploy a branch"
handler = "/home/alex/example-city/bin/telegram-deploy"
```

## Send and receive

Use `gc telegram reply-current --body-file <path> --idempotency-key <stable-key>`.
Optional `--conversation-id` must match the private configured chat. The native
controller reminder omits the mandatory idempotency flag; add it. This pack
command delegates to the same transport CLI and native GC outbound endpoint.
The direct form works before pack reload:

```sh
python3 <pack>/bridge/bridge.py --settings <settings> send --body-file <path> --idempotency-key <stable-key> --session mayor
```

The caller needs the native binding. There is no separate direct Telegram send
command. Plain text is limited to 4096 UTF-16 units, with link previews disabled;
split longer updates into separately keyed messages. Plain text is the default
and is sent as is: no Markdown or HTML parsing occurs. The `telegram-formatting`
skill is the writer's guide.

### Formatted messages (`--format html`)

`--format html` (on `send` and `reply-current`, so also `gc telegram
reply-current`) sends the body with Telegram's HTML `parse_mode`. It is an
explicit opt-in; without it nothing changes for a caller or its ledger rows.

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
  format is `idempotency_key_conflict`, like another body.
- GC's outbound relay carries no format, so an HTML send is delivered by the
  CLI process itself through the same `publish()` and ledger the daemon uses,
  like an attachment send. GC keeps no transcript entry of it; a health check
  that counts replies must read the delivery ledger too.

### Outbound attachments

`--attachment <absolute path>` (with optional
`--attachment-kind photo|video|document|voice|video_note`, inferred from the
extension otherwise: photo, video or document, never voice or video_note) sends
a file with the body as its caption: photos up to 10 MB, videos, documents,
voice notes (`sendVoice`, OGG/Opus) and round video notes (`sendVideoNote`, a
square MP4 of at most 60 s) up to 50 MB, a caption of at most 1024 UTF-16
units. A video note has no caption: its body must be empty and any text is
refused before sending (`video_note_takes_no_caption`). The bridge does not
probe the media; a wrong format or shape is Telegram's 4xx and takes the text
fallback. Like an HTML send it is delivered by the CLI process through the
shared ledger, one receipt per key (sent, not sent, or ambiguous). A path
containing `;` or `,` is refused. A file Telegram rejects falls back to one
plain text message carrying the caption (if any) and
`[attachment rejected: <reason>]`.

Inbound must receive a matching durable native GC transcript entry and a route
to the configured `agent` (default `mayor`) before advancing the SQLite offset.
HTTP 200 alone is insufficient: unrouted or hydration-pending responses retain
the cursor. Retried inbound retains the same provider message ID for
deduplication. GC is responsible for routing and waking that agent. This
confirms durable acceptance, not that the agent has answered. Initially the
cursor begins after the pairing message. A failed GC call retains the cursor,
even for a permanent rejection or an entry routed to any other agent, so an
operator can fix the binding without losing an authorized message. Diagnose
repeated transport failures instead of erasing state.

## Inbound media relay

An authorized private photo, document, video, voice, or audio message is
downloaded (photo: the largest size) via Telegram's `getFile` before the
message is relayed to the mayor, the same durable-inbound-before-cursor-
advance guarantee as text. Files land under
`<state_dir>/telegram-inbound/<YYYYMMDD>/<message_id>-<name>` (directory
mode 0700, file mode 0600); `<name>` is the original filename sanitized to a
safe basename (no path separators, no leading dot, ASCII-safe, length-capped)
or a kind-default (`photo.jpg`, `video.mp4`, `voice.ogg`, `audio.mp3`,
`document.bin`) when none is usable. Files over 50 MB are never downloaded; the
mayor gets a skip note with the reason and `file_id` instead. A download is
retried up to 3 times; if it still fails the mayor gets a "could not be
fetched" note with the `file_id` rather than the bridge retrying forever. The
relayed text is `[attachment saved: <absolute path> | <kind> | <size> bytes |
caption: <caption or (none)>]`, with the caption text itself appended as the
message body when present. The adapter never executes, opens, or logs
attachment content. The inbound root and each date directory are refused
outright if either is a symlink, and every create, download, and delete is
realpath-checked against the state directory right before it happens, so
a symlinked root or date directory can never send a write or a delete outside
the inbound tree. The download itself lands in a temp file the bridge opens
with `O_CREAT|O_EXCL|O_NOFOLLOW` under an unpredictable name, and every
response byte flows through that one file descriptor (curl's stdout, never a
second path-based open) before an fchmod/realpath-checked rename into place.
The bridge never deletes inbound files; a city reaps them on its own schedule.

## Voice-note transcription

With `[transcribe] enabled = true`, a `voice` (or `audio`) message is
downloaded exactly like any other inbound attachment and relayed at once as
`[attachment saved: <path> | voice <m:ss> | transcribing]`; the inbound cursor
advances immediately. One background worker thread then transcribes queued
notes one at a time and relays a second message, `[voice <m:ss> transcript
(msg <id>): <transcript>] (audio: <path>)`, using provider message id
`<id>-transcript`.

The transcriber is `<python> <pack>/transcriber/adapter.py AUDIO MODEL DEVICE`,
DEVICE `cpu` (`method = "cpu"`, the default, or legacy `gpu = false`), `check`
(`method = "auto"`), `gpu` (`method = "gpu"`) or `auto` (the legacy
`gpu = true` form, below).
It prints `{"state": "running"}` when its turn in the host-wide queue starts,
then, last, one JSON line: `{"text": ...}` or `{"error": <code>}`, with
`"device": <label>` once whisper ran. It runs
`<pack>/transcriber/voice_transcribe.py`: ffmpeg to 16 kHz mono WAV, then
whisper.cpp's `whisper-cli` on the CPU (`-ng`) or the GPU (the same call
without `-ng`) with the language **forced to
English** (`-l en`; there is no auto-detect and no language setting) and Silero
VAD (`--vad -vm`), so a note without speech gives an empty text. A phrase of
two or more words repeated three or more times in a row is retried once with
no text context (`-mc 0`) and the result with fewer repeats is kept. A
transcript that is mostly not Latin script is refused as not English.

The transcriber is fenced so it never takes the host from its user: before
decoding it confines itself (and so ffmpeg and whisper-cli) to the settings'
`cpus` with `sched_setaffinity`, runs at nice 10 with best-effort I/O level 7
(`ionice -c 2 -n 7`), the agents' priority, and runs at most 4 whisper threads.
Idle priority is not used: on a busy host it starves the note. A fence that
cannot be set fails the note (`fence_failed`) rather than running unfenced.

### GPU or CPU

`[transcribe] method` chooses where notes run: `cpu` (the default) is
always the CPU (`cpu: gpu-off`), `gpu` is always the GPU with no check,
and `auto` asks the configured GPU check (below) once per note. A settings
file with `method` may not also set `gpu`, `comfyui_url` or
`comfyui_free`; a city that wants its own GPU decision, including one
based on ComfyUI, writes it as its own `gpu_check` command. There is no
other off switch: no check at all is `method = "gpu"`. In every method
mode a GPU run that fails or runs past its GPU limit (a quarter of the
run limit, at least 30 s) is retried once on the CPU (`cpu: gpu-failed`).

#### The GPU check

With `method = "auto"`, once the note's turn has started and its audio is
decoded, right before whisper, the transcriber runs the check once:

- argv is `[gpu_check]` (default `<pack>/transcriber/gpu_check.py`), no
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

The pack's default check is a small standard-library script. It uses
`nvidia-smi --query-gpu=memory.used,memory.total` only when that is found
on PATH: free MiB at least `GPU_MIN_FREE_MB` (default 2048) answers
`{"gpu": true}`, less answers `{"gpu": false, "reason": "vram-low"}`,
and when it cannot tell (no `nvidia-smi`, a non-zero exit, or an answer
that is not one well-formed line) it answers
`{"gpu": true, "reason": "unknown"}`, so the GPU is tried and a failing
GPU run retries on the CPU. That `nvidia-smi` runs in its own process
group, reads at most 4 KiB in at most 5 s, and is killed by process group
on timeout or overrun. No vendor tool runs in the bridge, the
adapter or the method paths of the transcriber itself.

#### The legacy `gpu = true` form

A `[transcribe]` table without `method` keeps the form it had: with
`gpu = false` every note runs on the CPU exactly as before: `-ng`, and
the bridge starts the adapter with `CUDA_VISIBLE_DEVICES` empty. With
`gpu = true` the transcriber picks the device per note, once
the note reaches the front of the host-wide queue (and its audio is
decoded), and never disturbs a ComfyUI render on the same card:

1. It reads ComfyUI's local API at `comfyui_url`: `GET /queue`. Anything in
   `queue_running` or `queue_pending` means the CPU (`comfy-busy`).
2. Idle: `GET /system_stats`, its one CUDA device's `vram_free` less
   `torch_vram_free` (memory ComfyUI's allocator still holds and counts as
   free). At least `gpu_min_free_mb` MiB means the GPU, less means the CPU
   (`vram-low`). With `comfyui_free = false` (the default) ComfyUI's models
   are never unloaded.
3. With `comfyui_free = true` and ComfyUI idle but short of the margin:
   `GET /queue` once more (anything in it means the CPU, `comfy-busy`),
   then at once one `POST /free` with the body `{"unload_models": true}`,
   which makes ComfyUI unload its models and keep its cached node outputs,
   so its next run only loads the model again. `GET /system_stats` is then
   read every 0.5 s for at most 10 s (inside the note's run limit, never
   added to it): the margin reached means the GPU (`gpu: comfy-freed`),
   10 s without it the CPU (`vram-low`). One POST per note at most, sent
   only right after an empty `/queue`, and nothing is reloaded or restored
   afterwards. ComfyUI has no conditional `/free`, so a job can still be
   queued in the one round trip between that `/queue` answer and the POST;
   ComfyUI applies the flag only after the item it takes next has run, so
   that job still runs with its models loaded. Any error or non-200 answer
   on the POST or the reads means the CPU (`api-error`) with no second POST.
4. ComfyUI not listening (connection refused): `nvidia-smi
   --query-gpu=memory.used,memory.total` gives the free memory instead; one
   GPU, same margin; nothing is sent to ComfyUI.
5. Right before whisper starts on the GPU it reads `GET /queue` again; busy
   (or an error) then means the CPU. The loop retry checks again too.
6. A GPU run that fails (non-zero exit, a CUDA or out-of-memory error) or
   runs past its own limit (a quarter of the run limit, at least 30 s) is
   retried once on the CPU in the same turn (`gpu-failed`); the note stays
   on the CPU from there. The bridge's run limit grows by that GPU limit, so
   the CPU retry keeps its whole time.
7. Every error, 2 s timeout, non-200 answer, answer over 1 MiB or JSON of
   another shape means the CPU (`api-error`), as does a `comfyui_url` that is
   not a literal loopback address (it is then never contacted). Only
   `GET /queue`, `GET /system_stats` and, with `comfyui_free = true`, the one
   `POST /free` of step 3 are ever sent, over plain HTTP with no proxy and no
   redirect; never `/prompt`, `/interrupt` or any other write. No audio and
   no text leave the process.

A CPU run never sees the GPU (`CUDA_VISIBLE_DEVICES` empty), whichever path
led to it. The bridge logs one line per note with the device and the
reason, never the text: `voice transcription done (msg <id>): <s> s; device
gpu`, `device gpu: comfy-freed` (after step 3 of the legacy form), `device
gpu: <reason>` or `device cpu: <reason>` with a reason of 1-32 characters
of `a-z`, `0-9` or `-` (the legacy reasons are `comfy-busy`, `vram-low`,
`api-error`, `gpu-failed` and `gpu-off`); a failure line ends the same way.
An adapter device label outside that shape makes the result `invalid output`.
Measured on an RTX 5060 Ti 16 GB with `large-v3-turbo-q5_0`: a 72 s note in
2.4 s on the GPU at about 1.1 GB of video memory, against about 54 s per
audio minute on 4 CPU threads.

The unit needs nothing new for the GPU: it does not restrict devices
(`/dev/nvidia*` stay reachable), and ComfyUI is a loopback HTTP call.
`ProtectHome=read-only` leaves CUDA no writable kernel cache under `~/.nv`;
a GPU run that fails for that reason moves to the CPU like any other GPU
failure, and the journal line says `cpu: gpu-failed`.

One transcription runs at a time on the whole host, in arrival order, whichever
city's bridge (or the `voice-transcribe` command) asked: every caller joins one
FIFO queue in `~/.local/state/voice-transcribe` (owner-only). It is not a
setting: every bridge and `voice-transcribe` run as the host user derive the
same directory, so two cities can never end up in separate queues. A caller takes a numbered ticket under
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
| `whisper_cli` | `~/work/whisper.cpp/build/bin/whisper-cli` | the whisper.cpp CLI every city on the host shares |
| `models_dir` | `~/work/whisper.cpp/models` | holds `ggml-<model>.bin` |
| `model` | `large-v3-turbo-q5_0` | measured at 4 threads: about 210 core-seconds per minute of audio; `small.en` about 50 with more slips in names |
| `vad_model` | `silero-v6.2.0` | the Silero VAD model |
| `cpus` | `""` (every CPU the bridge has) | CPU list for the transcriber, such as `"2-7,10-15"` |
| `threads` | `4` | whisper-cli threads, 1-4 |
| `timeout` | `120` | base seconds of a run, 5-300 (the decode limit, and the start of the whisper limit) |
| `timeout_per_minute` | `120` | seconds of whisper limit added per minute of audio (the bridge uses Telegram's duration, assuming 10 minutes when it gives none) |
| `method` | `cpu` | `auto`, `cpu` or `gpu` (above); with it, `gpu`, `comfyui_url` and `comfyui_free` are refused |
| `gpu_check` | `<pack>/transcriber/gpu_check.py` | the GPU check command for `method = "auto"`: an existing executable regular file, writable only by its owner |
| `gpu` | `false` | legacy form without `method`: `true` runs a note on the GPU when ComfyUI leaves it free (above); `false` is the CPU only |
| `comfyui_url` | `"http://127.0.0.1:8188"` | legacy `gpu = true` only: ComfyUI's API, `http://` on a literal loopback IP (`127.x.x.x` or `[::1]`), optional port, no path; anything else is refused |
| `gpu_min_free_mb` | `2048` | free video memory, MiB, a GPU run needs (256-131072); whisper itself takes about 1.1 GB; also the check's `GPU_MIN_FREE_MB` |
| `comfyui_free` | `false` | legacy form only: `true` asks an idle ComfyUI that holds the memory to unload its models (step 3 above); matters only with `gpu = true` |

The supported models are the `ggml-<name>.bin` entries of
`<pack>/transcriber/models.sha256` except the VAD model. `load_settings`
accepts any well-formed `model` name, but `check-install` fails an
unlisted one with `FAIL  transcribe model <name> is not one of the
supported models: <list>`:

| model | file | speed at 4 threads | language |
| --- | --- | --- | --- |
| `large-v3-turbo-q5_0` | `ggml-large-v3-turbo-q5_0.bin`, about 574 MB | about 210 core-seconds per minute of audio | multilingual, forced to English |
| `small.en` | `ggml-small.en.bin`, about 488 MB | about 50 core-seconds per minute, with more slips in names | English-only |
| `small.en-q5_1` | `ggml-small.en-q5_1.bin`, about 191 MB | not measured here | English-only |
| `base.en` | `ggml-base.en.bin`, about 148 MB | not measured here | English-only |
| `small` | `ggml-small.bin`, about 488 MB | not measured here | multilingual, forced to English |

`install.sh` creates the queue directory and checks `whisper-cli`, both model files and
their SHA-256 against `<pack>/transcriber/models.sha256`.

On failure the second message carries `transcription failed: <reason>` with
the saved file path, where the reason is one of a fixed set: `timeout`,
`transcriber exited with an error`, `invalid output`, `output too large`,
`transcriber missing (ffmpeg not on PATH)`, `transcriber missing (adapter not
installed)`, `transcriber missing (whisper-cli not found)`, `transcriber
missing (model not found)`, `transcriber queue unavailable`, `audio could not
be decoded`, `whisper-cli failed`, `transcriber could not
be confined to its CPUs and priority`, `not English speech`, or `no speech
detected`. A failed transcription never blocks inbound
delivery. Nothing the child prints is relayed or logged: the bridge log line
carries only the category, exit code, byte counts, duration and message id.
Child stdout is read through a 64 KiB cap and the process group is killed on
overrun or timeout. The file is always kept. The transcript is
whitespace-collapsed, control-character-stripped and capped at 4000
characters. Oversize audio keeps the plain skip note.

`voice-transcribe [--model NAME|PATH] [--force] [--quiet] <file|dir>...` is the
same transcriber by hand: it writes `<name>.txt` next to each audio file and
prints the text (`install.sh` links it into `~/.local/bin`). It runs on the
CPU unless `VOICE_TRANSCRIBE_METHOD` is `auto` or `gpu` (with
`VOICE_TRANSCRIBE_GPU_CHECK` for `auto`), or, with no method set,
`VOICE_TRANSCRIBE_GPU=1` is set (then it picks the device by the legacy
form above, with `VOICE_TRANSCRIBE_COMFYUI_URL`,
`VOICE_TRANSCRIBE_GPU_MIN_FREE_MB` and `VOICE_TRANSCRIBE_COMFYUI_FREE=1` for
`comfyui_free`), and prints one
`voice-transcribe: <file>: device <label>` line per file on stderr.

### Turning the GPU on for a city

After the change is integrated, on the host:

1. Add `method = "auto"` under `[transcribe]` in the city's
   `~/.config/<city>/telegram-bridge.toml` (keep it mode 0600), removing
   any `gpu`, `comfyui_url` and `comfyui_free` there; or `method = "gpu"`
   for the GPU with no check at all. A city with its own GPU decision
   also sets `gpu_check` to its own command.
2. `python3 <pack>/bridge/bridge.py --settings <file> probe` must load the
   settings without an error.
3. `python3 <pack>/bridge/bridge.py --settings <file> check-install --gpu-probe`
   proves the GPU where the bridge runs, before the restart (below). It
   skips with `method = "cpu"` (or legacy `gpu = false`) and probes with
   `method = "auto"` or `"gpu"` (or legacy `gpu = true`).
4. `systemctl --user restart <city>-telegram.service`, then send a voice
   note: the journal (`journalctl --user -u <city>-telegram.service`) shows
   `voice transcription done (msg <id>): ... device gpu`. Back out with
   `method = "cpu"` and another restart.
5. The legacy form still works unchanged for a file without `method`:
   `gpu = true`, optionally with `comfyui_free = true` when ComfyUI keeps
   its models loaded while idle and notes show `device cpu: vram-low`;
   after a restart such a note shows `device gpu: comfy-freed`, and
   ComfyUI's next run loads its model again first. That form is removed
   by a later change; new setups use `method`.

`check-install --gpu-probe [--clip <path>]` transcribes a short clip with the
pack's transcriber and `VOICE_TRANSCRIBE_GPU=1` inside a transient unit
(`systemd-run --user --wait --pipe --collect`) that carries the bridge unit's
sandbox: every `[Service]` setting of the rendered unit except how the process
starts and restarts, `ReadWritePaths` included, so the two cannot drift. The
clip goes in on stdin and is written only inside that unit's `PrivateTmp`; it
waits in the host-wide transcription queue like any note. The default clip is
`samples/jfk.wav` of the whisper.cpp checkout `whisper_cli` is built in
(`<checkout>/build/bin/whisper-cli`). It prints one line, never transcript
text: `ok    gpu probe: device gpu (<seconds> s)` (or `device gpu:
comfy-freed`: the probe decides its device like a note, so with
`comfyui_free = true` it too may ask an idle ComfyUI to unload its models);
`FAIL  gpu probe: device
cpu: gpu-failed` (the GPU failed inside the sandbox; check-install exits
non-zero); `skip  gpu probe: ComfyUI busy or card full (<reason>), try later`
for `comfy-busy`, `vram-low` or `api-error` in the legacy form; one `skip` line with
transcription off, legacy `gpu` off or `method = "cpu"`; and a `FAIL` line
for a missing clip, a missing writable path of the unit or a transcriber error.
With `method = "auto"` a GPU check that answers `false` is not a failure:
any `cpu: <reason>` it gives (including `cpu: busy` without a reason and
the default check's `cpu: vram-low`) prints `skip  gpu probe: GPU not
free now (cpu: <reason>), try later` and check-install still succeeds, as
with the legacy busy answers; `cpu: gpu-failed` and `cpu: check-error`
(a check the transcriber could not use) stay `FAIL`.

### Durable transcript queue

Each queued voice note has a `<message_id>-transcript.pending` JSON file in the
same date directory as its audio (mode 0600, written through the descriptor-
pinned date directory like the download, before the first note is relayed):
`kind`, `duration`, `message_id`, `provider_id` (`<id>-transcript`),
`audio_name` (basename only; the path is rebuilt from the pinned directory and
a name with separators is refused), `attempts`, `note_sent`, `gave_up`,
`created_at`. `note_sent` flips to true once GC confirms the first note.

On start the bridge scans the inbound date directories (the state directory
and the inbound root must be real directories; symlinked date directories and
symlinked or malformed pending files are refused) and re-enqueues every
pending file with `note_sent` true and no `gave_up`. A file whose first note
was never confirmed is left alone: that update is redelivered by the cursor and
sends the note first. If the marker cannot be written after the first note, the
inbound cursor is retained and the update is reprocessed (GC dedups the note by
provider id). A successful follow-up deletes the file. A failed relay is
retried with backoff (5 s, then 30 s); the third failure sets `gave_up`, logs
`voice transcript relay gave up (msg <id>)` and keeps the file as a marker,
never retried automatically. The relay reuses the provider id
`<id>-transcript`, so a retry after a lost acknowledgement resolves to the same
GC entry rather than a second follow-up.

## Delivery recovery

`<state_dir>/telegram-bridge.sqlite3` stores the inbound offset and outbound
fingerprints/receipts, without message bodies. Keep it across restarts and back it
up privately. A token-wide `flock` (`<lock_dir>/telegram-bot-<bot_id>.lock`)
prevents two copies of this adapter consuming one bot even with different state
directories. It cannot exclude unrelated bot clients.

A delivery key is reserved durably **before** contacting Telegram. Repeating the
same key/body returns its saved receipt, including after a crash. A reused key
with another body is rejected. Telegram lacks send idempotency: a timeout or crash
may happen after delivery. Such a receipt is `permanent` with
`ambiguous_delivery_requires_manual_reconciliation`; it is never automatically
resent. Check the private chat before using any new key and record the outcome.
A crash before the actual send is conservatively ambiguous too.

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
GC 1.4.1 passes a receipt through unchanged and never retries a publish
itself; only the reason `transport_unavailable_not_sent` means not sent, since
GC also answers `transient` (with no reason) when it cannot reach or hear from
the bridge.

**Time bound.** Each attempt is limited to 20 s (5 s to connect, curl's limits)
and the whole send, waits included, to 25 s: three attempts that never get out
take at most 3 x 5 s + 4 s = 19 s, a later attempt starts only while at least
6 s of the 25 are left, and `publish_lock` is held throughout, so other sends queue behind it.
GC waits 30 s for the publish callback, and `reply-current`/`send` wait 35 s
for GC. A send queued behind another, or an HTML send whose fallback also
runs (at most 2 x 25 s, CLI only), can outlast GC's wait: GC then answers
`transient` without a reason and the bridge still finishes the send under the
key, so a retry with the same key returns the real receipt.

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
The adapter logs lifecycle/failure categories, never text, tokens, or callback URLs.

Validate locally with (fake Telegram, fake GC, fake whisper-cli; no network):

```sh
python3 -m unittest discover -s <pack>/bridge
```

Wire references: [GC v1.4.1 adapter](https://github.com/gastownhall/gascity/blob/v1.4.1/internal/extmsg/http_adapter.go),
[wire types](https://github.com/gastownhall/gascity/blob/v1.4.1/internal/extmsg/types.go),
[HTTP API](https://github.com/gastownhall/gascity/blob/v1.4.1/internal/api/huma_types_extmsg.go).
