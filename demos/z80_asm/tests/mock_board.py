"""Test doubles for the Z80 board hardware layer.

MockBoard emulates the z80_soc control ports in memory:
  * ROM:  Z80 addresses 0x0000..0x1FFF, addressed like hw.Z80Board.rom_*
  * RAM:  PS offsets 0..0xDFFF (Z80 0x2000..0xFFFF), addressed like
          hw.Z80Board.ram_* (i.e. offset = Z80 addr - 0x2000)
  * CPU state: halted/running with pulse counters for halt/reset/run.

run_main() patches z80_board.cli's hardware layer so cli.main(argv) runs
against a MockBoard without /dev/mem or the axis FIFO.  State is kept in the
MockBoard, so successive invocations behave like a live FPGA.
"""

import contextlib
import io
import os

from z80_board import hw


class MockBoard:
    """In-memory stand-in for hw.Z80Board."""

    def __init__(self, rom_init: bytes | None = None):
        self.rom = bytearray(rom_init or bytes(hw.ROM_SIZE))
        self.ram = bytearray(hw.RAM_SIZE)
        self.halted = True
        self.halt_pulses = 0
        self.run_pulses = 0
        self.reset_pulses = 0

    # -- CPU ----------------------------------------------------------
    def status(self) -> bool:
        return self.halted

    def halt(self):
        self.halted = True
        self.halt_pulses += 1

    def run(self):
        self.halted = False
        self.run_pulses += 1

    def reset(self):
        self.halted = True
        self.reset_pulses += 1

    # -- ROM ----------------------------------------------------------
    def rom_write(self, addr: int, data: int):
        self.rom[addr] = data

    def rom_read(self, addr: int) -> int:
        return self.rom[addr]

    def load_rom(self, data: bytes, start: int = 0):
        for i, b in enumerate(data, start):
            self.rom[i] = b

    # -- RAM ----------------------------------------------------------
    def ram_write(self, offset: int, data: int):
        self.ram[offset] = data

    def ram_read(self, offset: int) -> int:
        return self.ram[offset]

    def ram_bytes(self, z80_addr: int, length: int) -> bytes:
        """Convenience: read back RAM as bytes at a Z80 address."""
        off = z80_addr - hw.RAM_BASE
        return bytes(self.ram[off:off + length])

    def rom_bytes(self, z80_addr: int, length: int) -> bytes:
        return bytes(self.rom[z80_addr:z80_addr + length])

    # -- context manager (cli.main uses: with hw.Z80Board() as brd) -----
    def __enter__(self):
        return self

    def __exit__(self, *a):
        pass


def run_main(argv, board: MockBoard | None = None, fifo_dev="/dev/null",
             board_factory=None):
    """Run cli.main(argv) with the hardware layer replaced by mocks.

    Returns (exit_code, stdout, stderr).  ``board`` defaults to a fresh
    instance per call; pass a shared MockBoard to emulate a live FPGA across
    invocations.  ``fifo_dev`` names the device used for the axis FIFO fd;
    pass None to simulate the axis_fifo driver being absent.
    ``board_factory`` replaces Z80Board entirely (e.g. one whose __enter__
    raises PermissionError, to exercise the clean "need root" error).
    """
    import z80_board.cli as cli

    b = board if board is not None else MockBoard()

    class _FakeZ80Board:
        def __new__(cls):
            return b

        def __enter__(self):
            return b

        def __exit__(self, *a):
            pass

    def _no_fifo(dev):
        raise FileNotFoundError(dev)

    real = {k: getattr(cli.hw, k) for k in
            ("Z80Board", "open_fifo", "reset_fifo_buffers", "flush_fifo")}
    try:
        cli.hw.Z80Board = (board_factory if board_factory is not None
                           else _FakeZ80Board)
        cli.hw.open_fifo = _no_fifo if fifo_dev is None \
            else lambda dev: os.open(fifo_dev, os.O_RDWR | os.O_NONBLOCK)
        # FIFO serve-code is exercised on the real board; unit tests only
        # need the plumbing (no /dev/mem writes, assume nothing buffered).
        cli.hw.reset_fifo_buffers = lambda: None
        cli.hw.flush_fifo = lambda fd, settle=0.1: 0

        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            try:
                rc = cli.main(list(argv))
            except SystemExit as e:                # _err() paths
                rc = 0 if e.code is None else (e.code if isinstance(e.code, int) else 1)
        return rc, out.getvalue(), err.getvalue()
    finally:
        for k, v in real.items():
            setattr(cli.hw, k, v)
