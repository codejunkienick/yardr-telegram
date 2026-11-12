---
name: telegram-decisions
description: Choose and publish Telegram asks and decisions, approvals, run cards and siding cards through the yard's bridge. Covers chat ask keyboards, native tool selection, stable keys, refusals and fallbacks. Use for coordinating crew members holding the relevant per-call grants.
---

# Telegram asks and native cards

Use the Telegram source your `yardr prime` lists for this crew (for example
`tgbridge` or `tgsecretary`); replace `<source>` below with that name. When
answering a delivery, `yardr reply <seq>` selects its source automatically.
If prime lists several Telegram sources, match the delivery or this crew’s
subscription; the chat id alone cannot distinguish two bots. Ask the
coordinator if the destination is unclear.

## Choose the decision path

| Need | Use |
| --- | --- |
| No human decision is needed | No ask; send an update using `telegram-formatting`. |
| A chat question with two to four fixed options | `send` with chat ask buttons below. |
| A canonical option, words or generated manual-stage decision | `ask_card`. |
| Approval of a supported native proposal | `run_ask`. |
| A siding's waiting beads and a fold of exactly the shown beads | `siding_card`. |

Never substitute ordinary or chat buttons for native approval or a native answer.
Any message offering a choice goes out with buttons. Lettered option lines
(`A:` / `B:`, bulleted or in HTML) without a keyboard refuse as
`options_need_keyboard` before sending; `text_options=true` is only for a
deliberate text-only list. Message formatting and routing follow `telegram-formatting`.

## Chat asks with buttons

These are chat questions, not native yard asks: run approval and secrets stay
on the yard's cards. One question per message: each question of a multi-question
ask has its own message, key and keyboard. Choose the key first, then save JSON rows:

```json
[
  [{"text": "Yes", "callback_data": "ask:cad903a6a137a717:yes"},
   {"text": "No", "callback_data": "ask:cad903a6a137a717:no"}]
]
```

```sh
yardr call <source> send key=ask-demo-2026-10-25-01 text="$(cat question.html)" \
  keyboard="$(cat rows.json)" topic=<the topic, or general> --json
```

- The ask id is the first 16 hex characters of the SHA-256 of the send's key
  (`ask-demo-2026-10-25-01` gives `cad903a6a137a717`). Every button uses
  `callback_data` `ask:<ask-id>:<choice>` with that id; another key's id refuses.
- At most 8 buttons per row and 100 in total. A label holds no `|`, `]`, line
  break or control character; each `callback_data` fits in 64 bytes and is unique.
- The question (or photo caption) plus `\n✓ <longest label>` fits within 4096
  UTF-16 units of text, 1024 of caption. The question itself is free text and
  is never refused for its characters.
- Text or one photo only: no attachment or exactly one photo whose caption is
  the question. A photo keyboard holds only ask buttons; other attachments refuse.
- A tap reaches you as a delivery of `kind` `answer`, with
  `[ask answer | choice: <label> | message: <id>]`, then `question: ` and the
  visible question verbatim. In a topic, its topic line comes first.
- When the user answers by typing or chooses something outside the options,
  close the open chat ask with the decision made, in one line:

  ```sh
  yardr call <source> ask_close message_id=<id> decision="<the decision, one line>" --json
  ```

  `<id>` is the send's `message_id`; use `ask_key=<key>` instead if only its key
  is known. The buttons go and the ask shows `✓ Decided: <decision>`; later taps
  say "Already answered" and never reach you. For close refusals and recovery,
  see [Asks, A6 Closing with a decision](../../bridge/OPERATIONS.md#asks).

The send returns a delivery receipt (`delivered`, `message_id`, or a refusal
`reason`); delivery-key retry rules are in `telegram-formatting`.
See [Asks](../../bridge/OPERATIONS.md#asks) for keyboard and answer mechanics.

## Native cards

Native asks belong to the whole yard. Only the primary bridge has native
settings and grants in a yard with several bots; a secondary crew bot uses
chat asks above or the yard cards. Use the source explicitly granted the
native call, and ask the coordinator if it is absent from your prime.

The bridge reads canonical content from its configured yard. Never supply an
actor, command, weight or approval text to a native card call. Only the paired
human answers or approves; a crew member publishes the card.

Choose a stable key per request and the intended topic before publishing: a
native card stays in the conversation it was first sent to. Calls below return
`delivered: true` with a `message_id`, or `delivered: false` with a `reason` and
`failure_kind`. A delivered card confirms presentation, not approval or execution.
Read the canonical state in the yard for the answer, pending carry, success or
failure; native replies stay in chat rather than becoming chat-ask deliveries.

After definitely unsent parts, retry the same call and key. Ambiguous delivery
needs ledger reconciliation, never a new key or a blind resend. Keep the ledger
and review documents across restarts, moves and rollback. See
[Delivery recovery](../../bridge/OPERATIONS.md#delivery-recovery) and the card's
section below for its reconciliation path.

Native settings, source identity and explicit per-call grants are the operator's
bootstrap; builders never change live settings. See
[12. Native run cards (operator, optional)](../../yardr/SETUP.md#12-native-run-cards-operator-optional),
[13. Ordinary native decisions (operator; optional)](../../yardr/SETUP.md#13-ordinary-native-decisions-operator-optional)
and [14. Optional native siding cards](../../yardr/SETUP.md#14-optional-native-siding-cards).
Without the relevant grant, ask the coordinator; do not enable it yourself.

### Ordinary native decisions

Create a canonical ask with `yardr bead ask` (or use its generated manual-stage
ask), then publish its returned number:

```sh
yardr call <source> ask_card bead=<bead> ask=<number> topic=<topic> key=<stable-key> --json
```

It presents the complete question, context, choices, consequences, recommendation
and about-beads. Only ordinary decisions addressed to the configured paired
operator are supported. Unknown, operation-bearing, weighted or oversized cards
refuse whole; use the yard cards. Run proposals use `run_ask` instead.
See [Ordinary native ask cards](../../bridge/OPERATIONS.md#ordinary-native-ask-cards)
for human input, stale cards, conflicts and delivery recovery.

### Native run approval

```sh
yardr call <source> run_ask bead=<bead> ask=<ask-number> topic=<topic> key=<stable-key> --json
```

Supported proposals and their mechanics:

- [Self-contained native runs](../../bridge/OPERATIONS.md#native-run-cards).
- `group add` and `group set`, within the supported runners, settings and
  prompt limits in [Native group add and set review](../../bridge/OPERATIONS.md#native-group-add-and-set-review).
- `source add <file>`: a validated declaration and relative command file
  bundle. See [Source-add declaration and command review](../../bridge/OPERATIONS.md#source-add-declaration-and-command-review).
- Source-kind `pack apply <absolute-directory> --sha256 <digest>` or embedded
  `pack apply <name>`. See [Source pack bundles](../../bridge/OPERATIONS.md#source-pack-bundles).
- Refused: `skill add`; depot-kind pack plans; secret/setup/identity/depot_push
  payloads; parameter inputs; crew-add prompts; unfamiliar YAML/metadata or
  carried payloads outside the supported bundles.

Use the yard cards or a terminal for refused proposals. Invalid source packs
return `run_ask_invalid_source_pack_use_terminal` with
`use yardr pack apply <dir> in a terminal`. Bundles deliver the complete review
document before the compact approval card. Weighty cards require Approve, then
Confirm on that same card within 60 seconds; typed numbers never approve.
The linked sections hold the human's review and confirmation procedure.

### Siding status and the plain fold

Publish to show the operator the siding's waiting beads, or to propose a fold
of the eligible ones. The publishing crew member needs the `siding_card`
per-call grant; see
[14. Optional native siding cards](../../yardr/SETUP.md#14-optional-native-siding-cards).

```sh
yardr call <source> siding_card depot=<depot> key=<stable-key> --json
```

Add `flow=<flow> stage=<stage> group=<group>` to narrow discovery and
`topic=<topic>` or `conversation=<id>` to route it. The card lists every bead
waiting in the siding and proposes a fold of the eligible ones; over
`fold_max`, with fewer than two eligible, or with no single next stage it is
status only and carries no controls. Only the paired human's tap folds,
exactly the beads shown. An oversized card refuses whole: do not invent a
subset or another way to fold. The human may reply `fold <id> <id> …` for a
card of a chosen subset, or `release <id>` for a card that moves that one bead
on to the stage it enters; neither reply folds or releases anything, and only
the human's tap on that card does. A release card's tap refuses a bead that
changed since the card. Cards expire after 15 minutes. See
[Siding status and the plain fold](../../bridge/OPERATIONS.md#siding-status-and-the-plain-fold)
for subset replies, release cards, withdrawal, changed beads and the undo.

### Refresh a native outcome

```sh
yardr call <source> ask_close ask_key=<original-key> decision=refresh --json
```

Or select by `message_id=<id>`. Native close ignores the required decision text
and reads canonical state; it cannot answer or approve an open card. Follow the
matching OPERATIONS section when a refusal reports an open or uncertain card.

### When a card's send is unconfirmed

For an ordinary native card whose send is ambiguous:

1. Keep the original key and ledger; do not publish again under a new key.
2. Ask the paired operator to look for the original card and tap the action
   they intend. The tap reconciles delivery and may perform that action;
   a reply alone cannot recover it.
3. Check with the native `ask_close` call above, using the original key and
   `decision=refresh`. Expect `ask_card_delivery_unconfirmed` until the card
   is reconciled; then retry to show canonical state.
4. Alternatively, ask the operator to decide in the yard and manually delete
   the original Telegram card if present. Use this path if no card is visible
   or the tap cannot reconcile it.

The operator can list open decisions with `/decide`, then tap an entry or
send `/decide <bead>#<ask>` to reopen its card using the same delivery key.
An ambiguous send stays unconfirmed; `/decide` does not resend it. See
[Ordinary native ask cards](../../bridge/OPERATIONS.md#ordinary-native-ask-cards)
and [/decide: the operator's open decisions](../../bridge/OPERATIONS.md#decide-the-operators-open-decisions).

The source file and symbol behind each claim in this skill are in `citations.md`, next to this file; open it only when you need them.
