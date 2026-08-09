#!/usr/bin/env python3
"""
axis_fifo — userspace shim for the PS↔PL byte-stream bridge (v1 drop-24).

With the v1 drop-24 adapter: each 32-bit stream word carries one byte in
[7:0]; upper bits are dropped/zeroed.  read() returns one 4-byte packet per
call; the low byte is the data.  write() requires 4-byte multiples.

See doc/AXIS_FIFO_BRIDGE.md for the full design.
"""

import os
import time

DEV = "/dev/axis_fifo_0x7c450000"
TIMEOUT_SEC = 30  # retry loop for -EAGAIN


def write_bytes(fd, data):
    """
    Write an arbitrary-length byte stream through the v1 bridge.

    Pads to a 4-byte multiple with zeros (necessary for the driver).
    """
    n = len(data)
    padded = data + b"\x00" * ((-n) % 4)
    offset = 0
    deadline = time.monotonic() + TIMEOUT_SEC
    while offset < len(padded):
        try:
            written = os.write(fd, padded[offset:])
            offset += written
        except BlockingIOError:
            if time.monotonic() > deadline:
                raise TimeoutError("write_bytes timed out")
            time.sleep(0.001)


def read_bytes(fd, n):
    """
    Read exactly *n* bytes from the v1 bridge.

    Each read(fd, 4) returns one packet (low byte = data, bytes 1-3 = 0).
    Loops until *n* bytes are collected or timeout.
    """
    out = bytearray()
    deadline = time.monotonic() + TIMEOUT_SEC
    while len(out) < n:
        try:
            word = os.read(fd, 4)
            if not word:
                break
            out.append(word[0])
            # bytes 1-3 are zero in v1; discard
        except BlockingIOError:
            if time.monotonic() > deadline:
                raise TimeoutError("read_bytes timed out")
            time.sleep(0.001)
    return bytes(out)


def open_rw():
    """Open the device for reading and writing (blocking, non-blocking)."""
    fd = os.open(DEV, os.O_RDWR | os.O_NONBLOCK)
    return fd
