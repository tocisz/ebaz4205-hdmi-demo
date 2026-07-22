#!/usr/bin/env python3
"""Host-side helper: compile / simulate / run Brainfuck on the EBAZ4205 bf1 CPU.

This is the easy entry point when you have a plain .b source file on your PC.

Typical usage (from repo root or this directory):

  # One shot: compile + load + run on board, print UART output
  python3 demos/brainfuck_org/bf1.py src/sierpinski.b

  # Interactive program (needs a TTY — uses ssh -t)
  python3 demos/brainfuck_org/bf1.py src/ghost.b -i

  # Compile only
  python3 demos/brainfuck_org/bf1.py compile src/sierpinski.b -o /tmp/s.bin

  # Simulate only (no board)
  python3 demos/brainfuck_org/bf1.py sim src/sierpinski.b -o /tmp/out.txt

  # Run precompiled bytecode
  python3 demos/brainfuck_org/bf1.py run bin/sierpinski.bin --max-bytes 1552

Requires:
  - ssh access to the board (default host: ebaz, root)
  - Board running the bf1 SoC design with /dev/mem and /dev/ttyUL1

For day-to-day interactive use, prefer installing on the board and logging in:
  ./demos/brainfuck_org/install_to_board.sh && ssh -t ebaz bf1 ghost.b -i
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
DEFAULT_REMOTE_DIR = os.environ.get("EBAZ_BF1_DIR", "/tmp/bf1")
BOARD_RUNNER = HERE / "run_bf1_program.py"
COMPILER = HERE / "comp_bf.py"
INTERPRETER = HERE / "bf_interpret.py"


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


def is_bytecode_path(path: Path) -> bool:
    suf = path.suffix.lower()
    if suf in {".bin", ".bf1"}:
        return True
    if suf in {".b", ".bf", ".brainfuck", ".txt"}:
        return False
    # sniff: if mostly non-text, treat as bytecode
    data = path.read_bytes()[:64]
    if not data:
        return True
    textish = sum(1 for b in data if 32 <= b < 127 or b in (9, 10, 13))
    return textish < len(data) * 0.8


def looks_printable(data: bytes) -> bool:
    if not data:
        return True
    printable = sum(
        1 for b in data if 32 <= b < 127 or b in (9, 10, 13)
    )
    return printable >= len(data) * 0.9


def print_output(data: bytes, *, label: str = "UART output") -> None:
    print(f"\n----- {label} ({len(data)} bytes) -----", file=sys.stderr)
    if looks_printable(data):
        try:
            sys.stdout.write(data.decode("utf-8", errors="replace"))
        except Exception:
            sys.stdout.buffer.write(data)
        if data and not data.endswith(b"\n"):
            sys.stdout.write("\n")
        sys.stdout.flush()
    else:
        # hex dump first 256 bytes
        show = data[:256]
        for i in range(0, len(show), 16):
            chunk = show[i : i + 16]
            hexpart = " ".join(f"{b:02x}" for b in chunk)
            asc = "".join(chr(b) if 32 <= b < 127 else "." for b in chunk)
            print(f"{i:04x}  {hexpart:<48}  {asc}")
        if len(data) > 256:
            print(f"... ({len(data) - 256} more bytes)")
        sys.stdout.flush()
    print(f"----- end {label} -----", file=sys.stderr)
    print(f"SHA256: {sha256_hex(data)}", file=sys.stderr)


# ---------------------------------------------------------------------------
# compile / simulate
# ---------------------------------------------------------------------------

def do_compile(src: Path, out: Path | None, quiet: bool = False) -> tuple[Path, bytes]:
    sys.path.insert(0, str(HERE))
    from comp_bf import compile_file  # local import

    out_path, code, stats = compile_file(src, out, quiet=quiet)
    return out_path, code


def do_sim(
    src: Path,
    *,
    out_path: Path | None = None,
    max_steps: int = 200_000_000,
    tape_size: int = 32768,
    input_data: str = "",
) -> bytes:
    cmd = [
        sys.executable,
        str(INTERPRETER),
        str(src),
        "--max-steps",
        str(max_steps),
        "--tape-size",
        str(tape_size),
    ]
    if input_data:
        cmd.extend(["--input", input_data])
    cp = run_cmd(cmd, capture=True)
    # interpreter writes payload to stdout and a stats line to stderr
    data = cp.stdout
    if out_path is not None:
        out_path.parent.mkdir(parents=True, exist_ok=True)
        out_path.write_bytes(data)
        print(f"wrote {len(data)}B expected output to {out_path}", file=sys.stderr)
    return data


# ---------------------------------------------------------------------------
# board deploy / run
# ---------------------------------------------------------------------------

def ssh_base(host: str) -> list[str]:
    return [
        "ssh",
        "-o",
        "BatchMode=yes",
        "-o",
        "ConnectTimeout=10",
        host,
    ]


def remote_write(host: str, remote_path: str, data: bytes, quiet: bool = False) -> None:
    """Write bytes to a remote path via `ssh cat` (no sftp required)."""
    # ensure parent dir exists
    parent = str(Path(remote_path).parent)
    run_cmd(
        ssh_base(host) + [f"mkdir -p {shlex.quote(parent)}"],
        quiet=quiet,
    )
    run_cmd(
        ssh_base(host) + [f"cat > {shlex.quote(remote_path)}"],
        input_bytes=data,
        quiet=quiet,
    )


def ensure_runner_on_board(host: str, remote_dir: str, quiet: bool = False) -> str:
    remote_runner = f"{remote_dir}/run_bf1_program.py"
    remote_comp = f"{remote_dir}/comp_bf.py"
    remote_write(host, remote_runner, BOARD_RUNNER.read_bytes(), quiet=quiet)
    if COMPILER.is_file():
        remote_write(host, remote_comp, COMPILER.read_bytes(), quiet=quiet)
    run_cmd(
        ssh_base(host) + [f"chmod +x {shlex.quote(remote_runner)}"],
        quiet=quiet,
    )
    return remote_runner


def do_run_on_board(
    bytecode: Path | bytes,
    *,
    host: str = DEFAULT_HOST,
    remote_dir: str = DEFAULT_REMOTE_DIR,
    output_local: Path | None = None,
    max_bytes: int | None = None,
    max_time: float | None = 30.0,
    timeout: float = 0.5,
    min_idle: int = 3,
    no_clear_data_ram: bool = False,
    no_verify_load: bool = False,
    keep_remote: bool = False,
    quiet: bool = False,
    name: str = "program",
    interactive: bool = False,
    input_data: str | None = None,
) -> tuple[int, bytes]:
    """Deploy bytecode and run on board. Returns (rc, output bytes)."""
    if isinstance(bytecode, Path):
        code = bytecode.read_bytes()
        name = bytecode.stem
    else:
        code = bytecode

    if not BOARD_RUNNER.is_file():
        die(f"board runner missing: {BOARD_RUNNER}")

    remote_runner = ensure_runner_on_board(host, remote_dir, quiet=quiet)
    remote_bin = f"{remote_dir}/{name}.bin"
    remote_out = f"{remote_dir}/{name}.out"
    remote_in = f"{remote_dir}/{name}.input"

    remote_write(host, remote_bin, code, quiet=quiet)

    cmd_parts = [
        "python3",
        "-u",  # unbuffered — important for live interactive output
        shlex.quote(remote_runner),
        shlex.quote(remote_bin),
    ]
    if not interactive:
        cmd_parts += ["-o", shlex.quote(remote_out)]
    if max_time is not None:
        cmd_parts += ["--max-time", str(max_time)]
    if not interactive:
        cmd_parts += [
            "--timeout", str(timeout),
            "--min-idle", str(min_idle),
        ]
    if max_bytes is not None:
        cmd_parts += ["--max-bytes", str(max_bytes)]
    if no_clear_data_ram:
        cmd_parts.append("--no-clear-data-ram")
    if no_verify_load:
        cmd_parts.append("--no-verify-load")
    if quiet:
        cmd_parts.append("-q")
    if interactive:
        cmd_parts.append("-i")
    if input_data is not None and not interactive:
        # ship input as a file to avoid shell-quoting nightmares
        remote_write(
            host,
            remote_in,
            input_data.encode("latin-1", errors="replace"),
            quiet=quiet,
        )
        cmd_parts += ["--input", f"@{remote_in}"]

    remote_cmd = " ".join(cmd_parts)
    print(f"running on {host}: {remote_cmd}", file=sys.stderr)

    ssh = ssh_base(host)
    if interactive:
        # Allocate a TTY so raw keyboard mode and live output work.
        ssh = [
            "ssh",
            "-t",
            "-o", "BatchMode=yes",
            "-o", "ConnectTimeout=10",
            host,
        ]

    cp = subprocess.run(ssh + [remote_cmd], check=False)
    rc = cp.returncode

    out = b""
    if not interactive:
        try:
            fetch = run_cmd(
                ssh_base(host) + [f"cat {shlex.quote(remote_out)}"],
                capture=True,
                check=False,
                quiet=quiet,
            )
            if fetch.returncode == 0:
                out = fetch.stdout
        except Exception as e:
            print(f"WARNING: could not fetch remote output: {e}", file=sys.stderr)

        if output_local is not None and out:
            output_local.parent.mkdir(parents=True, exist_ok=True)
            output_local.write_bytes(out)
            print(f"saved output to {output_local}", file=sys.stderr)

    if not keep_remote:
        cleanup = (
            f"rm -f {shlex.quote(remote_bin)} {shlex.quote(remote_out)} "
            f"{shlex.quote(remote_in)}"
        )
        run_cmd(ssh_base(host) + [cleanup], check=False, quiet=True)

    return rc, out


# ---------------------------------------------------------------------------
# CLI
# ---------------------------------------------------------------------------

def add_board_args(p: argparse.ArgumentParser) -> None:
    p.add_argument(
        "--host",
        default=DEFAULT_HOST,
        help=f"SSH host for the board (default: {DEFAULT_HOST}, env EBAZ_HOST)",
    )
    p.add_argument(
        "--remote-dir",
        default=DEFAULT_REMOTE_DIR,
        help=f"Remote working directory (default: {DEFAULT_REMOTE_DIR})",
    )
    p.add_argument(
        "--output",
        "-o",
        help="Save captured UART output to this local file",
    )
    p.add_argument(
        "--max-bytes",
        "-n",
        type=int,
        default=None,
        help="Stop capture after N bytes",
    )
    p.add_argument(
        "--max-time",
        type=float,
        default=None,
        help="Capture wall-clock limit in seconds (default: 30 batch / none interactive)",
    )
    p.add_argument(
        "--timeout",
        "-t",
        type=float,
        default=0.5,
        help="UART idle window seconds (batch; default: 0.5)",
    )
    p.add_argument(
        "--min-idle",
        type=int,
        default=3,
        help="Idle windows before stop (batch; default: 3)",
    )
    p.add_argument(
        "-i",
        "--interactive",
        action="store_true",
        help="Live console over ssh -t (keyboard ↔ program UART)",
    )
    p.add_argument(
        "--input",
        default=None,
        help="Batch: bytes to feed to ',' (or @file)",
    )
    p.add_argument(
        "--no-clear-data-ram",
        action="store_true",
        help="Skip 32K data RAM clear (faster, less deterministic)",
    )
    p.add_argument(
        "--no-verify-load",
        action="store_true",
        help="Skip bytecode readback check",
    )
    p.add_argument(
        "--keep-remote",
        action="store_true",
        help="Do not delete remote temp files after run",
    )
    p.add_argument(
        "--bin-out",
        help="Where to write compiled bytecode (default: temp or alongside source)",
    )
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Less verbose host-side logging",
    )


def cmd_compile(args: argparse.Namespace) -> int:
    src = Path(args.source)
    if not src.is_file():
        die(f"source not found: {src}")
    out = Path(args.output) if args.output else src.with_suffix(".bin")
    try:
        path, code = do_compile(src, out, quiet=args.quiet)
    except Exception as e:
        die(str(e))
    print(f"{path}  ({len(code)}B)")
    return 0


def cmd_sim(args: argparse.Namespace) -> int:
    src = Path(args.source)
    if not src.is_file():
        die(f"source not found: {src}")
    out = Path(args.output) if args.output else None
    try:
        data = do_sim(
            src,
            out_path=out,
            max_steps=args.max_steps,
            input_data=args.input or "",
        )
    except subprocess.CalledProcessError as e:
        die(f"simulation failed (exit {e.returncode})")
    except Exception as e:
        die(str(e))
    print_output(data, label="simulated output")
    return 0


def prepare_bytecode(path: Path, bin_out: str | None, quiet: bool) -> tuple[Path, bytes, Path | None]:
    """Return (bin_path, code, source_path_or_None). May write a temp .bin."""
    if is_bytecode_path(path):
        code = path.read_bytes()
        return path, code, None

    # source → compile. Default to a temp file so we don't litter the source dir.
    if bin_out:
        out = Path(bin_out)
    else:
        tmp = tempfile.NamedTemporaryFile(
            prefix=f"{path.stem}_",
            suffix=".bin",
            delete=False,
        )
        out = Path(tmp.name)
        tmp.close()
    out_path, code = do_compile(path, out, quiet=quiet)
    return out_path, code, path


def cmd_run(args: argparse.Namespace) -> int:
    path = Path(args.program)
    if not path.is_file():
        die(f"file not found: {path}")

    try:
        bin_path, code, _src_path = prepare_bytecode(
            path, getattr(args, "bin_out", None), args.quiet
        )
    except Exception as e:
        die(str(e))

    print(f"bytecode: {bin_path} ({len(code)}B)", file=sys.stderr)

    # max_time default: unlimited interactive, 30s batch
    if args.max_time is None:
        max_time = None if args.interactive else 30.0
    else:
        max_time = args.max_time

    out_local = Path(args.output) if args.output else None
    rc, out = do_run_on_board(
        bin_path,
        host=args.host,
        remote_dir=args.remote_dir,
        output_local=out_local,
        max_bytes=args.max_bytes,
        max_time=max_time,
        timeout=args.timeout,
        min_idle=args.min_idle,
        no_clear_data_ram=args.no_clear_data_ram,
        no_verify_load=args.no_verify_load,
        keep_remote=args.keep_remote,
        quiet=args.quiet,
        name=path.stem,
        interactive=args.interactive,
        input_data=args.input,
    )

    # Interactive already streamed to the terminal via ssh -t.
    if not args.interactive:
        if out:
            print_output(out)
        else:
            print("WARNING: no output captured from board", file=sys.stderr)

    return rc


def build_parser() -> argparse.ArgumentParser:
    p = argparse.ArgumentParser(
        prog="bf1",
        description="Compile, simulate, and run Brainfuck on the EBAZ4205 bf1 CPU",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog="""
examples:
  %(prog)s src/sierpinski.b                  # shorthand for: run ...
  %(prog)s src/ghost.b -i                    # interactive (ssh -t)
  %(prog)s compile hello.b -o hello.bin
  %(prog)s sim hello.b
  %(prog)s run hello.bin --max-bytes 100
  %(prog)s run hello.b --host ebaz --no-clear-data-ram

environment:
  EBAZ_HOST      SSH host (default: ebaz)
  EBAZ_BF1_DIR   remote work dir (default: /tmp/bf1)
""",
    )
    sub = p.add_subparsers(dest="cmd")

    p_run = sub.add_parser(
        "run",
        help="Compile if needed, deploy, run on board",
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    p_run.add_argument("program", help="Brainfuck source (.b) or bytecode (.bin)")
    add_board_args(p_run)

    p_c = sub.add_parser("compile", help="Compile .b → bf1 bytecode")
    p_c.add_argument("source", help="Brainfuck source file")
    p_c.add_argument("-o", "--output", help="Output .bin path")
    p_c.add_argument("-q", "--quiet", action="store_true")

    p_s = sub.add_parser("sim", help="Run source on host interpreter")
    p_s.add_argument("source", help="Brainfuck source file")
    p_s.add_argument("-o", "--output", help="Write output bytes to file")
    p_s.add_argument("--max-steps", type=int, default=200_000_000)
    p_s.add_argument("-i", "--input", default="", help="Input for ',' commands")
    p_s.add_argument("-q", "--quiet", action="store_true")
    return p


def main(argv: list[str] | None = None) -> int:
    argv = list(sys.argv[1:] if argv is None else argv)
    parser = build_parser()

    # Shorthand: bf1.py program.b [flags]  →  bf1.py run program.b [flags]
    if argv and argv[0] not in {"run", "compile", "sim", "-h", "--help"}:
        argv = ["run"] + argv

    args = parser.parse_args(argv)

    if args.cmd == "compile":
        return cmd_compile(args)
    if args.cmd == "sim":
        return cmd_sim(args)
    if args.cmd == "run":
        return cmd_run(args)

    parser.print_help()
    return 2


if __name__ == "__main__":
    sys.exit(main())
