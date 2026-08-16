#!/usr/bin/env python3
"""Z80 board tool entry point (EBAZ4205 z80_soc).

Installed on the board as /root/z80 (symlinked from /usr/bin/z80).

The implementation lives in the z80_board package (hw.py, images.py, cli.py)
next to this file.  This thin wrapper only puts the package on sys.path and
delegates to z80_board.cli.main().

See the usage text of z80_board.cli for the command list (halt/run/reset/
status/load/dump/flush/term), or run:
    z80 --help
"""

import sys
from pathlib import Path

if __package__ in (None, ""):
    here = Path(__file__).resolve().parent
    if (here / "z80_board").is_dir():
        sys.path.insert(0, str(here))

from z80_board.cli import main  # noqa: E402

if __name__ == "__main__":
    sys.exit(main())
