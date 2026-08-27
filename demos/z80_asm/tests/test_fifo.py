"""FIFO drain / term-bridge tests (no hardware).

Byte-stream FIFO: one byte per TDATA beat.  ``term`` registers both stdin
and the new ``axi_byte_fifo`` descriptor with ``select.poll()``; the FIFO
readiness event drives output draining without a periodic timeout.
"""

import errno
import os
import unittest
from unittest import mock

from z80_board import hw
from z80_board import cli


def _packet(*bytes_) -> bytes:
    """Byte-stream packet helper: bytes(bytes_) (no 4-byte padding)."""
    return bytes(bytes_)


class ReadAvailableTest(unittest.TestCase):

    def setUp(self):
        self.r, self.w = os.pipe()
        os.set_blocking(self.r, False)

    def tearDown(self):
        os.close(self.r)
        os.close(self.w)

    def test_empty_pipe_is_empty_list(self):
        self.assertEqual(hw.read_available(self.r), [])

    def test_drains_every_queued_byte(self):
        os.write(self.w, _packet(0x41, 0x42, 0x43, 0x44))
        self.assertEqual(hw.read_available(self.r),
                         [0x41, 0x42, 0x43, 0x44])
        self.assertEqual(hw.read_available(self.r), [])

    def test_read_byte_is_one_from_available(self):
        os.write(self.w, _packet(0x10))
        self.assertEqual(hw.read_byte(self.r), 0x10)
        self.assertIsNone(hw.read_byte(self.r))

    def test_einval_is_transient(self):
        with mock.patch("os.read", side_effect=OSError(errno.EINVAL, "x")):
            self.assertEqual(hw.read_available(self.r), [])
            self.assertIsNone(hw.read_byte(self.r))

    def test_multiple_writes_coalesce(self):
        os.write(self.w, b"AB")
        os.write(self.w, b"CD")
        # read_available drains both writes in one call (pipe coalesce)
        got = hw.read_available(self.r)
        self.assertEqual(got, [ord("A"), ord("B"), ord("C"), ord("D")])

    def test_write_byte_is_single_byte(self):
        # hw.write_byte must be a 1-byte write (no 0x00 padding)
        r2, w2 = os.pipe()
        try:
            hw.write_byte(w2, 0x5A)
            os.close(w2)
            # non-blocking read from r2 should get exactly 1 byte
            os.set_blocking(r2, False)
            data = os.read(r2, 16)
            self.assertEqual(data, b"\x5A")
        finally:
            try:
                os.close(r2)
            except OSError:
                pass
            try:
                os.close(w2)
            except OSError:
                pass


class DrainAndTermTest(unittest.TestCase):

    def setUp(self):
        self.fifo_r, self.fifo_w = os.pipe()
        self.out_r, self.out_w = os.pipe()
        self.in_r, self.in_w = os.pipe()
        os.set_blocking(self.fifo_r, False)
        os.set_blocking(self.in_r, False)

    def tearDown(self):
        for fd in (self.fifo_r, self.fifo_w, self.out_r, self.out_w,
                   self.in_r, self.in_w):
            try:
                os.close(fd)
            except OSError:
                pass

    def test_drain_writes_all_pending_bytes(self):
        os.write(self.fifo_w, _packet(ord("0"), ord(" "), ord("1")))
        n = cli._drain_fifo_to(self.fifo_r, self.out_w)
        self.assertEqual(n, 3)
        self.assertEqual(os.read(self.out_r, 16), b"0 1")

    def test_ctrl_bracket_stops_but_still_drains(self):
        # FIFO has a burst queued; stdin only has the detach key.  The old
        # loop would ignore the FIFO unless poll reported POLLIN on it.
        os.write(self.fifo_w, _packet(*b"848"))
        os.write(self.in_w, b"\x1d")
        cli._term_session(self.fifo_r, self.in_r, self.out_w,
                          poll_timeout_ms=5)
        self.assertEqual(os.read(self.out_r, 16), b"848")

    def test_fifo_poll_event_drains_without_keystroke(self):
        os.write(self.fifo_w, _packet(*b"xyz"))
        os.write(self.in_w, b"\x1d")
        calls = []
        events = [[(self.fifo_r, cli.select.POLLIN)],
                  [(self.in_r, cli.select.POLLIN)]]

        class _Poll:
            def register(self, fd, mask):
                calls.append(("register", fd, mask))

            def unregister(self, fd):
                calls.append(("unregister", fd))

            def poll(self, timeout=None):
                calls.append(("poll", timeout))
                return events.pop(0)

        with mock.patch("z80_board.cli.select.poll", _Poll), \
                mock.patch("z80_board.cli.os.isatty", return_value=True):
            cli._term_session(self.fifo_r, self.in_r, self.out_w,
                              poll_timeout_ms=5)

        self.assertEqual(os.read(self.out_r, 16), b"xyz")
        self.assertIn(("register", self.fifo_r, cli.select.POLLIN), calls)
        self.assertIn(("register", self.in_r, cli.select.POLLIN), calls)
        # The normal interactive wait is indefinite; no 20 ms timeout spin.
        self.assertEqual(calls[2], ("poll", -1))

    def test_forward_ctrl_bracket_requests_stop(self):
        os.write(self.in_w, b"\x1d")
        self.assertFalse(cli._forward_stdin_to_fifo(self.in_r, self.fifo_w))


if __name__ == "__main__":
    unittest.main()
