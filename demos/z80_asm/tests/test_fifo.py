"""FIFO drain / term-bridge tests (no hardware).

The live axis_fifo driver has no f_op->poll, so term must drain with
non-blocking reads on every wake — including a poll timeout or a
keystroke — rather than waiting for POLLIN on the device.
"""

import errno
import os
import unittest
from unittest import mock

from z80_board import hw
from z80_board import cli


def _packet(*bytes_) -> bytes:
    out = bytearray()
    for b in bytes_:
        out.extend((b, 0, 0, 0))
    return bytes(out)


class LowBytesTest(unittest.TestCase):

    def test_one_word(self):
        self.assertEqual(hw._low_bytes_from_words(b"A\x00\x00\x00"),
                         [ord("A")])

    def test_several_words(self):
        self.assertEqual(hw._low_bytes_from_words(_packet(1, 2, 3)),
                         [1, 2, 3])

    def test_short_read_keeps_first_byte(self):
        self.assertEqual(hw._low_bytes_from_words(b"Z"), [ord("Z")])

    def test_empty(self):
        self.assertEqual(hw._low_bytes_from_words(b""), [])


class ReadAvailableTest(unittest.TestCase):

    def setUp(self):
        self.r, self.w = os.pipe()
        os.set_blocking(self.r, False)

    def tearDown(self):
        os.close(self.r)
        os.close(self.w)

    def test_empty_pipe_is_empty_list(self):
        self.assertEqual(hw.read_available(self.r), [])

    def test_drains_every_queued_packet(self):
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

    def test_timeout_drains_without_keystroke(self):
        os.write(self.fifo_w, _packet(*b"xyz"))
        # First iteration: empty stdin poll + drain.  Then feed Ctrl-] so
        # the session can exit.  This is the "PRINT 0..1000" case: output
        # must keep flowing with nobody typing.
        calls = {"n": 0}

        real_poll = cli.select.poll

        class _Poll:
            def register(self, *a, **k):
                self._p = real_poll()
                self._p.register(*a, **k)

            def unregister(self, *a, **k):
                return self._p.unregister(*a, **k)

            def poll(self, timeout=None):
                calls["n"] += 1
                if calls["n"] == 1:
                    return []          # timeout, no keystroke
                return self._p.poll(timeout)

        with mock.patch("z80_board.cli.select.poll", _Poll):
            os.write(self.in_w, b"\x1d")
            cli._term_session(self.fifo_r, self.in_r, self.out_w,
                              poll_timeout_ms=5)
        self.assertEqual(os.read(self.out_r, 16), b"xyz")
        self.assertGreaterEqual(calls["n"], 1)

    def test_forward_ctrl_bracket_requests_stop(self):
        os.write(self.in_w, b"\x1d")
        self.assertFalse(cli._forward_stdin_to_fifo(self.in_r, self.fifo_w))


if __name__ == "__main__":
    unittest.main()
