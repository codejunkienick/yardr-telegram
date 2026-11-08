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

## 12. Native run cards (operator, optional)

First install this source declaration (step 5). Give only the coordinating
crew the new update call; the source pack contains no grants. If the operator
user does not exist yet, first run `yardr user add <operator>`:

```sh
yardr source grant tgbridge run_ask --crew yardmaster
yardr user set <operator> role=operator
yardr user identity <operator> --source tgbridge --id <bot.user_id>
```

In the bridge's existing private settings, add both keys under `[yardr]`:

```toml
home = "~/.yardr"          # the exact yard this bridge serves
operator = "<operator>"
```

The home must exist, belong to the service user, and have no group/world
write permission. Its resolved `yard.sock` is the only native endpoint;
there is no socket override or caller-selected yard. The operator must be
a current operator user with the current source identity, checked at every
interaction. The same Telegram id in another yard gives no access here.

After reviewed code is installed, run `install.sh --settings <file>` and
`python3 bridge/bridge.py --settings <file> check-install`, then use the
normal service restart for that bridge. The installer imports `run_asks.py`
from beside `bridge.py`, including its runpy and absolute service launch
modes; keep the checkout together. The unit's existing sandbox restrictions
stay in place: no yard database/config/credential write access is added.
`check-install` checks the socket owner/type and the actual operator mapping.
Live settings, source apply, grants and restart belong to this operator
step, after code review.

Create a fresh native proposal, then publish its returned ask number:

```sh
yardr bead ask <bead> "Lower this source's call limit?" \
  --context "The synthetic source no longer needs the higher limit." \
  --run "source set example calls_per_hour=60"
yardr call tgbridge run_ask bead=<bead> ask=<ask> topic=general key=native-example-1
```

The asking crew proposes the operation; the paired human approves it. A
routine card needs one verified tap. A weighty card first shows a prompt:
reply **directly to that card** with its exact ask number within five
minutes. A free-standing number never confirms anything. Cancel clears the
confirmation; Decline records a native decline without execution. Telegram
never asks for a device or account password.

Self-contained native run asks retain their original behavior. Secret,
setup, identity,
depot-push, parameter input, carried source/pack files and crew-add prompt
payloads are refused before sending, except the verified single-file skill
snapshot described below. Self-contained cards over 3500 visible UTF16 units
are refused whole. Use Footplate or the yard cards for refused proposals;
ordinary send/tell summaries cannot authorize an approval.

Native `ask_close` keeps its existing selector contract:

```sh
yardr call tgbridge ask_close ask_key=native-example-1 decision=refresh
# Or: message_id=<Telegram message id>, with the same decision field.
```

For native cards the decision field is ignored: the bridge re-reads Bead,
refuses a still-open ask, and shows only the canonical state/result. For
ordinary chat asks it continues to carry the decision text. Taking back a
proposal uses `yardr bead ask --take-back <bead> <ask> "<reason>"`.

The bridge keeps send keys and confirmations in SQLite across restarts.
It never resends an ambiguously delivered card or retries a native write
without another verified interaction and fresh canonical read. After a
lost response it re-reads Bead; an unresolved outcome says "the yard did
not answer; read the card again". Approval recorded with execution pending
is shown as "approved, to be carried out", then refreshed on polling or
`ask_close`. Failed execution and later outcome edits remain distinct.
Long canonical outcomes are delivered whole in bounded, keyed messages.
Keep the ledger when moving/restarting the bridge; deleting it loses these
bindings and delivery history.


### Single-file skill snapshots

After the reviewed core and bridge are installed, propose a fresh skill
snapshot and publish its returned ask number with the same per-call grant
and pairing above:

```sh
yardr bead ask <bead> "Refresh the reviewed skill?" \
  --context "Review all quoted file and replacement diff parts before approval." \
  --run "skill add /absolute/path/to/release-notes --name release-notes"
yardr call tgbridge run_ask bead=<bead> ask=<ask> topic=general key=skill-review-1
```

Minimum core capability is the captured single-file contract introduced at
`a79f6f8162c8`, checked from the actual per-call payload, not inferred from a
version string. Core and bridge must agree on complete canonical review,
manifest, bytes/digests, old inventory, metadata, code weight and pending/
settled carry semantics. Hook-only configurations still need no native keys.
No new grant or filesystem sandbox exception is needed. Installation, source
updates, grants and service restart are the coordinator/operator's reviewed
steps; builders test only synthetic homes, never install their worktree.

Only one regular textual SKILL.md (at most 32 KiB on either side) is
supported; extra source/standing files and deletion are refused. Frontmatter
is limited to name/description with plain, quoted or simple block strings;
other metadata/YAML requires the native full client. The complete canonical
review, proposed/standing files and replacement diff arrive as numbered,
escaped, untrusted review parts before the approval card. Every part and the
card name the same bundle digest; the card replies to its final part and
that part replies to its predecessor. Follow those replies to review all
parts. Long lines and Unicode split safely without changing captured bytes.

The first Approve tap only asks for a direct reply with the exact ask number
within five minutes; only the paired operator can confirm. The source is a
captured snapshot, so later source edits/deletion do not change approval.
Changed standing state or holders are refused by core. Approval recorded
and pending carry are separate from verified installed success or failure;
check the canonical outcome after uncertainty. There is no automatic undo.

Repeat the same run_ask call after a definitely unsent part: confirmed parts
are retained, missing parts resume, and no button appears until complete.
An ambiguous part/card is never blindly resent, even with another key or
a restart; use existing delivery-ledger reconciliation with the operator.
Keep SQLite state. Details and rendering limits are in
[operations](../bridge/OPERATIONS.md#captured-single-file-skill-review).

## 13. Ordinary native decisions (operator; optional)

`ask_card` uses the same native home/operator and identity checks as step 12.
It supports ordinary option questions, questions wanting words, and actual
store-generated manual-stage cards. It refuses run, secret, setup, identity,
depot-push, unknown or weighted payloads before posting. In the current core,
ordinary weight belongs to identity mapping, which requires a different review;
no unconditional `weighed` approval is sent. Run cards retain their existing
bound ask-number confirmation.

Ordinary cards require core `7fa0d4c75ff6` or a compatible build exposing
`Bead.asks[].turn` and `AnswerAskExpected`, `DeferAskExpected` and
`DeclineAskExpected`. The isolated real integration verifies these named RPC
objects, option/word answers, and generated stage movement with note and sibling
take-back. The former `709c9ca24c94` baseline lacks the atomic guard and cannot
safely answer ordinary cards. Missing turn or method refuses without a legacy
write fallback. The skill-review baseline above is unchanged.

Do not assume the native keys exist. The operator can check, then add only
missing keys to a private settings file without printing it:

```sh
python3 scripts/native_settings.py --settings <private-settings> \
  --home <this-instance-home> --operator <operator>
# Review the check, then use exactly the same arguments with --apply.
python3 scripts/native_settings.py --settings <private-settings> \
  --home <this-instance-home> --operator <operator> --apply
```

Both keys must match if already present. Conflicts, unfamiliar table spelling,
unsafe ownership/modes and a pre-existing backup refuse. Apply preserves all
other bytes, keeps an owner-only `<settings>.before-native` backup, and replaces
the file atomically under an instance-local lock. Check mode writes nothing.
Keep the backup private; it contains the original settings. This helper neither
starts a service nor changes source configuration or roles.

After the operator has reviewed the source pack update, the sole new per-call
grant for ordinary cards is:

```sh
yardr source grant tgbridge ask_card --crew yardmaster
```

If this installation also lacks `run_ask`, grant it separately as in step 12.
Retain existing grants; do not grant all updates or change any builder role.
Core help confirms `source grant <source> <call> --crew <crew>`; neither this
command nor `pack apply` has a `--run` option.

Create the canonical ask first, then post it with a stable key:

```sh
yardr bead ask <bead> "Where should the links go?" --to <operator> \
  --context "The replacement is ready." --option "Move: Move the links." \
  --option "Keep: Keep them here." --recommend 1 --why "The target is ready."
yardr call tgbridge ask_card bead=<bead> ask=<ask-number> topic=general key=decision-1
# Direct CLI, through the same ledger:
python3 bridge/bridge.py --settings <private-settings> ask-card \
  --bead <bead> --ask <ask-number> --idempotency-key decision-1
```

The human taps Choose N, or replies directly to the card with `N optional
words`. A no-options ask takes a direct reply containing the answer. The bridge
keeps that reply in chat. It displays every canonical option, including You
decide only when core provides it, with every consequence, context, about-bead,
recommendation and reason. The complete card must fit 3500 visible UTF16 units;
otherwise nothing posts. The existing 100-button bound leaves at most 98
options after Later/Decline; larger option sets refuse whole. Later puts it aside natively, Decline declines it,
and neither answers or executes an operation. Polling and native ask_close
show the canonical state, including put-aside, due/reopened, taken-back and
later outcome edits. Generated stage choices use AnswerAskExpected: core checks stage
authority and moves the bead, retaining optional words on the answer note.
There is no client-side inference of stage authority from markdown or asker.

Every ordinary mutation names the input message's confirmed generation turn.
Each message presents one open turn forever. A changed open turn sends a new
card under a stable per-turn delivery key and retires the previous card without
controls. Stale replies/buttons refuse even if the retirement edit fails, and
known old-card replies stay native rather than reaching chat. A conflicting
external answer, defer, decline or take-back writes nothing; the bridge rereads
and presents canonical state. Confirmed sends never repeat; definitely unsent
sends retry on refresh; ambiguous sends remain unbound without automatic resend.
Restart preserves generation bindings. Ordinary rows without generation
bindings refuse as unconfirmed; ordinary cards were not previously released.
Run/skill/chat rows retain their existing contracts and upgrades.

`askcard:` is newly reserved for the bridge. Before upgrading, migrate any
custom `callback_prefix` overlapping it (also shorter prefixes such as
`askcard`) to a distinct namespace, updating the handler's generated callbacks.
Existing messages with old handler buttons need operator reconciliation;
do not expect those buttons to be reinterpreted as native decisions. Loading a
colliding configuration names the reserved prefix explicitly. Existing chat
`ask:` and native `runask:` contracts and old ledger rows remain supported.

### Source update and independent runtime handoff

The ordinary-card code adds no installer or restart interface. At this release,
`install.sh` still renders the checkout path; the separately planned independent
runtime installer/migration is a deployment prerequisite. Do not activate a new
version by restarting a shared mutable checkout. After that interface lands,
the operator must review its exact install/check/restart and rollback commands
for this instance. The generic code needs bridge/bridge.py, adjacent run_asks.py
and the existing runtime assets; the new optional settings helper is
scripts/native_settings.py. No other instance needs to be available.

For source-pack preparation use `yardr pack check <reviewed-release>/yardr`
(the check is the plan; there is no `pack plan` command). Check uses the
installation's current bindings and accepts no source-base/source-env flags.
The reviewed apply must explicitly retain the installation's existing
`--source-base tgbridge=<existing-alias>` and
`--source-env tgbridge=<existing-credential-file>` bindings. Existing named bases remain supported;
check the resulting declaration offers run_ask and ask_card, and retains all
existing calls, credential binding, call/update limits and grants. An isolated
real-core test applies this pack twice with an existing named base, private
credential file, 600/300 limits and a per-call grant, verifying preservation.
Do not print the credential file. Live metadata checks, apply, any missing key
setup, grants, independent install and restart belong to that instance's
operator after review; no final deployment command sequence is asserted while
the independent-runtime interface is pending.
