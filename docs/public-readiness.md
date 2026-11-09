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

## Capabilities and grants

Only grant calls a crew actually uses. `yardr source grant tgbridge <call>
--crew <crew>` publishes a card; human authority is separately checked against
the private home/operator, current source identity, bot/user/chat and native
proposal. A crew grant never authorizes the crew to approve its own card.

| Feature | Required core capability | Source update grant |
| --- | --- | --- |
| Ordinary send/react/pin/unpin/chat ask close | Source packs and matching loopback API | Individually `send`, `react`, `pin`, `unpin`, `ask_close` as needed |
| Routine run ask, weighted direct-reply confirmation | Canonical Bead/ApproveAsk and current operator mapping | `run_ask` |
| Captured skill file review | Captured single-file proposal contract (`a79f6f8162c8` baseline) | Same `run_ask` |
| Herdr group add/set | Complete canonical group/argv/role digest contract (`fc7375cf5ad4`) | Same `run_ask` |
| Ordinary option/words/manual-stage decisions and `/decide` | Bead ask turns, UserAsks and guarded Answer/Defer/DeclineAskExpected (`7fa0d4c75ff6`) | `ask_card` |
| Siding status and paired fold/release | Socket-only SidingProposal/Outcome and guarded expected writes (`24bcfd0955c9`), version 1 guard schema | `siding_card` |
| Explicit Markdown replies | Core explicit reply format transport (`0035b8c13e27`) plus `thread.formats: [markdown]` in installed source | No new grant for thread replies; `send`/`pin` for those calls |

These are verified capability baselines, not a single minimum version promise.
Unsupported/missing/unknown methods or payloads refuse; a version string alone
is insufficient. Existing settings without native home/operator still load
and retain hook/chat behavior.

Only one regular text SKILL.md, at most 32 KiB on either side and supported
name/description metadata, can use native captured file review. Complete
numbered review parts and replacement diff precede weighted confirmation.
Extra files, deletion, arbitrary source/pack files, secrets, identity/setup,
depot push, parameter input and crew-add prompt payloads are unsupported;
use the full native client. Unknown/oversized cards refuse whole. Ordinary
asks have no operation/weight/file payload. Siding mutations support only
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
binaries. Only the two hash-pinned offline wheels are approved binary inputs.
It allows generic `/home` or `/Users` examples, author/legal links, compatibility
names and path migration fixtures. It does not read private settings, env,
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
2. Regenerate scan/export from that clean landed commit; landing may squash
   and change its commit ID. The final compatibility/publication auditor must
   compare that exact tree/manifest/archive and review the remaining contextual
   identifiers, attribution, attachments, license sources and old history
   separately. This work does not certify historical exposure or rewrite it.
3. Resolve the destination/history strategy and exact public payload with the
   owner. Preserve this development repository. A future explicitly authorized
   publication step may use the reviewed clean export; none runs here.
4. Each instance's operator owns installation, source apply, minimum grants,
   activation and acceptance: probe, check-install, correct ExecStart/current,
   source health, ordinary chat/topic/file and any enabled native card. Verify
   another instance's release/config/state/binding did not change. Source-only
   fixtures prove compatibility/isolation under test, not live acceptance.
   Keep the rollback target and private state until those checks pass.
