#!/usr/bin/env python3
"""Host-side helper: assemble + upload + run Z80 programs on the EBAZ4205.

This is the easy entry point when you have a .s assembly source on your PC.

Typical usage (from repo root):

  # One shot: assemble + load + run on board, print output
  python3 demos/z80_asm/z80.py demos/z80_asm/src/counter.s -n 64

  # Interactive program (needs a TTY — uses ssh -t)
  python3 demos/z80_asm/z80.py demos/z80_asm/src/echo.s -i

  # Run pre-assembled binary
  python3 demos/z80_asm/z80.py run demos/z80_asm/bin/counter.bin -n 64

  # Assemble only
  python3 demos/z80_asm/z80.py assemble demos/z80_asm/src/counter.s -o /tmp/counter.bin

Requires:
  - ssh access to the board (default host: ebaz, root)
  - Board running the z80_soc design with /dev/mem and axis_fifo
  - binutils-z80 for assembly (apt install binutils-z80)

For day-to-day interactive use, prefer installing on the board and logging in:
  ./demos/z80_asm/install_to_board.sh && ssh -t ebaz z80 echo.bin -i
"""

from __future__ import annotations

import argparse
import hashlib
import os
import shlex
import subprocess
import sys
import tempfile
from pathlib import Path

HERE = Path(__file__).resolve().parent
DEFAULT_HOST = os.environ.get("EBAZ_HOST", "ebaz")
DEFAULT_REMOTE_DIR = os.environ.get("EBAZ_Z80_DIR", "/tmp/z80")
BOARD_RUNNER = HERE / "run_z80_program.py"
ASSEMBLER = HERE / "assemble_z80.py"

# ---------------------------------------------------------------------------
# helpers
# ---------------------------------------------------------------------------

def sha256_hex(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def die(msg: str, code: int = 1) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(code)


def run_cmd(
    cmd: list[str],
    *,
    check: bool = True,
    capture: bool = False,
    input_bytes: bytes | None = None,
    quiet: bool = False,
) -> subprocess.CompletedProcess:
    if not quiet:
        print("+", " ".join(shlex.quote(c) for c in cmd), file=sys.stderr)
    return subprocess.run(
        cmd,
        check=check,
        input=input_bytes,
        stdout=subprocess.PIPE if capture else None,
        stderr=None,
    )


def ssh_cmd(
    host: str,
    cmd_shell: str,
    *,
    quiet: bool = False,
    tty: bool = False,
) -> int:
    """Run a command on the board over SSH."""
    ssh_args = ["ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10"]
    if tty:
        ssh_args.append("-t")
    ssh_args.extend([host, cmd_shell])
    if not quiet:
        print("+", " ".join(shlex.quote(a) for a in ssh_args), file=sys.stderr)
    return subprocess.call(ssh_args)


def ssh_pipe(
    host: str,
    cmd_shell: str,
    input_bytes: bytes,
    *,
    quiet: bool = False,
) -> bytes:
    """Pipe data into a remote command and capture stdout."""
    ssh_args = [
        "ssh", "-o", "BatchMode=yes", "-o", "ConnectTimeout=10",
        host, cmd_shell,
    ]
    if not quiet:
        print("+", " ".join(shlex.quote(a) for a in ssh_args), file=sys.stderr)
    r = subprocess.run(ssh_args, input=input_bytes, capture_output=True)
    return r.stdout


# ---------------------------------------------------------------------------
# Actions
# ---------------------------------------------------------------------------

def cmd_assemble(args):
    """Assemble .s to .bin."""
    asm_args = [
        sys.executable, str(ASSEMBLER),
        args.source,
    ]
    if args.output:
        asm_args.extend(["-o", args.output])
    if args.quiet:
        asm_args.append("-q")
    # Add --org if provided
    if hasattr(args, 'org') and args.org is not None:
        asm_args.extend(["--org", hex(args.org)])

    run_cmd(asm_args, quiet=args.quiet)


def cmd_run(args):
    """Run a .bin or .s file on the board."""
    source_path = Path(args.source)
    if not source_path.exists():
        die(f"File not found: {source_path}")

    # Determine if we need to assemble first
    is_source = source_path.suffix.lower() in (".s", ".asm", ".z80")
    binary_data: bytes | None = None

    if is_source:
        if not ASSEMBLER.exists():
            die(f"Assembler script not found: {ASSEMBLER}")
        # Assemble to temp file
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tf:
            tmp_bin = tf.name
        try:
            asm_args = [
                sys.executable, str(ASSEMBLER),
                str(source_path),
                "-o", tmp_bin,
            ]
            if hasattr(args, 'org') and args.org is not None:
                asm_args.extend(["--org", hex(args.org)])
            run_cmd(asm_args, quiet=args.quiet)
            binary_data = Path(tmp_bin).read_bytes()
        finally:
            Path(tmp_bin).unlink(missing_ok=True)
    else:
        binary_data = source_path.read_bytes()

    assert binary_data is not None

    # Upload runner + binary to board
    remote_dir = args.remote_dir or DEFAULT_REMOTE_DIR
    host = args.host or DEFAULT_HOST

    # Create remote dir and upload files
    run_cmd([
        "ssh", host,
        f"mkdir -p {remote_dir}",
    ], quiet=args.quiet)

    # Upload the runner script
    with open(BOARD_RUNNER, "rb") as f:
        runner_data = f.read()
    run_cmd([
        "ssh", host,
        f"cat > {remote_dir}/run_z80.py",
    ], input_bytes=runner_data, quiet=args.quiet, check=True)

    # Upload the binary
    bin_name = f"program_{sha256_hex(binary_data)[:8]}.bin"
    run_cmd([
        "ssh", host,
        f"cat > {remote_dir}/{bin_name}",
    ], input_bytes=binary_data, quiet=args.quiet, check=True)

    # Build command-line for the board
    board_args = []
    if args.interactive:
        board_args.append("-i")
    if args.max_bytes:
        board_args.extend(["-n", str(args.max_bytes)])
    if args.max_time:
        board_args.extend(["--max-time", str(args.max_time)])
    if args.input:
        board_args.extend(["--input", args.input])
    if args.output:
        board_args.extend(["-o", args.output])
    if args.quiet:
        board_args.append("-q")
    if args.no_boot_rom:
        board_args.append("--no-boot-rom")

    board_cmd = (
        f"cd {remote_dir} && "
        f"python3 run_z80.py {bin_name} " + " ".join(shlex.quote(a) for a in board_args)
    )

    # Run on board
    code = ssh_cmd(host, board_cmd, quiet=args.quiet, tty=args.interactive)
    return code


def cmd_sim(args):
    """Reference simulation: just show binary contents (no board needed)."""
    source_path = Path(args.source)
    if not source_path.exists():
        die(f"File not found: {source_path}")

    is_source = source_path.suffix.lower() in (".s", ".asm", ".z80")

    if is_source:
        with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tf:
            tmp_bin = tf.name
        try:
            asm_args = [
                sys.executable, str(ASSEMBLER),
                str(source_path),
                "-o", tmp_bin,
                "-q",
            ]
            run_cmd(asm_args, quiet=True)
            binary_data = Path(tmp_bin).read_bytes()
        finally:
            Path(tmp_bin).unlink(missing_ok=True)
    else:
        binary_data = source_path.read_bytes()

    print(f"Binary: {len(binary_data)} bytes")
    for i in range(0, len(binary_data), 16):
        chunk = binary_data[i:i+16]
        hex_str = " ".join(f"{b:02x}" for b in chunk)
        ascii_str = "".join(chr(b) if 0x20 <= b < 0x7f else "." for b in chunk)
        print(f"{i:04x}: {hex_str:<48s}  {ascii_str}")


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def main():
    parser = argparse.ArgumentParser(
        description="Assemble + run Z80 programs on EBAZ4205",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("--host", help=f"SSH target (default: {DEFAULT_HOST})")
    parser.add_argument("--remote-dir", help=f"Remote working dir (default: {DEFAULT_REMOTE_DIR})")
    parser.add_argument("-q", "--quiet", action="store_true", help="Less output")

    sub = parser.add_subparsers(dest="action", required=False)

    # Default action: "run" when a positional is given
    run_p = sub.add_parser("run", help="Assemble (if .s) and run on board")
    run_p.add_argument("source", help=".s source or .bin binary")
    run_p.add_argument("-i", "--interactive", action="store_true",
                       help="Live console")
    run_p.add_argument("-n", "--max-bytes", type=int,
                       help="Stop after N bytes")
    run_p.add_argument("--max-time", type=float, default=10.0,
                       help="Wall-clock limit (default: 10s)")
    run_p.add_argument("--input", help="Feed string to Z80 IN")
    run_p.add_argument("-o", "--output", help="Save output to FILE")
    run_p.add_argument("--no-boot-rom", action="store_true",
                       help="Skip writing boot ROM")
    run_p.add_argument("--org", type=lambda x: int(x, 0), default=None,
                       help="Origin address override")

    ass_p = sub.add_parser("assemble", help="Assemble .s → .bin")
    ass_p.add_argument("source", help=".s source")
    ass_p.add_argument("-o", "--output", help="Output .bin path")
    ass_p.add_argument("--org", type=lambda x: int(x, 0), default=None,
                       help="Origin address override")

    sim_p = sub.add_parser("sim", help="Show binary contents (simulate)")
    sim_p.add_argument("source", help=".s source or .bin binary")
    sim_p.add_argument("-o", "--output", help="Save output to FILE")

    args, extra = parser.parse_known_args()

    # If no subcommand but a positional argument, treat as "run"
    if args.action is None and extra:
        args.source = extra[0]
        args.action = "run"
        args.interactive = False
        args.max_bytes = None
        args.max_time = 10.0
        args.input = None
        args.output = None
        args.no_boot_rom = False
        args.org = None

    if args.action is None and not extra:
        parser.print_help()
        sys.exit(1)

    if args.action == "assemble":
        cmd_assemble(args)
    elif args.action == "sim":
        cmd_sim(args)
    else:
        # Default: run
        cmd_run(args)


if __name__ == "__main__":
    main()
