# telegram pack

One private Telegram bridge for any Gas City: the mayor's chat with its paired
user (inbound text and files, outbound text, HTML formatting and attachments,
English voice transcription), and the skills an agent needs to use it. Each
city runs its own bridge process from this one copy of the code, configured
only by its own settings file; cities share the code and the host's whisper.cpp
build and models, never a process, a ledger or a bot.

| Path | What |
| --- | --- |
| `bridge/bridge.py` | the bridge: `run`, `probe`, `send`, `reply-current`, `set-commands`, `render-unit`, `check-install` |
| `bridge/OPERATIONS.md` | the operations guide: settings, launch, delivery recovery, transcription |
| `bridge/test_*.py` | `python3 -m unittest discover -s bridge` (fake Telegram, GC and whisper-cli; no network) |
| `settings.example.toml` | every settings key, with example values |
| `install.sh` | checks a city's settings and host, links `voice-transcribe`, renders its unit |
| `systemd/telegram-bridge.service.in` | the unit template |
| `transcriber/` | `voice_transcribe.py` (whisper.cpp, English only), `adapter.py` (the bridge's contract), `models.sha256` |
| `commands/reply-current/` | `gc telegram reply-current`: reply in the paired chat |
| `skills/` | the shared skills: `telegram-formatting`, `telegram-attachments`, `telegram-voice-transcripts` |

## Adopt in a city

For a city named `<city>` (gc's `workspace_name` in `<city>/.gc/site.toml`),
with this pack at `<pack>`:

1. Import the pack in the city's `pack.toml`:
   `[imports.telegram]` with `source = "<pack>"` (a local path) or the
   repository URL of this pack plus a pinned `version`. This brings the
   `reply-current` command and the skills. Re-run `gc reload` (or restart the
   sessions) for agents to see them.
2. Create a bot with BotFather, open a private chat with it and send `/start`.
   Save the token owner-only, for example
   `install -m 600 /dev/stdin ~/.config/<city>/telegram-bot-token` and paste it
   (never in a shell argument or history).
3. Write `~/.config/<city>/telegram-bridge.toml` from `settings.example.toml`,
   `chmod 600`: the city name, its own `state_dir` and `port`, the bot's and the
   user's numeric ids (`getMe`, and the `/start` update), the token file, the
   menu, and `[transcribe] enabled = true` if the host has whisper.cpp
   (`~/work/whisper.cpp` by default, models listed in
   `transcriber/models.sha256`). Every city on the host shares one FIFO
   queue, `~/.local/state/voice-transcribe` (not a setting), so two bridges
   never run whisper at once. `gpu = true` there runs a note on the GPU when
   ComfyUI (loopback, read-only unless `comfyui_free = true` lets an idle
   one be asked to unload its models) is idle and the card has room, else on
   the CPU (`bridge/OPERATIONS.md`, "GPU or CPU").
4. Bind the conversation to the mayor once (`gc extmsg bind`, agent-name
   `mayor`, the private chat id, no wildcard route).
5. `<pack>/install.sh --settings ~/.config/<city>/telegram-bridge.toml`: it
   creates the callback secret and the shared queue directory if they are
   missing, every check must say
   `ok`, and it renders `~/.config/systemd/user/<city>-telegram.service` (the
   unit may write its state directory, its lock directory and the queue).
6. `python3 <pack>/bridge/bridge.py --settings <file> probe`, then
   `systemctl --user daemon-reload && systemctl --user enable --now <city>-telegram.service`,
   then `python3 <pack>/bridge/bridge.py --settings <file> set-commands`.
7. Send a message from the chat and check the mayor received it; reply with
   `gc telegram reply-current` (the `telegram-formatting` skill).

### A second bridge for a second agent

One city can run a second bridge so another of its agents has its own chat.
It is a separate bot with a separate settings file, sharing nothing with the
first bridge but the code: its own `port`, `state_dir`, `unit`, `token_file`
and `callback_secret_file`, and `agent = "<agent>"`, the gc agent it serves.
Bind its conversation to that agent with
`gc extmsg bind --provider telegram --account-id <bot username>
--conversation-id <chat id> --agent <agent>` (step 4 with the second bot: the
chat id is the same user's, the account tells the two apart), install and
start its unit as in steps 5-6 (`unit` must differ from the first bridge's,
which the default `<city>-telegram.service` does not), and give that agent's
sessions `TELEGRAM_BRIDGE_SETTINGS=<its settings file>` in their
environment so their `gc telegram reply-current` answers through its bot. The
bridge accepts inbound only when GC routes it to its `agent`, and its
quota-exhausted notice watches that agent's session.

A city that transcribes some other way today (its own whisper.cpp command)
joins the host-wide queue only once it runs this pack's transcriber: the
bridge, or `voice-transcribe`, which `install.sh` links into `~/.local/bin`.
Until then the two do not wait for each other.

Run `install.sh` and the unit from a checkout path that stays put: the unit
names `bridge.py` by absolute path. A city that copies the pack runs its copy;
a city that imports it by path runs the shared one. Moving a city to this pack
from an older bridge copy: point `state_dir` at the old state directory so the
ledger, inbound offset and files carry over, keep the same `port`, and stop
the old unit before starting the new one (two pollers on one bot conflict).

## Skills

The pack's skills are the shared base set: universal, written for any city
and any bot, naming no city, person or product. Every city that imports the
pack gets them as `telegram.<name>`.

- **A city adds its own skill** in its own `skills/<name>/SKILL.md` (or its own
  pack), never by editing this pack: for example its menu commands' meaning,
  who reads the chat, or its message house style. Its skill may point at these
  ones by name.
- **Promoting a skill into the shared set**: when a city skill turns out to be
  true for every city (a Telegram rule, not a habit of one city or user),
  rewrite it without any city, person, product or path of that city, move it
  to `skills/<name>/SKILL.md` here with a citations table of the pack files it
  relies on in `citations.md` beside it (never in SKILL.md), and delete the city
  copy in the same change. The pack's tests
  refuse a city or company name anywhere in the pack outside this README.

## Example: imd-city (one real adopter)

This repository's own city (`imd-city`) runs the pack in place: `pack.toml`
imports it as `./packs/telegram`, its settings are
`~/.config/imd-city/telegram-bridge.toml` with `state_dir` at the city's
`.runtime/attention` (the ledger, offset and inbound files of its earlier
bridge), port 8931, `unit = "imd-city-telegram.service"`, `/quota` on with the
quota gate report, and `[transcribe] enabled = true`. Its outage watchdog,
`integrations/attention/telegram-bridge/city_watchdog.py`, stays the city's
own and imports this pack's `bridge.py`.
