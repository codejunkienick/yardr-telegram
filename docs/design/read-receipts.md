# Operator message read and done receipts

Design record, 2026-11-21: track each accepted operator Telegram message
through `landed` (🕊), `read` (👀) and `done` (👌) using one replacing
bot reaction per message (`setMessageReaction`), with zero yardr core
changes and clean degradation when a build lacks a read.

## Decisions

1. **Read access on unmodified upstream.**
   `GetCrew {"name": crew}` is a `Read` call in the public `Calls`
   contract (`internal/rpc/access.go`, `internal/rpc/api.go`) with no
   caller check in `store.GetCrew` (`internal/store/crew.go`). It
   returns the line session's `cursor`, `state`, `status` and `journal`.
   No core addition is needed.
   *Degrades off:* without `[yardr] home` (e.g. a secondary bridge),
   when the socket is absent, or when `GetCrew` omits an integer
   `cursor >= 0`.

2. **Mapping a Telegram message to its yard event `seq`.**
   The inbound hook's 2xx answer carries `seq` (`internal/hooks/receiver.go`),
   which is the event sequence `RecordHook` logs as kind `hook` and
   subscriptions index into the crew inbox (`internal/store/hooks.go`,
   `internal/store/subscriptions.go`). Duplicate deliveries answer the
   earlier `seq`. Because inbox cursors are yard-wide sequences, a crew
   member that never received the message would still pass its seq;
   therefore `EventsSince [seq-1, 1]` verifies once per message that the
   `hook` event came from our source and names the conversation's crew
   (`_quota_agent`) in `data.deliveries`. Read means `GetCrew.cursor >= seq`.
   *Degrades off:* if `EventsSince` is unsupported, refused or returns a
   mismatched event, or if the crew is not in `data.deliveries`, the row
   moves to `off`.

3. **Done signals.**
   `yardr reply <seq>` logs `replied` on the crew member's journal after
   `/v1/send` returns, carrying `data.seq`, `data.source` and `data.crew`
   (`internal/store/threads.go`). Because thread replies carry no parent
   for envelope type `message` (`internal/hooks/reply.go`), the bridge
   reads `Events [journal, n]` (64 doubling to 1024, validated by
   `run_asks._validate_events`) only after a non-reaction, non-readstate
   ledger send, holding the send mark for two rounds with no newer send.
   Done also comes from a `/v1/send` with `reply_to` naming the message
   or a `react` call (`CLI` or `/v1/react`) with the `done` emoji; `none`
   or any other emoji sets `manual` and stops tracking.

4. **Cadence, locking and typing isolation.**
   `read_states.refresh()` runs from `poll_once` right after
   `typed_nudge.refresh()`, at most once per 60 monotonic seconds and
   making zero native calls when no row is open (`landed` or `read`).
   `landed` rows older than 24 h move to `expired` without a reaction.
   Every reaction check, Telegram `setMessageReaction` and `read_states`
   state update runs under the reactions `flock`, stepping only forward
   (`landed -> read -> done`), using `readstate:<mid>:read|done` keys
   ignored by `Store.sent_since` so typing windows survive a read.

```mermaid
stateDiagram-v2
  [*] --> landed: hook 2xx seq (🕊 via on_accept)
  landed --> read: GetCrew cursor >= seq (👀)
  landed --> done: replied seq / reply_to / react done (👌)
  read --> done: replied seq / reply_to / react done (👌)
  landed --> manual: react none or other
  read --> manual: react none or other
  landed --> off: crew not in deliveries / unsupported answer
  landed --> expired: 24 h
```

## Unread nudge offer

When `[reactions] nudge_after` (`1`–`3600` seconds) is configured together
with `[reactions] read`, `[yardr] home`/`operator`, and the built-in `/nudge`:

1. **Reuse over a second card type:** Offers open via
   `TypedNudge.open_confirm(token=..., reply_to=mid, header=..., silent=True)`
   and use the existing `nudge:<token>:nudge` / `cancel` callback handler,
   operator verification, `NudgeSession` write, and `session_nudged`
   outcome follower. No new callback prefix or grant is added.
2. **Core capability probe:** `NudgeSession {}` is probed once per process
   on first eligible offer; `RPCUnsupported` (`no such method`) is cached
   and logged once, leaving upstream cores silent with zero writes.
3. **Zero message-text leakage:** The offered line is a fixed constant naming
   only the integer event `seq`, keeping operator message text out of the
   card and the agent's pane.
4. **Durability and close-on-read:** `read_states.offer_token` and the
   `typed_nudges` row (`message = None`) are saved atomically before the
   send under `typed-nudge:<token>:send`; a later round retries `not-sent`,
   replays confirmed sends, and retires ambiguous sends as offered. At most
   one offer per crew stays `open` at once, one offer per message ever, and
   moving out of `landed` (`read`, `done`, `manual`, `expired`, `off`)
   cancels an `open` offer under `ra.lock()` and edits away its buttons.
