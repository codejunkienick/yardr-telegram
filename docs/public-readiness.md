# Public-readiness and local release handoff

This is preparation of a working tree for review. Nothing here authorizes
publication, a push, history rewriting, changing repository visibility, live
settings/source/grant writes or service activation. A clean source tree,
installed code and a live accepted instance are three different states.

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

Release order: publish and accept the required yardr core changes first,
then publish this bridge naming the compatible public core. Core publication
belongs to the owner. Until that happens, there is no compatible public core
for the shipped pack, including hook/chat configurations without native keys.

Checked on 2026-11-09: public upstream
[`019090bef09973fb51ca8081b0f764a26a65e60a`](https://github.com/yardr-dev/yardr/commit/019090bef09973fb51ca8081b0f764a26a65e60a)
was built from source. In an isolated home, `yardr pack check yardr/` on
the unchanged shipped pack exited 1 with
`tgbridge.yaml:205: thread.formats: unknown key`. All 61 fetched heads and
tags across upstream and its public fork lacked `thread.formats` declaration
support. Keep `thread.formats: [markdown]` in the pack; omitting native
settings or disabling Markdown use does not make this declaration parse.

The hashes in the table and SETUP are local capability baselines, not public
release tags or install targets. These baselines were absent from the
fetched public history. The public fork's guarded-ask preview
[`10bf0b06e978`](https://github.com/codejunkienick/yardr/commit/10bf0b06e978e2a611dcd3074f878a37da9ae121)
does expose Answer/Defer/DeclineAskExpected, but still lacks the pack's
thread-format declaration support. Public previews of individual contracts
do not establish a compatible core for this pack.

Before bridge publication, replace each unavailable public-core entry below
with a publicly reachable tag or commit containing that contract. Build the
selected core and record a passing `yardr pack check yardr/` against the exact
candidate bridge commit, then check the instance's rendered `<root>/pack`
before applying it. The recorded failure above leaves this release gate open;
local-core tests cannot supply the required public-core success evidence.

## Capabilities and grants

Only grant calls a crew actually uses. `yardr source grant tgbridge <call>
--crew <crew>` publishes a card; human authority is separately checked against
the private home/operator, current source identity, bot/user/chat and native
proposal. A crew grant never authorizes the crew to approve its own card.

Every feature below requires the pack-application row first. Public-core
status is for use with this shipped pack, as checked on 2026-11-09.

| Feature | Required core capability (local baselines) | Compatible public core | Source update grant |
| --- | --- | --- | --- |
| Apply the pack, required even for hook/chat | Source packs accepting `thread.formats: [markdown]` (`0035b8c13e27`) | None yet | None |
| Ordinary send/react/pin/unpin/chat ask close | Source packs and matching loopback API | None yet; pack application blocked | Individually `send`, `react`, `pin`, `unpin`, `ask_close` as needed |
| Routine run ask, weighted direct-reply confirmation | Canonical Bead/ApproveAsk and current operator mapping | None yet | `run_ask` |
| Yard-local skill copies and crew/group assignment | `yardr skill add` and crew/group `skills=` (`a66722bb`) | None yet | None; operator CLI |
| Captured skill file review | Captured single-file proposal contract (`a79f6f8162c8`) | None yet | Same `run_ask` |
| Herdr group add/set | Complete canonical group/argv/role digest contract (`fc7375cf5ad4`) | None yet | Same `run_ask` |
| [Source-add declaration and command file review](../bridge/OPERATIONS.md#source-add-declaration-and-command-review) | Carried source-add snapshots, inventory/diffs and canonical carry outcomes (`546e3fdd2127`) | None yet | Same `run_ask` |
| [Source-kind pack review (directory and embedded)](../bridge/OPERATIONS.md#source-pack-bundles) | Complete carried-source pack bundles; directory snapshot/digest proposal contract (`ced24b0e849c`) for directory applies | None yet | Same `run_ask` |
| Ordinary option/words/manual-stage decisions and `/decide` | Bead ask turns, UserAsks and guarded Answer/Defer/DeclineAskExpected (`7fa0d4c75ff6`) | None yet; preview alone cannot apply pack | `ask_card` |
| Siding status and paired fold/release | Socket-only SidingProposal/Outcome and guarded expected writes (`24bcfd0955c9`), version 1 guard schema | None yet | `siding_card` |
| Explicit Markdown replies | Core explicit reply format transport (`0035b8c13e27`) plus `thread.formats: [markdown]` in installed source | None yet | No new grant for thread replies; `send`/`pin` for those calls |

These are locally verified capability baselines, not a public minimum version
promise. Unsupported/missing/unknown methods or payloads refuse; a version
string alone is insufficient. Existing settings without native home/operator still load
and retain hook/chat behavior on a compatible core; they cannot bypass the
pack's thread-format declaration requirement.

Only one regular text SKILL.md, at most 32 KiB on either side and supported
name/description metadata, can use native captured skill review. Complete
review document and replacement diff precede compact weighted confirmation.
Extra skill files and skill deletion require the full native client.

[`source add <file>`](../bridge/OPERATIONS.md#source-add-declaration-and-command-review)
reviews one declaration and its relative command files, including replacement
diffs and inventory, in one complete review document before controls appear.
Source-kind packs with no parameter inputs also support native review.
Directory `pack apply <absolute-directory> --sha256 <digest>` sends complete
review and snapshot in one document containing every named file,
including `pack.yaml` and setup notes, before controls appear. Approve then
requires a direct reply to the card with the exact ask number within five
minutes. Embedded `pack apply <name>` uses one complete carried-source review
document and core weight rules, without inventing a directory snapshot. See
[pack upgrade setup](../yardr/SETUP.md#the-source-pack-crew-then-you-in-telegram-shell-fallback)
and [source pack bundles](../bridge/OPERATIONS.md#source-pack-bundles).

Native run cards exclude depot-kind/summary pack plans, `crew add`,
`depot push`, multi-file skills, secret/setup/identity operations and
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
binaries. Only hash-pinned offline archives are approved binary inputs.
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

1. Complete normal/expert review and local landing. Run the committed-head
   gate again on the actual landing tree with `YARDR_LANDING=1`.
2. Close the [public-core release gate](#public-core-release-gate): the owner
   publishes the required core changes first; name the compatible public
   tag/commit for every capability and attach a passing pack check with that
   core on the exact candidate pack. This bridge stays unpublished until then.
3. Regenerate scan/export from that clean landed commit before freezing the
   candidate tree and running the final audit; landing may squash and change
   its commit ID. The final compatibility/publication auditor must
   compare that exact tree/manifest/archive and review the remaining contextual
   identifiers, attribution, attachments, license sources and old history
   separately. This work does not certify historical exposure or rewrite it.
4. Resolve the destination/history strategy and exact public payload with the
   owner. Preserve this development repository. A future explicitly authorized
   publication step may use the reviewed clean export; none runs here.
5. Each instance's operator owns installation, source apply, minimum grants,
   activation and acceptance: probe, check-install, correct ExecStart/current,
   source health, ordinary chat/topic/file and any enabled native card. Verify
   another instance's release/config/state/binding did not change. Source-only
   fixtures prove compatibility/isolation under test, not live acceptance.
   Keep the rollback target and private state until those checks pass.
