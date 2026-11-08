#!/bin/sh
# The offline test suite, the bridge's then the yardr pack's: no external network,
# no real settings, no unit.
set -eu
cd "$(dirname "$0")/.."
python3 -B -m unittest discover -s bridge "$@"
exec python3 -B -m unittest discover -s yardr/tests "$@"
