# Native source cards

Design record for how the bridge carries source and pack proposals. It
describes the implementation landed 2026-11-14, which trusts the core's
first-read rendering of each card's sentences and keeps only framing,
structured-payload and approval-time checks. Nothing here installs yardr
or the bridge, applies a pack, or exercises a live yard; installation
and activation stay with the operator as recorded in
[upstream alignment](upstream-alignment.md).

The native store is the authority. It writes each source or pack ask's
`does` sentences, carried declaration, files, diffs and inventory, and
weight lines. The bridge obtains them only through its configured
private Unix RPC (`Bead` as the paired operator in `bridge/run_asks.py`
`fetch`), and shows the full native disclosure beside the raw carried
bytes in the review (`source_bundle`, `pack_bundle`, `source_review`).
The card's `does` lines travel whole into the review document as
complete quoted lines, and unknown structured shapes refuse before
anything is sent (`validate`, `validate_run_card`, `run_scope`).

## What the bridge still recognizes

Small framing recognizers, and nothing wider:

- The operation lead is matched whole (`SOURCE_ADD_LEAD_RE`,
  `SOURCE_REPLACE_LEAD_RE`, `_pack_lead_pattern`): adds/replaces counts
  must equal the carried list length, and the pack label and digest
  formats must match. Malformed counts and labels refuse.
- Routing is fixed: the matching `back` sentence and `back_by` for an
  add or a replacement, and no `secret`, `setup`, `identity`,
  `depot_push`, `concealed` or `given` keys beside the run.
- One `source <name>:` anchor per carried source, in order, unique,
  starting at the second line. A second anchor for the same name, or an
  anchor for a name the payload does not carry, refuses.
- Per-source replacement and orphan-copy headers
  (`_replace_header_pattern`, `_grants_kept_pattern`, the two orphan
  sentences). Each header is bound to the standing state read from the
  carried inventory, removals and old diffs: a replacement header with
  no standing entry, or standing entries with no header, refuses. A
  replacement whose header carries no grants sentence, or one matching
  neither the kept-grants form nor the explicit
  `no grant of source <name> stands.` disclosure, raises
  `run_ask_missing_source_grants_review` before send, and the proposal
  lists as terminal-only in `/decide`; the explicit no-grant disclosure
  is accepted and shown.
- Weight is structural: classes are only `code` and `reads`, each once,
  each line non-blank within the cap and free of hidden characters.
  `code` is required for a source replacement and for any new or
  changed program file; a pack replacement whose carried files all
  match inventory is accepted with only `reads` weight
  (`_validate_source_weight`).
- The carried payload is checked as data, not prose: the declaration
  parses against the core reader's field names (`source_declaration`),
  every command slot holds a non-empty argv of non-empty words
  (`_source_command_slots`), and files, inventory, diffs and removals
  bind by SHA-256, size, mode and old digest (`validate_carried`).
- Envelope bounds: 4 to 40 `does` lines for a source, 1 to 40 for a
  pack, every line non-blank with no newlines, carriage returns or
  hidden characters. Size caps differ by kind: a source allows an 8 KiB
  declaration, 8 KiB per command file, 32 KiB of declaration plus
  files, and a 64 KiB payload of files, diffs and removed text; a pack
  allows 256 KiB of declarations in total (256 KiB per declaration,
  64 KiB per command file, 320 KiB of declaration plus files per
  carried source) and a 64 KiB payload. The review document caps at
  50 MiB (`PACK_DOCUMENT_BYTES`).

## What the bridge deliberately does not check

The first canonical response is trusted as the core's rendering of the
card's sentences. Capability, call, credential, limits, thread, hook,
event, link and command-file sentences are not reverse-parsed against a
second YAML interpretation: unfamiliar sentences inside a supported
envelope are displayed whole, subject only to the framing and
presentation limits above. Reordered, truncated or reworded capability
sentences that keep the lead, anchors and grants framing still deliver;
a first-read lie inside a well-formed envelope is therefore not
detected at display time. That lost independent semantic-check property
is intentional, and this record does not claim otherwise.

Change after display is caught at approval; a stable first-read lie
is not. `ensure` re-reads the ask and compares the locally computed
proposal (binding, yard-home identity, bead, ask, run, weight,
conversation) with the displayed one, so any drift marks the row
stale and removes the action buttons — but a false first response
that stays stable matches itself, and the native operation can still
be approved with its semantics undetected:

- Weighty cards need two taps: approve arms a nonce, and only confirm
  with that nonce inside its window approves with `weighed=true`.
  `ApproveAsk` names the bead and ask; the core approves its immutable
  stored ask, not the bridge's rendering — caller-supplied Does, Seen,
  Back and weight are discarded when the ask is stored and the core
  regenerates them (`test_real_native_source_contract_and_authority`),
  and weight consistency is enforced by the core against the `weighed`
  flag. The bridge holds no weight authority of its own.
- After `ApproveAsk` or `DeclineAsk` the bridge re-reads again and only
  the canonical state decides. A stale native answer on a source or
  pack approval marks the row stale with "nothing ran".

## Digests are local, not server-issued

The proposal digest and the review document SHA-256 are computed by the
bridge (`proposal`, `review_bundle`) and shown on the card so the
operator can compare what was displayed with what the yard now holds.
No server-issued digest and no atomic expected-digest approval exists:
`ApproveAsk` carries no digest, and this design adds none. Do not read
the card's document hash as an approval verified by the core.

## Supported and refused cases

Public core revision
[52e22d570cfff58bae725e6df53c047bd63ff4a6](https://github.com/yardr-dev/yardr/commit/52e22d570cfff58bae725e6df53c047bd63ff4a6)
condenses absent-capability disclosure into one ordered `it has ...`
sentence after present capabilities; older aligned builds keep one
sentence per absent capability. Both shapes deliver, as do aligned and
upstream directory, replacement and embedded packs, orphan creates with
a standing copy, and the rich, address and numeric sources. Evidence is
in `bridge/fixtures/README.md` and `bridge/test_run_asks.py`:

- `test_source_and_pack_every_does_row_preserves_prose_and_refuses_framing`
  and `test_source_and_pack_prose_values_are_native_owned`: content
  mutations that keep framing are presented whole; framing breaks
  (counts, labels, anchors, grants, short envelopes) refuse.
- `test_unsupported_source_pack_and_malformed_payloads_never_send`:
  unknown inserted rows, dropped or reordered sentences and truncated
  capability rows still deliver, except a spoofed extra anchor and a
  short source envelope, which refuse.
- `test_source_pack_replacement_accounting_fails_closed`: aligned,
  upstream, public-main and pre-alignment replacement packs without
  grants disclosure all refuse whole, with zero sends and zero native
  writes; pre-alignment shapes stay refused while older supported
  shapes keep delivering.
- `test_source_pack_weight_presence_is_structural` and
  `test_source_pack_quoted_tokens_and_plural_counts`: weight presence
  and well-formed file/command counts are structural; malformed counts
  refuse while well-formed large counts (up to twenty sources and
  commands in the table) are shown whole.
- `test_source_pack_first_read_prose_is_presented_verbatim`:
  first-read prose, however unfamiliar, is quoted verbatim, and any
  bound change between display and approval stales the card.
- `test_upstream_run_rows_show_whole_or_refuse_before_send`: upstream
  rows outside group, source, pack, skill and crew are shown whole or
  refused by shape, never reinterpreted.
- `RealNativeTests.test_real_native_source_and_pack_apply_once`,
  `test_real_native_source_contract_and_authority` and
  `test_real_native_source_limit_and_trust_weight_apply_once` drive the
  same paths against an isolated yardr over its private socket,
  comparing live responses with the captures and carrying real
  approvals, declines, stale state and replays. They run only with a
  yardr binary supplied; the default suite proves the synthetic and
  fixture-driven contract, not a live acceptance.

A public-main replacement card omits the grants review, exactly as
plain upstream does, so it stays refused whole and terminal-only: no
grant omission in this design is ever accepted.

```mermaid
sequenceDiagram
    participant Y as Yard socket
    participant B as Bridge
    participant O as Paired operator
    Y-->>B: Bead ask: does, carried, weight
    B->>B: Envelope, framing, declaration, payload, weight
    B-->>O: Card + review (native sentences whole, local digest)
    O->>B: Approve tap (weighty: confirm tap)
    B->>Y: Re-read ask, compare proposal
    alt proposal drifted
        B-->>O: Stale; use the yard cards
    else proposal matches
        B->>Y: ApproveAsk
        B->>Y: Re-read canonical state
        Y-->>B: Committed or stale
        B-->>O: Canonical decision, or stale with nothing ran
    end
```

## Rollback

Rollback is the existing per-instance path recorded in
[upstream alignment](upstream-alignment.md): check with
`sh install.sh --settings <file> --dry-run`, then
`sh install.sh --settings <file> --rollback`, reapply the pack,
restart the service. No new service, shared
executable or runtime checkout dependency is added here, and no
product, settings or fixture file changes with this record.
