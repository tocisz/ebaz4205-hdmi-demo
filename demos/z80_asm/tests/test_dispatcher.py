"""End-to-end cli.main() tests with the hardware layer mocked out.

A shared MockBoard is passed to run_main() so state persists across
invocations, exactly like the live FPGA does between z80 commands.
"""

import os
import tempfile
import unittest
from unittest import mock

from tests.mock_board import MockBoard, run_main


class DispatcherTest(unittest.TestCase):

    def setUp(self):
        self.b = MockBoard()
        self._tmp = tempfile.mktemp(suffix=".bin")
        with open(self._tmp, "wb") as f:
            f.write(b"\xC3\x00\x20\xDE\xAD")

    def tearDown(self):
        try:
            os.unlink(self._tmp)
        except FileNotFoundError:
            pass

    def invoke(self, *argv):
        """Run one board-tool invocation against the shared MockBoard."""
        return run_main(list(argv), board=self.b)

    # -- chains ---------------------------------------------------------

    def test_full_chain_leaves_cpu_running(self):
        rc, out, err = self.invoke("halt", "load", "ram", self._tmp,
                                   "reset", "run")
        self.assertEqual(rc, 0, err)
        self.assertEqual(self.b.halt_pulses, 1)
        self.assertEqual(self.b.reset_pulses, 1)
        self.assertEqual(self.b.run_pulses, 1)
        self.assertFalse(self.b.halted)
        self.assertEqual(self.b.ram_bytes(0x2000, 5), b"\xC3\x00\x20\xDE\xAD")

    def test_state_persists_across_invocations(self):
        self.invoke("halt", "load", "ram", self._tmp, "reset", "run")
        rc, out, _ = self.invoke("status")
        self.assertEqual(rc, 0)
        self.assertIn("halted=no", out)

        rc, out, _ = self.invoke("halt", "status")
        self.assertIn("halted=yes", out)

    def test_load_refused_while_running(self):
        self.invoke("reset", "run")
        rc, _, err = self.invoke("load", "ram", self._tmp)
        self.assertEqual(rc, 1)
        self.assertIn("halt it first", err)

    def test_chain_with_term_must_be_last(self):
        rc, _, err = self.invoke("term", "run")
        self.assertEqual(rc, 1)
        self.assertIn("must be the last", err)

    def test_unknown_first_word_is_legacy(self):
        # 'z80 frobnicate.bin' → legacy positional one-shot; missing file
        # errors cleanly before any hardware access.
        rc, _, err = self.invoke("frobnicate.bin", "-n", "4")
        self.assertEqual(rc, 1)
        self.assertIn("not found", err)

    # -- individual verbs -----------------------------------------------

    def test_status_when_halted(self):
        rc, out, _ = self.invoke("halt")
        self.assertEqual(rc, 0)
        rc, out, _ = self.invoke("status")
        self.assertIn("halted=yes", out)

    def test_reset_halts(self):
        self.invoke("run")
        rc, _, _ = self.invoke("reset")
        self.assertEqual(rc, 0)
        self.assertTrue(self.b.halted)

    def test_dump(self):
        self.invoke("halt", "load", "ram", self._tmp)
        rc, out, _ = self.invoke("dump", "ram", "0x2000", "4")
        self.assertEqual(rc, 0)
        self.assertTrue(out.splitlines()[0].startswith("2000: c3 00 20 de"))

    def test_dump_rom_matches_loaded_image(self):
        self.b.load_rom(b"\xC3\x00\x20", 0)     # pretend written earlier
        rc, out, _ = self.invoke("dump", "rom", "0", "3")
        self.assertEqual(rc, 0)
        self.assertTrue(out.splitlines()[0].startswith("0000: c3 00 20"))

    def test_stop_start_aliases(self):
        self.invoke("start")
        self.assertFalse(self.b.halted)
        rc, _, _ = self.invoke("stop")
        self.assertEqual(rc, 0)
        self.assertTrue(self.b.halted)

    # -- flush / fifo ---------------------------------------------------

    def test_flush_without_driver_is_clean_error(self):
        rc, _, err = run_main(["flush"], board=self.b, fifo_dev=None)
        self.assertEqual(rc, 1)
        self.assertIn("driver loaded", err)

    def test_flush_success(self):
        rc, _, err = self.invoke("flush")
        self.assertEqual(rc, 0)

    def test_term_without_tty_pipe_mode_works(self):
        # 'z80 term' without a tty is now pipe mode (e.g. echo "hi" | z80 term)
        # and should succeed without the old "ssh -t" error.
        # Use a pipe-like stdin (os.pipe) which is not a tty.
        r, w = os.pipe()
        os.close(w)  # EOF immediately
        try:
            with open(r, "rb", buffering=0) as pipe_in:
                with mock.patch("sys.stdin", pipe_in):
                    rc, _, err = self.invoke("term", "--flush")
            self.assertEqual(rc, 0, err)
        finally:
            try:
                os.close(r)
            except OSError:
                pass

    def test_need_root_is_clean_error(self):
        # Non-root /dev/mem access must produce a hint, not a traceback.
        class NoRoot:
            def __enter__(self):
                raise PermissionError("/dev/mem")
            def __exit__(self, *a):
                pass
        rc, _, err = run_main(["status"], board_factory=NoRoot)
        self.assertEqual(rc, 1)
        self.assertIn("root", err)

    # -- legacy ----------------------------------------------------------

    def test_legacy_help(self):
        rc, out, _ = self.invoke("run", "--help")
        self.assertEqual(rc, 0)
        self.assertIn("Legacy single-shot", out)


if __name__ == "__main__":
    unittest.main()
