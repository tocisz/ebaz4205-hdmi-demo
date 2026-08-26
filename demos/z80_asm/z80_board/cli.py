"""z80_board.cli — command-line front-end for the Z80 board tool.

The board tool (installed as /root/z80) controls a *live* z80_soc CPU.
Each invocation performs one or more small operations instead of the
legacy all-in-one load→run→halt flow.  The CPU, BRAMs and AXI FIFO stay
up between invocations.

Common flows::

    z80 halt load ram prog.hex reset run       # chain on one line
    z80 flush term --flush                     # attach terminal, discard buffer
    z80 halt dump ram 0x2000 64                # inspect RAM after a run
"""

import argparse
import errno
import os
import select
import sys
import termios
import time
import tty
from pathlib import Path

from . import hw
from .images import ImageError, format_hexdump, parse_image

# ---------------------------------------------------------------------------
# Verbs
# ---------------------------------------------------------------------------
VERBS = ("halt", "stop", "run", "start", "reset", "status",
         "load", "dump", "term", "connect", "flush")
RESERVED = set(VERBS)
ALIASES = {"stop": "halt", "start": "run", "connect": "term"}

USAGE = """\
z80 — interactive Z80 SoC control (EBAZ4205)

Usage:
  z80 <command> [args] [<command> [args] ...]

Commands:
  halt | stop            Pulse the CPU halt line (idempotent)
  run  | start           Pulse the CPU run line (resume; PC unchanged)
  reset                  Pulse reset; CPU ends halted at PC=0
  status                 Print CPU state (halted / running)
  load rom|ram FILE [ADDR]
                         Write an image (Intel HEX or raw binary) to ROM/RAM.
                           rom defaults to 0x0000, ram to 0x2000.
                           HEX records carry addresses; [ADDR] is an added base.
                           Options: --vector ADDR (rom only, writes JP vector at 0),
                           --fill BYTE (zero-fill loaded region, ram only),
                           --verify-all | --no-verify, --force-halt, --strict
  dump rom|ram [ADDR [LEN]]
                         Read memory; hexdump to stdout. rom default: 0 0x2000,
                         ram default: 0x2000 256. LEN 'all' = rest of space.
                           Options: -o FILE (raw bytes), --binary, --force-halt
  flush                  Reset + drain the AXI FIFO (discard both directions)
  term | connect         Attach stdin/stdout to the Z80 I/O stream.
                           Options: --flush (discard FIFO before attach;
                           default keeps buffered output)
                           Detach with Ctrl-] ; the CPU keeps running.

Legacy one-shot (compatibility, halts CPU on exit):
  z80 run FILE.bin [old options]
  z80 FILE.bin [old options]         (positional form)
  Old options: -i -n N --max-time S --input S -o FILE --no-boot-rom
               --run-from-rom --rom-image --rom-org --ram-image --ram-org

Chaining: commands run left to right; 'term' must be last.
Examples:
  z80 halt load ram /root/z80-examples/counter.bin reset run
  z80 halt load rom rom_ebaz.bin load ram hello.hex flush reset run
  z80 halt dump ram 0x8400 64
  z80 reset run term --no-flush
"""


def _err(msg: str) -> None:
    print(f"ERROR: {msg}", file=sys.stderr)
    sys.exit(1)


def _int(v: str, what: str = "number") -> int:
    try:
        return int(v, 0)
    except ValueError:
        _err(f"bad {what}: {v!r}")


def _len_or_all(v: str) -> int | None:
    if v.lower() in ("all", "max"):
        return None
    return _int(v, "length")


# ---------------------------------------------------------------------------
# Per-verb argument scanners
# ---------------------------------------------------------------------------

def _parse_load(cargs: list[str]) -> tuple[dict, list[str]]:
    opts = {"vector": None, "fill": None, "verify_all": False,
            "no_verify": False, "force_halt": False, "strict": False}
    pos: list[str] = []
    i = 0
    while i < len(cargs):
        a = cargs[i]
        if a == "--vector":
            if i + 1 >= len(cargs):
                _err("--vector requires an address")
            opts["vector"] = _int(cargs[i + 1], "vector address")
            i += 2
        elif a == "--fill":
            if i + 1 >= len(cargs):
                _err("--fill requires a byte value")
            opts["fill"] = _int(cargs[i + 1], "fill byte") & 0xFF
            i += 2
        elif a == "--verify-all":
            opts["verify_all"] = True
            i += 1
        elif a == "--no-verify":
            opts["no_verify"] = True
            i += 1
        elif a == "--force-halt":
            opts["force_halt"] = True
            i += 1
        elif a == "--strict":
            opts["strict"] = True
            i += 1
        elif a.startswith("-"):
            _err(f"unknown load option: {a}")
        else:
            pos.append(a)
            i += 1
    return opts, pos


def _parse_dump(cargs: list[str]) -> tuple[dict, list[str]]:
    opts = {"output": None, "binary": False, "force_halt": False}
    pos: list[str] = []
    i = 0
    while i < len(cargs):
        a = cargs[i]
        if a in ("-o", "--output"):
            if i + 1 >= len(cargs):
                _err(f"{a} requires a file path")
            opts["output"] = cargs[i + 1]
            i += 2
        elif a == "--binary":
            opts["binary"] = True
            i += 1
        elif a == "--force-halt":
            opts["force_halt"] = True
            i += 1
        elif a.startswith("-"):
            _err(f"unknown dump option: {a}")
        else:
            pos.append(a)
            i += 1
    return opts, pos


def _parse_term(cargs: list[str]) -> tuple[dict, None]:
    opts = {"flush": False}
    i = 0
    while i < len(cargs):
        a = cargs[i]
        if a == "--flush":
            opts["flush"] = True
            i += 1
        elif a == "--no-flush":
            opts["flush"] = False
            i += 1
        elif a.startswith("-"):
            _err(f"unknown term option: {a}")
        else:
            _err(f"unexpected argument for term: {a}")
    return opts, None


# ---------------------------------------------------------------------------
# Chain splitting
# ---------------------------------------------------------------------------

def _split_chain(argv: list[str]) -> tuple[bool, list[tuple[str, list[str]]]]:
    """Turn argv (excluding program name) into (quiet, [(verb, cargs), ...]).

    A leading positional value (not a verb word) or ``run`` followed by
    arguments selects the legacy single-shot compatibility path, returned
    as [("__legacy__", tokens)].
    """
    quiet = False
    tokens = list(argv)
    while tokens and tokens[0] in ("-q", "--quiet"):
        quiet = True
        tokens = tokens[1:]

    if not tokens:
        return quiet, []

    if tokens[0] in ("-h", "--help"):
        print(USAGE)
        sys.exit(0)

    if tokens[0] not in RESERVED:
        # Legacy positional form:  z80 FILE.bin [old options]
        return quiet, [("__legacy__", (["-q"] if quiet else []) + tokens)]

    actions: list[tuple[str, list[str]]] = []
    i = 0
    while i < len(tokens):
        tok = tokens[i]
        if tok not in RESERVED:
            _err(f"unexpected argument: {tok!r} "
                 f"(command words: {' '.join(VERBS)})")
        verb = ALIASES.get(tok, tok)
        i += 1
        cargs: list[str] = []
        while i < len(tokens) and tokens[i] not in RESERVED:
            cargs.append(tokens[i])
            i += 1
        if verb == "run" and cargs:
            # Legacy form:  z80 run FILE [old options] — only valid when
            # it is the first verb; a mid-chain 'run FILE' would silently
            # drop the earlier commands.
            if actions:
                _err("new-style 'run' takes no arguments; legacy one-shot "
                     "'z80 run FILE' must be alone on the line")
            return quiet, [("__legacy__", cargs)]
        actions.append((verb, cargs))

    return quiet, actions


# ---------------------------------------------------------------------------
# Command implementations (new style)
# ---------------------------------------------------------------------------

def _require_halted(brd: hw.Z80Board, force_halt: bool) -> None:
    """Make sure the CPU is halted before GP1/GP2 access."""
    if brd.status():
        return
    if force_halt:
        brd.halt()
        return
    _err("CPU is running — halt it first (z80 halt) or rerun with --force-halt")


def _cmd_load(brd: hw.Z80Board, target: str, opts: dict, pos: list[str],
              quiet: bool) -> None:
    if target not in ("rom", "ram"):
        _err(f"load target must be 'rom' or 'ram', got {target!r}")
    if len(pos) not in (1, 2):
        _err(f"usage: z80 load {target} FILE [ADDR] [options]")
    if opts["vector"] is not None and target != "rom":
        _err("--vector is only valid for load rom")
    if opts["fill"] is not None and target != "ram":
        _err("--fill is only valid for load ram")

    path = Path(pos[0])
    if not path.exists():
        _err(f"file not found: {path}")

    default_addr = 0x0000 if target == "rom" else hw.RAM_BASE
    explicit_addr = _int(pos[1], "load address") if len(pos) == 2 else None

    try:
        kind, payload = parse_image(path)
    except ImageError as e:
        _err(f"{path}: {e}")

    if kind == "bin":
        addr = explicit_addr if explicit_addr is not None else default_addr
        segs = [(addr, payload)]
    else:  # ihex
        # HEX records carry addresses; [ADDR] is an optional added base.
        segs = payload
        if explicit_addr is not None:
            segs = [(a + explicit_addr, b) for a, b in segs]

    lo, hi = (0x0000, hw.ROM_SIZE) if target == "rom" \
        else (hw.RAM_BASE, 0x10000)

    _require_halted(brd, opts["force_halt"])

    # Optional fill of the loaded region (RAM only, default-off so that old
    # program residue survives between loads).
    if opts["fill"] is not None:
        f_lo = min(a for a, _ in segs)
        f_hi = max(a + len(b) for a, b in segs)
        f_lo, f_hi = max(f_lo, lo), min(f_hi, hi)
        if f_hi > f_lo:
            if not quiet:
                print(f"  Filling 0x{f_lo:04X}..0x{f_hi - 1:04X} with "
                      f"{opts['fill']:#04x}...", file=sys.stderr)
            for a in range(f_lo, f_hi):
                brd.ram_write(a - hw.RAM_BASE, opts["fill"])

    # Write segments, clipping to the selected space.
    flat: dict[int, int] = {}
    written = 0
    reported = 0
    for base, blob in segs:
        s = max(base, lo)
        e = min(base + len(blob), hi)
        if e <= s:
            msg = (f"segment 0x{base:04X}+{len(blob)} lies outside {target} "
                   "space; skipped")
            if opts["strict"]:
                _err(msg)
            print(f"  WARNING: {msg}", file=sys.stderr)
            continue
        if s != base or e != base + len(blob):
            msg = (f"segment 0x{base:04X}+{len(blob)} clipped to {target} "
                   f"range 0x{s:04X}..0x{e - 1:04X}")
            if opts["strict"]:
                _err(msg)
            print(f"  WARNING: {msg}", file=sys.stderr)
        chunk = blob[s - base: e - base]
        for off, b in enumerate(chunk):
            if target == "rom":
                brd.rom_write(s + off, b)
            else:
                brd.ram_write(s + off - hw.RAM_BASE, b)
            flat[s + off] = b
        written += len(chunk)
        if (not quiet and len(chunk) > 4096
                and written - reported >= 4096):
            print(f"  ...{written} bytes written", file=sys.stderr)
            reported = written

    if written and not quiet:
        print(f"  Loaded {written} byte(s) into {target} "
              f"0x{min(flat):04X}..0x{max(flat):04X}", file=sys.stderr)

    # Optional reset vector (rom): JP <addr> at 0x0000.  The vector bytes
    # are also folded into the verification map because they may overwrite
    # the first bytes of an image loaded at 0x0000.
    if target == "rom" and opts["vector"] is not None:
        v = opts["vector"]
        if not (0 <= v < hw.ROM_SIZE):
            _err(f"vector 0x{v:04X} outside ROM space")
        vec = bytes([0xC3, v & 0xFF, (v >> 8) & 0xFF])
        brd.load_rom(vec, 0)
        for i, byte in enumerate(vec):
            flat[i] = byte
        if not quiet:
            print(f"  Reset vector: JP 0x{v:04X} at 0x0000", file=sys.stderr)

    if not flat:
        _err(f"nothing was written into {target} space — the image segments "
             "lie outside its address range (see warnings above)")

    # Verify.
    if not opts["no_verify"]:
        if opts["verify_all"]:
            sample = sorted(flat)
        else:
            addrs = sorted(flat)
            if len(addrs) <= 8:
                sample = addrs              # tiny images: check everything
            else:
                # First/last bytes plus a few interior samples (as planned)
                # — byte-wise GP readback is slow, so don't walk the whole
                # image on a default load.
                sample = addrs[:4] + addrs[-4:]
                for q in (1, 2, 3):
                    sample.append(addrs[len(addrs) * q // 4])
                sample = sorted(set(sample))
        bad = 0
        for a in sample:
            v = brd.rom_read(a) if target == "rom" \
                else brd.ram_read(a - hw.RAM_BASE)
            if v != flat[a]:
                bad += 1
                if bad <= 5:
                    print(f"  VERIFY MISMATCH at 0x{a:04X}: "
                          f"got {v:02x} want {flat[a]:02x}", file=sys.stderr)
        scope = "all" if opts["verify_all"] else "sample"
        if bad:
            print(f"  WARNING: verify ({scope}) failed: {bad} byte(s)",
                  file=sys.stderr)
        elif not quiet:
            print(f"  Verify ({scope}, {len(sample)} byte(s)) OK",
                  file=sys.stderr)


def _cmd_dump(brd: hw.Z80Board, target: str, opts: dict, pos: list[str],
              quiet: bool) -> None:
    if target not in ("rom", "ram"):
        _err(f"dump target must be 'rom' or 'ram', got {target!r}")
    if len(pos) > 2:
        _err("usage: z80 dump rom|ram [ADDR [LEN]]")

    lo, hi = (0x0000, hw.ROM_SIZE) if target == "rom" \
        else (hw.RAM_BASE, 0x10000)
    addr = _int(pos[0], "dump address") if pos else lo
    if addr < lo or addr >= hi:
        _err(f"start address 0x{addr:04X} outside {target} space")
    if len(pos) >= 2:
        length = _len_or_all(pos[1])
        length = hi - addr if length is None else length
    else:
        length = (hi - addr if target == "rom" else 256)
    length = max(0, min(length, hi - addr))
    if length == 0:
        _err("empty dump range")

    _require_halted(brd, opts["force_halt"])

    if target == "rom":
        data = bytes(brd.rom_read(addr + i) for i in range(length))
    else:
        data = bytes(brd.ram_read(addr - hw.RAM_BASE + i) for i in range(length))

    if opts["output"]:
        Path(opts["output"]).write_bytes(data)
        if not quiet:
            print(f"  Saved {length} byte(s) to {opts['output']}",
                  file=sys.stderr)
    elif opts["binary"]:
        sys.stdout.buffer.write(data)
        sys.stdout.buffer.flush()
    else:
        for line in format_hexdump(data, addr):
            print(line)


def _cmd_flush(fifo_fd: int, quiet: bool) -> None:
    hw.reset_fifo_buffers()
    time.sleep(0.005)
    discarded = hw.flush_fifo(fifo_fd)
    if discarded and not quiet:
        print(f"  Discarded {discarded} stale FIFO packet(s)", file=sys.stderr)


def _drain_fifo_to(fifo_fd: int, out_fd: int) -> int:
    """Copy every currently queued FIFO byte to ``out_fd``. Return count."""
    data = hw.read_available(fifo_fd)
    if data:
        os.write(out_fd, bytes(data))
    return len(data)


def _forward_stdin_to_fifo(stdin_fd: int, fifo_fd: int) -> bool:
    """Send one keystroke to the Z80. Return False if the user asked to detach."""
    ch = os.read(stdin_fd, 1)
    if not ch or ch == b"\x1d":  # Ctrl-]
        return False
    try:
        hw.write_byte(fifo_fd, ch[0])
    except BlockingIOError:
        pass
    except OSError as exc:
        if exc.errno not in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINVAL):
            raise
    return True


def _term_session(fifo_fd: int, stdin_fd: int, stdout_fd: int,
                  poll_timeout_ms: int = 20) -> None:
    """Bridge stdin/stdout to the AXI FIFO until Ctrl-].

    ``axis_fifo`` has no ``f_op->poll``, so POLLIN on the device is not a
    usable edge.  The previous loop read *one* byte per poll event and then
    waited: after a burst the RX FIFO filled, the Z80 stalled on TDRE, and
    each keystroke (the only remaining wake-up) released one more byte.
    Drain on every wake — including the timeout — and poll stdin only.
    """
    poll = select.poll()
    poll.register(stdin_fd, select.POLLIN)
    running = True
    while running:
        events = poll.poll(poll_timeout_ms)
        for fd, event in events:
            if fd == stdin_fd and (event & select.POLLIN):
                if not _forward_stdin_to_fifo(stdin_fd, fifo_fd):
                    running = False
                    break
        _drain_fifo_to(fifo_fd, stdout_fd)


def _cmd_term(fifo_fd: int, opts: dict, quiet: bool) -> None:
    if opts["flush"]:
        _cmd_flush(fifo_fd, quiet)
    try:
        old_attr = termios.tcgetattr(sys.stdin)
    except termios.error:
        _err("term requires an interactive terminal — use ssh -t z80 term")
    try:
        tty.setraw(sys.stdin)
        if not quiet:
            print("  Terminal attached — Ctrl-] to detach (CPU keeps running).",
                  file=sys.stderr)
        _term_session(fifo_fd, sys.stdin.fileno(), sys.stdout.fileno())
    finally:
        termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attr)


# ---------------------------------------------------------------------------
# Legacy one-shot runner (compatibility)
# ---------------------------------------------------------------------------

LEGACY_EPILOG = """\
Legacy single-shot behaviour: reset, load images, reset the FIFO both
directions, run, capture output (or attach a terminal with -i), then HALT
the CPU.  This exists so existing scripts and the host z80.py wrapper keep
working.  New-style usage:  z80 halt load rom X load ram Y flush reset run
"""


def legacy_main(argv: list[str]) -> int:
    """Compatibility path: mirrors the original run_z80_program.py main()."""
    parser = argparse.ArgumentParser(
        prog="z80 run",
        description="Legacy single-shot Z80 runner (compatibility)",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=LEGACY_EPILOG,
    )
    parser.add_argument("program", nargs="?",
                        help="Legacy RAM binary (.bin) to load")
    parser.add_argument("-i", "--interactive", action="store_true",
                        help="Live console: keyboard ↔ Z80 I/O")
    parser.add_argument("-n", "--max-bytes", type=int,
                        help="Stop batch capture after N output bytes")
    parser.add_argument("--max-time", type=float, default=10.0,
                        help="Wall-clock limit in seconds (default: 10)")
    parser.add_argument("--input", help="Batch: feed string to Z80 IN")
    parser.add_argument("-o", "--output", help="Save captured bytes to FILE")
    parser.add_argument("-q", "--quiet", action="store_true",
                        help="Less status noise")
    parser.add_argument("--fifo", default=hw.FIFO_DEV,
                        help=f"Axis FIFO device (default: {hw.FIFO_DEV})")
    parser.add_argument("--no-boot-rom", action="store_true",
                        help="Skip writing the boot ROM (jp 0x2000)")
    parser.add_argument("--run-from-rom", action="store_true",
                        help="Legacy ROM-only mode; use --rom-image for "
                             "multi-image loads")
    parser.add_argument("--ram-image", help="RAM binary (.bin) to load")
    parser.add_argument("--ram-org", type=lambda x: int(x, 0), default=0x2000,
                        help="RAM image Z80 address (default: 0x2000)")
    parser.add_argument("--rom-image", help="ROM binary (.bin) to load")
    parser.add_argument("--rom-org", type=lambda x: int(x, 0), default=None,
                        help="ROM image Z80 address; nonzero also generates "
                             "JP at 0x0000")
    args = parser.parse_args(argv[1:])

    if args.run_from_rom:
        if args.program is None or args.ram_image or args.rom_image:
            print("ERROR: --run-from-rom requires only the positional program",
                  file=sys.stderr)
            sys.exit(1)
        if args.no_boot_rom:
            print("ERROR: --run-from-rom requires its ROM boot vector",
                  file=sys.stderr)
            sys.exit(1)
        rom_path = Path(args.program)
        ram_path = None
        rom_start = 0x0100 if args.rom_org is None else args.rom_org
        generate_vector = True
    else:
        if args.program and args.ram_image:
            print("ERROR: specify RAM input either positionally or with "
                  "--ram-image, not both", file=sys.stderr)
            sys.exit(1)
        ram_path = (Path(args.ram_image or args.program)
                    if (args.ram_image or args.program) else None)
        rom_path = Path(args.rom_image) if args.rom_image else None
        rom_start = 0 if args.rom_org is None else args.rom_org
        generate_vector = rom_path is not None and rom_start != 0

    if ram_path and not ram_path.exists():
        print(f"ERROR: RAM program not found: {ram_path}", file=sys.stderr)
        sys.exit(1)
    if rom_path and not rom_path.exists():
        print(f"ERROR: ROM image not found: {rom_path}", file=sys.stderr)
        sys.exit(1)
    ram_bytes = ram_path.read_bytes() if ram_path else None
    rom_bytes = rom_path.read_bytes() if rom_path else None

    if ram_bytes is None and rom_bytes is None:
        print("ERROR: provide a RAM image/program and/or a ROM image",
              file=sys.stderr)
        sys.exit(1)
    if ram_bytes is not None:
        ram_offset = args.ram_org - 0x2000
        if args.ram_org < 0x2000 or ram_offset + len(ram_bytes) > hw.RAM_SIZE:
            print(
                f"ERROR: RAM image range 0x{args.ram_org:X}.."
                f"0x{args.ram_org + len(ram_bytes) - 1:X} is invalid "
                "(RAM is 0x2000..0xFFFF)", file=sys.stderr)
            sys.exit(1)
    else:
        ram_offset = 0
    if rom_bytes is not None and (rom_start < 0
                                  or rom_start + len(rom_bytes) > hw.ROM_SIZE):
        print(
            f"ERROR: ROM image range 0x{rom_start:X}.."
            f"0x{rom_start + len(rom_bytes) - 1:X} is invalid "
            f"(ROM is 0x0000..0x{hw.ROM_SIZE - 1:04X})", file=sys.stderr)
        sys.exit(1)
    if rom_bytes is not None and generate_vector and args.no_boot_rom:
        print("ERROR: --no-boot-rom conflicts with nonzero --rom-org",
              file=sys.stderr)
        sys.exit(1)

    # Open FIFO
    try:
        fifo_fd = hw.open_fifo(args.fifo)
    except FileNotFoundError:
        print(f"ERROR: {args.fifo} not found. Is the axis_fifo driver loaded?",
              file=sys.stderr)
        sys.exit(1)

    try:
        with hw.Z80Board() as brd:
            # Reset CPU (ensures halted state)
            brd.reset()
            if not args.quiet:
                print(f"  CPU reset, halted={brd.status()}", file=sys.stderr)

            if rom_bytes is not None:
                # An explicit ROM image is loaded exactly at rom_start.  If
                # --rom-org was nonzero, prepend a reset-vector jump at 0.
                if generate_vector:
                    boot_vector = bytes([
                        0xC3,
                        rom_start & 0xFF,
                        (rom_start >> 8) & 0xFF,
                    ])
                    brd.load_rom(boot_vector, 0)
                if not args.quiet:
                    print(
                        f"  Loading ROM image at 0x{rom_start:04X} "
                        f"({len(rom_bytes)} bytes)...",
                        file=sys.stderr,
                    )
                brd.load_rom(rom_bytes, rom_start)
                check_n = min(4, len(rom_bytes))
                loaded = bytes(brd.rom_read(rom_start + i)
                               for i in range(check_n))
                if loaded != rom_bytes[:check_n]:
                    print(f"  WARNING: ROM verify failed: {loaded.hex()}",
                          file=sys.stderr)
                elif not args.quiet:
                    print("  ROM image verified OK", file=sys.stderr)
                if generate_vector and not args.quiet:
                    print(f"  Reset vector: JP 0x{rom_start:04X}",
                          file=sys.stderr)
            elif not args.no_boot_rom:
                # Legacy RAM-only mode: install the standard JP 0x2000.
                existing = bytes(brd.rom_read(i) for i in range(3))
                if existing != hw.BOOT_ROM:
                    if not args.quiet:
                        print(f"  Writing boot ROM ({len(hw.BOOT_ROM)} bytes)...",
                              file=sys.stderr)
                    brd.load_rom(hw.BOOT_ROM)
                    v = bytes(brd.rom_read(i) for i in range(3))
                    if v == hw.BOOT_ROM:
                        if not args.quiet:
                            print("  Boot ROM verified OK", file=sys.stderr)
                    else:
                        print(f"  WARNING: boot ROM verify failed: {v.hex()}",
                              file=sys.stderr)
                elif not args.quiet:
                    print("  Boot ROM already present, skipping", file=sys.stderr)

            if ram_bytes is not None:
                if not args.quiet:
                    print(
                        f"  Loading RAM image at 0x{args.ram_org:04X} "
                        f"({len(ram_bytes)} bytes)...", file=sys.stderr)
                brd.load_ram(ram_bytes, ram_offset)
                if not args.quiet:
                    ok = brd.verify_ram(ram_bytes, ram_offset,
                                        min(4, len(ram_bytes)))
                    print(f"  RAM verify: {'OK' if ok else 'FAILED'}",
                          file=sys.stderr)

            # Reset and flush both FIFO directions.  The FIFO/bridge buffers
            # are not reset by the z80_soc ctrl_reset signal.
            hw.reset_fifo_buffers()
            discarded = hw.flush_fifo(fifo_fd)
            if discarded and not args.quiet:
                print(f"  Discarded {discarded} stale FIFO packet(s)",
                      file=sys.stderr)

            # Start the CPU (reset again to ensure PC=0x0000, then run).
            # Repeat the FIFO reset after this reset to catch a byte that was
            # held in the bridge staging register.
            brd.reset()
            hw.reset_fifo_buffers()
            discarded = hw.flush_fifo(fifo_fd)
            if discarded and not args.quiet:
                print(f"  Discarded {discarded} post-reset FIFO packet(s)",
                      file=sys.stderr)
            brd.run()
            if not args.quiet:
                print("  CPU running...", file=sys.stderr)

            if args.interactive:
                # Interactive mode: live keyboard ↔ Z80 I/O
                old_attr = termios.tcgetattr(sys.stdin)
                try:
                    tty.setraw(sys.stdin)
                    if not args.quiet:
                        print("  Interactive mode. Ctrl-C or Ctrl-] to quit.",
                              file=sys.stderr)
                    _term_session(fifo_fd, sys.stdin.fileno(),
                                  sys.stdout.fileno())
                finally:
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attr)
            else:
                # Batch mode: send input (if any), then capture output
                if args.input:
                    for ch in args.input.encode():
                        hw.write_byte(fifo_fd, ch)
                    time.sleep(0.05)  # Let Z80 process

                captured = hw.capture_fifo_output(
                    fifo_fd,
                    max_bytes=args.max_bytes,
                    max_time=args.max_time,
                )

                # Save output
                if args.output:
                    out_path = Path(args.output)
                    out_path.write_bytes(bytes(captured))
                    if not args.quiet:
                        print(f"  Saved {len(captured)} bytes to {out_path}",
                              file=sys.stderr)

                # Print output as hex
                if captured:
                    for i in range(0, len(captured), 16):
                        chunk = captured[i:i + 16]
                        hex_str = " ".join(f"{b:02x}" for b in chunk)
                        ascii_str = "".join(
                            chr(b) if 0x20 <= b < 0x7f else "."
                            for b in chunk)
                        print(f"{i:04x}: {hex_str:<48s}  {ascii_str}")
                    print(f"\nTotal: {len(captured)} bytes", file=sys.stderr)
                else:
                    print("  No output captured", file=sys.stderr)

            # Halt the CPU (legacy one-shot always ends halted)
            brd.halt()

    except PermissionError:
        print("ERROR: Need root (sudo) for /dev/mem access.", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        os.close(fifo_fd)
    return 0


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main(argv: list[str] | None = None) -> int:
    if argv is None:
        argv = sys.argv[1:]

    quiet, actions = _split_chain(argv)
    if not actions:
        print(USAGE, file=sys.stderr)
        return 1

    if actions[0][0] == "__legacy__":
        return legacy_main(["z80"] + actions[0][1])

    # Validate chain shape before touching hardware.
    for idx, (verb, cargs) in enumerate(actions):
        if verb == "term" and idx != len(actions) - 1:
            _err("'term' must be the last command in a chain")
        if verb == "run" and cargs:
            _err("new-style 'run' takes no arguments "
                 "(legacy: z80 run FILE.bin [old options])")

    needs_fifo = any(verb in ("flush", "term") for verb, _ in actions)
    fifo_fd = None
    if needs_fifo:
        try:
            fifo_fd = hw.open_fifo(hw.FIFO_DEV)
        except FileNotFoundError:
            _err(f"{hw.FIFO_DEV} not found. Is the axis_fifo driver loaded?")
        except PermissionError:
            _err("need root (sudo) to open the axis FIFO device")

    try:
        with hw.Z80Board() as brd:
            for verb, cargs in actions:
                if verb in ("halt", "stop"):
                    if cargs:
                        _err(f"'{verb}' takes no arguments")
                    brd.halt()
                elif verb in ("run", "start"):
                    if cargs:
                        _err(f"'{verb}' takes no arguments")
                    brd.run()
                elif verb == "reset":
                    if cargs:
                        _err("'reset' takes no arguments")
                    brd.reset()
                elif verb == "status":
                    if cargs:
                        _err("'status' takes no arguments")
                    s = brd.status()
                    print("halted=yes" if s else "halted=no")
                elif verb == "load":
                    if len(cargs) < 2:
                        _err("usage: z80 load rom|ram FILE [ADDR] [options]")
                    opts, pos = _parse_load(cargs[1:])
                    _cmd_load(brd, cargs[0], opts, pos, quiet)
                elif verb == "dump":
                    if len(cargs) < 1:
                        _err("usage: z80 dump rom|ram [ADDR [LEN]]")
                    opts, pos = _parse_dump(cargs[1:])
                    _cmd_dump(brd, cargs[0], opts, pos, quiet)
                elif verb == "flush":
                    if cargs:
                        _err("'flush' takes no arguments")
                    _cmd_flush(fifo_fd, quiet)
                elif verb == "term":
                    opts, _ = _parse_term(cargs)
                    _cmd_term(fifo_fd, opts, quiet)
    except PermissionError:
        print("ERROR: Need root (sudo) for /dev/mem access.", file=sys.stderr)
        return 1
    finally:
        if fifo_fd is not None:
            os.close(fifo_fd)
    return 0


if __name__ == "__main__":
    sys.exit(main())
