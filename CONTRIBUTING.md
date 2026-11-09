# Contributing

Thanks for helping improve this pack. Every yard that adopts it runs its own
installed copy of the same code, each configured only by its own settings
file, so changes must stay true for every adopter. Instances on one host may
run different releases, and an operator may roll one back
(`docs/design/independent-instances.md`).

## Run the tests

```sh
sh scripts/test.sh
```

The suite is offline: it fakes Telegram and whisper-cli,
uses temporary directories, and reaches no external network. When yardr is on
PATH it also starts an isolated native store/server and proves a stored
operation executes once; that test skips without the binary. Its subprocesses
use a temporary HOME/state/PATH with no YARDR variables. It needs no real
settings file and starts no unit. It needs Python 3.11 or later, and curl for the tests that run the real curl against a loopback server (they skip without it). Extra arguments go to unittest, for
example `sh scripts/test.sh -v`. CI runs the same suite on Ubuntu and macOS with Python 3.11 and 3.13, and Windows is covered only through the Linux job because hosted Windows runners cannot run WSL 2.

## Settings compatibility

Every settings key an existing yard's file already holds keeps loading with
the same meaning. A new behaviour is a new key with a default that
preserves the old behaviour; never repurpose or silently drop a key.
A settings problem refuses to load with one line naming it.

## Release compatibility

- The SQLite ledger a release writes stays readable by the release before
  it, so `install.sh --rollback` works without restoring state.
- The shared transcription queue's on-disk protocol (`QUEUE_SUBDIR`,
  `queue.lock` and the tickets in `transcriber/voice_transcribe.py`) stays
  compatible across releases: instances on one host may run different ones
  against the same queue.
- An installed runtime imports only the standard library and its own
  release's `vendor/`: nothing from the checkout, another instance, a shared
  virtual environment or the network.

## What belongs here

- New setup/examples use fictional operators and yards, with configurable
  paths/ports/timezones. Keep protocol names, legacy source aliases, compatibility
  fixtures and author/legal attribution. Credentials stay in each yard's
  owner-only files, never in this repository. See `docs/public-readiness.md`.
- The pack ships no `[[command]]` handler. A command or button a yard
  wants is that yard's own handler program, registered in its own
  settings; see "Commands" in `bridge/OPERATIONS.md`.
- Every send goes through the delivery ledger; there is no direct
  send path around it.
- Behaviour changes carry a test in `bridge/` in the same change,
  using the existing fakes.

## Commit messages

Use `<type>(<scope>): <imperative summary>` with a short body that says
what changed and why, for example:

```text
fix(commands): Preserve the callback answer on handler failure

Keep the fixed failure notice and the empty callback answer in the
ledger order the dispatch already used.
```

Keep the subject at most 72 characters, with no final period. Add no
tool, model or AI attribution lines, and no internal tracker or task
ids. When the change fixes a GitHub issue of this repository, end the
body with `Refs #<n>`.
