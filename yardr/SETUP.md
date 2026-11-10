# Setting up tgbridge in a yard

Check the [public-core release gate](../docs/public-readiness.md#public-core-release-gate)
before starting: as of 2026-11-09 no compatible public core is available for
this pack, even for hook/chat alone. The core hashes below are local
capability baselines, not public install targets. The required core changes
must be published and accepted before this bridge is published; applying
the pack also requires `thread.formats` support when native keys are omitted.

One bridge serves one yard with one bot. The examples use a fictional yard
`example`, operator `operator`, timezone `UTC`, bridge port 8941 and hook
port 8795. Choose your own names, home, ports and timezone; every instance
has its own settings, bot credentials, state, socket, unit and install root.
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
own bridge with no `base=`. The `odyssey` and `imd` keys are deprecated
compatibility aliases, retained with their original hosts. New installs use
the rendered default; no alias is needed. install.sh renders it on every
install and `--rollback`, and
`yardr pack apply tgbridge` reads it again from there.

An installation that already has an alias selected (`yardr source show
tgbridge` says `(base <alias>)`) keeps it, and names the env file it already
has (`source.env_file` in `yardr source show tgbridge --json`):

```sh
yardr pack apply <root>/pack \
  --source-base tgbridge=<existing-alias> --source-env tgbridge=<existing-env-file>
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

What lands in your inbox reaches the chat, in General or a topic:

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
`<root>/bin/telegram-bridge check-install`, then use the normal service
restart for that bridge. Each release carries `run_asks.py` beside its
`bridge.py`, which loads it from there under the service, the launcher
and the installer alike. The unit's existing sandbox restrictions
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
setup, identity, depot-push, parameter input, carried source/pack files and
crew-add prompt payloads are refused before sending, except the verified
single-file skill snapshots, herdr group cards, validated `source add`
declaration and relative command file bundles, and source-kind `pack apply`
bundles described below. Approval controls appear only after complete review
delivery; core checks authority and stale state before applying.
Self-contained cards over 3500 visible UTF16 units are refused whole. Use
Footplate or the yard cards for refused proposals; ordinary send/tell
summaries cannot authorize an approval.

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
review, proposed/standing files and replacement diff arrive in one untrusted
UTF-8 document before the compact approval card. The document and card name
the same bundle digest; the card replies to the document and shows its
SHA-256, size, plan hashes and current holders. Open the complete document
before approving. No captured bytes are changed.

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

### Source declarations and command files

After reviewed source is installed into this instance's release, a proposer
can publish a native source-add ask with the existing per-call grant:

```sh
yardr bead ask <bead> "Add or replace this source?" \
  --context "Read every declaration, command and replacement diff part." \
  --run "source add /absolute/path/to/source.yaml"
yardr call tgbridge run_ask bead=<bead> ask=<ask> topic=general key=source-review-1
```

Verified minimum contract: core `546e3fdd2127` with carried source-add
snapshots, inventory/diffs and canonical carry outcomes, plus this bridge.
The bridge validates actual envelopes on every read. Keep the existing
native home/operator/source and paired identity; there is no new grant,
setting, package install or filesystem permission. The release includes its
pinned pure-Python YAML reader. Absolute command programs, aliases/tags,
unknown fields and over-limit snapshots refuse before sending; see
[operations](../bridge/OPERATIONS.md#source-add-declaration-and-command-review).

Open the complete review document before approving. The declaration and
command files, every replacement diff and inventory/removed records are
quoted under exact hashes. The compact card names their bundle digest,
size/counts and document hash and replies to the document. Core supplies the weight: replacements and new or
changed code require a direct reply with the ask number after the first
tap; a routine commands-free add applies on Approve. Source provenance can
change or disappear after capture; the server installs the captured bytes.
A changed standing copy is stale and writes nothing. Canonical pending,
success and failed carry remain distinct; ambiguous delivery never resends.

Installation and activation belong to this instance's operator after review:
use its existing independent release installer, `check-install` and printed
`next:` service step. Source landed, release installed and service live are
separate states. Before activation, inspect `preview_run_asks.py --source`
and the isolated native gate; after activation check this instance's native
identity and review a source proposal before any approval. No source-pack
change or added grant is needed for this renderer. Roll back with the
existing instance-root installer/printed restart step, retaining its ledger,
identity and source bindings. Withdraw open source cards first; older bridge
code refuses these bundles. Builders neither install nor activate them.

### Herdr group add and set

A crew member proposes a herdr group, or a group's model and args, and the
paired operator approves it from Telegram:

```sh
yardr bead ask <bead> "Create the builders?" --context "Review every part before approval." \
  --run "group add builders runner=herdr kind=claude model=<model> limit=2 arg=--permission-mode arg=auto arg=--effort arg=medium prompt=/absolute/role.md"
yardr bead ask <bead> "Raise the planners' effort?" \
  --run "group set planners model=<model> arg=--permission-mode arg=auto arg=--effort arg=xhigh"
yardr call tgbridge run_ask bead=<bead> ask=<ask> topic=general key=group-review-1
```

Minimum: core `fc7375cf5ad4` (group proposal contract) and this bridge. The
bootstrap and grants are step 12's alone: `run_ask` granted to the crew that
publishes cards, the operator user with the bridge identity, and the native
`home`/`operator` keys. Verified against an isolated real core: the proposer
needs nothing beyond `bead ask`; the bridge reads Bead (and GetGroup, a read
like Bead, for a group that names no prompt) and calls
ApproveAsk/DeclineAsk on the yard socket as the operator, as for any run
card. No new grant, setting, socket, whole-yard write or sandbox exception is
added; the role file is written by the core server, never by the bridge.

All parts arrive before the card: every sentence of the card, each role
whole as quoted lines with its SHA-256 and bytes. The tap only opens the
prompt; a direct reply with the ask number within five minutes approves. A
group or role that changed since the card is refused by core with nothing
written. Env, credentials, logins, skills, worktrees, other runners and
prompt changes are not proposable and refuse before sending. Details:
[operations](../bridge/OPERATIONS.md#native-group-add-and-set-review).

Rollout: the coordinator installs reviewed core first, then this bridge as
its own release copy (`install.sh --settings <file>`, `check-install`, the
printed `next:` line; see the source update below); an older core never
produces these cards, and an older bridge renders a short one through the
generic run card or refuses a long one. Rollback: `<root>/current/install.sh
--settings <file> --rollback` and its `next:` line; open group cards then
fall back to Footplate or the yard cards. Groups already approved
stay as core wrote them; undo is each card's Back (`yardr group rm <name>`,
or the `yardr group set` line it shows).

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
# Direct CLI, through the service's own release and ledger:
<root>/bin/telegram-bridge ask-card \
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

Every ordinary mutation names the input message's verified generation turn.
Each message presents one open turn forever. A changed open turn sends a new
card under a stable per-turn delivery key and retires the previous card without
controls. Stale replies/buttons refuse even if the retirement edit fails, and
known old-card replies stay native rather than reaching chat. A conflicting
external answer, defer, decline or take-back writes nothing; the bridge rereads
and presents canonical state. Confirmed sends never repeat; definitely unsent
sends retry on refresh; ambiguous sends remain unbound until verified callback
reconciliation, without automatic resend.
Restart preserves generation bindings. Ordinary rows without generation
bindings refuse as unconfirmed; ordinary cards were not previously released.
Run/skill/chat rows retain their existing contracts and upgrades.

If Telegram delivered an ordinary card but its response timed out, the paired
operator can tap the intended action on that original card after the reviewed
bridge release is installed and activated. The tap recovers the message id
only when its bot, paired chat/topic, non-forwarded full text and turn keyboard
match the row and its saved ambiguous-send fingerprint. It then takes the
selected action only if the canonical ask is still open at that turn. Restart
does not change this check. Replies alone cannot recover a missing id.

The send receipt stays ambiguous, including identical `ask_card` calls after
recovery; no card is resent. Before a verified tap, `ask_close` by the original
ask key explains that the message id is unknown and asks for that tap, or a
decision in the yard followed by manually deleting the Telegram card. After
recovery it can edit the original message using canonical state. A tap missing
proof explicitly directs the operator to decide in Footplate or the yard and
delete the card manually. Use that route if the card is absent or recovery is
refused. Never clear the ledger or switch keys to bypass ambiguity. No new
settings, grants or migration are required; retain the ledger on rollback.

`askcard:` is newly reserved for the bridge. Before upgrading, migrate any
custom `callback_prefix` overlapping it (also shorter prefixes such as
`askcard`) to a distinct namespace, updating the handler's generated callbacks.
Existing messages with old handler buttons need operator reconciliation;
do not expect those buttons to be reinterpreted as native decisions. Loading a
colliding configuration names the reserved prefix explicitly. Existing chat
`ask:` and native `runask:` contracts and old ledger rows remain supported.

### /decide

With the native keys set, `/decide` in the paired chat lists the operator's
open decisions from the core's `UserAsks` and opens a supported one as the same
native card (`/decide <bead>#<ask>`). It needs no grant beyond step 13 and no
new setting. Run `set-commands` again after enabling native keys to add it to
the menu. A `[[command]]` or `[[menu]]` named `decide`, or a `callback_prefix`
overlapping `decide:`, refuses to load with native keys: rename it first.
See [the operations guide](../bridge/OPERATIONS.md#decide-the-operators-open-decisions).

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
instead. The release carries bridge/bridge.py, adjacent run_asks.py, the
runtime assets and scripts/native_settings.py. No other instance needs to be
available, and none is changed.

For source-pack preparation use `yardr pack check <root>/pack`
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
operator after review. A bridge whose unit still runs a checkout moves first,
by [Move an existing bridge to its own installation](#move-an-existing-bridge-to-its-own-installation).

## 14. Optional native siding cards (store-backed first slice)

Keep this instance's existing native home/operator/source identity and
pairing. The verified minimum here is installed native core `24bcfd0955c9`,
whose socket-only contract provides `SidingProposal`, `SidingOutcome`,
`FoldSidingExpected` and `ReleaseSidingExpected`. The bridge checks the
actual methods and complete version 1 guard schema before offering controls;
a version string alone is insufficient. All bound inputs must have
`guard=store` with no refusals. Repository-own-flow and repository-dependent
until releases remain status only, with named unguarded inputs. This release
does not complete repository-backed siding mutations or all administration.

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

These commands are checked against the installed CLI and exercised on an
isolated source/loopback bridge. No blanket grant is needed. The caller can
publish canonical status; only the paired human can approve the exact shown
fold or release. See `bridge/OPERATIONS.md` for selection replies, expiry,
withdrawal, ceiling limits, stale proposals and pruned-outcome uncertainty.
No siding, flow, route or group is created by enabling the feature.

A source commit is not an installation or activation. Before activation,
review offline previews and the isolated integration gate. After an
operator-owned install/restart, check the instance's configured native
identity and a status-only request before reviewing any real action. To
roll back, use the existing instance-root `install.sh --rollback` procedure,
review its printed service step and restart only that instance. Keep its
ledger, home, credentials, source bindings and grants. Withdraw open siding
cards first; old code cannot process their callbacks or source call. Siding
rows occupy a separate table in the same ledger so older ordinary/run card
readers remain compatible. Builder validation performs no live install,
source apply, grant, restart or Telegram action.

## Move an existing bridge to its own installation

For a bridge whose unit still runs `bridge.py` from a checkout. Its settings,
token, secrets, state directory, port, bot, source credential, alias and
grants all stay as they are; what moves is the code the unit runs, the
launchers, the pack's origin and the yard-local skill copies. Move one
instance at a time: no other instance is changed or needs to be running.
The design and what it does not promise: `docs/design/independent-instances.md`.

The placeholders: `<settings>` the bridge's settings file, `<unit>` its
`unit` (`<yard>-telegram.service` by default), `<root>` its install root
(`~/.local/share/yardr-telegram/<unit stem>` unless you pass `--root <root>`
to every `install.sh`), `<state_dir>` its `state_dir`, `<checkout>` the
reviewed checkout, `<alias>` the named base the source has selected, if any,
`<credential file>` the source's env file, and `<service file>` the unit file
the dry-run names (`$XDG_CONFIG_HOME/systemd/user/<unit>`, by default
`~/.config/systemd/user/<unit>`). Run every `yardr` command with
this yard's `YARDR_HOME`. On macOS the service steps are the `launchctl`
ones in `bridge/OPERATIONS.md`, "Install and launch".

### Dry-run (shell)

From the reviewed, merged checkout, with no tracked changes:

```sh
git -C <checkout> status --short --untracked-files=no   # prints nothing
<checkout>/install.sh --settings <settings> --dry-run
```

It writes nothing and does not contact Telegram. Where the settings name a
`[yardr] home`, its checks ask that yard's socket for the operator (a read),
so that yard must be running. Look for:

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
on and the host's shared queue does not exist yet.

The existing service file and `<root>/bin/telegram-bridge` must name this
same settings path. Install, dry-run and rollback refuse before writing when
either names another path, or its settings ownership cannot be read. A
collision diagnostic names the conflicting file and both settings paths.
Reinstalling, upgrading and rolling back with the same settings path keeps
working.

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

With core `ced24b0e849c`'s directory source-pack proposal contract and this
bridge installed, propose the pack published by the installer as a native
card. Install core first, then the bridge; yardmaster owns installation and
the restart above. Landing source alone does not activate either release.
Use `pack check` to read the digest and complete plan, then:

```sh
yardr pack check <root>/pack
yardr bead ask <bead> "Apply this tgbridge source pack upgrade?" \
  --context "Read the complete plan, diffs, grants kept and snapshot document." \
  --run "pack apply <root>/pack --sha256 <digest>"
yardr call tgbridge run_ask bead=<bead> ask=<ask> topic=general key=pack-upgrade-1
```

Open the single review document. It contains the complete canonical review
and every snapshot file whole, including the declaration, command files and
setup notes. The compact card names both digests, document hash, size and
counts, plus changed paths/hashes and kept grants; its weight lines come first. No controls arrive until the whole
bundle is confirmed. Tap Approve, then reply directly to that **card** with
the exact ask number within five minutes. The tap alone writes nothing;
a wrong number requires another tap. Replying to a part or document cannot
approve. Core applies the captured bytes even if `<root>/pack` later changes.
Check the canonical ran/failed state after carrying; stale state writes
nothing and needs a new ask. Existing credentials, selected base, limits
and grants stay under the core's usual replacement rules, with grants kept
shown in the review. The proposal takes no binding or answering flags.

The document ceiling is 50 MiB including framing, within core's existing
64-file/256-KiB snapshot bounds. An oversized review refuses before sending
or buttons and names an explicit part-based fallback; it never truncates.
Definitely unsent documents resume under their original key; ambiguous ones
need existing ledger reconciliation, including confirmed size, and are
never blindly resent. Saved legacy generations keep their parts and keys. Preserve the state directory. See
[source pack bundles](../bridge/OPERATIONS.md#source-pack-bundles).

For an older core/bridge, a refusal, or an explicit terminal rollback, keep
the terminal path below. A code rollback uses `install.sh --rollback`; a
pack rollback applies a reviewed earlier snapshot or the terminal command.

Read the bindings the source has, without printing the credential:

```sh
yardr source show tgbridge          # "(base <alias>)" when an alias is selected
yardr source show tgbridge --json   # source.env_file is the <credential file>
yardr pack check <root>/pack
```

`pack check` plans against the current bindings and takes no binding flags.
Apply with the bindings the source already has: `--source-base` only where an
alias is selected, `--source-env` always.

```sh
yardr pack apply <root>/pack --source-base tgbridge=<alias> --source-env tgbridge=<credential file>
# or, with no alias selected:
yardr pack apply <root>/pack --source-env tgbridge=<credential file>
yardr pack list                     # tgbridge is imported from <root>/pack
yardr source check tgbridge
```

The apply keeps the alias and its credential pairing, the limits, the grants
and the user identity; `yardr source show tgbridge` still shows the same base.
Later installs and rollbacks publish into `<root>/pack`, and `yardr pack apply
tgbridge` reads it from there.

### The crew's skills (shell)

The yard's operator copies this instance's skills into the yard's own home.
Keep `YARDR_HOME` explicitly set to this yard as above:

```sh
yardr skill add <root>/current/skills/telegram-formatting
yardr skill add <root>/current/skills/telegram-attachments
yardr skill add <root>/current/skills/telegram-voice-transcripts
yardr skill list
```

These are copies under `$YARDR_HOME/skills/<name>`, independent of the
checkout and the install root. Give the names to the intended crew or groups
with `yardr crew set <crew> skills=...` or `yardr group set <group> skills=...`,
keeping their other skill names in the list. A crew member reads the new
skills at its next session (`yardr crew restart <crew>`); groups use them
at their next session too.

Repeat the three `skill add` commands after an install or rollback to
refresh this yard's copies from its active release. They replace the copies
only in the selected yard; copies in another yard stay as they are, and
removing this instance's root does not break another yard's skills.

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
`--rollback` goes forward again. Refresh this yard's skill copies with the
three `skill add` commands above.

Undoing this first move, when there is no previous release (`--rollback`
says so and names `service.previous`):

```sh
systemctl --user stop <unit>
install -m 644 <root>/service.previous <service file>   # the file the refusal names
systemctl --user daemon-reload
systemctl --user start <unit>
```

The unit runs the checkout again, so the checkout must still be there. The
pack applied above still works with it (the same port or alias); apply
`<checkout>/yardr` with the same flags only to move the recorded origin back.
To revert the skill version too, repeat `yardr skill add` from
`<checkout>/skills/<name>` for this yard. The host-wide voice-transcribe
link remains the operator's choice as above.

### Never

- Copy the token, a secret, the credential file or the settings into the
  root, the checkout, a note or a chat: install.sh reads them where they are.
- Start a second unit, or a second `run`, for the same bot: two pollers
  conflict, and two ledgers lose track of each other's sends. Restart the
  existing `<unit>`.
- Delete the checkout, the state directory or `<root>/service.previous`
  before the verification and the pack steps pass: the undo above runs the
  checkout and its old unit.
- Reset the alias, re-key the source or grant the calls again: the apply
  keeps them.

## Explicit Markdown replies

Install only the reviewed merged checkout with the existing installer, then
check the installed release and follow its `next:` line. Apply `<root>/pack`
using step 5, retaining the selected source alias and existing env-file binding.
The declaration adds `thread.formats: [markdown]`; core `0035b8c13e27` or a
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
