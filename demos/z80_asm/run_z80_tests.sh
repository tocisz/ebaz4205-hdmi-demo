#!/usr/bin/env bash
# Host-side unit tests for the Z80 board tool (no board, no SSH required).
#
# Usage:
#   ./demos/z80_asm/run_z80_tests.sh
#   python3 -m unittest discover -s demos/z80_asm/tests -t demos/z80_asm -v
set -euo pipefail
cd "$(dirname "$0")"
exec python3 -m unittest discover -s tests -t . -v
