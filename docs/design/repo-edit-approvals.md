# Native repository edit proposals (`depot edit`)

> Status: implemented on this branch (`yard/yardr-telegram-t8tv`) and verified against isolated local `yardr` builds (`e50e27b0` v1 capture-only; `d5296bea` v2 materialization and exact assembly landing); **fork-only / not installed or live** until upstream accepts the `RepoEditCapabilities`, `depot edit`, and `NativeEditStatus` core contracts and an operator upgrades/installs.

## Capability probe and public-core safety

Every repository-edit card (`run.repo_edit`, `argv: ["depot", "edit", "<depot>"]`) probes the yard socket's optional `RepoEditCapabilities` read RPC before rendering or approving:

- Public/upstream cores without `RepoEditCapabilities` receive `no such method RepoEditCapabilities` (`RPCUnsupported`). The bridge rejects the card locally with `run_ask_repo_edit_unavailable; open in the yard instead` (`FALLBACK`), sends zero Telegram messages or documents, and lists the ask in `/decide` as `terminal-only run` so it cannot crash `/decide` or block any existing native card (`decision`, `source add`, `pack apply`, `group add`/`set`, `general`, or `skill`).
- Capability probing is strictly per-family: absence of `RepoEditCapabilities` or `SkillDigest` never flips a global core switch.
- Supported capability contracts are restricted to known schema/version pairs: version `1` (`materialize: false`, capture-only) and version `2` (`materialize: true`, materialized flow/review/assembly). Unknown fields, unknown versions, mismatched limits (`paths=16`, `text_bytes=65536`, `context_bytes=8192`, `classes=['markdown', 'plain-text']`), or version mismatches between the proposal's `request.version` and `RepoEditCapabilities.version` refuse cleanly.

## Validation and canonical digest binding

Before any Telegram delivery or confirmation, [`bridge/repo_edit.py`](../../bridge/repo_edit.py) verifies the complete `run.repo_edit` payload locally:

1. Exact `run` keys (`argv`, `command`, `description`, `does`, `back`, `back_by`, `repo_edit`, plus optional `seen`), `weight` (`code` class), `question`/`context` UTF-8 NUL-free `<= 8192` bytes, and `author == committer == "yardr native proposal <native-proposal@yardr.invalid>"`.
2. Recomputed canonical Go `encoding/json` SHA-256 (`repo_edit_digest`) matching `repo_edit.digest` and `seen["repo edit"]`, escaping `<`, `>`, `&`, `\u2028`, `\u2029`, control characters, and preserving arbitrary-precision integers and unescaped non-ASCII UTF-8 bytes identically to Go's `json.Marshal`.
3. Path normalization and security constraints (1..16 paths, strictly sorted, case-fold unique without file/directory collisions, normalized relative `.md`/`.txt` paths without traversal, `.git`, hidden segments, or secret/credential/key filenames), `<= 65536` total before/after bytes, mode `100644`, SHA-256 hashes, no-op rejection, exact unified diff (`expected_diff`), and exact `does` sentences (`expected_does`).
4. Version `2` requires `review_route`, `flow`, `flow_source`, `routes`, and `rules`. Proposal identity binds `yardr_home` (`home_identity` device/inode) and active yard/pairing/conversation.

## Complete document delivery, compact approval, and settlement

- One complete UTF-8 Markdown document (`review-<proposal>.md`, mode `0600`, `<= 50 MiB`) is delivered first, containing exact quoted old/new UTF-8 bytes with byte counts, trailing LF counts, non-LF control/separator offsets, complete diffs, depot/base/head commit, operation, subject, truthful non-operator authorship (`yardr native proposal <native-proposal@yardr.invalid>`), configured depot test command, review/assembly route, and inverse rollback.
- Only after document delivery and integer `document_size` verification is one compact bound card sent (`reply_parameters` pointing to the document).
- First tap (`Approve`) arms a 60-second same-card `Confirm` step (`ApproveAsk` with `weighed=True` and typed `values={"repo_edit_version": ..., "repo_edit_digest": ...}`). Wrong user/bot/chat/topic/message/forward, bad nonce, expired timer, and typed replies are refused with zero yard mutations.
- Stale core refusals (`repo edit changed`, `review route changed`) mark the row `stale` and retire card controls (`nothing ran`).
- On `answered` v2 asks, the bridge extracts the reserved held patch bead ID from `ran.pending_reason` or `ran.result`, polls `NativeEditStatus` while non-terminal, verifies `bead`, `depot`, `operation`, and `digest`, and relays truthful status transitions (`pending` / `carrying`, `prepared (not landed or installed)`, `reviewed (in assembly; not landed or installed)`, `held`, `failed`, `withdrawn`, and `landed (source landed; not installed or live)`), stopping status polls once terminal.

## Recovery and isolated evidence

`RealNativeTests.test_real_native_repo_edit_end_to_end_and_boundaries` drives
`Bridge.poll_once` (refresh before drain), the real Unix RPC/store, fake
Telegram and SQLite. It publishes using the checked-in `run_ask` declaration
through `yardr call`, opens `/decide` in a topic, and declines in the paired
private chat. It checks missing/expired confirmation, wrong bot/chat/user/
message/topic/home, typed replies, base/file/card drift, delivery-key and native
operation collisions, uncertain document/card sends, crashes after delivery,
and a lost approval reply followed by both core and bridge restart. One patch
commit passes the configured test and normal review stages before assembly;
its approved paths/bytes, untouched file, author, subject and landed head are
checked. A second proposal deliberately fails its preparation test and remains
failed after restart without changing the base or reserving a second bead.

The shared bundle/binding tests also exercise repository cards, including
home replacement, two yards, nonce reuse, document-size receipts and retries.
Malformed status shapes refuse with a fixed error naming the native status.
The synthetic preview opens the entire document beside its compact card,
confirmation and canonical pending/prepared/landed receipts; it has no scroll
clipping. Neither these tests nor the preview use a live bot or installed
runtime. The operator owns installation and activation of reviewed source;
rollback uses the existing bridge release rollback, while landed repository
content needs a fresh inverse proposal against the current base.
