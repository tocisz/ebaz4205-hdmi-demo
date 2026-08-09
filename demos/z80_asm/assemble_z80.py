#!/usr/bin/env python3
"""Assemble Z80 source (.s) to raw binary using binutils-z80.

Requires: binutils-z80 (z80-unknown-coff-as, z80-unknown-coff-ld)

Usage:
  python3 assemble_z80.py counter.s -o counter.bin
  python3 assemble_z80.py counter.s                      # writes counter.bin in same dir
  python3 assemble_z80.py --org 0x2000 counter.s -o prog.bin
"""

from __future__ import annotations

import argparse
import subprocess
import sys
import tempfile
from pathlib import Path


def assemble(
    src: str | Path,
    *,
    org: int = 0x2000,
    output: str | Path | None = None,
    quiet: bool = False,
) -> bytes:
    """Assemble a Z80 source file to raw binary bytes.

    Args:
        src: Path to .s assembly source.
        org: Origin/load address (default 0x2000 — RAM base).
        output: Optional output path for the .bin file.
        quiet: Suppress stderr messages.

    Returns:
        The raw binary bytes (without leading padding).
    """
    src = Path(src)
    if not src.exists():
        raise FileNotFoundError(f"Source not found: {src}")

    # Step 1: assemble .s → .o
    with tempfile.NamedTemporaryFile(suffix=".o", delete=False) as obj_f:
        obj_path = obj_f.name

    as_cmd = ["z80-unknown-coff-as", "-o", obj_path, str(src)]
    if not quiet:
        print(f"+ {' '.join(as_cmd)}", file=sys.stderr)

    result = subprocess.run(as_cmd, capture_output=True, text=True)
    if result.returncode != 0:
        print(f"Assembly failed:\n{result.stderr}", file=sys.stderr)
        Path(obj_path).unlink(missing_ok=True)
        sys.exit(1)

    # Step 2: link .o → binary (with padding to ORG)
    with tempfile.NamedTemporaryFile(suffix=".elf", delete=False) as elf_f:
        elf_path = elf_f.name

    ld_cmd = [
        "z80-unknown-coff-ld",
        "--oformat", "binary",
        "-Ttext", f"0x{org:X}",
        "-o", elf_path,
        obj_path,
    ]
    if not quiet:
        print(f"+ {' '.join(ld_cmd)}", file=sys.stderr)

    result = subprocess.run(ld_cmd, capture_output=True, text=True)
    Path(obj_path).unlink(missing_ok=True)

    if result.returncode != 0:
        print(f"Link failed:\n{result.stderr}", file=sys.stderr)
        Path(elf_path).unlink(missing_ok=True)
        sys.exit(1)

    # Step 3: read binary, strip leading padding (zeros from 0 to ORG)
    with open(elf_path, "rb") as f:
        data = f.read()

    Path(elf_path).unlink(missing_ok=True)

    # Strip leading zero-padding up to ORG
    if len(data) > org:
        code = data[org:]
    else:
        code = data

    # Also strip trailing zeros (optional but keeps files small)
    code = code.rstrip(b"\x00")

    if not quiet:
        print(f"  Assembled {len(code)} bytes (org=0x{org:04X})", file=sys.stderr)

    # Write output file if requested
    if output:
        out_path = Path(output)
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(code)
        if not quiet:
            print(f"  Written to {out_path}", file=sys.stderr)

    return code


def main() -> None:
    parser = argparse.ArgumentParser(
        description="Assemble Z80 .s source to raw binary",
    )
    parser.add_argument("source", help="Z80 assembly source (.s)")
    parser.add_argument("-o", "--output", help="Output .bin path")
    parser.add_argument(
        "--org", type=lambda x: int(x, 0), default=0x2000,
        help="Origin/load address (default: 0x2000)",
    )
    parser.add_argument("-q", "--quiet", action="store_true", help="Less output")

    args = parser.parse_args()

    src = Path(args.source)
    if not args.output:
        args.output = src.with_suffix(".bin")

    assemble(
        args.source,
        org=args.org,
        output=args.output,
        quiet=args.quiet,
    )


if __name__ == "__main__":
    main()
