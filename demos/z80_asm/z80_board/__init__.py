"""z80_board — interactive Z80 control tool for the EBAZ4205 (z80_soc).

Modules:
  hw       — /dev/mem register access (Z80Board) and AXI-Stream FIFO helpers
  images   — Intel HEX / raw binary image parsing and hexdump formatting
  cli      — command-line dispatcher (halt/run/reset/load/dump/term/flush)
"""

__version__ = "1.0.0"
