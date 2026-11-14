# Public-readiness and local release handoff

This record separates source landed locally, source published publicly,
code installed, source applied/granted and a live accepted instance. Source
publication and an upstream documentation proposal can proceed after review
and exact-candidate audit; runtime installation, settings/source/grant writes
and activation remain each instance operator's separate action.

**Public source, not an installable release (2026-11-14).** Basic adoption
passes in isolation on public core
[`52e22d57`](https://github.com/yardr-dev/yardr/commit/52e22d570cfff58bae725e6df53c047bd63ff4a6).
The latest release checked, [v0.17.0](https://github.com/yardr-dev/yardr/releases/tag/v0.17.0),
refuses the full pack on `thread.formats`, including hook/chat without native
keys. The installation instructions apply after the
[public-core release gate](#public-core-release-gate) closes. A newer upstream
main is not the tested core. See [the upstream proposal](design/upstream-proposal.md).

## Portable installation

Follow [SETUP](../yardr/SETUP.md) for a clean install, using the fictional
`example` values only as a starting point. Choose the operator, display name,
bot/chat/user IDs, timezone (IANA name or UTC), absolute home, state and secret
paths, loopback ports and unit independently for each instance. `~` paths
resolve under the service user's home, not under the checkout. Python 3.11+,
curl, a supported service manager and a local yardr are required. `install.sh`
also needs git, a clean committed checkout and an existing state directory
or parent (SETUP creates the private directory first); rollback from a release needs
no git. The pinned Markdown runtime is shipped offline in `vendor/`.

Native cards require the selected yard daemon/socket and current operator
mapping during install's `check-install`; hook-only settings omit both native
keys. Use the existing `scripts/native_settings.py` check, then the same
arguments with `--apply` only after reviewing the result (SETUP step 13).
It preserves all other settings bytes and refuses conflicting identities.
It neither provisions a yard nor grants authority. No other yard is detected
or used as fallback.

Linux/systemd and WSL 2 are the primary service targets. macOS/launchd and
its CPU/Metal transcriber branches are rendered/tested synthetically; a real
macOS installation/client has not been validated here. Native Windows is not
a service target. Optional transcription needs ffmpeg, git, CMake, make/Ninja,
a C++ compiler and a SHA-256 tool; CUDA requires its toolkit, while a build
without nvcc uses CPU and macOS can use Metal. Model sizes vary; the default
quantized Whisper plus VAD is about 600 MB. Existing shared read-only models,
builds, GPU and the compatible host transcription queue are intentional host
resources; they are not another yard's runtime or credentials. The manual
voice launcher takes `VOICE_TRANSCRIBE_*` environment values; it does not
load bridge settings automatically. See [third-party notices](../THIRD_PARTY_NOTICES.md).

## Upgrade and rollback

Use [the existing migration/rollback runbook](../yardr/SETUP.md#move-an-existing-bridge-to-its-own-installation)
and [the instance design](design/independent-instances.md). Do not create a
second installer or start two pollers for one bot. Install from the reviewed
clean merged checkout into that instance's root, check the release and follow
the printed `next:` commands for that same unit. Keep the settings, ledger,
bot, operator/home and credential identity. Keep a private state snapshot
with the unit stopped if needed. Do not put those files in this repository
or a release export.

Before changing the source, inspect its selected alias and env-file metadata
without dumping the env file. Run `yardr pack check <root>/pack`, then apply
that rendered pack with the existing `--source-base tgbridge=<alias>` only
when an alias is selected, and its existing `--source-env tgbridge=<file>`.
Neither `pack apply` nor `source grant` has `--run`. A new installation uses
the rendered default base, which follows its configured port.

The `odyssey` and `imd` aliases are deprecated compatibility keys with unchanged
hosts. They must remain available before, during and after apply. Do not
remove them and repair routing afterwards or rebind a credential to the new
default host. A custom valid alias must likewise exist with the same host in
the operator's reviewed declaration; the stock installer copies its declaration
and does not invent custom aliases. If the candidate lacks one, stop before
apply and retain the old installed source until a compatible declaration is
reviewed. A source's selected alias is not an instruction to read another
yard's settings or copy its secret.

Rollback uses `<root>/current/install.sh --settings <settings> --rollback`,
then the same unit restart and matching `<root>/pack` check/apply with the
same bindings. The first checkout-to-release move has no previous release:
restore `service.previous` as documented and retain the old checkout until
acceptance. SQLite tables/fields, `GC_RECEIPT`, quota method names and legacy
chat/run/source/CLI/API contracts remain compatible. New required callback
namespaces are the explicit configuration exception below. No automatic DB
rewrite or broad credential rebinding is part of public preparation.

## Retained compatibility names

The project retains its MIT notice, `Copyright (c) 2026 the telegram-pack
authors`, exactly as recorded in [LICENSE](../LICENSE). The following names
remain stable for existing installations and delivery consumers:

| Name or format | Reason and compatibility evidence |
| --- | --- |
| `GC_RECEIPT` and its `_print_gc_receipt` helper | Delivery consumers parse this prefix and its complete JSON receipt. `bridge/test_bridge.py`, `test_send_retry.py`, `test_ask_close.py`, `test_pin.py` and `test_text_options.py` exercise actual send/CLI failure paths, including permanent, transient and ambiguous outcomes. |
| `reply-current`, ignored `--session`, conversation `scope_id` and `session_id`, `provider_message_id` | Existing CLI callers and delivery keys keep their meaning. `bridge/test_send_retry.py` drives the CLI alias; `test_yard_api.py` checks the session flag and HTTP routes; `test_bridge.py` pins send fingerprints and retries the same inbound provider ID. |
| Ledger tables/columns, including `accepted`, `accepted_conversations`, `auto_topics.agent`, `auto_topics.bound_at`, `asks.decided` and `asks.conversation_id` | Existing rows retain their values. `TopicLedgerTests.test_an_existing_base_ledger_opens_unchanged`, `AutoTopicLedgerTests` (including reopening a populated binding timestamp) and the old-ledger case in `test_ask_close.py` verify additive upgrades, repeated opens and usable asks. Stored crew names are preserved. |
| `city`, `agent`, `gc_base_url`, `gc_api_token_file`, `[[topic]] agent` and the internal `GC_SETTINGS` map | These input keys remain explicitly refused, with the replacement key named; they never select a backend. `SettingsTests.test_legacy_keys_are_refused_by_name` and `TopicSettingsTests` exercise the loader. Only diagnostic prose changes. |
| `mayor_quota_episode`, internal `agent` and `city_commands` names | Existing method/config names remain intact. The quota cases in `bridge/test_bridge.py`, settings config assertions and command tests exercise them using yard health reports and configured handlers. |
| `odyssey`, `imd` source aliases | `yardr/tgbridge.yaml` keeps their existing loopback hosts. `RenderedPackTests` verifies rendered bytes; the isolated real-store source-upgrade case in `bridge/test_run_asks.py` checks apply/reapply, selected aliases, credentials and grants. `InstanceTests` checks independent install/upgrade/rollback. |

Legacy configuration-directory/environment identifiers occur only in the
negative lookup fixtures in `bridge/test_settings.py`: those fixtures prove
that only `--settings` or `TELEGRAM_BRIDGE_SETTINGS` selects settings, and
that the removed root flag refuses. The scanner permits those exact fixture
tokens only in that file. Python garbage-collector calls in
`bridge/test_bridge.py` have a separate exact-line exception. Synthetic scope
and crew labels in tests exercise arbitrary stored names and queue isolation.
No persisted or wire identifier is renamed by this preparation.

## Public-core release gate

The owner's 2026-11-10 decision remains: public source may be published;
installable presentation waits for compatible released core and a final
exact-candidate re-check. Reviewed source refresh and upstream documentation
publication may proceed before that release. Keep `thread.formats: [markdown]`;
there is no formats-less release mode.

Thread formats [PR #20](https://github.com/yardr-dev/yardr/pull/20) merged as
`dd8d8b5be4e86b384f5ac774deb334cd9f3f09d6` on November 10. Guarded answers
[PR #17](https://github.com/yardr-dev/yardr/pull/17) merged as
`3d7837c4eddf93669afe4ba75be7ea0d87662c47` on November 11. Both postdate
[v0.17.0](https://github.com/yardr-dev/yardr/releases/tag/v0.17.0)
(`b8595f6f5c873513e67cb15cc734984b075712d8`). No duplicate prerequisite PR
is needed; neither merge alone closes the release gate.

The frozen offline proof landed in bridge `4c2b886`, after `7b526ef`
(selected-core test repair) and `757c340` (native source/pack rendering
boundary). It tested public core
[`52e22d570cfff58bae725e6df53c047bd63ff4a6`](https://github.com/yardr-dev/yardr/commit/52e22d570cfff58bae725e6df53c047bd63ff4a6)
and the exact v0.17.0 commit above. Public-main `pack check yardr/` exited 0
and acknowledged `runs 2 programs of the pack as you: tgbridge/normalize, tgbridge/thread`.
The release check exited 1 with `thread.formats: unknown key`, before apply.
Omitting native home/operator or avoiding Markdown does not bypass parsing.

| Frozen proof target | Ran | Passed | Skipped | Failed/error methods |
| --- | ---: | ---: | ---: | ---: |
| Default aligned core | 1097 | 1095 | 2 | 0 |
| Public `52e22d57` | 1097 | 1094 | 2 | 1 |
| v0.17.0 `b8595f6f` | 1097 | 1084 | 3 | 10 |

These totals combine bridge and separately executed pack suites. On public
builds `scripts/test.sh` stopped after the bridge's nonzero result; pack tests
were then run separately with the same selected core. They are historical
proof counts, not the latest documentation candidate's default-suite totals.
The public-main failure is solely the fork-only `skill add` prerequisite in
`MayorSliceTests.test_installed_mayor_bot_reaches_its_inbox_and_answers_through_its_own_source`
([test](../bridge/test_mayor_handover.py)). It fails before mayor configuration
or adoption: no public mayor installation/skills claim follows. The default
suite passes this fork workflow. The two common skips are the removed
pre-alignment skill-card method and the Apple-Silicon-only GPU check on Linux.

The release's ten failed/error methods include formats prerequisites, guarded
`ask_card`, `/decide`'s `UserAsks` turn shape, group `effort=` setup and skill
add. They are not formats-only failures. Its extra skip first asserts pack
refusal and no applied source, then skips the adoption legs. No unclassified
public-main incompatibility remained in this proof; the known skill failure
still makes its suite non-green.

Read-only preflight on 2026-11-14 observed newer upstream main
`32878da29cb401b16b78bf966dd0a699474d5edb`, latest release v0.17.0 and open
PRs #25/#26, with no duplicate alternative-pack proposal. That newer main was
not tested here. The bridge public main was
`ce7ebd5af7d1098346cbc69264e4ac14502186cc`, with no tags/releases; it needs the
reviewed source refresh before upstream links it. Recompute the publication
range at audit time rather than reusing an older candidate's counts.

Before installable presentation, select a public release containing formats
and guarded answers, build that exact core and re-check the exact final bridge
candidate. Record pack check/apply and the basic hook-to-reply adoption slice,
classify per-capability limits, and check the instance's rendered `<root>/pack`
before any operator-owned apply. Local aligned hashes below and in SETUP are
historical capability baselines, not public release tags or install targets.

## Capabilities and grants

Only grant calls a crew actually uses. `yardr source grant tgbridge <call>
--crew <crew>` publishes a card; human authority is separately checked against
the private home/operator, current source identity, bot/user/chat and native
proposal. A crew grant never authorizes the crew to approve its own card.

The matrix is the frozen proof at public `52e22d57` and release v0.17.0,
not a claim about untested main. PASS names the exercised surface. Standalone
callbacks or native methods do not establish adoption of this full pack;
normal use through the declared source requires its application first.

| Capability | Public `52e22d57` | v0.17.0 `b8595f6f` | Fork boundary / minimum grant |
| --- | --- | --- | --- |
| Rendered pack, hook, identity/subscription, full thread read, ordinary reply | Native adoption PASS: positive seq, dedupe, full text, one send [A] | Full pack REFUSED on formats; no source applied [A] | Basic SETUP 1–11 omits native keys/grants |
| Send | Native reply via `/v1/send`, posted/ledger recovery PASS [A] | Standalone callback PASS [B]; no release-pack adoption | `send` only when used as a call; no extra grant for thread replies |
| React, pin/unpin, chat ask_close | Standalone callbacks PASS [B] | Standalone callbacks PASS [B] | Individually `react`, `pin`, `unpin`, `ask_close` as used |
| Explicit Markdown reply | Native PASS, exact escaped HTML; scratch declaration without formats refuses before callback [A] | Unsupported: full pack refused | No new thread-reply grant |
| Weighted run_ask, Approve then Confirm | Native PASS [C] | Native case not supported: formats prerequisite refused before body | `run_ask`; identity/proposal/nonce/expiry checks retained |
| Ordinary ask_card | Native PASS, including decline without held [C] | REFUSED: `ask_card_requires_guarded_core` | `ask_card` |
| /decide | Selected-core queue/open/answer/outcome PASS [D] | REFUSED: invalid native `UserAsks` response | Native operator mapping; `ask_card` for ordinary cards |
| Siding fold/release | Native plain FoldBeads and guarded Advance PASS [E] | Standalone native PASS [E]; full-declaration registration separately refused on formats | `siding_card`; existing plain-fold race remains |
| Group add/set | Native supported fields PASS; group-set `model=` REFUSED [C] | Native setup unsupported: `effort` config key refused | `model=` is fork-only; same `run_ask` |
| Source add | Native new/free/orphan creation PASS; replacement missing grants disclosure REFUSED before send/write [C] | Native case not supported: formats prerequisite refused before body | Same `run_ask`; explicit replacement-grants disclosure required |
| Embedded/directory source-pack run cards | Native fresh/orphan cases PASS; replacements missing grants disclosure REFUSED [C] | Native case not supported: formats prerequisite refused before body | Same `run_ask`; complete directory setup/manifest snapshot contract remains fork-only |
| /quota, /mayor_model | Native PASS [F] | Native PASS [F] | Does not prove public pack or mayor/skills installation |
| skill add, crew/group skills= | Unsupported; skill card refuses whole, /decide terminal-only [G] | Unsupported [G] | Fork-only operator terminal workflow; no skill card/grant |

Evidence locations (the named tests use fake Telegram and isolated real native
RPC/store where stated):

- [A] [bridge/test_adoption.py](../bridge/test_adoption.py),
  `NativeAdoptionTests.test_rendered_pack_hook_reply_retry_markdown_and_full_thread`;
  [yardr/tests/test_pack.py](../yardr/tests/test_pack.py),
  `DeclarationTests.test_yardr_reads_the_pack_and_sends_explicit_attachment_kinds`.
- [B] [bridge/test_yard_api.py](../bridge/test_yard_api.py), `YardApiTests`:
  `test_send_answers_delivered_with_the_message_id_and_the_conversation`,
  `test_react_sets_a_named_or_emoji_reaction_on_a_message`,
  `test_pin_keeps_a_slot_pinned_and_unpins_it`, `test_ask_close_closes_a_recorded_ask`.
- [C] [bridge/test_run_asks.py](../bridge/test_run_asks.py), `RealNativeTests`:
  `test_real_native_source_limit_and_trust_weight_apply_once`,
  `test_real_native_decision_decline_without_held_works`,
  `test_real_native_group_add_and_set_apply_once_through_their_gates`,
  `test_real_native_source_and_pack_apply_once`, `test_real_native_source_contract_and_authority`.
- [D] [bridge/test_decide.py](../bridge/test_decide.py),
  `RealDecideTests.test_real_core_lists_opens_answers_and_reports_outcomes`.
- [E] [bridge/test_siding_cards.py](../bridge/test_siding_cards.py), `SidingTests`:
  `test_card_folds_exactly_the_shown_beads_and_unfold_returns_them`,
  `test_release_card_moves_one_bead_on_one_tap`.
- [F] [bridge/test_yard_commands.py](../bridge/test_yard_commands.py),
  `RealNativeTests.test_real_native_quota`, `MayorRealNativeTests.test_real_native_mayor_model`.
- [G] [bridge/test_run_asks.py](../bridge/test_run_asks.py),
  `test_skill_add_refuses_before_send_and_lists_terminal_only`.

Long polling, topics, buttons/menu/add-ons, typing, media, silent replies and
optional local transcription are bridge features covered by its offline
suite; this does not add a native adoption claim for each feature. Unsupported
methods, unknown payloads and oversized advanced cards refuse whole. Version
strings alone do not establish capability.

The basic slice uses SETUP steps 1–11 without native home/operator or
native-card grants. Public builds lack `skill add` and `skills=`: skip the
crew-skills section and associated skill install/upgrade/rollback steps.
The bridge carries no skill card; skill asks refuse before send with a
terminal-only `/decide` line. Optional native keys and per-call grants belong
on one primary bridge per yard, under the operator's authority.

Source/pack repair `757c340` trusts canonical native Does prose while retaining
structural validation, inventory/command constraints, current actor/identity/
proposal binding and explicit replacement-grants disclosure. Missing grants
framing refuses public replacements; it is never assumed safe. New-source
and fresh pack cards show the complete carried declaration/program/diff review
before controls. Native directory digest review on public core does not
include a snapshot of setup/manifest files. That full snapshot contract is
fork-specific; see [source pack bundles](../bridge/OPERATIONS.md#source-pack-bundles).
Weighty cards use Approve then Confirm on the same card within 60 seconds;
typed numbers never approve. Ordinary decisions retain guarded ask turns.

Native run cards exclude depot-kind/summary pack plans, `crew add`,
`depot push`, `skill add`, secret/setup/identity operations and
parameter input; use the full native client for those. Unknown/oversized
cards refuse whole. Ordinary asks have no operation/weight/file payload.
Siding mutations support only
`guard=store` with no refusals; repository flow or until inputs are visibly
status-only. Stale proposals, ambiguous writes and pruned outcomes require
canonical reconciliation; another key is not a retry workaround.

Reserved callback prefixes are `ask:`, `runask:`, `askcard:`, `siding:` and,
with native keys, `decide:` plus the `/decide` command/menu. Overlapping
shorter or longer handler prefixes also conflict. Rename conflicting custom
handlers and their generated callbacks before upgrading; old posted handler
buttons need operator reconciliation. This is a tested fail-before-write
configuration diagnostic, not an assertion of universal drop-in compatibility.

## Committed-tree scan and export

From the clean reviewed checkout:

```sh
nice -n10 sh scripts/test.sh
python3 -B scripts/public_readiness.py
python3 -B scripts/public_readiness.py --export /absolute/path/to/new-review-export
```

The scan reads committed blobs only and refuses a dirty/untracked tree. It
reports category, path, line and counts, never matched bytes. It checks bot-token
syntax, private-key markers, known former operator absolute paths in product
files, accidentally tracked runtime secret/DB names and unreviewed NUL-bearing
binaries. Only hash-pinned offline archives and the two synthetic README captures
under `docs/images/` are approved binary inputs, each only at its path and exact
SHA-256.
It also rejects former backend relationship/provenance references, including
case/separator variants, split-line names, the former fork revision and API
name, and unknown prefixed identifiers. The exact compatibility tokens and
fixture exceptions above do not exempt surrounding prose or entire files.
It allows generic `/home` or `/Users` examples, author/legal links, retained
compatibility names and path migration fixtures. It does not read private settings, env,
units, stores or Git history. It never deletes attachments or scrubs executable
data. Review each finding at its source privately; do not paste values into
logs. A clean result is a focused gate, not a general secret detector: opaque
credentials, account identifiers, non-NUL attachments and contextual company
material still require a human publication review.

Export creates a new owner-only directory outside the worktree, refuses to
overwrite it and writes `source.tar` plus `manifest.json`. It verifies that
Git's archive has every committed file, matching bytes and executable bits;
export-ignore/subst omissions or unsupported tree types refuse rather than
silently dropping content. The manifest records commit/tree IDs, each path's
blob ID, mode, byte count and SHA-256, and the archive SHA-256. No `.git`, history,
untracked files, installed releases or private data are added. No initial
commit, guessed author or remote is created. The export is a review artifact;
`install.sh` still requires a clean Git checkout, so an extracted archive alone
is not an installable checkout.

## Remaining release steps

1. Complete ordinary/expert and capability-claim review, then local landing.
   Run the default committed-head gate again on the actual landing tree with
   `YARDR_LANDING=1`. Source landed locally is not source published or installed.
2. Audit the exact final bridge candidate from the then-current public
   ancestor: tree and complete history exposure, committed scan/export,
   manifest/archive, attachments, attribution and contextual private material.
   Freeze commit/tree IDs, source range and archive hashes. Re-run the default
   gate and selected public-core suite plus separate pack suite. Classify the
   fork-only skill failure as a known limitation, never all-green; any new
   unclassified incompatibility blocks publication. A moved candidate requires
   renewed exact-candidate acceptance.
3. The publication coordinator fast-forwards the audited exact bridge ref to
   the existing public repository and verifies the published SHA. Then publish
   the accepted upstream documentation preview and open its PR. The existing
   source-publication decision covers this reviewed refresh/proposal without
   a second confirmation or core-release wait. No repository creation,
   visibility change, tag, runtime install or activation is included. Public
   exposure cannot be undone; corrections use a new commit, and the proposal
   can be withdrawn by closing the PR.
4. Installable presentation alone waits for a compatible public core release
   and the [exact-candidate re-check](#public-core-release-gate). A predicted
   release, merged change or source-built-main proof does not close this gate.
5. After a separate operational instruction, each instance's operator owns
   install, source apply, minimum grants, activation and acceptance: probe,
   check-install, correct ExecStart/current, source health, ordinary chat/topic/
   file and any enabled native card. Check that another instance's release,
   config, state and binding did not change. Keep rollback target, private
   settings/credentials and ledger; rollback uses the existing installer and
   matching rendered pack with the same alias/env bindings. The first move
   retains `service.previous`. Never reset delivery state or retry ambiguous
   writes under a new key. Source-only tests do not establish live acceptance.
