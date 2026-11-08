# Native skill fixture

`skill_add.json` is a Bead response captured from an isolated real core
server at a79f6f8162c8 with a synthetic operator/proposer and generic files.
Only ask number, timestamp and source provenance/command were stabilized.
The carried bytes, metadata, inventory, hashes, full diff, quoted Does lines
and code weight retain the core wire shape. No live yard data is used.

The real native integration in `test_run_asks.py` checks this shape against
the running core and exercises snapshot installation and native settlement.
