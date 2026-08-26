"""Hardware access for the Z80 board tool (EBAZ4205 / z80_soc).

Provides:
  * Z80Board — mmap'd /dev/mem access to the axi_gpreg control registers
    (GP0 = CPU control/status, GP1 = RAM port, GP2 = ROM port).
  * FIFO helpers — open / reset / stream bytes through
    /dev/axis_fifo_0x7c450000 (PS <-> PL byte bridge).

Register map and protocol are identical to the previous single-file runner
(and to bf2_soc before it); no RTL changes required.
"""

import errno
import mmap
import os
import struct
import time

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
ROM_SIZE = 8192          # 8K × 8  (Z80 addresses 0x0000–0x1FFF)
RAM_SIZE = 56 * 1024     # 56K × 8 (Z80 addresses 0x2000–0xFFFF)

# Z80 address of the first RAM byte (PS offset = Z80 addr - RAM_BASE)
RAM_BASE = 0x2000

# AXI-Stream FIFO device and PG080 register map
FIFO_DEV = "/dev/axis_fifo_0x7c450000"
FIFO_BASE = 0x7C450000
FIFO_TDFR = 0x08  # transmit FIFO reset (PS -> Z80)
FIFO_RDFR = 0x18  # receive FIFO reset (Z80 -> PS)
FIFO_RESET_VALUE = 0xA5

# ---------------------------------------------------------------------------
# Boot ROM: JP 0x2000 (3 bytes)
# ---------------------------------------------------------------------------
BOOT_ROM = bytes([0xC3, 0x00, 0x20])  # jp 0x2000


class Z80Board:
    """Control the z80_soc CPU via mmap'd /dev/mem register access.

    Usage:
        with Z80Board() as brd:
            brd.halt()
            brd.load_rom(BOOT_ROM)
            brd.load_ram(program_bytes)
            brd.reset()
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

    def load_rom(self, data: bytes, start: int = 0):
        """Write ``data`` to ROM starting at CPU address ``start``."""
        if start < 0 or start + len(data) > ROM_SIZE:
            raise ValueError(
                f"ROM image 0x{start:X}..0x{start + len(data) - 1:X} "
                f"does not fit in {ROM_SIZE} bytes"
            )
        for i, b in enumerate(data, start):
            self.rom_write(i, b)

    # -- RAM (gp1) -----------------------------------------------------

    def ram_write(self, addr: int, data: int):
        """Write one byte to RAM at zero-based offset ``addr`` (0 .. 57343).

        Offset 0 is Z80 address 0x2000; offset 0xDFFF is Z80 address
        0xFFFF.
        """
        assert 0 <= addr < RAM_SIZE
        self._w(GP1_OUT,
                (addr & 0xFFFF) | ((data & 0xFF) << 16) | (1 << 24))
        self._wait_done(GP1_IN)
        self._w(GP1_OUT, 0)

    def ram_read(self, addr: int) -> int:
        """Read one byte from RAM at zero-based offset ``addr``."""
        assert 0 <= addr < RAM_SIZE
        self._w(GP1_OUT, (addr & 0xFFFF) | (1 << 25))
        self._wait_done(GP1_IN)
        v = self._r(GP1_IN) & 0xFF
        self._w(GP1_OUT, 0)
        return v

    def load_ram(self, data: bytes, start: int = 0):
        """Load bytes into RAM at zero-based offset ``start``.

        RAM offset 0 corresponds to Z80 address 0x2000.

        NOTE: for backwards compatibility this zero-fills the loaded region
        first (legacy one-shot behaviour).  The interactive ``load`` command
        in cli.py uses raw ram_write loops instead so state survives between
        loads unless --fill is requested.
        """
        if start < 0 or start + len(data) > RAM_SIZE:
            raise ValueError(
                f"RAM image offset 0x{start:X}..0x{start + len(data) - 1:X} "
                f"does not fit in {RAM_SIZE} bytes"
            )
        clear_n = min(max(len(data), 16), RAM_SIZE - start)
        for i in range(start, start + clear_n):
            self.ram_write(i, 0)
        for i, b in enumerate(data, start):
            self.ram_write(i, b)

    def verify_ram(self, data: bytes, start: int = 0, n_check: int = 4) -> bool:
        """Read back the first ``n_check`` bytes at RAM offset ``start``."""
        return all(self.ram_read(start + i) == data[i]
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


def reset_fifo_buffers() -> None:
    """Reset both AXI FIFO data paths.

    The AXI FIFO and axis_byte_bridge have buffering independent of the
    z80_soc ctrl_reset signal.  Draining the character device alone can miss
    store-forward packets that become visible only after the next transfer.
    Uses the documented PG080 reset registers to discard both directions
    explicitly.
    """
    fd = os.open("/dev/mem", os.O_RDWR | os.O_SYNC)
    try:
        mem = mmap.mmap(
            fd, 0x1000, mmap.MAP_SHARED,
            mmap.PROT_READ | mmap.PROT_WRITE,
            offset=FIFO_BASE,
        )
        try:
            mem[FIFO_TDFR:FIFO_TDFR + 4] = struct.pack("<I", FIFO_RESET_VALUE)
            mem[FIFO_RDFR:FIFO_RDFR + 4] = struct.pack("<I", FIFO_RESET_VALUE)
        finally:
            mem.close()
    finally:
        os.close(fd)
    # Allow the FIFO reset and any bridge-stage transfer to settle before the
    # drain pass below.
    time.sleep(0.005)


def write_byte(fd: int, b: int) -> None:
    """Send one byte through the v1 drop-24 bridge (4-byte write)."""
    os.write(fd, bytes([b & 0xFF, 0, 0, 0]))


def _low_bytes_from_words(word: bytes) -> list[int]:
    """Take the payload byte of each 32-bit drop-24 word."""
    if len(word) < 4:
        return [word[0]] if word else []
    return [word[i] for i in range(0, len(word) - (len(word) % 4), 4)]


def read_available(fd: int, max_bytes: int = 4096) -> list[int]:
    """Non-blocking drain of currently readable AXI-Stream packets.

    The staging ``axis_fifo`` driver has no ``f_op->poll``.  Userspace must
    not wait for POLLIN: a non-blocking ``read()`` is the only reliable way
    to learn that a packet is queued.  Each v1 packet is one 32-bit word
    (low byte = data).  The read size is large so a stuck RLR > 4 (missed
    TLAST) cannot wedge the FIFO with EINVAL-on-4-byte-read.
    """
    out: list[int] = []
    while len(out) < max_bytes:
        try:
            word = os.read(fd, 4096)
        except BlockingIOError:
            break
        except OSError as exc:
            if exc.errno in (errno.EAGAIN, errno.EWOULDBLOCK, errno.EINVAL):
                break
            raise
        if not word:
            break
        chunk = _low_bytes_from_words(word)
        if not chunk:
            break
        out.extend(chunk)
    return out


def read_byte(fd: int) -> int | None:
    """Read one byte from the v1 drop-24 bridge.

    Returns the byte or None if no data is available.  The axis-fifo driver
    can report both EAGAIN and EINVAL while a packet is not yet readable;
    both are treated as transient for non-blocking reads.  Other errors are
    propagated instead of being silently mistaken for an empty FIFO.
    """
    got = read_available(fd, max_bytes=1)
    return got[0] if got else None


def flush_fifo(fd, settle: float = 0.10) -> int:
    """Flush stale RX packets until the FIFO is quiet for ``settle`` seconds.

    The FIFO and byte bridge are pipelined, so a single empty read is not
    sufficient to establish that the previous program's output is gone.
    Return the number of packets discarded; this is useful for diagnostics.
    """
    discarded = 0
    quiet_deadline = time.monotonic() + settle

    while True:
        got = read_available(fd)
        now = time.monotonic()
        if got:
            discarded += len(got)
            quiet_deadline = now + settle
        if now >= quiet_deadline:
            return discarded

        # Do not wait on poll/select: axis_fifo has no f_op->poll, so those
        # either spin (DEFAULT_POLLMASK) or never report a queued packet.
        timeout = min(0.005, quiet_deadline - now)
        if timeout > 0:
            time.sleep(timeout)


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

        remaining = None if max_bytes is None else max_bytes - len(captured)
        got = read_available(fd, max_bytes=remaining or 4096)
        if got:
            captured.extend(got)
            last_activity = time.monotonic()
            continue
        if time.monotonic() - last_activity > idle_timeout:
            break
        time.sleep(0.05)

    return captured
