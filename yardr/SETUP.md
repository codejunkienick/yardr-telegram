# Setting up tgbridge in a yard

**Public source, not an installable release (2026-11-14).** Public core
`52e22d57` proved basic chat offline; v0.17.0 refuses `thread.formats` even
without native keys. Installable use waits for a compatible public release
and the [exact-candidate check](../docs/public-readiness.md#public-core-release-gate).
Other hashes below are local baselines, not public install targets.
Steps 1–11 are the basic slice: omit native home/operator and native-card
grants. Steps 12–14 are optional; unsupported cards refuse whole. See the
[per-build limits](../docs/public-readiness.md#capabilities-and-grants).

One bridge serves one bot in one yard; several bots may serve the same yard.
The examples use a fictional yard
`example`, operator `operator`, timezone `UTC`, bridge port 8941 and hook
port 8795. Choose your own names, home, ports and timezone; every instance
has its own settings, bot credentials, state, source/hook, port, unit and
install root. Bridges in one yard share that yard's home/socket. See
[Add a second crew member's bot](#add-a-second-crew-members-bot).
Run every `yardr` command with that yard's `YARDR_HOME` explicitly set:

```sh
export YARDR_HOME="/absolute/path/to/your/yard"
```

This selects an existing yard, not another bridge's settings or credentials.
For upgrades, use [the migration procedure](#move-an-existing-bridge-to-its-own-installation)
first; the initial key/hook steps below are for a new installation.

Each step says who does it: (you) in Telegram, (shell) in your terminal,
(agent or shell) for what an agent may also run.

Two pollers cannot share a bot: a bot an existing bridge still polls is
taken over only after that bridge's unit is stopped.

## 1. The bot (you)

In @BotFather send `/newbot`. Open a
private chat with it and send `/start`. For topics, turn topic mode on in
the bot's settings, and leave "users may create topics" on if
`[auto_topics]` will be enabled.

Save the token owner-only, typed or pasted into no command line:

```sh
install -d -m 700 ~/.config/example-telegram
install -m 600 /dev/stdin ~/.config/example-telegram/telegram-bot-token
```

Paste, Enter, Ctrl-D.

## 2. The settings (agent or shell)

Read the ids with the script in README.md ("Adopt in a yard"), then
write `~/.config/example-telegram/telegram-bridge.toml` (mode 600) from
`settings.example.toml`:

```toml
yard = "example"
state_dir = "~/.local/state/example-telegram"
port = 8941
token_file = "~/.config/example-telegram/telegram-bot-token"
callback_secret_file = "~/.config/example-telegram/callback-secret"
user_display_name = "Operator"
timezone = "UTC"
# crew = "yardmaster"

[yardr]
hook_url = "http://127.0.0.1:8795/hooks/tgbridge"
hook_secret_file = "~/.config/example-telegram/hook-secret"

[bot]
bot_id = <getMe id>
user_id = <the /start message's from.id>
chat_id = <the same>
account_id = "<getMe username>"
```

Add `[[menu]]`, `[[command]]`, `[[topic]]`, `[reactions]`, `[typing]` and
`[transcribe]` tables as the example shows. Moving a bot from an existing
bridge: copy that bridge's state directory to `state_dir` first, so the
offset, the ledger, the pins and the topics' thread ids carry over, and
rename each `[[topic]] agent` to `crew`.

## 3. Install (shell)

Create this instance's private state directory first (use your `state_dir`
if it differs). Preflight needs it or its parent to exist, including on a
fresh home with no `~/.local/state` yet.

```sh
install -d -m 700 ~/.local/state/example-telegram
<checkout>/install.sh --settings ~/.config/example-telegram/telegram-bridge.toml
```

Run it from a checkout with no tracked changes: it installs that HEAD as
this yard's own copy under `~/.local/share/yardr-telegram/example-telegram`
(`--root` names another root). It mints the callback secret and the hook
secret (owner-only, never printed), checks the host from the copy, points
the root's `current` at it, and renders `example-telegram.service`, the
launcher `bin/telegram-bridge` and the yard's pack `pack` there (step 5).
Start nothing yet. The checkout is no longer what runs: install again
after a pull to update this yard, and `install.sh --settings <file>
--rollback` returns it to the release before.

## 4. The yard's hook listener (shell)

First check `yardr settings get hook_addr`. Keep an existing loopback listener
and use its actual port in `hook_url`; set a new unused port only if needed.
The commands below restart that yard, so schedule them with its operator.

```sh
yardr settings set hook_addr 127.0.0.1:8795
yardr server stop      # the next yardr command starts it again on the address
yardr doctor           # … hooks: the server takes webhooks on 127.0.0.1:8795
```

The address is loopback: nothing outside this machine reaches it.

## 5. The pack and its credential (shell)

```sh
yardr pack apply ~/.local/share/yardr-telegram/example-telegram/pack
sed 's/^/TGBRIDGE_SECRET=/' ~/.config/example-telegram/callback-secret | yardr source key tgbridge
yardr source set tgbridge calls_per_hour=600
yardr source set tgbridge updates_per_hour=300
yardr source check tgbridge               # after step 9; health answers 200
```

`<root>/pack` is the active release's `yardr/` with its `base` line
naming this settings file's port, so the source's calls go to this yard's
own bridge. install.sh renders it on every install and `--rollback`, and
`yardr pack apply tgbridge` reads it again from there. Name the env file
the source already has (`source.env_file` in
`yardr source show tgbridge --json`):

```sh
yardr pack apply <root>/pack --source-env tgbridge=<existing-env-file>
```

The first apply from `<root>/pack` moves the yard's recorded import origin
(`yardr pack list`) off the checkout: later re-applies read the installed
pack, rather than `<checkout>/yardr`.

A caller gets 60 calls of a source an hour, 10 of them updates, unless the
source says otherwise; a chat needs more. `yardr reply` is not a call and
is not counted.

## 6. The hook (shell)

```sh
yardr hook add tgbridge --source tgbridge \
  --secret-file ~/.config/example-telegram/hook-secret --unknown ignore
```

`--unknown ignore` drops what a sender the yard does not know writes.
The bridge then keeps its cursor and logs why, so no message is lost
while step 7 is missing.

## 7. Who writes (shell)

```sh
yardr user identity <operator> --source tgbridge --id <bot.user_id>
```

Each delivery then carries `speaker=<operator>`, and the yardmaster takes it as
your word, as your mail.

## 8. Who answers (shell)

`yardr reply` runs the source's reply with the replying crew member's own
credential. Put the same `TGBRIDGE_SECRET` line in the yardmaster's env
file, mode 600 (`yardr crew show yardmaster` names it; set one with
`yardr crew set yardmaster env_file=<file>` if it has none), and
restart it:

```sh
umask 077
sed 's/^/TGBRIDGE_SECRET=/' ~/.config/example-telegram/callback-secret >> <the yardmaster's env file>
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
B=~/.local/share/yardr-telegram/example-telegram/bin/telegram-bridge   # carries --settings
$B probe
systemctl --user daemon-reload && systemctl --user enable example-telegram.service
systemctl --user restart example-telegram.service   # starts it, or restarts it after an install
$B set-commands
$B topics          # creates the [[topic]]s once; run each subscribe: line
```

## 10. Telling (shell, optional)

What lands in your inbox reaches the chat, in General or a topic
([whole mail](../bridge/OPERATIONS.md#whole-mail)):

```sh
printf 'TGBRIDGE_CONVERSATION=%s\n' <chat id, or <chat id>:topic:<thread id>> | yardr user key <operator> tgbridge
yardr user set <operator> notify=tgbridge
```

## 11. Try it (you, then the yardmaster)

Write to the bot. The yardmaster's inbox shows

```
hook (all) tgbridge message create by Operator on "Telegram": …
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

In the bridge's existing private settings, name the one card owner under `[yardr]`:

```toml
home = "~/.yardr"          # the exact yard this bridge serves
operator = "<operator>"
cards = true             # exactly one owner in this yard
```

The home must exist, belong to the service user, and have no group/world
write permission; `yard.sock` is the only native endpoint (no socket
override). `[reactions] read` also needs `[yardr] home` (it follows the
native `GET /events` stream for `inbox_read` and `replied`, falling back
once-logged to a 5 s open-row poll on cores without `inbox_read`);
`nudge_after` needs `read`, `operator`, built-in `/nudge`, and
`NudgeSession` (off otherwise).
Every instance can read with `home` alone; `cards = false` keeps all native
cards and approvals off while allowing message states and operator nudges. The operator
must keep their role and source identity, checked at every interaction.

After reviewed code is installed, run `install.sh --settings <file>`,
`<root>/bin/telegram-bridge check-install`, and restart the service. The
unit sandbox stays unchanged; `check-install` verifies socket ownership and
operator mapping. Settings, source apply, grants and restart are this operator
step. Live check for `nudge_after`: send to an idle crew without acking, wait
`nudge_after` s, tap Nudge and check the `session_nudged` reply.

Create a fresh native proposal, then publish its returned ask number:

```sh
yardr bead ask <bead> "Lower this source's call limit?" \
  --context "The synthetic source no longer needs the higher limit." \
  --run "source set example calls_per_hour=60"
yardr call tgbridge run_ask bead=<bead> ask=<ask> topic=general key=native-example-1
```

The asking crew proposes the operation; the paired human approves it on the
card. A routine card needs one verified tap; a weighty one takes Approve, then
Confirm on the same card within 60 seconds. Typed numbers never approve, and
Telegram never asks for a device or account password. Refused proposals go
through Footplate or the yard cards; ordinary send and tell summaries never
authorize an approval. Self-contained cards, their refusals and size limits are
in [native run cards](../bridge/OPERATIONS.md#native-run-cards).

Native `ask_close` keeps its existing selector contract:

```sh
yardr call tgbridge ask_close ask_key=native-example-1 decision=refresh
# Or: message_id=<Telegram message id>, with the same decision field.
```

For native cards the decision field is ignored: the bridge re-reads Bead and
shows only the canonical state. For ordinary chat asks it still carries the
decision text. Taking back a proposal uses
`yardr bead ask --take-back <bead> <ask> "<reason>"`.

The bridge keeps send keys and confirmations in SQLite across restarts, and
never resends an ambiguous card or native write without a fresh verified
interaction. Keep the ledger when moving or restarting the bridge: deleting it
loses these bindings and delivery history. Recovery is in
[delivery recovery](../bridge/OPERATIONS.md#delivery-recovery).

### Skill copies (shell)

Specific captured `skill add` cards refuse. Fork skills: [setup](#the-crews-skills-shell).
For unchecked General on public `5dc0bfca`/`52e22d57`, see [operator opt-in,
complete words, expiry and recovery](../bridge/OPERATIONS.md#native-general-cards).
No remote bootstrap or live activation is implied.

### Source declarations and command files

After reviewed source is installed into this instance's release, a proposer
can publish a native source-add ask with the existing per-call grant:

```sh
yardr bead ask <bead> "Add or replace this source?" \
  --context "Read every declaration, command and replacement diff part." \
  --run "source add /absolute/path/to/source.yaml"
yardr call tgbridge run_ask bead=<bead> ask=<ask> topic=general key=source-review-1
```

Native review shows every declaration, program, diff and inventory line before
the card's controls; replacements and new or changed code need Approve, then
Confirm within 60 seconds. Refusals and bounds are in
[source-add review](../bridge/OPERATIONS.md#source-add-declaration-and-command-review).
No new grant or setting is needed.

The operator installs and activates reviewed code with the existing installer,
`check-install` and the printed restart, then inspects `preview_run_asks.py
--source` and verifies identity and outcome. Rollback keeps the ledger and
bindings; withdraw open source cards first. Source landing is separate from
installation and live activation.

### Depot pack imports and activation

Depot packs need an absolute registered-main directory, an explicit matching
`--sha256` and `--depot`, and a complete native plan and mappings. One complete
Markdown document discloses every object, change, consequence, hash and carried
source or grant before the card. Limits and refusals are in
[depot pack bundles](../bridge/OPERATIONS.md#depot-pack-bundles).

The operator stops the old bridge, installs reviewed code and explicitly
records native `Head` as the global event watermark before activation. Only
asks with bound creation evidence after that watermark get controls; earlier
asks stay blocked everywhere, `/decide` included, so withdraw them and propose
fresh. Keep the ledger, documents and watermark on rollback. Run
`preview_run_asks.py --depot` first; source tests do not activate a service or
prove public compatibility. Old ask-number markers need explicit recapture at
activation. Design and activation: [depot pack
approvals](../docs/design/depot-pack-approvals.md).

### Declaration notes

The declaration's comments, the thread command protocols and the hook keys are
reference, not setup: see
[declaration notes](../bridge/OPERATIONS.md#declaration-notes).

### Group add and set

A crew member proposes a group, or a change to a group's prompt, command,
permission preset, model or effort, and the paired operator approves it
from Telegram:

```sh
yardr bead ask <bead> "Add the builders?" --context "Review every part before approval." \
  --run "group add builders --runner herdr --limit 2 --set kind=claude --set prompt=<home>/roles-in/builder.md"
yardr bead ask <bead> "Replace the planners' prompt?" --context "The new prompt is on the card." \
  --run "group set planners prompt=<home>/roles-in/planner.md"
yardr bead ask <bead> "Raise the drafters' effort?" --context "Effort only." \
  --run "group set drafters effort=high"
yardr call tgbridge run_ask bead=<bead> ask=<ask> topic=general key=group-review-1
```

`bead ask` reads the prompt file `prompt=` names (under the yard's home) and
the card carries its text. `model=` works only on yardr's aligned build. The
proposer needs nothing beyond `bead ask`: no new grant, setting, socket or
sandbox exception is added, and the yard writes the prompt file, never the
bridge.

The review document arrives before the card and quotes a new prompt whole,
with its SHA-256. A group add and a set of a prompt, command, preset or model
are weighted: Approve only opens the Confirm step, tapped on that same card
within 60 seconds. A set of the effort alone approves with one tap. A group
changed since the card is refused with nothing written. Details:
[operations](../bridge/OPERATIONS.md#native-group-add-and-set-review).

Rollout: install the aligned yardr first, then this bridge as its own release
copy (see the source update below). A group card written before the alignment
is refused, so decide it in the yard. Rollback:
`<root>/current/install.sh --settings <file> --rollback` and its `next:` line;
open group cards then fall back to Footplate or the yard cards. Approved groups
stay as the yard wrote them; undo each with its card's Back (`yardr group rm
<name>`, or the `yardr group set` line it shows).

## 13. Ordinary native decisions (operator; optional)

`ask_card` uses the same native home/operator and identity checks as step 12.
It supports option questions, questions wanting words and store-generated
manual-stage cards, and refuses run, secret, setup, identity, depot-push,
unknown or weighted payloads before posting. Run cards keep their bound
Approve, then Confirm gate.

Ordinary cards require core `7fa0d4c75ff6` or a compatible build exposing
`Bead.asks[].turn` and `AnswerAskExpected`, `DeferAskExpected` and
`DeclineAskExpected`. The isolated real integration verifies these named RPC
objects, option/word answers, and generated stage movement with note and sibling
take-back. The former `709c9ca24c94` baseline lacks the atomic guard and cannot
safely answer ordinary cards. Missing turn or method refuses without a legacy
write fallback.

Do not assume the native keys exist. The operator can check, then add only
missing keys to a private settings file without printing it:

The helper adds only `home` and `operator`; `cards` is required with them,
so set `cards = true` on the owning bridge (or `cards = false` on every other)
first. The helper preserves it and validates the resulting settings.

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
# Direct CLI, through the service's own release and ledger:
<root>/bin/telegram-bridge ask-card \
  --bead <bead> --ask <ask-number> --idempotency-key decision-1
```

The human taps Choose N, or replies to the card with `N optional words`; a
no-options ask takes a plain reply. The card shows every canonical option with
its consequences, context and recommendation, and must fit 3500 visible UTF16
units or nothing posts; more than 98 options refuse whole. Later puts the ask
aside and Decline declines it; neither answers or executes the operation. Stage
choices use AnswerAskExpected, and core checks the stage authority.

Each message presents one open turn. A changed turn sends a new card and
retires the old one, and stale replies or buttons refuse. Confirmed sends never
repeat. An ambiguous send is recovered only by the paired operator's tap on the
original card, once the reviewed release is active, and only with proof that the
card matches. Never clear the ledger or switch keys to get past it. Details:
[ordinary native ask cards](../bridge/OPERATIONS.md#ordinary-native-ask-cards).

`askcard:` is reserved for the bridge. Before upgrading, move any custom
`callback_prefix` that overlaps it (or a shorter prefix such as `askcard`) to a
distinct namespace; old buttons need operator reconciliation.

### /decide

With the native keys set, `/decide` in the paired chat lists the operator's
open decisions from the core's `UserAsks` and opens a supported one as the same
native card (`/decide <bead>#<ask>`). It needs no grant beyond step 13 and no
new setting. Run `set-commands` again after enabling native keys to add it to
the menu. A `[[command]]` or `[[menu]]` named `decide`, or a `callback_prefix`
overlapping `decide:`, refuses to load with native keys: rename it first.
See [the operations guide](../bridge/OPERATIONS.md#decide-the-operators-open-decisions).

### /quota (optional)

With the native keys set, `[yardr.quota]` in the bridge's settings turns on
the native `/quota`: the yard's providers, self-reporting sources and
configured groups as one table, read from `yardr limits` with no login of
its own. Run `set-commands` again after switching; a `[[command]]` or
`[[menu]]` named `quota` must go first. See [the operations
guide](../bridge/OPERATIONS.md#native-yard-commands).

### /switch_model (optional)

With the native keys set, `[yardr.switch_model]` and its
`[[yardr.switch_model.picks]]` turn on the native `/switch_model`: the crew
member's kind, model and effort, with one button per pick. A tap by the
operator sets the crew member to the pick with `yardr crew set ...
--handoff` and waits for handoff (or Force apply). Remove any conflicting
`[[command]]` `switch_model` table in the same edit. Check with
`<root>/bin/telegram-bridge switch-model --dry-run <pick>`, which changes
nothing, then restart and run `set-commands` again. See [the operations
guide](../bridge/OPERATIONS.md#switch_model).

### Source update and independent runtime handoff

The ordinary-card code adds no installer or restart interface of its own.
Never activate a new version by restarting a shared mutable checkout: each
instance runs its own release copy under `<root>` (step 3), so update this
instance from a reviewed, clean checkout:

```sh
<checkout>/install.sh --settings <settings> --dry-run
<checkout>/install.sh --settings <settings>
<root>/bin/telegram-bridge check-install
```

Then run the `next:` line the install printed (probe, daemon-reload, enable,
restart, set-commands). To go back, run
`<root>/current/install.sh --settings <settings> --rollback` and the `next:`
line it prints; after the first migration from a checkout unit there is no
previous release, so restore `<root>/service.previous` over the service file
instead.

After a pack apply, check that the declaration offers `run_ask` and `ask_card`
and keeps its calls, credential, limits and grants. Pack check, apply and the
bindings they keep are in [The source
pack](#the-source-pack-crew-then-you-in-telegram-shell-fallback). A bridge whose
unit still runs a checkout moves first, by [Move an existing bridge to its own
installation](#move-an-existing-bridge-to-its-own-installation).

## 14. Optional native siding cards

Keep this instance's existing native home/operator/source identity and
pairing. The cards use yardr's ordinary calls (`GetDepot`, `Groups`, `Routes`,
`Settings`, `ListBeads`, `SidingBeads`, `NextStages`, `GetBead`, `FoldBeads` and
`Advance`), which the aligned yardr, plain upstream and the earlier local build
all have. A release card moves one bead on its own tap, to the stage the card
shows, and refuses a bead that changed since the card (`bridge/OPERATIONS.md`).

After independent review, the operator owns installation into this
instance's release root, supported source-pack planning/application, and
activation/restart. Check the existing native keys with step 13's helper;
retain source base, credential file, limits, grants and other settings.
Review the concrete pack plan before applying it. The sole additional
per-call grant, if this crew member should publish siding cards, is:

```sh
yardr source grant tgbridge siding_card --crew yardmaster
yardr call tgbridge siding_card depot=<depot> key=<stable-card-key>
```

No blanket grant is needed: the caller publishes status, and only the paired
human's tap folds or releases, and only the exact beads shown. Enabling it
creates no siding, flow, route or group; `bridge/OPERATIONS.md` covers subset
replies, release cards, expiry, `fold_max` and undo.

A source commit is not an installation or activation. Before activation,
review the offline previews. After the operator-owned restart, check the native
identity and a status-only request before any real action. To roll back, use the
instance-root `install.sh --rollback`, review its printed service step and
restart only that instance; keep its ledger, home, credentials, source bindings
and grants. Siding rows have their own table (`siding_fold_cards`), which the
previous release also reads.

## Add a second crew member's bot

These steps are the operator's, after the source change has landed. They add
an existing crew member `secretary` to the same fictional yard `example`.
They do not change the primary chat's topics or crew role. Use a distinct
bot, source/hook `tgsecretary`, settings, secrets, state, port, unit and
install root. Keep the same `YARDR_HOME` and existing loopback hook listener:

```sh
export YARDR_HOME="/absolute/path/to/your/yard"
yardr settings get hook_addr
yardr crew show secretary
```

1. In @BotFather run `/newbot`, create the secretary's bot, then open its
   private chat and send `/start`. Leave topic mode off for a plain chat.
   Save its new token without placing it in an argument or shell history:

   ```sh
   install -d -m 700 ~/.config/example-secretary-telegram
   install -m 600 /dev/stdin ~/.config/example-secretary-telegram/telegram-bot-token
   ```

   Paste, Enter, Ctrl-D. Use the token-file `getMe`/pairing script in
   README.md, "Adopt in a yard", with this new directory. It must report a
   different `bot_id`/username from the first bot. The same human's
   `user_id` and private `chat_id` will be identical on both bots; that is
   expected. Never use a first bot's token for a second poller, and do not
   apply core's webhook `telegram` pack to either bot.

2. Write a new owner-only settings file from `settings.example.toml`.
   Example (replace the numeric bot id and paired ids with the new bot's
   observed values, and 8795 with the existing hook listener's port):

   ```toml
   yard = "example"
   crew = "secretary"
   unit = "example-secretary-telegram.service"
   state_dir = "~/.local/state/example-secretary-telegram"
   port = 8943
   token_file = "~/.config/example-secretary-telegram/telegram-bot-token"
   callback_secret_file = "~/.config/example-secretary-telegram/callback-secret"
   lock_dir = "~/.config/example-telegram"
   user_display_name = "Operator"
   timezone = "UTC"

   [yardr]
   source = "tgsecretary"
   hook_url = "http://127.0.0.1:8795/hooks/tgsecretary"
   hook_secret_file = "~/.config/example-secretary-telegram/hook-secret"
   home = "~/.yardr"             # read this conversation crew's message states
   cards = false                # native cards belong to the one owner instance
   # operator = "<operator>"    # optional: verified /nudge and unread offers

   [reactions]
   on_accept = true
   seen = "🕊"
   read = "👀"
   # nudge_after = 300          # also needs operator and core NudgeSession

   [bot]
   bot_id = 1000000003
   user_id = 1000000002
   chat_id = 1000000002
   account_id = "example_secretary_bot"
   ```

   `lock_dir` must be the first installation's lock directory (its token
   directory by default), shared by every poller on this host: its
   `telegram-bot-<bot_id>.lock` fences each bot and the yard-home identity's
   `yardr-cards-<device>-<inode>.lock` fences native-card ownership. A distinct
   lock directory cannot. Pick an unused bridge port and a unique unit and root, and copy
   nothing of the first bot's ledger, offset, pins or topic rows. Start with no
   `[[topic]]` and no `[auto_topics]`. Add transcription or commands only with
   this instance's paths and crew; external handlers keep their own state
   separate.

3. From the clean landed checkout, dry-run first, then install and inspect:

   ```sh
   chmod 600 ~/.config/example-secretary-telegram/telegram-bridge.toml
   install -d -m 700 ~/.local/state/example-secretary-telegram
   <checkout>/install.sh --settings ~/.config/example-secretary-telegram/telegram-bridge.toml --dry-run
   <checkout>/install.sh --settings ~/.config/example-secretary-telegram/telegram-bridge.toml
   ~/.local/share/yardr-telegram/example-secretary-telegram/bin/telegram-bridge check-install
   ```

   Installation activates neither unit. The rendered `<root>/pack` names the
   source `tgsecretary` with the second port.

4. Declare the new source and hook in the same yard, with their own secrets:

   ```sh
   yardr pack apply ~/.local/share/yardr-telegram/example-secretary-telegram/pack
   sed 's/^/TGBRIDGE_SECRET=/' ~/.config/example-secretary-telegram/callback-secret | yardr source key tgsecretary
   yardr source set tgsecretary calls_per_hour=600
   yardr source set tgsecretary updates_per_hour=300
   yardr hook add tgsecretary --source tgsecretary \
     --secret-file ~/.config/example-secretary-telegram/hook-secret --unknown ignore
   yardr user identity <operator> --source tgsecretary --id <bot.user_id>
   ```

   Leave the primary source, hook and credential unchanged. Do not configure the
   operator's optional `tell` address on both sources unless duplicate inbox
   notices are wanted.

5. Give only this bot's callback credential to the secretary's environment.
   `yardr crew show secretary` names its existing `env_file`; preserve its
   other entries and replace any old `TGBRIDGE_SECRET` assignment. If it has
   no env file, choose a new owner-only file and set
   `yardr crew set secretary env_file=<secretary-env-file>`. With no old
   assignment remaining, append the new one without displaying it:

   ```sh
   umask 077
   sed 's/^/TGBRIDGE_SECRET=/' ~/.config/example-secretary-telegram/callback-secret >> <secretary-env-file>
   chmod 600 <secretary-env-file>
   for call in send react pin unpin ask_close; do yardr source grant tgsecretary $call --crew secretary; done
   yardr subscribe secretary tgsecretary --scope all --where type=message
   yardr crew restart secretary
   ```

   For a crew member that does not exist yet, add it with `yardr crew add`
   (its own `env_file` from the start) and subscribe it before its bot or
   anything else delivers to it: a new crew member's inbox starts at the
   yard's current event head, so nothing delivered before the add reaches
   it. With no daemon running the add only records the definition; the
   session starts when the daemon does.

   A `yardr reply` uses its caller's credential, so a shell test must supply this
   bot's. Do not reuse the yardmaster's environment for the secretary. Do not grant
   `run_ask`, `ask_card` or `siding_card` on the second source. Keep
   `cards = false` there: `home` enables reads and optional `operator`
   enables verified nudges without claiming the yard-wide queue or `/decide`.
   The non-owner refuses native-card calls, while
   ordinary chat asks and buttons remain available. Native approval publishing
   stays on the primary.

   `check-install` and startup report landed/read/done and nudge-offer
   configuration per instance, with off reasons. `cards` is required wherever
   home and operator are both set: `cards = true` on the owning bridge,
   `cards = false` on every other instance.

   Before rolling back to a release without `cards`, restore the settings file
   kept before the upgrade edit, then run the previous release's own script:
   `<root>/previous/install.sh --settings <settings> --root <root> --rollback`
   (`--dry-run` first). The upgraded `current` script refuses the restored
   settings before swapping. Rollback checks the older settings before
   activation; the ledger schema is unchanged.
   Installation belongs to the operator; settings take effect after restart.

6. Activate only the second bot, then check both paths:

   ```sh
   B=~/.local/share/yardr-telegram/example-secretary-telegram/bin/telegram-bridge
   $B probe
   systemctl --user daemon-reload
   systemctl --user enable --now example-secretary-telegram.service
   $B set-commands
   yardr source check tgsecretary
   yardr source check tgbridge
   ```

   Send distinct text to each bot, then a voice note if transcription is enabled,
   and check that only the intended crew receives each delivery. When both bots
   share the human's chat, read each delivery with `yardr thread show <seq>` and
   answer it with `yardr reply <seq> "<answer>"`, so the reply goes out through
   the bot that delivered the message. `/decide` stays on the primary. On the
   fork, copy the shared Telegram skills with
   [The crew's skills](#the-crews-skills-shell), then assign them to the secretary.

For an upgrade, dry-run/install with this same second `--settings`, restart
only its unit, then reapply its `<root>/pack`. Roll back its code with
`<root>/current/install.sh --settings <second-settings> --rollback`, restart
only the second unit and reapply that pack. On the first activation there is no
previous release: `systemctl --user disable --now example-secretary-telegram.service`
stops that new bot; retain its ledger and credentials for recovery. The primary
keeps running throughout. Activation, BotFather changes, credentials, grants and
subscriptions remain the operator's.

## Move an existing bridge to its own installation

For a bridge whose unit still runs `bridge.py` from a checkout. Its settings,
token, secrets, state directory, port, bot, source credential and
grants all stay as they are; what moves is the code the unit runs, the
launchers, the pack's origin and (fork-only) yard-local skill copies. Move one
instance at a time: no other instance is changed or needs to be running.
The design and what it does not promise: `docs/design/independent-instances.md`.

Placeholders: `<settings>` is the bridge's settings file, `<unit>` its `unit`,
`<root>` its install root (`~/.local/share/yardr-telegram/<unit stem>` unless you
pass `--root <root>` to every `install.sh`), `<state_dir>` its `state_dir`,
`<checkout>` the reviewed checkout, `<credential file>` the source's env file, and `<service file>`
the unit file the dry-run names (`~/.config/systemd/user/<unit>` by default). Run
every `yardr` command with this yard's `YARDR_HOME`. On macOS the service steps
are the `launchctl` ones in `bridge/OPERATIONS.md`, "Install and launch".

### Dry-run (shell)

From the reviewed, merged checkout, with no tracked changes:

```sh
git -C <checkout> status --short --untracked-files=no   # prints nothing
<checkout>/install.sh --settings <settings> --dry-run
```

It writes nothing and does not contact Telegram. Where the settings name a
`[yardr] home`, its checks read the current operator identity, or every
configured conversation crew when no operator is set, so that yard must be running. Look for:

- `would stage <commit> as <root>/releases/<commit>`;
- `would replace <service file>: bridge <checkout>/bridge/bridge.py -> <root>/current/bridge/bridge.py; settings <settings> -> <settings>; the old file is kept in <root>/service.previous`;
- an `ok` (or `WARN`) on every check line, and no `FAIL`;
- `would write pack <root>/pack (base http://127.0.0.1:<port>)`.

It also prints the whole unit it would write: that text names private paths,
so do not paste it anywhere. Stop and fix the settings, then dry-run again, on:

- `would create callback secret …` or `would create hook secret …`: a running
  bridge has both, so the settings name the wrong file, and a new secret would
  match neither the yard's credential nor its hook;
- a line naming a setting that `is inside the source` or `depends on the
  source … through …`: that path would go with the checkout. Do what the line
  says (copy a program or model out, move a file with the unit stopped, or
  point `python` at an interpreter outside the checkout). install.sh never
  rewrites the settings.

`would create <queue directory> (mode 700)` is expected where transcription is
on and the host's shared queue does not exist yet. Install, dry-run and rollback
refuse when the existing service file or `<root>/bin/telegram-bridge` names
another settings path.

### Move a settings file (shell)

Before moving a settings file, disable and stop its old instance so it
cannot return at login or reboot. Retain the old settings and installation
for rollback:

- Linux and WSL 2: `systemctl --user disable --now <old unit>`. Keep the old
  unit file in place, disabled.
- macOS (`<old label>` is the old `unit` without `.service`):

  ```sh
  launchctl bootout gui/$(id -u)/<old label>
  mv ~/Library/LaunchAgents/<old label>.plist <old root>/<old label>.plist
  ```

Give the new settings file a fresh `unit` and use a fresh root, then dry-run
and install it using this procedure. Run the installer's printed `next:`
line for the new instance: it includes `systemctl --user enable <new unit>`
and restart on Linux/WSL 2, or bootstrap of the new plist on macOS. Verify
the new instance below. Keep the old unit disabled or its saved plist out
of `~/Library/LaunchAgents`, so only the new instance starts after login or
reboot.

If verification fails or you later undo the settings move, stop and disable
the new instance before returning to the retained old settings and service:

- Linux and WSL 2:

  ```sh
  systemctl --user disable --now <new unit>
  systemctl --user enable --now <old unit>
  ```

- macOS (retain the new plist too):

  ```sh
  launchctl bootout gui/$(id -u)/<new label>
  mv ~/Library/LaunchAgents/<new label>.plist <new root>/<new label>.plist
  mv <old root>/<old label>.plist ~/Library/LaunchAgents/<old label>.plist
  launchctl bootstrap gui/$(id -u) ~/Library/LaunchAgents/<old label>.plist
  ```

Only the old instance now starts, including after login or reboot. The
release rollback steps below still apply within each root; they do not
switch between these installations. There is no flag to take over an
existing unit or root.

### Install (shell)

```sh
<checkout>/install.sh --settings <settings>
```

It copies the commit to `<root>/releases/<commit>`, checks the copy, points
`current` at it, writes the new unit (the old one goes to
`<root>/service.previous`), `<root>/bin/telegram-bridge`,
`<root>/bin/voice-transcribe` and `<root>/pack`. It starts and stops nothing:
the running bridge keeps its old code until the restart below.

### Snapshot the state (shell, optional)

With the unit stopped, so the ledger is not written mid-copy, into a new
owner-only directory (`mkdir` fails if `<snapshot>` exists: pick another):

```sh
systemctl --user stop <unit>
mkdir -m 700 <snapshot> && tar -C <state_dir> -cf <snapshot>/state.tar .
```

Keep `<snapshot>` outside the root and the checkout. A rollback does not
need it: a release reads the ledger the next one wrote. Restore it only for a
damaged state directory, with the unit stopped; it lacks every send made since.

### Restart the same unit (shell)

```sh
<root>/bin/telegram-bridge probe
systemctl --user daemon-reload
systemctl --user restart <unit>
<root>/bin/telegram-bridge set-commands
```

The same unit, the same bot: one poller, before and after.

### Verify (shell)

```sh
systemctl --user show -p ExecStart <unit>   # its argv[]= names <root>/current/bridge/bridge.py
systemctl --user is-active <unit>           # active
<root>/bin/telegram-bridge check-install    # ok release files match SHA256SUMS, ok nothing the settings name depends on the source <checkout>
yardr source check tgbridge                 # health answers 200
```

### The source pack (crew, then you in Telegram; shell fallback)

With upstream's source-pack cards and this bridge installed, propose a copy
of `yardr/` inside a registered depot's **main checkout**. The installed
`<root>/pack` and a worktree cannot be proposed; use the terminal fallback
below for those. Source landing does not install or activate a release.
The operator owns installation, pack apply and restart.

Use `pack check` to read the digest and complete plan, then:

```sh
yardr pack check <depot-main-checkout>/yardr
yardr bead ask <bead> "Apply this tgbridge source pack upgrade?" \
  --context "Read the complete declarations, programs, diffs and grants kept." \
  --run "pack apply <depot-main-checkout>/yardr --sha256 <digest>"
yardr call tgbridge run_ask bead=<bead> ask=<ask> topic=general key=pack-upgrade-1
```

The review document, its approval rules and the bounds are in
[source pack bundles](../bridge/OPERATIONS.md#source-pack-bundles). After carrying,
check the canonical ran or failed state, `source show` and `source check`. Plain
upstream refuses source and pack replacement cards, so apply replacements through
the terminal fallback there.

Keep the ledger/documents; ambiguous sends require reconciliation. See
[source pack bounds and legacy retirement](../bridge/OPERATIONS.md#source-pack-bundles).

For an older core/bridge, a refusal, or an explicit terminal rollback, keep
the terminal path below. A code rollback uses `install.sh --rollback`; a
pack rollback applies a reviewed earlier snapshot or the terminal command.

Read the bindings the source has, without printing the credential:

```sh
yardr source show tgbridge          # the rendered base of this installation
yardr source show tgbridge --json   # source.env_file is the <credential file>
yardr pack check <root>/pack
```

`pack check` plans against the current bindings and takes no binding flags.
Apply with the source's env file:

```sh
yardr pack apply <root>/pack --source-env tgbridge=<credential file>
yardr pack list                     # tgbridge is imported from <root>/pack
yardr source check tgbridge
```

The apply keeps the credential pairing, the limits, the grants
and the user identity; `yardr source show tgbridge` still shows the same base.
Later installs and rollbacks publish into `<root>/pack`, and `yardr pack apply
tgbridge` reads it from there.

### The crew's skills (shell)

Fork-only: public core lacks `skill add` and `skills=`. Skip this section and
all related skill install, upgrade and rollback steps on public core.

The operator (on a direct-add core, also a user or crew member) runs the
four adds below to copy this instance's skills into the yard's own home.
Keep `YARDR_HOME` explicitly set to this yard as above:

```sh
yardr skill add <root>/current/skills/telegram-formatting
yardr skill add <root>/current/skills/telegram-attachments
yardr skill add <root>/current/skills/telegram-voice-transcripts
yardr skill add <root>/current/skills/telegram-decisions
yardr skill list
```

These are copies under `$YARDR_HOME/skills/<name>`, independent of the
checkout and the install root. The operator gives the names to the intended
crew or groups with `yardr crew set <crew> skills=...` or
`yardr group set <group> skills=...`,
keeping their other names (or approves a [skills card](../bridge/OPERATIONS.md#native-skill-administration-cards)). A crew member reads the new
skills at its next session (`yardr crew restart <crew>`); groups use them
at their next session too.

Give `telegram-decisions` only to coordinating crew members holding the
relevant `run_ask`, `ask_card` or `siding_card` per-call grants (steps 12–14).
Message writers keep `telegram-formatting`, `telegram-attachments` and
`telegram-voice-transcripts` as appropriate. Adding a skill grants no call access.

After an install or rollback, repeat the four `skill add` commands to refresh
this yard's copies from its active release; a source commit alone does not. For a
rollback to a release without `telegram-decisions`, refresh the three skills
present there, remove `telegram-decisions` from the crew's skill list (keeping its
other names), then restart that crew member.

Harness folders such as `~/.claude/skills` and `~/.codex/skills` are shared
by every yard using that harness on the host. Never relink an entry there
to one instance's `current`. If an old shared link points into the checkout
or an install root, first migrate every yard that uses it to its own skill
copies, then retire the shared link before removing its target.

### The voice-transcribe link (shell, your choice)

An older install made `~/.local/bin/voice-transcribe` a link into a checkout;
install.sh no longer creates, changes or removes it. Each instance has its
own `<root>/bin/voice-transcribe`, and every one joins the same host queue.
Choose one:

```sh
readlink ~/.local/bin/voice-transcribe
# leave it, as long as the checkout it names stays; or one instance's own:
ln -sfn <root>/bin/voice-transcribe ~/.local/bin/voice-transcribe
# or none, so callers name <root>/bin/voice-transcribe:
rm ~/.local/bin/voice-transcribe
```

### Roll back (shell)

After a later install, when there is a previous release:

```sh
<root>/current/install.sh --settings <settings> --rollback --dry-run
<root>/current/install.sh --settings <settings> --rollback
<root>/bin/telegram-bridge probe
systemctl --user daemon-reload
systemctl --user restart <unit>
yardr pack check <root>/pack        # where it shows a change, apply as above
```

It verifies `previous` against its `SHA256SUMS`, swaps `current` and
`previous`, and returns `<root>/pack` to that release's own. A second
`--rollback` goes forward again. On the fork, refresh skill
copies as above for that release.

When the upgrade changed settings compatibility (a key the new release
requires or renamed), restore the pre-edit settings file first and run the
rollback from the previous release instead:
`<root>/previous/install.sh --settings <settings> --rollback --dry-run`, then
without `--dry-run`. The upgraded `current` script loads the settings with its
own parser and refuses the older file before swapping.

Undoing this first move, when there is no previous release (`--rollback`
says so and names `service.previous`):

```sh
systemctl --user stop <unit>
install -m 644 <root>/service.previous <service file>   # the file the refusal names
systemctl --user daemon-reload
systemctl --user start <unit>
```

The unit runs the checkout again, so it must still be there. The
pack applied above still works with it (the same port); apply
`<checkout>/yardr` with the same flags only to move the recorded origin back.
On the fork, revert skills with `yardr skill add` from
`<checkout>/skills/<name>` for this yard.
The host-wide voice-transcribe link stays the operator's choice.

### Never

- Copy the token, a secret, the credential file or the settings into the
  root, the checkout, a note or a chat: install.sh reads them where they are.
- Start a second unit, or a second `run`, for the same bot: two pollers
  conflict, and two ledgers lose track of each other's sends. Restart the
  existing `<unit>`.
- Delete the checkout, the state directory or `<root>/service.previous`
  before the verification and the pack steps pass: the undo above runs the
  checkout and its old unit.
- Re-key the source or grant the calls again: the apply
  keeps them.

## Explicit Markdown replies

Install only the reviewed merged checkout with the existing installer, then
check the installed release and follow its `next:` line. Apply `<root>/pack`
using step 5, retaining the existing env-file binding.
The declaration adds `thread.formats: [markdown, html]`; core `0035b8c13e27` or a
compatible explicit reply-format transport is required. The core alone does
not activate the source capability. No new grants, settings or Python installs
are needed for formatting. The release includes pinned parser packages and
licenses; CLI/Linux/macOS launchers all use the existing `python` setting.

Use `yardr reply <seq> --format markdown <text>` (also `-` for stdin and
`--file` for attachments), or `yardr call tgbridge send text=<text>
format=markdown --json`. Omit the flag for legacy auto. Test an ordinary
reply and visible history after activation; a refusal, timeout or unknown
outcome follows existing reconciliation rather than another key/mode.
Rollback/reapply uses the previous release and its matching rendered pack.
See [formatting](../docs/formatting.md) for the supported subset, whitespace
and exact evidence policy. Source tests are not live/client/macOS validation.
