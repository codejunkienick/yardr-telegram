# Setting up tgbridge in a yard

One bridge serves one yard with one bot. These steps set up the personal
yard `odyssey` (home `~/.yardr`, bridge port 8941, hook listener
127.0.0.1:8795); the company yard `imd` differs where marked
(home `~/.yardr-imd`, bridge port 8942, hook listener 127.0.0.1:8796,
which it already has for its peer link). Run every `yardr` command with
the yard's `YARDR_HOME`.

Each step says who does it: (you) in Telegram, (shell) in your terminal,
(agent or shell) for what an agent may also run.

Two pollers cannot share a bot: a bot a Gas City bridge still polls is
taken over only after that bridge's unit is stopped.

## 1. The bot (you)

In @BotFather send `/newbot` (imd: a company bot of its own). Open a
private chat with it and send `/start`. For topics, turn topic mode on in
the bot's settings, and leave "users may create topics" on if
`[auto_topics]` will be enabled.

Save the token owner-only, typed or pasted into no command line:

```sh
install -d -m 700 ~/.config/odyssey-telegram
install -m 600 /dev/stdin ~/.config/odyssey-telegram/telegram-bot-token
```

Paste, Enter, Ctrl-D.

## 2. The settings (agent or shell)

Read the ids with the script in README.md ("Adopt in a yard", step 3), then
write `~/.config/odyssey-telegram/telegram-bridge.toml` (mode 600) from
`settings.example.toml`:

```toml
yard = "odyssey"
state_dir = "/home/nick/.local/state/odyssey-telegram"
port = 8941                                   # imd: 8942
token_file = "~/.config/odyssey-telegram/telegram-bot-token"
callback_secret_file = "~/.config/odyssey-telegram/callback-secret"
user_display_name = "Nick"
timezone = "Asia/Yekaterinburg"
# crew = "yardmaster"

[yardr]
hook_url = "http://127.0.0.1:8795/hooks/tgbridge"   # imd: 8796
hook_secret_file = "~/.config/odyssey-telegram/hook-secret"

[bot]
bot_id = <getMe id>
user_id = <the /start message's from.id>
chat_id = <the same>
account_id = "<getMe username>"
```

Add `[[menu]]`, `[[command]]`, `[[topic]]`, `[reactions]`, `[typing]` and
`[transcribe]` tables as the example shows. Moving a bot from a Gas City
bridge: copy that bridge's state directory to `state_dir` first, so the
offset, the ledger, the pins and the topics' thread ids carry over, and
rename each `[[topic]] agent` to `crew`.

## 3. Install (shell)

```sh
~/work/yardr-telegram/install.sh --settings ~/.config/odyssey-telegram/telegram-bridge.toml
```

It mints the callback secret and the hook secret (owner-only, never
printed), checks the host, and renders `odyssey-telegram.service`. Start
nothing yet.

## 4. The yard's hook listener (shell; odyssey only)

```sh
yardr settings set hook_addr 127.0.0.1:8795
yardr server stop      # the next yardr command starts it again on the address
yardr doctor           # … hooks: the server takes webhooks on 127.0.0.1:8795
```

The address is loopback: nothing outside this machine reaches it.

## 5. The pack and its credential (shell)

```sh
yardr pack apply ~/work/yardr-telegram/yardr
yardr source set tgbridge base=imd        # imd only
sed 's/^/TGBRIDGE_SECRET=/' ~/.config/odyssey-telegram/callback-secret | yardr source key tgbridge
yardr source set tgbridge calls_per_hour=600
yardr source set tgbridge updates_per_hour=300
yardr source check tgbridge               # after step 9; health answers 200
```

A caller gets 60 calls of a source an hour, 10 of them updates, unless the
source says otherwise; a chat needs more. `yardr reply` is not a call and
is not counted.

## 6. The hook (shell)

```sh
yardr hook add tgbridge --source tgbridge \
  --secret-file ~/.config/odyssey-telegram/hook-secret --unknown ignore
```

`--unknown ignore` drops what a sender the yard does not know writes.
The bridge then keeps its cursor and logs why, so no message is lost
while step 7 is missing.

## 7. Who writes (shell)

```sh
yardr user identity nick --source tgbridge --id <bot.user_id>
```

Each delivery then carries `speaker=nick`, and the yardmaster takes it as
your word, as your mail.

## 8. Who answers (shell)

`yardr reply` runs the source's reply with the replying crew member's own
credential. Put the same `TGBRIDGE_SECRET` line in the yardmaster's env
file, mode 600 (`yardr crew show yardmaster` names it; set one with
`yardr crew set yardmaster env_file=<file>` if it has none), and
restart it:

```sh
umask 077
sed 's/^/TGBRIDGE_SECRET=/' ~/.config/odyssey-telegram/callback-secret >> <the yardmaster's env file>
yardr crew restart yardmaster
```

Grant the calls it makes beyond replies:

```sh
for call in send react pin unpin ask_close; do yardr source grant tgbridge $call --crew yardmaster; done
```

Then route the chat. General first, then each topic's line as
`bridge.py topics` prints it (step 9):

```sh
yardr subscribe yardmaster tgbridge --scope all --where type=message --where topic=general
```

A crew member subscribed to several topics takes repeated `--where
topic=…`, which match any one of them. A topic you create in the chat is
routed by its thread id (`--where thread=<id>`, as `topics` prints it),
so renaming it keeps its route.

## 9. Start (shell)

```sh
B=~/work/yardr-telegram/bridge/bridge.py S=~/.config/odyssey-telegram/telegram-bridge.toml
python3 $B --settings $S probe
systemctl --user daemon-reload && systemctl --user enable --now odyssey-telegram.service
python3 $B --settings $S set-commands
python3 $B --settings $S topics          # creates the [[topic]]s once; run each subscribe: line
```

## 10. Telling (shell, optional)

What lands in your inbox reaches the chat, in General or a topic:

```sh
printf 'TGBRIDGE_CONVERSATION=%s\n' <chat id, or <chat id>:topic:<thread id>> | yardr user key nick tgbridge
yardr user set nick notify=tgbridge
```

## 11. Try it (you, then the yardmaster)

Write to the bot. The yardmaster's inbox shows

```
hook (all) tgbridge message create by Nick on "Telegram": …
```

and `yardr reply <seq> "<b>Got it.</b>"` answers in the same chat or
topic, in Telegram HTML when the text is valid HTML. A message over 500
characters (a long voice transcript) is cut in the inbox line; `yardr
thread show <seq>` reads it whole from the bridge.
