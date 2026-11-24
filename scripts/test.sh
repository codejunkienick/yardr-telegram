#!/bin/sh
# The offline test suite, the bridge's then the yardr pack's: no external network,
# no real settings, no unit.
set -eu
cd "$(dirname "$0")/.."
exec python3 -B scripts/run_tests.py "$@"
