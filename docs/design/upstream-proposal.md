# Offering the independent Telegram bridge upstream

Decision record, 2026-11-14. Propose one documentation link to
[yardr-telegram](https://github.com/codejunkienick/yardr-telegram) as an
alternative to yardr's bundled minimal `telegram` pack. Keep the bridge
independently deployed and keep the bundled pack unchanged. This is public
source preparation; it does not install, apply, grant or activate anything.

## Problem and result

The bridge has long polling, a verified loopback hook, durable delivery and
full message history, topics, buttons, pins/reactions/typing, media, add-on
handlers and optional on-host transcription. Its public documentation and
published source predate the latest alignment. A link that implies released
core compatibility would mislead adopters: the basic path works on the tested
public main revision, but v0.17.0 refuses the unchanged pack.

The smallest honest upstream change is at the end of
[docs/integrations.md, “Telegram, where it differs”](https://github.com/yardr-dev/yardr/blob/32878da29cb401b16b78bf966dd0a699474d5edb/docs/integrations.md#telegram-where-it-differs).
A short subsection should link the refreshed bridge source, state the core
proof/release limit and the operational cost, and tell readers to choose one
integration for each bot. It adds no declaration, pack copy, core API or daemon.

The bundled pack at the
[tested baseline](https://github.com/yardr-dev/yardr/tree/52e22d570cfff58bae725e6df53c047bd63ff4a6/packs/telegram)
uses a public HTTPS webhook, hears operator chats, supports messages/files and
needs no separate bridge service. Its thread reader and posted lookup return
empty results; retrying a reply after a lost answer can post twice. The bridge
adds a ledger/history and richer interaction, at the cost of Python 3.11+ and
one independently updated service, secret set, state directory and rollback
root per bot. `setWebhook` conflicts with `getUpdates`: never apply both
integrations to the same bot.

## Decisions

- Preserve the owner's [2026-11-10 source-only rule](https://github.com/codejunkienick/yardr-telegram/blob/ce7ebd5af7d1098346cbc69264e4ac14502186cc/docs/public-readiness.md#public-core-release-gate).
  Installable presentation waits for a compatible **public release** and a
  re-check of the exact final bridge candidate. Keep `thread.formats: [markdown]`.
- The November 13 continuation permits reviewed source refresh and upstream
  proposal publication before that release. Publication targets the existing
  public repositories after exact-candidate audit and reviews; there is no
  additional confirmation or release wait for those source artifacts.
- Skills decision S1 remains: no skill-install card. `skill add` and crew/group
  `skills=` are fork-only operator terminal workflows, skipped on public core,
  including their upgrade and rollback instructions.
- Match the earlier accepted upstream commits' no-Signed-off-by style. If a
  maintainer actually requires an attestation, bring the exact requirement to
  the author; do not invent an attestation or request one speculatively.
- Builders prepare local commits; the publication coordinator publishes only
  accepted exact refs. No tag, repository creation, runtime deployment,
  source activation, config/grant/account change or live Telegram test is
  included. Operational adoption belongs to each instance operator.

## Frozen compatibility evidence

The bridge proof landed at `4c2b8860d527f7e9efa991b3083494df098298a7`,
including `7b526ef` (selected-core test repair) and `757c340` (trusted native
source/pack rendering). The two actual public runtime targets were:

- main [`52e22d570cfff58bae725e6df53c047bd63ff4a6`](https://github.com/yardr-dev/yardr/commit/52e22d570cfff58bae725e6df53c047bd63ff4a6);
- release [v0.17.0](https://github.com/yardr-dev/yardr/releases/tag/v0.17.0),
  `b8595f6f5c873513e67cb15cc734984b075712d8`.

On main, pack check exited 0 and acknowledged both programs:
`tgbridge/normalize`, `tgbridge/thread`. On the release, it exited 1 with
`thread.formats: unknown key` before apply, even without native keys.
Formats [#20](https://github.com/yardr-dev/yardr/pull/20) and guarded answers
[#17](https://github.com/yardr-dev/yardr/pull/17) are merged after that release;
no duplicate prerequisite PR is needed.

| Exercised surface | Public `52e22d57` | v0.17.0 | Boundary |
| --- | --- | --- | --- |
| Rendered pack → verified hook → identity/subscription → store → reply/read | Native PASS, dedupe and one Telegram send | Full pack REFUSED; adoption test asserts no apply then skips remaining legs | Basic slice without native keys |
| Send | Native reply/posted/ledger recovery PASS | Standalone callback PASS only | No release-pack adoption claim |
| React, pin/unpin, chat ask_close | Standalone callback PASS | Standalone callback PASS | No separate native adoption claim |
| Explicit Markdown reply | Native PASS, exact escaped HTML | Unsupported: pack refused | Removing formats in a scratch declaration refuses before callback |
| Weighted run_ask | Native Approve/Confirm PASS | Native case blocked by formats prerequisite | Existing identity/proposal/one-use gates |
| ask_card | Native PASS | REFUSED: guarded core required | Guarded decline/answer/defer |
| /decide | Native queue/open/answer/outcome PASS | REFUSED: invalid UserAsks turn shape | Current native operator mapping |
| Siding fold/release | Native PASS | Standalone native PASS | Release full-declaration registration refused on formats; plain-fold race unchanged |
| Group add/set | Native supported fields PASS; model= set refused | Native setup refuses effort config | model= is fork-only |
| Source add | Native new/free/orphan PASS; missing replacement grants refuses | Native case blocked before body by formats | Explicit replacement-grants disclosure retained |
| Embedded/directory source-pack cards | Native fresh/orphan PASS; missing replacement grants refuses | Native case blocked before body by formats | Full directory setup/manifest snapshots remain fork-only |
| /quota, /mayor_model | Native PASS | Native PASS | Does not establish pack or mayor/skills installation |
| skill add, crew/group skills= | Unsupported | Unsupported | Fork-only terminal work; skill cards refuse whole |

The [capability record](../public-readiness.md#capabilities-and-grants) maps
each row to exact test names in `bridge/test_adoption.py`, `test_yard_api.py`,
`test_run_asks.py`, `test_decide.py`, `test_siding_cards.py`,
`test_yard_commands.py` and `yardr/tests/test_pack.py`. Bridge-side features
such as media/transcription have offline suite coverage, not a new native
adoption claim for each feature.

Frozen combined totals: aligned default **1097 ran / 1095 passed / 2 skipped /
0 failed**; public main **1097 / 1094 / 2 / 1**; release **1097 / 1084 / 3 / 10**.
On public runs the shell script stopped at the failing bridge suite; the pack
suite was run separately with the same selected executable. These are proof
counts, not the later documentation candidate's fresh default counts.

Public main is not all-green: its sole failure is fork-only `skill add` in
`MayorSliceTests.test_installed_mayor_bot_reaches_its_inbox_and_answers_through_its_own_source`,
before mayor configuration/adoption. No public mayor installation or skills
claim follows. The release failures also include guarded ask_card, /decide
UserAsks shape and group effort setup, as well as formats and skill add.
The two common skips are the removed pre-alignment skill-card method and
Apple-Silicon GPU checking on Linux; the release additionally skips adoption
after asserting pack refusal. No unclassified public-main incompatibility
remained in the frozen proof.

The end-to-end adoption test drives the production bridge drain through real
native hook/RPC/store, with fake Telegram and isolated homes/state. A lost
accept response reuses the delivery ID and gets the same positive seq; an
unknown sender retains the cursor until identity is corrected. A lost send
response reconciles through posted/ledger and produces one Telegram message.
A message longer than 500 characters reads back whole, and explicit Markdown
produces the expected escaped HTML. Existing native tests cover supported
source/pack stale proposals, exact installed bytes/mode, restart/replay and
one operation, alongside identity, key collision, nonce/expiry, persisted
attempt and ambiguous-write checks. This is synthetic acceptance, not live
account or service validation.

`757c340` trusts canonical native Does prose whole rather than rebuilding its
meaning with a prose whitelist. It retains structural, inventory/command,
current actor/identity/proposal checks and replacement-grants disclosure.
Unsupported or oversized advanced cards refuse whole. Fresh directory apply
on public core is distinct from the fork's complete setup/manifest snapshot.

## Rejected alternatives

Editing `packs/telegram` or its SETUP would alter the bundled pack's embedded
digest and couple a separate service to core releases. Copying this pack into
`yardr-packs` would duplicate configuration/rendered-port ownership. Removing
formats for v0.17.0 would create a second mode and contradict the source-only
decision. None is needed for a documentation link. The
[pack API](https://github.com/yardr-dev/yardr/blob/52e22d570cfff58bae725e6df53c047bd63ff4a6/docs/pack-api.md)
gives source commands 30 seconds and offers no long-lived pack daemon: the
poller, ledger and transcription service stay outside core.

## Publication sequence and exact refs

1. Finish this bridge documentation candidate, normal/expert and capability
   reviews, default exact-head tests and committed-tree public scan; land
   locally. Record actual base/head and fresh test counts in review evidence.
2. Audit the then-current public ancestor through the exact landed candidate,
   including source and complete commit-history exposure. Freeze bridge
   commit/tree, range, manifest/archive hashes and committed scan/export.
   Re-run default and selected public-core suites, running pack tests separately
   when the bridge suite fails. The fork-only skill failure is an expected
   limitation, not a pass; any new/unclassified failure blocks publication.
   A changed candidate needs renewed acceptance.
3. Prepare one upstream preview commit changing only `docs/integrations.md`,
   on an explicitly recorded public upstream base. Record exact base/head and
   self-contained problem/behavior/risk/validation prose; run upstream's
   `sh .yardr/test` on that preview in isolation. A newer preview base does not
   inherit the runtime proof: cite `52e22d57` or reproduce a new claim at the
   new exact core ref. Do not land it onto a personal aligned branch.
4. The coordinator fast-forwards the accepted bridge ref to its existing
   public repository and verifies its SHA, then publishes the accepted
   upstream preview and opens the PR. Source artifacts need no release wait.
   The old public bridge ref observed on November 14 was
   `ce7ebd5af7d1098346cbc69264e4ac14502186cc`; it is an observation, not the
   final audit range. Do not reuse superseded refresh counts.

Local source landing, public source publication, upstream PR open/merged,
bridge installation, source apply/grants and a live accepted service are
separate states. The last three are excluded here. Public exposure is
irreversible; source-publication rollback is a corrective commit, and the
upstream proposal can be withdrawn by closing its PR. No history rewrite or
runtime rollback is part of source publication.

Suggested upstream title: “Document the independent Telegram polling bridge”.
The PR prose should describe the separate service and source-only status,
link this evidence and the audited published bridge ref, state that the bundled
pack/digest is untouched, explain the one-bot conflict, and give exact upstream
base/head plus its gate result. The preview is not frozen or published by this
record; its later review must supply those actual refs and results.

## Operator-owned installation, usability and rollback

After a compatible released-core result and a separate operational
instruction, follow [SETUP steps 1–11](../../yardr/SETUP.md) for the basic slice.
Use the existing independent installer and rendered pack check/apply, verified
hook, source credential, operator identity, answering crew env/subscription and
one unit per bot. Omit native home/operator and native-card grants; skip
fork-only skill add/skills= and related upgrade/rollback instructions.
Ordinary replies need no new call grant. Grant send/react/pin/unpin/ask_close/
tell individually only where used. Optional native run_ask/ask_card/siding_card
grants and keys remain operator-owned, normally on one primary bridge per yard,
with the per-build limitations above.

Before broadening, verify paired chat → accepted delivery → intended crew
reply → one Telegram post → full thread readback. Run probe/check-install and
source health, verify the installed release and ExecStart/current, and check
that another instance's release/config/state/binding did not change. Optional
native cards need their own live usability checks; source-only proof does not
supply them.

Runtime rollback uses the existing `install.sh --rollback`, matching rendered
pack and the same selected alias/env-file binding, then the same unit restart.
The first checkout-to-release move retains the documented `service.previous`
fallback. Keep private settings/credentials, ledger and rollback target; do
not reset delivery state or repeat ambiguous writes under a new key. This
record performed no runtime install, activation or live rollback.

## Remaining waits and risks

The source refresh waits for its exact-candidate audit and acceptance; the
upstream preview waits for that audit's published-source facts. Their owner is
the publication coordinator, who proceeds with accepted refs when released.
Installable presentation waits only on maintainers publishing a compatible
core release and the coordinator's exact-candidate compatibility re-check;
then each operator separately owns operational adoption. No predicted release
or installed aligned binary substitutes for that event.

Read-only November 14 preflight saw upstream main
`32878da29cb401b16b78bf966dd0a699474d5edb`, which is newer and untested here.
Open [#26](https://github.com/yardr-dev/yardr/pull/26) changes source-card prose;
future release checks must account for native shapes and whole-card refusal.
Public-main drift, private context missed by scanners and irreversible history
exposure require exact-ref audit. Maintainers may decline an external link;
a docs-only proposal is cheap to withdraw. Renumbering the pack API is not a
prerequisite or part of this change. Keep source-only presentation until the
release gate closes, even if release is expected soon.
