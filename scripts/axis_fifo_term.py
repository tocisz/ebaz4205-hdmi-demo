#!/usr/bin/env python3
"""
axis_fifo_term — interactive terminal for the PS↔PL byte-stream bridge.

Opens /dev/axis_fifo_0x7c450000, puts your local terminal in raw mode, and
bridges your keyboard ↔ the PL byte interface (case_toggle / bf2_soc).

Usage:
  # Deploy the bitstream + modules, then:
  ./scripts/axis_fifo_term.py

  # Override device path:
  ./scripts/axis_fifo_term.py /dev/axis_fifo_0x7c450000

Keys (when stdin is a TTY):
  Ctrl-C      — stop
  Ctrl-D      — send \\0 byte through the bridge (null), then exit
  Ctrl-]      — stop (telnet-style; useful if Ctrl-C is trapped)

Notes:
  - The driver requires 4-byte-aligned writes.  Each keystroke is padded to
    4 bytes (byte 0 = data, bytes 1-3 = 0), so the v1 drop-24 bridge delivers
    1 byte per keypress to the PL side.
  - Each read returns one 4-byte packet (byte 0 = data, bytes 1-3 = 0).
  - Piped input is also forwarded; EOF on stdin sends \\0 + stop.
"""

import fcntl
import os
import select
import sys
import termios
import time
import tty

DEV_DEFAULT = "/dev/axis_fifo_0x7c450000"

# ASCII
CTRL_C = b"\x03"  # interrupt / stop
CTRL_D = b"\x04"  # EOF → send \0 byte
CTRL_BRACKET = b"\x1d"  # telnet-style escape


def open_fifo(path: str) -> int:
    """Open the axis_fifo device for read/write (non-blocking).

    Must be non-blocking from open() time because the driver caches
    f->f_flags at open() and never re-reads it; an fcntl(F_SETFL)
    after open is invisible to the driver's read/write paths.
    """
    fd = os.open(path, os.O_RDWR | os.O_NONBLOCK)
    return fd


def write_byte(fd: int, b: int) -> None:
    """Send one byte through the v1 drop-24 bridge.

    The driver requires 4-byte writes.  We pad to 4 bytes; the bridge's
    [7:0] carries the actual byte and upper 24 bits are dropped on the
    PL side.
    """
    buf = bytes([b, 0, 0, 0])
    os.write(fd, buf)


def read_byte(fd: int) -> int | None:
    """Read one byte from the v1 drop-24 bridge.

    Returns the byte or None if no data available (EAGAIN).
    Each read() returns one 4-byte packet from the RX FIFO; byte 0 is data.
    """
    try:
        word = os.read(fd, 4)
        if not word:
            return None
        return word[0]
    except (BlockingIOError, OSError):
        return None


def interactive_session(
    fifo_fd: int,
    max_time: float | None = None,
) -> None:
    """Live console: keyboard → axis_fifo TX, axis_fifo RX → stdout.

    Ctrl-D sends a \\0 byte through the bridge, then stops.
    """
    stdin_fd = sys.stdin.fileno()
    stdout_fd = sys.stdout.fileno()
    is_tty = os.isatty(stdin_fd)

    old_term = None
    if is_tty:
        old_term = termios.tcgetattr(stdin_fd)
        tty.setraw(stdin_fd)

    # Make stdin non-blocking so select is the sole gate.
    old_stdin_flags = fcntl.fcntl(stdin_fd, fcntl.F_GETFL)
    fcntl.fcntl(stdin_fd, fcntl.F_SETFL, old_stdin_flags | os.O_NONBLOCK)

    # fifo_fd already opened with O_NONBLOCK — the driver's cached
    # read_flags/write_flags are correct.  No fcntl needed.
    t0 = time.monotonic()
    stdin_open = True
    stop_reason = "quit"

    try:
        while True:
            if max_time is not None and (time.monotonic() - t0) >= max_time:
                stop_reason = "max_time"
                break

            rlist = [fifo_fd]
            if stdin_open:
                rlist.append(stdin_fd)

            try:
                rl, _, _ = select.select(rlist, [], [], 0.05)
            except InterruptedError:
                stop_reason = "interrupt"
                break

            # ── RX from FIFO → stdout ──
            if fifo_fd in rl:
                while True:
                    b = read_byte(fifo_fd)
                    if b is None:
                        break
                    try:
                        if is_tty:
                            # Display byte; handle LF→CRLF for TTY
                            chunk = bytes([b]).replace(b"\n", b"\r\n")
                        else:
                            chunk = bytes([b])
                        os.write(stdout_fd, chunk)
                    except OSError:
                        pass

            # ── stdin → TX to FIFO ──
            if stdin_open and stdin_fd in rl:
                try:
                    data = os.read(stdin_fd, 256)
                except BlockingIOError:
                    data = b""

                if not data:
                    # EOF on stdin — send null, then stop
                    write_byte(fifo_fd, 0)
                    stop_reason = "eof"
                    break

                # Check for stop keys
                if CTRL_C in data:
                    stop_reason = "interrupt"
                    break
                if CTRL_BRACKET in data:
                    stop_reason = "quit"
                    break

                # Ctrl-D: send \0 byte through the bridge, then stop
                if CTRL_D in data:
                    write_byte(fifo_fd, 0)
                    stop_reason = "ctrl_d"
                    break

                # Forward all other keystrokes
                for byte_val in data:
                    write_byte(fifo_fd, byte_val)

    except KeyboardInterrupt:
        stop_reason = "interrupt"
    finally:
        fcntl.fcntl(stdin_fd, fcntl.F_SETFL, old_stdin_flags)
        if old_term is not None:
            termios.tcsetattr(stdin_fd, termios.TCSADRAIN, old_term)
        if is_tty:
            try:
                os.write(stdout_fd, b"\r\n")
            except OSError:
                pass

    if is_tty:
        match stop_reason:
            case "interrupt":
                os.write(stdout_fd, b"^C\r\n")
            case "ctrl_d":
                os.write(stdout_fd, b"^D (\\0 sent)\r\n")
            case "eof":
                os.write(stdout_fd, b"^D (EOF)\r\n")
            case _:
                pass


def main() -> None:
    dev = sys.argv[1] if len(sys.argv) > 1 else DEV_DEFAULT

    if not os.path.exists(dev):
        print(f"ERROR: {dev} not found", file=sys.stderr)
        print(f"  Deploy the bitstream and load axis_fifo.ko first.", file=sys.stderr)
        sys.exit(1)

    try:
        fd = open_fifo(dev)
    except OSError as e:
        print(f"ERROR: cannot open {dev}: {e}", file=sys.stderr)
        sys.exit(1)

    print(f"axis_fifo_term — interactive byte bridge ({dev})", file=sys.stderr)
    print(f"  Ctrl-C  stop", file=sys.stderr)
    print(f"  Ctrl-D  send \\0 byte + stop", file=sys.stderr)
    print(f"  Ctrl-]  stop (telnet escape)", file=sys.stderr)
    print(file=sys.stderr)

    try:
        interactive_session(fd)
    finally:
        os.close(fd)


if __name__ == "__main__":
    main()
