# yardr-telegram: the Telegram bridge for a yardr yard

The bridge connects Telegram to a yardr yard through a verified inbound
hook and a loopback API. It owns the delivery ledger, topics, asks, pins,
reactions, typing, transcription and the `[[command]]` add-on protocol;
the yard owns conversations and crew routing.

One bridge serves one bot in one yard. A yard may have several bridges, one
per crew member's bot, sharing its home and socket. Each has a distinct source
and hook name, settings, bot credentials, state directory, port, unit and
independently versioned install root. A second yard has its own home/socket.
See [Add a second crew member's bot](yardr/SETUP.md#add-a-second-crew-members-bot).

## Why the bridge stays a process of its own

yardr ships a `telegram` pack, but it is a webhook source: plain text and
files in one chat, from the operator only. The pack API offers a pack no
long-lived process and kills a source's command after 30 s
(`docs/pack-api.md` in yardr), so polling, typing, the transcription
queue, the add-on handlers and the ledger cannot live inside a pack. The
bridge keeps running as a systemd user unit, and the yard talks to it as a
declared source on loopback, which yardr allows for a `http://` base
(`internal/source/declaration.go`, `LoopbackHost`).

Do not apply yardr's own `telegram` pack to a yard this bridge serves:
setWebhook on the same bot stops `getUpdates` polling.

## The two directions

```
phone ── Telegram ──(getUpdates)── bridge ──POST /hooks/<hook>──▶ yard ──▶ crew inbox
phone ◀─ Telegram ◀──────────────── bridge ◀──POST /v1/<call>──── yard ◀── yardr reply / yardr call
```

### Inbound: bridge → yard

Each message the paired user writes (after commands are routed to their
handlers and voice notes are saved) is one POST to the yard's hook
listener (`hook_addr`, loopback):

```
POST <[yardr] hook_url>          e.g. http://127.0.0.1:8797/hooks/tgbridge
X-Tgbridge-Secret: <the contents of [yardr] hook_secret_file>
Content-Type: application/json

{
  "id":           "<bot_id>:<conversation_id>:<provider_message_id>",
  "kind":         "message" | "transcript" | "answer",
  "chat":         "<chat_id>",
  "conversation": "<chat_id>" | "<chat_id>:topic:<thread_id>",
  "topic":        "general" | "<topic name>",
  "thread_id":    <int> | null,
  "message_id":   "<provider message id>",
  "reply_to":     "<message id>" | null,
  "from":         {"id": "<user_id>", "name": "<user_display_name>"},
  "text":         "<the whole relay text, headers and transcript included>",
  "forwarded":    true | false,
  "received_at":  "<ISO 8601 UTC>"
}
```

The yard verifies the secret (`hook.verify.scheme: secret`), dedupes on
`id` (`delivery: {body: id}`), normalizes it with `yardr/tgbridge/normalize`
and routes it by subscription. A 2xx answer with a positive `seq` (a
duplicate answers the earlier one) is the durable accept point:
the bridge advances its cursor, sets the `seen`
reaction and opens typing. Anything else keeps the cursor, logs why, and
the update is tried again: another status, no `seq`, and
`{"dropped": true}`, which the yard answers 200 for a sender it does not
hear.

The yard keeps at most 500 characters of a delivery's text
(`hooks.MaxText`). The whole text stays in the bridge's message log and is
read back with `yardr thread show <seq>` (`thread.read`, below).

### Outbound: yard → bridge

The bridge's loopback port serves the yard's calls and thread commands. Every
request carries `Authorization: Bearer <the contents of callback_secret_file>`;
the yard holds the same value as the source's credential `TGBRIDGE_SECRET`.
Every answer is JSON. A refused or unconfirmed send answers 200 with
`delivered: false` and a `reason`, so that the caller can tell a replayed
key from a failure; a malformed request answers 400 and a bad secret 403.

| route | body | answer |
|---|---|---|
| `POST /v1/send` | `conversation` (default the paired chat) or `topic` (a name), `text`, `key` (else the `Idempotency-Key` header), optional `format` (`plain`, `html`, `markdown`, `auto`; default `auto`), `silent`, `keyboard` (rows, or rows as a JSON string), `text_options`, `reply_to`, `attachments` (host paths), `attachment_kind` | `{delivered, message_id, conversation, reason?, failure_kind?}` |
| `POST /v1/tell` | `conversation`, `title`, `text`, optional `link` | as `/v1/send`; plain text, the title on its own first line. The yard tells with no key, so the key is the content's hash and the minute: the same tell retried within the minute is sent once |
| `POST /v1/posted` | `key` | `{message_id}` when the ledger holds a confirmed send under the key, else `{}` |
| `POST /v1/read` | `conversation`, optional `limit` (1-200, default 50) | `{messages: [{id, direction, from, text, at}]}`, oldest first |
| `POST /v1/react` | `reaction` (`seen`, `done`, `none` or an emoji), optional `message_id`, `conversation` | `{delivered, message_id, reason?}` |
| `POST /v1/pin` | `slot`, `text` and optional `format`, or `unpin: true` | `{delivered, message_id, changed?, reason?}` |
| `POST /v1/ask-close` | `decision`, and `message_id` or `ask_key` | `{delivered, message_id, reason?}` |
| `GET /v1/health` | | `{ok, bot, last_poll_ok}` |

`format: auto` sends Telegram HTML when the text is valid under the
bridge's HTML subset and holds at least one tag or entity, and plain text
otherwise; a parse Telegram rejects is sent once more as plain text, as
with `html`.

### The command line

`bridge/bridge.py --settings <file> <command>` keeps `send`,
`reply-current`, `ask-close`, `react`, `pin`, `topics`, `probe`,
`set-commands`, `render-unit` and `check-install`, with the flags they had.
`--settings` falls back to `$TELEGRAM_BRIDGE_SETTINGS`. Every send is
delivered in the calling process through the shared ledger; nothing goes
through the yard. The `GC_RECEIPT {...}` line on stderr keeps its name,
because existing delivery consumers parse it.

`topics` prints, for each topic, the subscription that routes it:

```
topic homelab: thread 12
subscribe: yardr subscribe yardmaster tgbridge --scope all --where type=message --where topic=homelab
```

## Settings

`settings.example.toml` lists every key:

- `yard`: the yard's name, in the default unit name
  (`<yard>-telegram.service`) and in conversation refs.
- `crew`: the crew member a topic's subscription line
  names (default `yardmaster`), and whose quota the notice watches (the internal method keeps its legacy name).
  `[[topic]] crew` overrides it for one topic.
- `[yardr]`: `hook_url` (`http://127.0.0.1:<port>/hooks/<name>`,
  loopback only), `hook_secret_file` (owner-only), `source` (default
  `tgbridge`, the declared source's name, used by the rendered installation
  pack and printed lines).

Legacy `city`, `agent`, `gc_base_url` and `gc_api_token_file` settings
are refused with the replacement key named. A legacy `[[topic]] agent`
must likewise become `crew`. See [compatibility names and their checks](docs/public-readiness.md#retained-compatibility-names).

## The yardr pack

`yardr/` is a pack of kind `source` that declares `tgbridge`:

- `hook`: verify `secret` in `X-Tgbridge-Secret`, `bodies: unique`,
  delivery id from the body's `id`, normalize `yardr/tgbridge/normalize`,
  keys `chat`, `topic`, `kind`, `sender` and `thread` (a topic's thread
  id: a topic the user created is routed by it, since a rename changes its
  name).
- `thread`: `reply` (to `/v1/send`, `format: auto`, files as attachments),
  `posted` (to `/v1/posted`), `read` (to `/v1/read`).
- `calls`: `send`, `react`, `pin`, `unpin`, `ask_close`, `run_ask`,
  `ask_card`, `siding_card` and `tell` (updates), and `health` (the check),
  each under `http://127.0.0.1:<port>`. See
  [Native operator approvals](#native-operator-approvals) for `run_ask` and
  `ask_card`, and
  [Siding status and guarded store actions](bridge/OPERATIONS.md#siding-status-and-guarded-store-actions)
  for `siding_card`.
- `notify`: the `tell` call to the user's `TGBRIDGE_CONVERSATION`, so what
  lands in the operator's inbox can reach the phone.
- `hear`: by identity, on the `sender` field: the operator's Telegram id
  is their identity in the source (`yardr user identity`), and a
  delivery carries `speaker=<user>`.

`send` takes an optional `key`, which wins over the yard's
`Idempotency-Key`: an ask's buttons carry an id made from the send's
key, so the caller must choose it. A `file` parameter would make the
yard send a multipart upload, so `attachment` is a host path the bridge
reads, as `bridge.py send --attachment` does.

The yard holds each caller to 60 calls of a source an hour, 10 of them
updates, by default; a yard that talks through the chat raises both
(`yardr source set tgbridge calls_per_hour=600 updates_per_hour=300`).
`yardr reply` is a thread command, not a call, and is not counted.

`yardr/SETUP.md` walks a yard through it.

## Add-ons

Add-ons talk to this instance's bridge. Their handler and configuration
requirements are:

| add-on | change |
|---|---|
| telegram-quota | `/quota` and `/resets` belong to the external handler. A handler that changes a crew member's model uses `yardr crew set <crew> kind=… model=…` and `yardr crew restart <crew>`; the bridge does not edit crew configuration |
| telegram-waiting | the external snapshot producer must target the configured yard; the bridge does not supply or migrate it |
| telegram-sysmon | settings only: `bridge_py` and `settings_file` point at this bridge |
| telegram-pricewatch | settings only, as sysmon |
| telegram-secret-drop | `send_command` targets this instance's launcher; secret storage and its CLI are owned by the external handler |

## Conversation storage and routing

- The bridge's message log (`/v1/read`) holds each conversation's full text.
- Binding topics: a topic is routed by a subscription in the yard, made
  once by the operator from the line `topics` prints.
- Two private bots see the same user's chat id. Conversation IDs stay
  `<chat_id>` (or `<chat_id>:topic:<thread_id>`); the yard scopes threads,
  subscriptions and replies by **source + thread**, not by thread alone.
  `yardr reply <seq>` selects the delivery's source and its bridge endpoint.
  Inbound delivery IDs additionally include the **bot id**. Each bridge's
  ledger, delivery keys, pins, message log, offsets and transcript jobs live
  under its own **state_dir**; never share or copy a first bot's state into
  a second bot's installation.
- Menus are set through each bot's token with Telegram's chat scope. Quota
  notices watch `crew` (or the topic's crew), using each bridge's ledger.
  `[[command]]` registrations and their handler settings belong to one
  installation; external handlers must select the intended yard and crew
  and keep their own mutable state separate. The transcription FIFO alone
  is intentionally shared per host user; input files and results stay in
  the submitting bridge's state directory.
- Native asks are **yard-wide**, without a destination-source field.
  Keep `[yardr] home`/`operator` and native grants on the primary bridge
  alone. A secondary omits both keys: it does no native discovery or
  reconciliation, offers no `/decide`, and refuses `run_ask`, `ask_card` and
  `siding_card`. It can still use ordinary chat questions and buttons.
  Source/bot bindings protect existing native callbacks; they do not route
  a yard-wide ask to a particular bot. Enabling native keys on both bots
  would expose the same operator queue on both and is outside this setup.

## Native operator approvals

Optional `[yardr] home` and `operator` enable authenticated
`POST /v1/run-ask`, the `run_ask` source update call and:

```sh
<root>/bin/telegram-bridge run-ask \
  --bead <bead> --ask <number> --idempotency-key <key>
```

The instance launcher supplies its settings and Python interpreter.
The CLI also accepts `--topic`, `--conversation-id`, and `--silent`.
The HTTP body accepts exactly `bead`, `ask`, `key` (else Idempotency-Key),
`topic`, `conversation`, and `silent`; its delivery receipt matches send.
The configured private home alone determines `home/yard.sock`. A current
operator role and `identities[source].id == bot.user_id` are required.

Only bead/ask/routing/key are supplied by callers. Question, proposer,
context, command, description, every does/current/proposed/back line and
canonical weight come from Bead. No actor, command, weight, weighed or
approval text is accepted. `runask:` is reserved from add-ons/publish.
Routine approval calls native ApproveAsk unweighed. Weighty approval first
opens a 60-second confirmation on the same card. The paired human then
taps Confirm on its own second row; Cancel takes Approve's former place
and sends no approval. Typed numbers never approve. All interactions recheck
home, pairing, recipient, proposal, message, conversation, current role/mapping and native state.

The supported scope, installation/bootstrap commands, explicit per-call
operator grant and recovery limitations are in
[SETUP.md](yardr/SETUP.md#12-native-run-cards-operator-optional).
Unsupported or oversized cards refuse whole, with Footplate/cards as the
fallback. Native ask_close refreshes canonical state and ignores caller
closure text; ordinary chat asks retain their existing behavior. SQLite
preserves one-use confirmations, delivery keys and canonical revisions;
ambiguous writes re-read Bead, execution pending is separate from success,
and polling reconciles settlements and outcome edits. Long results split
into bounded ledger-backed messages without truncating the canonical text.


Native skill snapshots additionally support `skill add <absolute-source>
[--name <name>]` using the core's existing `run.carried` envelope, never a
new run.skill field. The bridge verifies its single-file manifest, metadata,
old inventory, exact bytes/digests, replacement diff, complete canonical Does
and strong code weight. It delivers the entire quoted review as one digest-bound document before
the compact approval card, with confirmed bytes/size and no rewriting of
file content. The same weighted
human confirmation and canonical carry settlement apply. Extra skill files
and skill deletion remain refused. See
[the exact scope and recovery limits](bridge/OPERATIONS.md#captured-single-file-skill-review)
and [bootstrap requirements](yardr/SETUP.md#single-file-skill-snapshots).
From core `58e93b7f`, crew members (including the yardmaster), yard users
and the operator may add directly, making this card dormant for new
proposals under the core policy; it still serves earlier stored asks and
yards whose policy keeps adding the operator's, as earlier cores do.

Native `source add <file>` reviews the complete declaration, relative command
files, replacement diffs and inventory before controls appear (local core
baseline `546e3fdd2127`, not a public install target). It uses the same
`run_ask` grant, pairing and canonical carry settlement; core's weight rules
determine whether approval needs Approve, then Confirm within 60 seconds. See
[source review](bridge/OPERATIONS.md#source-add-declaration-and-command-review)
and [setup](yardr/SETUP.md#source-declarations-and-command-files).

Source-kind `pack apply` bundles without parameter inputs are also supported.
A directory proposal (`pack apply <absolute-directory> --sha256 <digest>`,
local core baseline `ced24b0e849c`, not a public install target) delivers
one document containing the complete review and every captured snapshot
file before the compact weighted approval card. Embedded source-kind
`pack apply <name>` uses one complete carried-source review document
without inventing a directory snapshot. Both use the existing `run_ask` grant and
operator checks; depot-kind and summary pack cards remain refused. See
[source pack bundles](bridge/OPERATIONS.md#source-pack-bundles) and
[the source upgrade procedure](yardr/SETUP.md#the-source-pack-crew-then-you-in-telegram-shell-fallback).

Native herdr group cards (`group add` and `group set`, local core baseline
`fc7375cf5ad4`, not a public install target)
are read sentence by sentence against their argv; every role is re-derived
from the card and must hash to the digest it shows. The whole review, roles
quoted line by line, goes out as one document before the compact weighted
card; unknown fields, runners, sentences or env/credential/login keys refuse
before sending. See [operations](bridge/OPERATIONS.md#native-group-add-and-set-review)
and [setup](yardr/SETUP.md#herdr-group-add-and-set).

Native ordinary decisions use authenticated `POST /v1/ask-card`, source update
`ask_card` and CLI `ask-card` with the same bead/ask/routing/key-only contract.
The configured operator can pick stored options or reply directly with a
number plus optional words; no-options cards take words with option zero.
Later/Decline and manual-stage choices use the guarded native primitives with
the displayed ask's turn (minimum verified core `7fa0d4c75ff6`, no legacy
fallback). A concurrent answer survives a stale Telegram action. Canonical
state controls closing and reopening. The shared ledger and socket seam
retain old run/chat behavior; newly reserved `askcard:` collisions require an
explicit handler-prefix migration. Complete ordinary cards must fit 3500
UTF16 units. Operation-bearing, weighted or unfamiliar ordinary payloads
refuse whole. See setup step 13 for exact scope, operator-only key/grant
bootstrap and the independent-runtime deployment prerequisite.

`/decide` lists the configured operator's open canonical decisions in this
yard (core `UserAsks`) with `<bead>#<ask>` IDs, and a button or
`/decide <ID>` opens the same native card; asks a card cannot show whole are
listed without a button, and a closed one reports its outcome. It is the
bridge's own command only with native keys, which also reserve `decide:`. See
[the operations guide](bridge/OPERATIONS.md#decide-the-operators-open-decisions).

Explicit `format=markdown` renders the documented CommonMark subset through
the existing HTML validator/delivery path. Ordinary `yardr reply <seq> --format
markdown <text>` requires the installed core format transport and reviewed
`thread.formats: [markdown]` source declaration; no flag keeps legacy auto.
See [formatting](docs/formatting.md) for policy, limits and packaging.

Native siding cards use authenticated `POST /v1/siding-card`, source update
`siding_card`, and CLI `siding-card --depot <depot> --idempotency-key <key>`.
Optional flow/stage/group narrow discovery; topic/conversation and silent
retain the usual routing contract. The caller supplies no actor, command,
member set, target, digest or result. Canonical waiting/stuck status and exact
fold proposals come from socket-only `SidingProposal`; a paired human reply
`fold id id …` or `release id` creates a new review card before any effect.
Only the new card's verified human button can call `FoldSidingExpected` or
`ReleaseSidingExpected`. Version 1 guard=store with empty refusals is
required; repository-own-flow or eligible until repository inputs are visibly
status only. No legacy write fallback is allowed. The saved resolved
ask/digest/op goes to `SidingOutcome` before recomputation, including after
restart, withdrawal or an ambiguous write; null may mean pruned history and
never permits a blind retry. `siding:` joins the reserved callback prefixes.
See the operations and setup guides for the first-slice limit and the sole
additional per-call grant. Existing ordinary/run/chat cards keep their paths.
