#!/usr/bin/env python3
"""Build the split ROM image used by acia_irq_test.s.

The RAM program starts at 0x2000, while the IM 1 handler must be resident at
ROM address 0x0038.  Keeping this tiny image as explicit bytes avoids putting
an ``.org`` directive in the Z80 source: assemble_z80.py already supplies the
linker origin for RAM programs.
"""

from __future__ import annotations

import argparse
from pathlib import Path

ROM_SIZE = 0x4A
RESET_VECTOR = bytes((0xC3, 0x00, 0x20))  # JP 0x2000

# 0x0038:
#   push af
#   in a,(81h)             ; explicit ACIA RX data-register read
#   push af                ; preserve received byte while polling TDRE
# wait_tdre:
#   in a,(80h)
#   and 02h
#   jp z,wait_tdre
#   pop af
#   out (81h),a            ; ACIA echo
#   pop af
#   ei
#   reti
ISR = bytes(
    (
        0xF5,
        0xDB, 0x81,
        0xF5,
        0xDB, 0x80,
        0xE6, 0x02,
        0xCA, 0x3D, 0x00,
        0xF1,
        0xD3, 0x81,
        0xF1,
        0xFB,
        0xED, 0x4D,
    )
)


def build_rom() -> bytes:
    image = bytearray(ROM_SIZE)
    image[: len(RESET_VECTOR)] = RESET_VECTOR
    image[0x0038 : 0x0038 + len(ISR)] = ISR
    return bytes(image)


def main() -> None:
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument(
        "-o", "--output",
        type=Path,
        default=Path(__file__).with_name("bin") / "acia_irq_rom.bin",
        help="ROM binary output (default: demos/z80_asm/bin/acia_irq_rom.bin)",
    )
    args = parser.parse_args()

    image = build_rom()
    args.output.parent.mkdir(parents=True, exist_ok=True)
    args.output.write_bytes(image)
    print(f"Wrote {len(image)} bytes to {args.output}")
    print(f"  reset vector: 0x0000 -> JP 0x2000")
    print(f"  IM 1 handler: 0x0038..0x{0x0038 + len(ISR) - 1:04x}")


if __name__ == "__main__":
    main()
