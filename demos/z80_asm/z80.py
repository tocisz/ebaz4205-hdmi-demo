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

  # Load separate ROM and RAM images
  python3 demos/z80_asm/z80.py run \
      --rom rom.s --rom-org 0x0100 \
      --ram app.s --ram-org 0x2000

  # Session-style control (the SoC stays up between invocations)
  python3 demos/z80_asm/z80.py dump ram 0x2000 64
  python3 demos/z80_asm/z80.py load rom boot.bin --vector 0x0100
  python3 demos/z80_asm/z80.py term --no-flush        # needs a TTY (ssh -t)

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
# Board-side package files that must travel with the runner.
BOARD_PKG = HERE / "z80_board"
TOOL_FILES = ["hw.py", "images.py", "cli.py"]
# Tokens that must never be treated as filenames, even if a cwd file matches.
_NOT_UPLOAD = frozenset({
    "halt", "stop", "run", "start", "reset", "status",
    "load", "dump", "term", "connect", "flush",
    "rom", "ram", "all", "max",
})

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


def prepare_binary(source: str, org: int, quiet: bool) -> bytes:
    """Read a binary or assemble a source at the requested Z80 origin."""
    source_path = Path(source)
    if not source_path.exists():
        die(f"File not found: {source_path}")
    if source_path.suffix.lower() not in (".s", ".asm", ".z80"):
        return source_path.read_bytes()
    if not ASSEMBLER.exists():
        die(f"Assembler script not found: {ASSEMBLER}")
    with tempfile.NamedTemporaryFile(suffix=".bin", delete=False) as tf:
        tmp_bin = tf.name
    try:
        run_cmd([
            sys.executable, str(ASSEMBLER), str(source_path),
            "-o", tmp_bin, "--org", hex(org),
        ], quiet=quiet)
        return Path(tmp_bin).read_bytes()
    finally:
        Path(tmp_bin).unlink(missing_ok=True)


def upload_board_pkg(host: str, remote_dir: str, quiet: bool) -> None:
    """Upload the runner entry point + z80_board package to the board."""
    run_cmd(["ssh", host, f"mkdir -p {remote_dir}/z80_board"], quiet=quiet)
    for fn in TOOL_FILES:
        with open(BOARD_PKG / fn, "rb") as f:
            data = f.read()
        run_cmd(["ssh", host, f"cat > {remote_dir}/z80_board/{fn}"],
                input_bytes=data, quiet=quiet, check=True)
    with open(BOARD_RUNNER, "rb") as f:
        runner = f.read()
    run_cmd(["ssh", host, f"cat > {remote_dir}/run_z80.py"],
            input_bytes=runner, quiet=quiet, check=True)


def _is_upload_token(tok: str) -> bool:
    """True if ``tok`` is a local file that should be copied to the board.

    Any existing regular file is uploaded — relative or absolute — so
    ``load ram ~/hello.hex`` works.  Flags and reserved command words
    (``status``, ``halt``, ``all``, ...) are never treated as files, even
    if a same-named file exists in the cwd.
    """
    if tok.startswith("-") or tok in _NOT_UPLOAD:
        return False
    p = Path(tok)
    return p.exists() and p.is_file()


def remote_tokens(host: str, remote_dir: str, tokens: list[str],
                 quiet: bool) -> str:
    """Upload local files referenced in ``tokens`` and build the remote shell
    command line (with local filenames replaced by upload names)."""
    upload_board_pkg(host, remote_dir, quiet)
    mapped = []
    for tok in tokens:
        if not _is_upload_token(tok):
            mapped.append(tok)
            continue
        data = Path(tok).read_bytes()
        name = f"u_{sha256_hex(data)[:8]}_{Path(tok).name}"
        run_cmd(["ssh", host, f"cat > {remote_dir}/{name}"],
                input_bytes=data, quiet=quiet, check=True)
        mapped.append(name)
    return (f"cd {remote_dir} && python3 run_z80.py "
            + " ".join(shlex.quote(a) for a in mapped))


def cmd_run(args):
    """Load optional ROM and RAM images, then run on the board (legacy)."""
    if args.run_from_rom:
        if not args.source or args.ram_source or args.rom_source:
            die("--run-from-rom requires only the positional source")
        rom_org = args.org if args.org is not None else (
            args.rom_org if args.rom_org is not None else 0x0100)
        rom_data = prepare_binary(args.source, rom_org, args.quiet)
        ram_data = None
    else:
        if args.source and args.ram_source:
            die("specify RAM input either positionally or with --ram, not both")
        ram_source = args.ram_source or args.source
        rom_data = (prepare_binary(args.rom_source, args.rom_org or 0, args.quiet)
                    if args.rom_source else None)
        ram_data = (prepare_binary(ram_source, args.org if args.org is not None
                                   else args.ram_org, args.quiet)
                    if ram_source else None)
        rom_org = args.rom_org

    if ram_data is None and rom_data is None:
        die("provide a RAM source and/or a ROM source")

    remote_dir = args.remote_dir or DEFAULT_REMOTE_DIR
    host = args.host or DEFAULT_HOST

    upload_board_pkg(host, remote_dir, args.quiet)

    uploaded = {}
    for kind, data in (("ram", ram_data), ("rom", rom_data)):
        if data is None:
            continue
        name = f"{kind}_{sha256_hex(data)[:8]}.bin"
        run_cmd([
            "ssh", host, f"cat > {remote_dir}/{name}",
        ], input_bytes=data, quiet=args.quiet, check=True)
        uploaded[kind] = name

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

    if args.run_from_rom:
        board_args.extend(["--run-from-rom", "--rom-org", hex(rom_org)])
        image_args = [uploaded["rom"]]
    else:
        image_args = []
        if "ram" in uploaded:
            image_args.extend(["--ram-image", uploaded["ram"],
                                "--ram-org", hex(args.ram_org)])
        if "rom" in uploaded:
            image_args.extend(["--rom-image", uploaded["rom"]])
            if args.rom_org is not None:
                image_args.extend(["--rom-org", hex(args.rom_org)])

    board_cmd = (
        f"cd {remote_dir} && python3 run_z80.py "
        + " ".join(shlex.quote(a) for a in image_args + board_args)
    )
    return ssh_cmd(host, board_cmd, quiet=args.quiet, tty=args.interactive)


# ---------------------------------------------------------------------------
# New-style interactivity (pass-through to the board tool)
# ---------------------------------------------------------------------------

def cmd_ctrl(args):
    """halt / reset / status / flush: zero-arg control verbs."""
    host = args.host or DEFAULT_HOST
    remote_dir = args.remote_dir or DEFAULT_REMOTE_DIR
    board_cmd = remote_tokens(host, remote_dir, [args.action], args.quiet)
    return ssh_cmd(host, board_cmd, quiet=args.quiet)


def _flag_tokens(args, flags) -> list[str]:
    toks = []
    for attr, board_flag, is_value in flags:
        val = getattr(args, attr, None)
        if val is None:
            continue
        if not is_value and not val:
            continue  # store_true flags are emitted only when True
        toks.append(board_flag)
        if is_value:
            # value flags: keep explicit 0 (e.g. --fill 0) via is-Not-None
            toks.append(val if isinstance(val, str) else hex(val))
    return toks


def _load_tokens(target: str, args) -> list[str]:
    """Build the board ``load`` chain for host ``load``.

    Positionals are FILE [ADDR] only; each flag is emitted exactly once from
    the matching argparse option, so a flag can never be doubled (once via
    ``args.args`` and again via ``_flag_tokens``).
    """
    for tok in args.args:
        if tok.startswith("-"):
            die(f"unexpected token {tok!r} in load positionals: options "
                "like --vector/--fill belong after 'load rom|ram' "
                "(see z80.py load --help)")
    tokens = ["load", target] + list(args.args)
    tokens += _flag_tokens(args, [
        ("vector", "--vector", True),
        ("fill", "--fill", True),
        ("verify_all", "--verify-all", False),
        ("no_verify", "--no-verify", False),
        ("force_halt", "--force-halt", False),
        ("strict", "--strict", False),
    ])
    return tokens


def cmd_host_load(args):
    """z80.py load rom|ram FILE [ADDR] [options] → board load."""
    host = args.host or DEFAULT_HOST
    remote_dir = args.remote_dir or DEFAULT_REMOTE_DIR
    board_cmd = remote_tokens(host, remote_dir,
                              _load_tokens(args.target, args), args.quiet)
    return ssh_cmd(host, board_cmd, quiet=args.quiet)


def _dump_tokens(target: str, args) -> tuple[list[str], str | None]:
    """Build the board ``dump`` chain for host ``dump``.

    Returns (tokens, remote_out_name) where the output name is generated
    here and reused by the caller to fetch the file back.
    """
    tokens = ["dump", target] + list(args.args)
    tokens += _flag_tokens(args, [
        ("force_halt", "--force-halt", False),
    ])
    remote_out = None
    if args.output:
        remote_out = f"dump_{sha256_hex(os.urandom(8))[:8]}.bin"
        tokens += ["-o", remote_out]
    return tokens, remote_out


def cmd_host_dump(args):
    """z80.py dump rom|ram [ADDR [LEN]] [-o FILE] → board dump."""
    host = args.host or DEFAULT_HOST
    remote_dir = args.remote_dir or DEFAULT_REMOTE_DIR
    tokens, remote_out = _dump_tokens(args.target, args)
    board_cmd = remote_tokens(host, remote_dir, tokens, args.quiet)
    rc = ssh_cmd(host, board_cmd, quiet=args.quiet)
    if remote_out is not None and rc == 0:
        with open(args.output, "wb") as f:
            out = ssh_pipe(host, f"cat {remote_dir}/{remote_out}", b"",
                           quiet=args.quiet)
            f.write(out)
        if not args.quiet:
            print(f"Saved dump to {args.output}")
    return rc


def _term_tokens(args) -> list[str]:
    """Board ``term`` chain for host ``term``.

    The board default is keep-buffer; both --flush and --no-flush are sent
    through explicitly so the remote behaviour matches the user's request.
    """
    tokens = ["term"]
    if args.flush is True:
        tokens.append("--flush")
    elif args.flush is False:
        tokens.append("--no-flush")
    return tokens


def cmd_host_term(args):
    """z80.py term [--flush|--no-flush] → attach a live console (ssh -t)."""
    host = args.host or DEFAULT_HOST
    remote_dir = args.remote_dir or DEFAULT_REMOTE_DIR
    board_cmd = remote_tokens(host, remote_dir, _term_tokens(args), args.quiet)
    return ssh_cmd(host, board_cmd, quiet=args.quiet, tty=True)


def cmd_board(args):
    """z80.py board <raw board tool argv...> — full pass-through.

    Example: python3 demos/z80_asm/z80.py board \
        halt load ram app.hex flush reset run
    """
    host = args.host or DEFAULT_HOST
    remote_dir = args.remote_dir or DEFAULT_REMOTE_DIR
    tokens = list(args.tokens)
    if not tokens:
        die("board: nothing to run")
    board_cmd = remote_tokens(host, remote_dir, tokens, args.quiet)
    wants_tty = "term" in tokens or "connect" in tokens
    return ssh_cmd(host, board_cmd, quiet=args.quiet, tty=wants_tty)


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
    run_p.add_argument("source", nargs="?", help="Legacy RAM source or binary")
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
    run_p.add_argument("--run-from-rom", action="store_true",
                       help="Legacy ROM-only mode")
    run_p.add_argument("--ram", dest="ram_source",
                       help="RAM source or binary (can be combined with --rom)")
    run_p.add_argument("--ram-org", type=lambda x: int(x, 0), default=0x2000,
                       help="RAM image Z80 address (default: 0x2000)")
    run_p.add_argument("--rom", dest="rom_source",
                       help="ROM source or binary (can be combined with --ram)")
    run_p.add_argument("--rom-org", type=lambda x: int(x, 0), default=None,
                       help="ROM image Z80 address; nonzero generates JP at 0x0000")
    run_p.add_argument("--org", type=lambda x: int(x, 0), default=None,
                       help="Legacy positional-source origin override")

    ass_p = sub.add_parser("assemble", help="Assemble .s → .bin")
    ass_p.add_argument("source", help=".s source")
    ass_p.add_argument("-o", "--output", help="Output .bin path")
    ass_p.add_argument("--org", type=lambda x: int(x, 0), default=None,
                       help="Origin address override")

    for verb in ("halt", "reset", "status", "flush"):
        sub.add_parser(verb, help=f"Board control: {verb} (pass-through)")

    load_p = sub.add_parser("load", help="Load image into board ROM/RAM")
    load_p.add_argument("target", choices=("rom", "ram"))
    load_p.add_argument("args", nargs="*",
                        help="FILE [ADDR] — the image path and optional "
                             "address only; flags (--vector, --fill, "
                             "--verify-all, --no-verify, --force-halt, "
                             "--strict) are separate options")
    load_p.add_argument("--vector", type=lambda x: int(x, 0), default=None)
    load_p.add_argument("--fill", type=lambda x: int(x, 0), default=None)
    load_p.add_argument("--verify-all", action="store_true")
    load_p.add_argument("--no-verify", action="store_true",
                        dest="no_verify")
    load_p.add_argument("--force-halt", action="store_true",
                        dest="force_halt")
    load_p.add_argument("--strict", action="store_true")

    dump_p = sub.add_parser("dump", help="Dump board ROM/RAM contents")
    dump_p.add_argument("target", choices=("rom", "ram"))
    dump_p.add_argument("args", nargs="*", help="[ADDR [LEN]]")
    dump_p.add_argument("-o", "--output",
                        help="Save raw bytes to local FILE")
    dump_p.add_argument("--force-halt", action="store_true",
                        dest="force_halt")

    term_p = sub.add_parser("term", help="Attach a live Z80 console")
    term_p.add_argument("--flush", action="store_true", default=None,
                        help="Discard FIFO before attaching (default: keep)")
    term_p.add_argument("--no-flush", action="store_false", dest="flush",
                        help="Keep buffered output before attaching (default)")

    board_p = sub.add_parser(
        "board", help="Raw pass-through to the board tool: "
                       "z80.py board halt load ram x.hex reset run")
    board_p.add_argument("tokens", nargs="*", help="Board tool argv")

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
        args.run_from_rom = False
        args.ram_source = None
        args.ram_org = 0x2000
        args.rom_source = None
        args.rom_org = None
        args.org = None

    if args.action is None and not extra:
        parser.print_help()
        sys.exit(1)

    if args.action == "assemble":
        cmd_assemble(args)
    elif args.action == "sim":
        cmd_sim(args)
    elif args.action in ("halt", "reset", "status", "flush"):
        cmd_ctrl(args)
    elif args.action == "load":
        cmd_host_load(args)
    elif args.action == "dump":
        cmd_host_dump(args)
    elif args.action == "term":
        cmd_host_term(args)
    elif args.action == "board":
        cmd_board(args)
    else:
        # Default: run (legacy)
        cmd_run(args)


if __name__ == "__main__":
    main()
