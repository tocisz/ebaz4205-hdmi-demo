#!/usr/bin/env python3
"""
Compile (optional) and run a Brainfuck / bf1 program on the EBAZ4205 FPGA.

Intended for use *on the board* (e.g. installed as /root/bf1). Accepts either:
  - plain Brainfuck source (.b / .bf / …) — compiled on the fly via comp_bf.py
  - precompiled bf1 bytecode (.bin)

Loads bytecode into code RAM, clears data RAM (fast on-CPU BF program; see
CLEAR_DATA_RAM.md), starts the CPU, and puts the program's UART on your console.

Usage (on board):
  bf1 hello.b
  bf1 hello.b -n 256 -o /tmp/out.txt
  bf1 ghost.b -i                         # interactive: live keyboard ↔ UART
  bf1 prog.b --input $'a\nb\n' -n 100    # batch feed for ',' programs

  Brainfuck programs do not halt the CPU. Batch capture stops on max-bytes,
  UART idle, or max-time (default 30s). Interactive mode (-i) streams output
  live and forwards your keyboard until Ctrl-C / Ctrl-].

Comments:
  Non-command characters are ignored. Bracketed dead-loops like
  ``[this is a comment with dots...]`` are valid BF (skipped when the cell is
  0 — true after data-RAM clear). Unmatched ``[``/``]`` still fail to compile.

Dependencies:
  - Board running the bf1 SoC design (axi_gpreg at 0x7C440000).
  - Root access to /dev/mem and /dev/ttyUL1.
  - Python 3 with mmap, select, struct, termios (stdlib).
  - comp_bf.py beside this script (only needed when given a .b source).
"""

import argparse
import fcntl
import mmap
import os
import select
import struct
import subprocess as sp
import sys
import termios
import time
import tty
from pathlib import Path

# Allow "import comp_bf" when installed as /root/bf1 next to /root/comp_bf.py
_SCRIPT_DIR = Path(__file__).resolve().parent
if str(_SCRIPT_DIR) not in sys.path:
    sys.path.insert(0, str(_SCRIPT_DIR))

# ---------------------------------------------------------------------------
# Physical register map (axi_gpreg at 0x7C440000)
# ---------------------------------------------------------------------------
REG_BASE = 0x7C440000
REG_SIZE = 0x1000  # 4 KB page — base is page-aligned

GP0_OUT = 0x404  # CPU control
GP0_IN  = 0x408  # CPU status
GP1_OUT = 0x444  # Data RAM access (address+data+ctrl)
GP1_IN  = 0x448  # Data RAM read result
GP2_OUT = 0x484  # Code RAM access (address+data+ctrl)
GP2_IN  = 0x488  # Code RAM read result

# GP0 control bits
_HALT  = 1 << 0
_RESET = 1 << 1
_RUN   = 1 << 3

# Memory sizes
CODE_RAM_SIZE = 8192   # 8K × 8
DATA_RAM_SIZE = 32768  # 32K × 8

# UART
UART_DEV = "/dev/ttyUL1"
UART_BAUD = 115200

# ---------------------------------------------------------------------------
# Fast data-RAM clear (binary riding counter on the bf1 CPU)
# 32768 = 2048 × 16; binary counter N=11, K=16.  See CLEAR_DATA_RAM.md.
# Host polls m[0] == 0xFF as the done flag, then zeros m[0] itself.
# ---------------------------------------------------------------------------
CLEAR_DATA_RAM_CODE = bytes((
    0x83, 0x7F, 0x80, 0x50, 0x8F, 0x7F, 0x01, 0x83, 0x7F, 0x80, 0x3F, 0x86,
    0x7F, 0x01, 0x41, 0x3F, 0x80, 0x01, 0x80, 0x30, 0x01, 0x41, 0xA0, 0x42,
    0x0F, 0x83, 0x7F, 0x80, 0x50, 0x8F, 0x7F, 0x01, 0x83, 0x7F, 0x80, 0x3F,
    0x86, 0x7F, 0x01, 0x41, 0x3F, 0x80, 0x01, 0x80, 0x21, 0x0C, 0x83, 0x7F,
    0x80, 0x4C, 0x92, 0x7F, 0x3F, 0x86, 0x7F, 0x10, 0x41, 0x30, 0x80, 0x01,
    0x86, 0x7F, 0x3F, 0x41, 0x01, 0x80, 0x3F, 0x80, 0x10, 0x83, 0x01, 0x80,
    0x41, 0x3F, 0x84, 0x7F, 0x3F, 0x80, 0x01, 0x41, 0x0C, 0x86, 0x34, 0x7F,
    0x0C, 0x7F, 0x80, 0x34, 0x80, 0x3F, 0x7F, 0x82, 0x80,
))

# ===================================================================
# Board control class
# ===================================================================

class Bf1Board:
    """Control the bf1 CPU via mmap'd /dev/mem register access.

    Usage:
        with Bf1Board() as brd:
            brd.reset()
            brd.load_program(code_bytes)
            brd.run()
    """

    def __init__(self):
        fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
        try:
            self._mem = mmap.mmap(
                fd, REG_SIZE, mmap.MAP_SHARED,
                mmap.PROT_READ | mmap.PROT_WRITE,
                offset=REG_BASE,
            )
        finally:
            os.close(fd)

    # -- low-level register helpers ------------------------------------

    def _w(self, off: int, val: int):
        """Write 32-bit little-endian value to register offset."""
        self._mem[off: off + 4] = struct.pack("<I", val)

    def _r(self, off: int) -> int:
        """Read 32-bit little-endian value from register offset."""
        return struct.unpack("<I", self._mem[off: off + 4])[0]

    def _wait_done(self, off: int, max_loops: int = 500_000):
        """Spin until done bit (bit 8) goes high, or raise on timeout."""
        for _ in range(max_loops):
            if self._r(off) & 0x100:
                return
        raise RuntimeError(f"wait_done timeout at offset 0x{off:X}")

    # -- code RAM ------------------------------------------------------

    def code_write(self, addr: int, data: int):
        """Write one byte to code RAM at ``addr`` (0 .. 8191)."""
        assert 0 <= addr < CODE_RAM_SIZE
        self._w(GP2_OUT,
                (addr & 0x1FFF) | ((data & 0xFF) << 16) | (1 << 24))
        self._wait_done(GP2_IN)
        self._w(GP2_OUT, 0)

    def code_read(self, addr: int) -> int:
        """Read one byte from code RAM at ``addr``."""
        assert 0 <= addr < CODE_RAM_SIZE
        self._w(GP2_OUT, (addr & 0x1FFF) | (1 << 25))
        self._wait_done(GP2_IN)
        v = self._r(GP2_IN) & 0xFF
        self._w(GP2_OUT, 0)
        return v

    def code_clear(self, count: int = 16):
        """Zero the first ``count`` code-RAM cells to remove residual state."""
        for i in range(min(count, CODE_RAM_SIZE)):
            self.code_write(i, 0)

    def load_program(self, code_bytes: bytes):
        """Load bytecode after clearing the prefix to at least the program length (min 16)."""
        clear_n = max(len(code_bytes), 16)
        self.code_clear(clear_n)
        for i, b in enumerate(code_bytes):
            self.code_write(i, b)

    def verify_load(self, code_bytes: bytes, n_check: int = 4) -> bool:
        """Read back first ``n_check`` locations and compare."""
        return all(self.code_read(i) == code_bytes[i]
                   for i in range(min(n_check, len(code_bytes))))

    # -- data RAM ------------------------------------------------------

    def data_write(self, addr: int, data: int):
        """Write one byte to data RAM at ``addr`` (0 .. 32767)."""
        assert 0 <= addr < DATA_RAM_SIZE
        self._w(GP1_OUT,
                (addr & 0x7FFF) | ((data & 0xFF) << 16) | (1 << 24))
        self._wait_done(GP1_IN)
        self._w(GP1_OUT, 0)

    def data_read(self, addr: int) -> int:
        """Read one byte from data RAM at ``addr``."""
        assert 0 <= addr < DATA_RAM_SIZE
        self._w(GP1_OUT, (addr & 0x7FFF) | (1 << 25))
        self._wait_done(GP1_IN)
        v = self._r(GP1_IN) & 0xFF
        self._w(GP1_OUT, 0)
        return v

    def clear_data_ram(self, timeout: float = 2.0) -> float:
        """Zero all 32K data-RAM cells via a short on-CPU Brainfuck program.

        Far faster than poking each cell from the PS (tens of ms vs >1 s).
        Loads ``CLEAR_DATA_RAM_CODE``, runs it, polls ``m[0] == 0xFF`` as the
        done flag, halts, then writes ``m[0] = 0`` so the tape is all zeros.

        Returns elapsed seconds. Raises ``RuntimeError`` on timeout.
        """
        t0 = time.monotonic()
        self.halt()
        # Avoid a false done if dirty RAM already had 0xFF at cell 0.
        self.data_write(0, 0)
        self.load_program(CLEAR_DATA_RAM_CODE)
        self.reset()
        self.run()

        deadline = t0 + timeout
        while self.data_read(0) != 0xFF:
            if time.monotonic() >= deadline:
                self.halt()
                got = self.data_read(0)
                raise RuntimeError(
                    f"data RAM clear timed out after {timeout:.1f}s "
                    f"(m[0]=0x{got:02X}, expected 0xFF)"
                )
            time.sleep(0.0005)

        self.halt()
        # Done flag left m[0]=0xFF — restore a fully clean tape for the user program.
        self.data_write(0, 0)
        return time.monotonic() - t0

    # -- CPU control ---------------------------------------------------

    def halt(self):
        """Assert halt signal then release."""
        self._w(GP0_OUT, _HALT)
        self._w(GP0_OUT, 0)
        time.sleep(0.01)

    def reset(self):
        """Assert reset signal then release."""
        self._w(GP0_OUT, _RESET)
        self._w(GP0_OUT, 0)
        time.sleep(0.01)

    def run(self):
        """Start (or resume) CPU execution."""
        self._w(GP0_OUT, _RUN)
        self._w(GP0_OUT, 0)
        time.sleep(0.002)

    def status(self):
        """Return ``(halted: bool, pc: int)`` tuple."""
        s = self._r(GP0_IN)
        return bool(s & 1), (s >> 3) & 0x1FFF

    def close(self):
        try:
            self._w(GP0_OUT, 0)
            self._w(GP1_OUT, 0)
            self._w(GP2_OUT, 0)
        except Exception:
            pass
        if hasattr(self, "_mem"):
            self._mem.close()

    def __enter__(self):
        return self

    def __exit__(self, *args):
        self.close()


# ===================================================================
# UART helpers
# ===================================================================

def setup_uart(device: str = UART_DEV, baud: int = UART_BAUD):
    """Configure the UART port and return a non-blocking fd."""
    sp.run(
        ["stty", "-F", device, str(baud),
         "-echo", "-onlcr", "raw"],
        capture_output=True,
        check=True,
    )
    return os.open(device, os.O_RDWR | os.O_NOCTTY | os.O_NONBLOCK)


def drain_uart(fd):
    """Discard any stale data lingering in the UART RX buffer."""
    while True:
        rl, _, _ = select.select([fd], [], [], 0.05)
        if not rl:
            return
        try:
            if not os.read(fd, 4096):
                return
        except BlockingIOError:
            return


def capture_uart_output(
    fd,
    idle_timeout: float = 0.5,
    min_idle_loops: int = 3,
    max_bytes: int | None = None,
    max_time: float | None = 30.0,
) -> tuple[bytes, str]:
    """Read UART with bounded capture. Returns (data, stop_reason).

    Stop reasons:
      max_bytes — reached byte limit
      max_time  — exceeded wall-clock time limit
      idle      — ``min_idle_loops`` consecutive select timeouts
    """
    out = bytearray()
    idle_count = 0
    t0 = time.monotonic()

    while True:
        # 1. Byte limit check (highest priority)
        if max_bytes is not None and len(out) >= max_bytes:
            return bytes(out[:max_bytes]), "max_bytes"

        # 2. Time limit check
        if max_time is not None and (time.monotonic() - t0) >= max_time:
            return bytes(out), "max_time"

        # 3. Idle detection
        if idle_count >= min_idle_loops:
            return bytes(out), "idle"

        rl, _, _ = select.select([fd], [], [], idle_timeout)
        if rl:
            try:
                to_read = 4096 if max_bytes is None else max(1, max_bytes - len(out))
                chunk = os.read(fd, to_read)
                if chunk:
                    out.extend(chunk)
                    idle_count = 0
                    continue
            except BlockingIOError:
                pass
        idle_count += 1


def feed_uart_input(fd, data: bytes, inter_byte_delay: float = 0.0) -> None:
    """Write ``data`` to the UART (program ``,`` input). Optional pacing."""
    if not data:
        return
    if inter_byte_delay <= 0:
        os.write(fd, data)
        return
    for b in data:
        os.write(fd, bytes([b]))
        time.sleep(inter_byte_delay)


def interactive_session(
    uart_fd,
    max_time: float | None = None,
    max_bytes: int | None = None,
    cr_to_lf: bool = True,
) -> tuple[bytes, str]:
    """Live console bridge: keyboard → UART TX, UART RX → stdout.

    Returns (captured_output, stop_reason) where stop_reason is one of:
      quit / interrupt / eof / max_time / max_bytes

    Keys (when stdin is a TTY, local terminal is put in raw mode):
      Ctrl-C (\\x03)  — stop (same as SIGINT)
      Ctrl-] (\\x1d)  — stop (telnet-style escape; useful if Ctrl-C is trapped)
      Enter           — sent as LF (\\n) when cr_to_lf is True (default)

    Non-TTY stdin (piped input) is still forwarded; EOF ends the session after
    draining remaining UART output briefly.
    """
    out = bytearray()
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    is_tty = os.isatty(stdin_fd)

    old_term = None
    if is_tty:
        old_term = termios.tcgetattr(stdin_fd)
        # raw: deliver keys immediately, no local echo (program paints the screen)
        tty.setraw(stdin_fd)

    # Make stdin non-blocking so select is the sole gate.
    old_stdin_flags = fcntl.fcntl(stdin_fd, fcntl.F_GETFL)
    fcntl.fcntl(stdin_fd, fcntl.F_SETFL, old_stdin_flags | os.O_NONBLOCK)

    t0 = time.monotonic()
    stop_reason = "quit"
    stdin_open = True

    try:
        while True:
            if max_bytes is not None and len(out) >= max_bytes:
                stop_reason = "max_bytes"
                break
            if max_time is not None and (time.monotonic() - t0) >= max_time:
                stop_reason = "max_time"
                break

            rlist = [uart_fd]
            if stdin_open:
                rlist.append(stdin_fd)

            try:
                rl, _, _ = select.select(rlist, [], [], 0.05)
            except InterruptedError:
                stop_reason = "interrupt"
                break

            if uart_fd in rl:
                try:
                    to_read = 4096 if max_bytes is None else max(1, max_bytes - len(out))
                    chunk = os.read(uart_fd, to_read)
                except BlockingIOError:
                    chunk = b""
                if chunk:
                    out.extend(chunk)
                    try:
                        disp = chunk.replace(b'\n', b'\r\n') if is_tty else chunk
                        os.write(stdout_fd, disp)
                    except OSError:
                        pass
                    if max_bytes is not None and len(out) >= max_bytes:
                        stop_reason = "max_bytes"
                        break

            if stdin_open and stdin_fd in rl:
                try:
                    data = os.read(stdin_fd, 256)
                except BlockingIOError:
                    data = b""
                if not data:
                    # EOF on stdin — keep reading UART a little, then exit
                    stdin_open = False
                    # brief drain of any remaining/following output
                    drain_deadline = time.monotonic() + 0.3
                    while time.monotonic() < drain_deadline:
                        rl2, _, _ = select.select([uart_fd], [], [], 0.05)
                        if not rl2:
                            continue
                        try:
                            chunk = os.read(uart_fd, 4096)
                        except BlockingIOError:
                            chunk = b""
                        if chunk:
                            out.extend(chunk)
                            try:
                                disp = chunk.replace(b'\n', b'\r\n') if is_tty else chunk
                                os.write(stdout_fd, disp)
                            except OSError:
                                pass
                            drain_deadline = time.monotonic() + 0.3
                        else:
                            break
                    stop_reason = "eof"
                    break

                # Local stop keys (raw mode delivers these as bytes)
                if b"\x03" in data:  # Ctrl-C
                    stop_reason = "interrupt"
                    break
                if b"\x1d" in data:  # Ctrl-]
                    stop_reason = "quit"
                    break

                if cr_to_lf:
                    data = data.replace(b"\r\n", b"\n").replace(b"\r", b"\n")

                if data:
                    try:
                        os.write(uart_fd, data)
                    except OSError as e:
                        print(f"\n[uart write error: {e}]", file=sys.stderr)
                        stop_reason = "uart_error"
                        break
    except KeyboardInterrupt:
        stop_reason = "interrupt"
    finally:
        fcntl.fcntl(stdin_fd, fcntl.F_SETFL, old_stdin_flags)
        if old_term is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_term)
        # Ensure cursor returns to a fresh line after raw mode
        if is_tty:
            try:
                os.write(stdout_fd, b"\r\n")
            except OSError:
                pass

    if max_bytes is not None and len(out) > max_bytes:
        out = out[:max_bytes]
    return bytes(out), stop_reason


# ===================================================================
# CLI
# ===================================================================

def _looks_like_source(path: str, data: bytes) -> bool:
    """Heuristic: treat as Brainfuck source unless suffix/content says bytecode."""
    suf = Path(path).suffix.lower()
    if suf in {".bin", ".bf1"}:
        return False
    if suf in {".b", ".bf", ".brainfuck", ".txt"}:
        return True
    if not data:
        return False
    sample = data[:64]
    textish = sum(1 for b in sample if 32 <= b < 127 or b in (9, 10, 13))
    return textish >= len(sample) * 0.8


def load_program_bytes(path: str, save_bin: str | None = None) -> tuple[bytes, str]:
    """Load bytecode from a .bin, or compile a .b source. Returns (code, label)."""
    raw = open(path, "rb").read()
    if not _looks_like_source(path, raw):
        return raw, path

    try:
        from comp_bf import compile_source
    except ImportError:
        print(
            "ERROR: need comp_bf.py next to this script to compile source files.\n"
            f"       looked in {_SCRIPT_DIR}",
            file=sys.stderr,
        )
        sys.exit(1)

    text = raw.decode("utf-8", errors="replace")
    try:
        code, stats = compile_source(text)
    except (SyntaxError, ValueError) as e:
        print(f"ERROR: compile failed: {e}", file=sys.stderr)
        sys.exit(1)

    print(
        f"Compiled {path} → {len(code)}B bytecode "
        f"(long_jumps={stats['long_jumps']}, max_depth={stats['max_depth']})",
        file=sys.stderr,
    )
    if save_bin:
        Path(save_bin).write_bytes(code)
        print(f"Saved bytecode to {save_bin}", file=sys.stderr)
    return code, f"{path} (compiled)"


def _looks_printable(data: bytes) -> bool:
    if not data:
        return True
    printable = sum(1 for b in data if 32 <= b < 127 or b in (9, 10, 13))
    return printable >= len(data) * 0.9


def parse_args(argv=None):
    p = argparse.ArgumentParser(
        prog=Path(sys.argv[0]).name,
        description="Compile (optional) & run a Brainfuck/bf1 program on the FPGA.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=(
            "Examples (on board):\n"
            "  %(prog)s hello.b\n"
            "  %(prog)s sierpinski.b -n 1552\n"
            "  %(prog)s ghost.b -i                  # live keyboard ↔ UART\n"
            "  %(prog)s prog.b --input $'a\\nb\\n' -n 100\n"
            "  %(prog)s prog.b --save-bin prog.bin --no-clear-data-ram\n"
            "\n"
            "Batch mode: BF does not halt — capture stops on --max-bytes,\n"
            "UART idle, or --max-time (default 30s). Output is printed.\n"
            "Interactive (-i): live I/O; quit with Ctrl-C or Ctrl-].\n"
            "Comments: non-ops ignored; balanced [dead loops] OK when cell=0.\n"
        ),
    )
    p.add_argument(
        "program",
        help="Brainfuck source (.b) or compiled bf1 bytecode (.bin)",
    )
    p.add_argument(
        "--save-bin",
        metavar="FILE",
        help="When compiling source, also write bytecode to FILE",
    )
    p.add_argument(
        "--output", "-o",
        help="Also save captured UART output to this file",
    )
    p.add_argument(
        "--max-bytes", "-n", type=int, default=None,
        help="Stop capture after this many output bytes",
    )
    p.add_argument(
        "--max-time", type=float, default=None,
        help=(
            "Maximum wall-clock seconds for capture. "
            "Default: 30s in batch mode, unlimited in --interactive."
        ),
    )
    p.add_argument(
        "--timeout", "-t", type=float, default=0.5,
        help="UART idle-detection window in seconds (batch mode; default: 0.5)",
    )
    p.add_argument(
        "--min-idle", type=int, default=3,
        help="Consecutive idle windows before capture stops (batch; default: 3)",
    )
    p.add_argument(
        "-i", "--interactive",
        action="store_true",
        help=(
            "Live console: stream UART output as it arrives and forward "
            "keyboard to the program (for ',' input). Quit: Ctrl-C or Ctrl-]."
        ),
    )
    p.add_argument(
        "--input",
        metavar="DATA",
        default=None,
        help=(
            "Batch-mode only: bytes to send on UART after start "
            "(use $'...' escapes). Prefix @file to read a file."
        ),
    )
    p.add_argument(
        "--input-delay",
        type=float,
        default=0.0,
        metavar="SEC",
        help="Delay between --input bytes (default: 0)",
    )
    p.add_argument(
        "--no-cr-to-lf",
        action="store_true",
        help="Interactive: do not translate Enter (CR) to LF",
    )
    p.add_argument(
        "--no-clear-data-ram", action="store_true",
        help=(
            "Skip zeroing all 32K data RAM before the program "
            "(less deterministic; clear is normally a fast on-CPU BF program)"
        ),
    )
    p.add_argument(
        "--no-verify-load", action="store_true",
        help="Skip read-back check after loading bytecode",
    )
    p.add_argument(
        "-q", "--quiet",
        action="store_true",
        help="Less status noise on stderr (program output still on stdout)",
    )
    return p.parse_args(argv)


def _log(msg: str = "", *, quiet: bool = False, end: str = "\n", flush: bool = False):
    """Status messages go to stderr so stdout is clean program output."""
    if not quiet:
        print(msg, file=sys.stderr, end=end, flush=flush)


# ===================================================================
# Main
# ===================================================================

def main():
    args = parse_args()
    quiet = args.quiet

    # -- 1. Read / compile program ---------------------------------------
    if not os.path.isfile(args.program):
        print(f"ERROR: program file not found: {args.program}", file=sys.stderr)
        sys.exit(1)

    code, label = load_program_bytes(args.program, save_bin=args.save_bin)
    if len(code) > CODE_RAM_SIZE:
        print(
            f"ERROR: bytecode is {len(code)}B, but code RAM is only "
            f"{CODE_RAM_SIZE}B ({len(code) - CODE_RAM_SIZE}B over limit)",
            file=sys.stderr,
        )
        sys.exit(1)
    if not code:
        print("ERROR: empty program", file=sys.stderr)
        sys.exit(1)

    _log(f"Loaded {len(code)}B from {label}", quiet=quiet)

    max_bytes = args.max_bytes
    if args.max_time is None:
        max_time = None if args.interactive else 30.0
    else:
        max_time = args.max_time

    # Resolve batch --input payload (not used in interactive mode)
    input_bytes = b""
    if args.input is not None:
        if args.interactive:
            print(
                "WARNING: --input ignored in --interactive mode "
                "(type on the console instead)",
                file=sys.stderr,
            )
        elif args.input.startswith("@"):
            ipath = args.input[1:]
            if not os.path.isfile(ipath):
                print(f"ERROR: --input file not found: {ipath}", file=sys.stderr)
                sys.exit(1)
            input_bytes = open(ipath, "rb").read()
        else:
            # latin-1 keeps raw byte values from $'\x00' shell escapes
            input_bytes = args.input.encode("latin-1", errors="replace")

    board = None
    fd = None
    try:
        board = Bf1Board()
        board.halt()
        board.reset()

        if not args.no_clear_data_ram:
            _log("Clearing data RAM (on-CPU) ...", quiet=quiet, end="", flush=True)
            try:
                dt_clear = board.clear_data_ram()
            except RuntimeError as e:
                print(f"\nERROR: {e}", file=sys.stderr)
                sys.exit(1)
            _log(f" done ({dt_clear * 1000.0:.1f} ms)", quiet=quiet)

        _log("Loading program ...", quiet=quiet, end="", flush=True)
        board.load_program(code)
        if not args.no_verify_load:
            if not board.verify_load(code, n_check=min(8, len(code))):
                print("ERROR: code RAM readback mismatch", file=sys.stderr)
                for i in range(min(8, len(code))):
                    got = board.code_read(i)
                    exp = code[i]
                    if got != exp:
                        print(
                            f"  code[{i}]: expected 0x{exp:02X}, read 0x{got:02X}",
                            file=sys.stderr,
                        )
                sys.exit(1)
        _log(" ok", quiet=quiet)

        # Reset after load so PC/ptr/stack start clean. Required after the
        # on-CPU clear program, which leaves PC in its trailing spin-loop.
        board.reset()

        fd = setup_uart()
        drain_uart(fd)

        t0 = time.monotonic()
        board.run()

        if args.interactive:
            _log("Interactive — quit with Ctrl-C or Ctrl-]", quiet=quiet)
            if not os.isatty(sys.stdin.fileno()):
                print(
                    "note: stdin is not a TTY (use `ssh -t` for a real keyboard)",
                    file=sys.stderr,
                )
            output, stop_reason = interactive_session(
                fd,
                max_time=max_time,
                max_bytes=max_bytes,
                cr_to_lf=not args.no_cr_to_lf,
            )
        else:
            if input_bytes:
                time.sleep(0.05)  # let CPU reach first ',' if any
                feed_uart_input(fd, input_bytes, args.input_delay)
                _log(f"Fed {len(input_bytes)}B input", quiet=quiet)
            output, stop_reason = capture_uart_output(
                fd,
                idle_timeout=args.timeout,
                min_idle_loops=args.min_idle,
                max_bytes=max_bytes,
                max_time=max_time,
            )

        dt = time.monotonic() - t0
        board.halt()

        # -- print program output (batch; interactive already streamed) --
        if not args.interactive:
            if _looks_printable(output):
                sys.stdout.write(output.decode("utf-8", errors="replace"))
            else:
                sys.stdout.buffer.write(output)
            if output and not output.endswith(b"\n") and _looks_printable(output):
                sys.stdout.write("\n")
            sys.stdout.flush()

        if args.output:
            os.makedirs(os.path.dirname(args.output) or ".", exist_ok=True)
            with open(args.output, "wb") as f:
                f.write(output)
            _log(f"Saved {len(output)}B to {args.output}", quiet=quiet)

        _log(
            f"Done: {len(output)}B in {dt:.2f}s ({stop_reason})",
            quiet=quiet,
        )
        sys.exit(0)

    finally:
        if fd is not None:
            try:
                os.close(fd)
            except OSError:
                pass
        if board is not None:
            try:
                board.halt()
            except Exception:
                pass
            board.close()


if __name__ == "__main__":
    main()
