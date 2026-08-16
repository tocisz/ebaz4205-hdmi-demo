"""Host-side unit tests for the z80_board package (no hardware required).

Run from the repo root:

    python3 -m unittest discover -s demos/z80_asm/tests -t demos/z80_asm -v

or via the convenience script (works from inside demos/z80_asm):

    ./demos/z80_asm/run_z80_tests.sh
"""

import sys
from pathlib import Path

# Make `z80_board` (and the host helper `z80`) importable when tests are
# discovered by path.
_HERE = Path(__file__).resolve().parent
_TOP = _HERE.parent
if str(_TOP) not in sys.path:
    sys.path.insert(0, str(_TOP))
