#!/usr/bin/env python3
"""
Load and run a Z80 program on the EBAZ4205 FPGA (z80_soc).

Intended for use *on the board* (e.g. installed as /root/z80). Accepts a
raw Z80 binary (.bin), loads it into RAM at 0x2000, programs the ROM boot
vector (jp 0x2000), resets the CPU, and bridges I/O through the PS↔PL
AXI-Stream FIFO bridge (/dev/axis_fifo_0x7c450000).

Usage (on board):
  z80 counter.bin                       # capture output, print hex
  z80 counter.bin -n 256                # capture up to 256 bytes
  z80 echo.bin -i                       # interactive: send bytes, read back
  z80 program.bin --input $'abc\n'      # batch feed for 'IN' programs

  Infinite-loop programs (counter, walk) need --max-bytes or --max-time.
  Interactive mode (-i) streams output live and forwards keyboard.

Dependencies:
  - Board running the z80_soc design (axi_gpreg at 0x7C440000, bridge at 0x7C450000).
  - Root access to /dev/mem and /dev/axis_fifo_0x7c450000.
  - Python 3 with mmap, select, struct, termios (stdlib).
"""

import argparse
import fcntl
import mmap
import os
import select
import struct
import sys
import termios
import time
import tty
from pathlib import Path

# ---------------------------------------------------------------------------
# Physical register map (axi_gpreg at 0x7C440000) — same as bf2_soc
# ---------------------------------------------------------------------------
REG_BASE = 0x7C440000
REG_SIZE = 0x1000  # 4 KB page

GP0_OUT = 0x404  # CPU control
GP0_IN  = 0x408  # CPU status
GP1_OUT = 0x444  # RAM access (address+data+ctrl)
GP1_IN  = 0x448  # RAM read result
GP2_OUT = 0x484  # ROM access (address+data+ctrl)
GP2_IN  = 0x488  # ROM read result

# GP0 control bits (same as bf2_soc)
_HALT  = 1 << 0
_RESET = 1 << 1
_RUN   = 1 << 3

# Memory sizes
ROM_SIZE = 8192   # 8K × 8
RAM_SIZE = 8192   # 8K × 8

# AXI-Stream FIFO device
FIFO_DEV = "/dev/axis_fifo_0x7c450000"

# ---------------------------------------------------------------------------
# Boot ROM: JP 0x2000 (3 bytes)
# ---------------------------------------------------------------------------
BOOT_ROM = bytes([0xC3, 0x00, 0x20])  # jp 0x2000

# ---------------------------------------------------------------------------
# Board control class
# ---------------------------------------------------------------------------

class Z80Board:
    """Control the z80_soc CPU via mmap'd /dev/mem register access.

    Usage:
        with Z80Board() as brd:
            brd.reset()
            brd.load_rom(BOOT_ROM)     # one-time setup
            brd.load_ram(program_bytes)
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

    # -- ROM (gp2) -----------------------------------------------------

    def rom_write(self, addr: int, data: int):
        """Write one byte to ROM at ``addr`` (0 .. 8191)."""
        assert 0 <= addr < ROM_SIZE
        self._w(GP2_OUT,
                (addr & 0x1FFF) | ((data & 0xFF) << 16) | (1 << 24))
        self._wait_done(GP2_IN)
        self._w(GP2_OUT, 0)

    def rom_read(self, addr: int) -> int:
        """Read one byte from ROM at ``addr``."""
        assert 0 <= addr < ROM_SIZE
        self._w(GP2_OUT, (addr & 0x1FFF) | (1 << 25))
        self._wait_done(GP2_IN)
        v = self._r(GP2_IN) & 0xFF
        self._w(GP2_OUT, 0)
        return v

    def load_rom(self, data: bytes):
        """Write bytes to ROM starting at address 0."""
        for i, b in enumerate(data):
            self.rom_write(i, b)

    # -- RAM (gp1) -----------------------------------------------------

    def ram_write(self, addr: int, data: int):
        """Write one byte to RAM at ``addr`` (0 .. 8191)."""
        assert 0 <= addr < RAM_SIZE
        self._w(GP1_OUT,
                (addr & 0x1FFF) | ((data & 0xFF) << 16) | (1 << 24))
        self._wait_done(GP1_IN)
        self._w(GP1_OUT, 0)

    def ram_read(self, addr: int) -> int:
        """Read one byte from RAM at ``addr``."""
        assert 0 <= addr < RAM_SIZE
        self._w(GP1_OUT, (addr & 0x1FFF) | (1 << 25))
        self._wait_done(GP1_IN)
        v = self._r(GP1_IN) & 0xFF
        self._w(GP1_OUT, 0)
        return v

    def load_ram(self, data: bytes):
        """Load bytes into RAM starting at address 0."""
        # Clear first (at least to program length)
        clear_n = max(len(data), 16)
        for i in range(min(clear_n, RAM_SIZE)):
            self.ram_write(i, 0)
        # Write program
        for i, b in enumerate(data):
            if i >= RAM_SIZE:
                break
            self.ram_write(i, b)

    def verify_ram(self, data: bytes, n_check: int = 4) -> bool:
        """Read back first ``n_check`` RAM locations and compare."""
        return all(self.ram_read(i) == data[i]
                   for i in range(min(n_check, len(data))))

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

    def step(self):
        """Execute one instruction (Z80 must be halted)."""
        self._w(GP0_OUT, 1 << 2)
        self._w(GP0_OUT, 0)
        time.sleep(0.01)

    def status(self) -> bool:
        """Return True if CPU is halted."""
        s = self._r(GP0_IN)
        return bool(s & 1)

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
# FIFO helpers
# ===================================================================

def open_fifo(device: str = FIFO_DEV) -> int:
    """Open the axis_fifo device for read/write (non-blocking from open)."""
    return os.open(device, os.O_RDWR | os.O_NONBLOCK)


def write_byte(fd: int, b: int) -> None:
    """Send one byte through the v1 drop-24 bridge (4-byte write)."""
    os.write(fd, bytes([b & 0xFF, 0, 0, 0]))


def read_byte(fd: int) -> int | None:
    """Read one byte from the v1 drop-24 bridge.

    Returns the byte or None if no data available (EAGAIN).
    """
    try:
        word = os.read(fd, 4)
        if not word:
            return None
        return word[0]
    except (BlockingIOError, OSError):
        return None


def flush_fifo(fd, settle: float = 0.05):
    """Flush all stale bytes from the FIFO RX buffer."""
    while True:
        b = read_byte(fd)
        if b is None:
            break
    deadline = time.monotonic() + settle
    while time.monotonic() < deadline:
        rl, _, _ = select.select([fd], [], [], max(0, deadline - time.monotonic()))
        if not rl:
            break
        while True:
            b = read_byte(fd)
            if b is None:
                break


def capture_fifo_output(
    fd,
    idle_timeout: float = 0.5,
    max_bytes: int | None = None,
    max_time: float | None = None,
) -> list[int]:
    """Read bytes from the FIFO until one of the stop conditions.

    Args:
        fd: FIFO file descriptor (non-blocking).
        idle_timeout: Stop after this many seconds with no new data.
        max_bytes: Stop after capturing this many bytes.
        max_time: Hard wall-clock timeout in seconds.

    Returns:
        List of captured bytes (integers 0-255).
    """
    captured: list[int] = []
    last_activity = time.monotonic()
    deadline = time.monotonic() + max_time if max_time else None

    while True:
        if deadline and time.monotonic() >= deadline:
            break
        if max_bytes and len(captured) >= max_bytes:
            break

        rl, _, _ = select.select([fd], [], [], 0.05)
        if rl:
            while True:
                b = read_byte(fd)
                if b is None:
                    break
                captured.append(b)
                last_activity = time.monotonic()
                if max_bytes and len(captured) >= max_bytes:
                    break
        elif time.monotonic() - last_activity > idle_timeout:
            break

    return captured


# ===================================================================
# Main
# ===================================================================

def main():
    parser = argparse.ArgumentParser(
        description="Load and run a Z80 program on the FPGA",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=__doc__,
    )
    parser.add_argument("program", help="Z80 binary (.bin) to load")
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
    parser.add_argument("--fifo", default=FIFO_DEV,
                        help=f"Axis FIFO device (default: {FIFO_DEV})")
    parser.add_argument("--no-boot-rom", action="store_true",
                        help="Skip writing the boot ROM (jp 0x2000)")
    args = parser.parse_args()

    # Load program binary
    prog_path = Path(args.program)
    if not prog_path.exists():
        print(f"ERROR: program not found: {prog_path}", file=sys.stderr)
        sys.exit(1)
    program_bytes = prog_path.read_bytes()
    if len(program_bytes) > RAM_SIZE:
        print(f"ERROR: program {len(program_bytes)} bytes exceeds RAM {RAM_SIZE}", file=sys.stderr)
        sys.exit(1)

    if not args.quiet:
        print(f"Program: {prog_path} ({len(program_bytes)} bytes)", file=sys.stderr)

    # Open FIFO
    try:
        fifo_fd = open_fifo(args.fifo)
    except FileNotFoundError:
        print(f"ERROR: {args.fifo} not found. Is the axis_fifo driver loaded?", file=sys.stderr)
        sys.exit(1)

    try:
        with Z80Board() as brd:
            # Reset CPU (ensures halted state)
            brd.reset()
            if not args.quiet:
                print(f"  CPU reset, halted={brd.status()}", file=sys.stderr)

            # Write boot ROM (only needs to be done once per FPGA boot)
            if not args.no_boot_rom:
                # Check if ROM already contains our boot vector
                existing = bytes(brd.rom_read(i) for i in range(3))
                if existing != BOOT_ROM:
                    if not args.quiet:
                        print(f"  Writing boot ROM ({len(BOOT_ROM)} bytes)...", file=sys.stderr)
                    brd.load_rom(BOOT_ROM)
                    v = bytes(brd.rom_read(i) for i in range(3))
                    if v == BOOT_ROM:
                        if not args.quiet:
                            print("  Boot ROM verified OK", file=sys.stderr)
                    else:
                        print(f"  WARNING: boot ROM verify failed: {v.hex()}", file=sys.stderr)
                else:
                    if not args.quiet:
                        print("  Boot ROM already present, skipping", file=sys.stderr)

            # Load program into RAM
            if not args.quiet:
                print(f"  Loading program to RAM...", file=sys.stderr)
            brd.load_ram(program_bytes)
            if not args.quiet:
                ok = brd.verify_ram(program_bytes, min(4, len(program_bytes)))
                print(f"  RAM verify: {'OK' if ok else 'FAILED'}", file=sys.stderr)

            # Flush any stale FIFO data
            flush_fifo(fifo_fd)

            # Start the CPU (reset again to ensure PC=0x0000, then run)
            brd.reset()
            brd.run()
            if not args.quiet:
                print(f"  CPU running...", file=sys.stderr)

            if args.interactive:
                # -------------------------------------------------------
                # Interactive mode: live keyboard ↔ Z80 I/O
                # -------------------------------------------------------
                old_attr = termios.tcgetattr(sys.stdin)
                try:
                    tty.setraw(sys.stdin)
                    poll = select.poll()
                    poll.register(sys.stdin, select.POLLIN)
                    poll.register(fifo_fd, select.POLLIN)

                    if not args.quiet:
                        print("  Interactive mode. Ctrl-C or Ctrl-] to quit.",
                              file=sys.stderr)

                    running = True
                    while running:
                        events = poll.poll(50)  # 50 ms timeout
                        for fd, event in events:
                            if fd == sys.stdin.fileno():
                                ch = os.read(sys.stdin.fileno(), 1)
                                if not ch or ch == b'\x1d':  # Ctrl-]
                                    running = False
                                    break
                                # Send byte to Z80
                                write_byte(fifo_fd, ch[0])
                            elif fd == fifo_fd:
                                b = read_byte(fifo_fd)
                                if b is not None:
                                    os.write(sys.stdout.fileno(), bytes([b]))
                                    sys.stdout.flush()
                finally:
                    termios.tcsetattr(sys.stdin, termios.TCSADRAIN, old_attr)
            else:
                # -------------------------------------------------------
                # Batch mode: send input (if any), then capture output
                # -------------------------------------------------------
                if args.input:
                    for ch in args.input.encode():
                        write_byte(fifo_fd, ch)
                    time.sleep(0.05)  # Let Z80 process

                captured = capture_fifo_output(
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
                    # Show as hex dump
                    for i in range(0, len(captured), 16):
                        chunk = captured[i:i+16]
                        hex_str = " ".join(f"{b:02x}" for b in chunk)
                        ascii_str = "".join(chr(b) if 0x20 <= b < 0x7f else "." for b in chunk)
                        print(f"{i:04x}: {hex_str:<48s}  {ascii_str}")
                    print(f"\nTotal: {len(captured)} bytes", file=sys.stderr)
                else:
                    print("  No output captured", file=sys.stderr)

            # Halt the CPU
            brd.halt()

    except PermissionError:
        print("ERROR: Need root (sudo) for /dev/mem access.", file=sys.stderr)
        sys.exit(1)
    except FileNotFoundError as e:
        print(f"ERROR: {e}", file=sys.stderr)
        sys.exit(1)
    finally:
        os.close(fifo_fd)


if __name__ == "__main__":
    main()
