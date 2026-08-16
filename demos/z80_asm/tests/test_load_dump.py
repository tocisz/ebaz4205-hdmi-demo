"""load / dump command handler tests against an in-memory MockBoard."""

import contextlib
import io
import os
import tempfile
import unittest
from unittest import mock

from z80_board import cli, hw
from tests.mock_board import MockBoard


def _hex_record(addr: int, data: bytes) -> str:
    rec = bytes([len(data)]) + addr.to_bytes(2, "big") + bytes([0]) + data
    csum = (-sum(rec)) & 0xFF
    return ":" + (rec + bytes([csum])).hex().upper()


def _write_tmp(content: bytes, suffix: str) -> str:
    fd, path = tempfile.mkstemp(suffix=suffix)
    with os.fdopen(fd, "wb") as f:
        f.write(content)
    return path


class LoadDumpTestCase(unittest.TestCase):

    def setUp(self):
        self.b = MockBoard()
        self._tmp: list[str] = []

    def tmp(self, content: bytes, suffix: str = ".bin") -> str:
        p = _write_tmp(content, suffix)
        self._tmp.append(p)
        return p

    def tearDown(self):
        for p in self._tmp:
            try:
                os.unlink(p)
            except FileNotFoundError:
                pass

    def load(self, target: str, *rest):
        opts, pos = cli._parse_load(list(rest))
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cli._cmd_load(self.b, target, opts, pos, quiet=True)
        return err.getvalue()

    def dump(self, target: str, *rest):
        opts, pos = cli._parse_dump(list(rest))
        out, err = io.StringIO(), io.StringIO()
        with contextlib.redirect_stdout(out), contextlib.redirect_stderr(err):
            cli._cmd_dump(self.b, target, opts, pos, quiet=True)
        return out.getvalue(), err.getvalue()

    # -- binary load --------------------------------------------------

    def test_load_ram_binary_default_address(self):
        p = self.tmp(b"\xC3\x00\x20\x01\x02\x03")
        self.load("ram", p)
        self.assertEqual(self.b.ram_bytes(0x2000, 6),
                         b"\xC3\x00\x20\x01\x02\x03")
        # Sparse: nothing beyond the image is zeroed.
        self.assertEqual(self.b.ram[6], 0)

    def test_load_rom_binary_default_address(self):
        p = self.tmp(b"\xC3\x4A\x01\x00\xFF")
        self.load("rom", p)
        self.assertEqual(self.b.rom_bytes(0x0000, 5),
                         b"\xC3\x4A\x01\x00\xFF")

    def test_load_ram_binary_explicit_address(self):
        p = self.tmp(b"\xDE\xAD")
        self.load("ram", p, "0x8400")
        self.assertEqual(self.b.ram_bytes(0x8400, 2), b"\xDE\xAD")

    def test_load_rom_binary_explicit_address(self):
        p = self.tmp(b"\xAA")
        self.load("rom", p, "0x0100")
        self.assertEqual(self.b.rom_bytes(0x0100, 1), b"\xAA")

    # -- hex load -----------------------------------------------------

    def test_load_hex_records_carry_address(self):
        # Equivalent of RC2014 `objcopy -O ihex -j.ram` output at 0x8400.
        p = self.tmp((":10840000060A211784CDE4013E0B90CD8E1E211E5D\n"
                      ":1084100084CDE40110ECC948656C6C6F20000A0043\n"
                      ":00000001FF\n").encode(), ".hex")
        self.load("ram", p)
        self.assertEqual(self.b.ram_bytes(0x8400, 4), b"\x06\x0A\x21\x17")
        # record 2: ... C9 'H' 'e' 'l' 'l' 'o' ' ' 00 0A 00
        self.assertEqual(self.b.ram_bytes(0x8417, 5), b"Hello")
        self.assertEqual(self.b.ram_bytes(0x841C, 4), b" \x00\x0a\x00")

    def test_load_hex_added_base(self):
        p = self.tmp((_hex_record(0x1000, b"\x01\x02") + "\n"
                      + ":00000001FF\n").encode(), ".hex")
        self.load("ram", p, "0x2000")       # every record shifted up by 0x2000
        self.assertEqual(self.b.ram_bytes(0x3000, 2), b"\x01\x02")
        self.assertEqual(self.b.ram[0], 0)  # 0x2000 itself untouched

    def test_load_hex_two_segments(self):
        text = (_hex_record(0x2000, b"\xAA") + "\n"
                + _hex_record(0x3000, b"\xBB") + "\n"
                + ":00000001FF\n")
        p = self.tmp(text.encode(), ".hex")
        self.load("ram", p)
        self.assertEqual(self.b.ram_bytes(0x2000, 1), b"\xAA")
        self.assertEqual(self.b.ram_bytes(0x3000, 1), b"\xBB")

    # -- vector -------------------------------------------------------

    def test_vector_writes_jp_and_verify_stays_clean(self):
        p = self.tmp(b"\xC3\x4A\x01\x00\xFF")   # image starts at 0x0000
        err = self.load("rom", p, "--vector", "0x100")
        self.assertEqual(self.b.rom_bytes(0, 3), b"\xC3\x00\x01")  # JP 0x100
        self.assertEqual(self.b.rom_bytes(3, 2), b"\x00\xFF")      # image rest
        self.assertNotIn("MISMATCH", err)
        self.assertNotIn("failed", err)

    def test_vector_without_image(self):
        with self.assertRaises(SystemExit):
            self.load("rom", "--vector", "0x100")   # no file -> usage error
        p = self.tmp(b"")
        self.load("rom", p, "--vector", "0x0200")   # vector only, empty image
        self.assertEqual(self.b.rom_bytes(0, 3), b"\xC3\x00\x02")

    def test_vector_rejected_for_ram(self):
        p = self.tmp(b"\x01")
        with self.assertRaises(SystemExit):
            self.load("ram", p, "--vector", "0x100")

    # -- fill ---------------------------------------------------------

    def test_fill_clears_region_only(self):
        self.b.ram[0:8] = bytes([0xFF] * 8)     # old program residue
        p = self.tmp(b"\x01\x02")
        self.load("ram", p, "--fill", "0x00")
        self.assertEqual(self.b.ram_bytes(0x2000, 2), b"\x01\x02")
        # fill covers exactly the loaded span; residue beyond stays
        self.assertEqual(self.b.ram_bytes(0x2002, 2), b"\xFF\xFF")

    def test_fill_spans_sparse_hex_gaps(self):
        # Two hex records with a gap between them: --fill must zero the gap.
        text = (_hex_record(0x2000, b"\xAA") + "\n"
                + _hex_record(0x3000, b"\xBB") + "\n"
                + ":00000001FF\n")
        self.b.ram[0:0x1000] = bytes([0xFF]) * 0x1000
        p = self.tmp(text.encode(), ".hex")
        self.load("ram", p, "--fill", "0x00")
        self.assertEqual(self.b.ram_bytes(0x2000, 1), b"\xAA")
        self.assertEqual(self.b.ram_bytes(0x2001, 0x1000 - 2), bytes(0xFFE))  # gap
        self.assertEqual(self.b.ram_bytes(0x3000, 1), b"\xBB")

    def test_fill_rejected_for_rom(self):
        p = self.tmp(b"\x01")
        with self.assertRaises(SystemExit):
            self.load("rom", p, "--fill", "0x00")

    # -- safety / bounds ----------------------------------------------

    def test_load_requires_halted(self):
        self.b.run()
        p = self.tmp(b"\x01")
        with self.assertRaises(SystemExit) as cm:
            self.load("ram", p)
        self.assertEqual(cm.exception.code, 1)
        # --force-halt halts and proceeds
        self.load("ram", p, "--force-halt")
        self.assertTrue(self.b.halted)
        self.assertEqual(self.b.ram_bytes(0x2000, 1), b"\x01")

    def test_overflow_clipped_with_warning(self):
        p = self.tmp(bytes(0xF000))            # 0x2000+0xF000 ends at 0x11000
        err = self.load("ram", p)
        self.assertIn("clipped", err)
        self.assertEqual(self.b.ram_bytes(0xFFFF, 1), b"\x00")
        self.assertEqual(self.b.ram_bytes(0x2000, 1), b"\x00")

    def test_overflow_strict_is_error(self):
        p = self.tmp(bytes(0xF000))
        with self.assertRaises(SystemExit):
            self.load("ram", p, "--strict")

    def test_all_out_of_space_errors_cleanly(self):
        # Nothing at all landed in ROM -> error instead of a success report
        # with 0 bytes written.
        p = self.tmp(b"\x01")
        with self.assertRaises(SystemExit):
            self.load("rom", p, "0x3000")      # outside ROM entirely

    def test_partial_clip_warns_and_loads_rest(self):
        # First record lands; second runs past the top of RAM and is
        # clipped with a warning instead of aborting the whole load.
        # 0xFFFE + 3 bytes overflows 0x10000 by one.
        text = (_hex_record(0x2000, b"\xAA") + "\n"
                + _hex_record(0xFFFE, b"\xBB\xCC\xDD") + "\n"
                + ":00000001FF\n")
        p = self.tmp(text.encode(), ".hex")
        err = self.load("ram", p)
        self.assertIn("clipped", err)
        self.assertEqual(self.b.ram_bytes(0x2000, 1), b"\xAA")
        self.assertEqual(self.b.ram_bytes(0xFFFE, 2), b"\xBB\xCC")

    def test_out_of_space_strict_is_error(self):
        p = self.tmp(b"\x01")
        with self.assertRaises(SystemExit):
            self.load("rom", p, "0x3000", "--strict")

    def test_missing_file(self):
        with self.assertRaises(SystemExit) as cm:
            self.load("ram", "/no/such/file.bin")
        self.assertEqual(cm.exception.code, 1)

    def test_malformed_hex(self):
        p = self.tmp(b":BOGUS\n", ".hex")
        with self.assertRaises(SystemExit) as cm:
            self.load("ram", p)
        self.assertNotEqual(str(cm.exception), "")  # clean error, exit 1

    def test_wrong_target_usage(self):
        p = self.tmp(b"\x01")
        with self.assertRaises(SystemExit):
            self.load("nope", p)

    # -- verification -------------------------------------------------

    def test_verify_mismatch_detected(self):
        class Flaky(MockBoard):
            """Read-back returns a corrupted byte for one offset."""

            def __init__(self):
                super().__init__()
                self.fail_offset = 0

            def ram_read(self, offset):
                v = super().ram_read(offset)
                if offset == self.fail_offset:
                    return (v + 1) & 0xFF
                return v

        b = Flaky()
        opts, pos = cli._parse_load([self.tmp(b"\xC3\x00\x20")])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cli._cmd_load(b, "ram", opts, pos, quiet=True)
        self.assertIn("0x2000", err.getvalue())   # mismatch reported at Z80 addr
        self.assertIn("failed", err.getvalue())

    def test_verify_samples_interior_bytes(self):
        # A 32-byte image is sampled (not fully verified): the default
        # sample must include interior quartiles so a middle corruption is
        # caught without --verify-all.
        class Flaky(MockBoard):
            def __init__(self):
                super().__init__()
                self.bad = 16                # Z80 0x2010 (middle quartile)

            def ram_read(self, offset):
                v = super().ram_read(offset)
                return (v + 1) & 0xFF if offset == self.bad else v

        b = Flaky()
        opts, pos = cli._parse_load([self.tmp(bytes(range(32)))])
        err = io.StringIO()
        with contextlib.redirect_stderr(err):
            cli._cmd_load(b, "ram", opts, pos, quiet=True)
        self.assertIn("VERIFY MISMATCH at 0x2010", err.getvalue())

    # -- dump ---------------------------------------------------------

    def test_dump_ram_defaults(self):
        self.b.ram[0:4] = b"\xC3\x00\x20\xDE"
        out, _ = self.dump("ram")               # default: 0x2000, 256 bytes
        first = out.splitlines()[0]
        self.assertTrue(first.startswith("2000: c3 00 20 de"), first)
        self.assertEqual(len(out.splitlines()), 16)

    def test_dump_rom_all(self):
        self.b.load_rom(b"\xAA", 0)
        out, _ = self.dump("rom")               # default: full 0x2000 bytes
        self.assertTrue(out.splitlines()[0].startswith("0000: aa"))
        self.assertEqual(len(out.splitlines()), 512)  # 8192/16

    def test_dump_length_all(self):
        self.b.ram[0] = 0x12
        out, _ = self.dump("ram", "0x2000", "all")
        self.assertEqual(len(out.splitlines()), hw.RAM_SIZE // 16)

    def test_dump_to_file(self):
        self.b.ram[0:2] = b"\x12\x34"
        out_path = self.tmp(b"")                # placeholder; overwritten
        self.dump("ram", "0x2000", "2", "-o", out_path)
        with open(out_path, "rb") as f:
            self.assertEqual(f.read(), b"\x12\x34")

    def test_dump_binary_stdout(self):
        self.b.ram[0:3] = b"\xDE\xAD\xBE"
        opts, pos = cli._parse_dump(["0x2000", "3", "--binary"])
        raw = io.BytesIO()
        fake_stdout = type("FakeStdout", (), {
            "buffer": raw,
            "write": lambda self, s: None,
            "flush": lambda self: None,
        })()
        with mock.patch("sys.stdout", fake_stdout):
            cli._cmd_dump(self.b, "ram", opts, pos, quiet=True)
        self.assertEqual(raw.getvalue(), b"\xDE\xAD\xBE")

    def test_dump_to_file_bytes(self):
        self.b.load_rom(bytes(range(256)), 0)
        out_path = self.tmp(b"")
        self.dump("rom", "0", "256", "-o", out_path)
        with open(out_path, "rb") as f:
            self.assertEqual(f.read(), bytes(range(256)))

    def test_dump_requires_halted(self):
        self.b.run()
        with self.assertRaises(SystemExit):
            self.dump("ram", "0x2000", "4")
        self.dump("ram", "0x2000", "4", "--force-halt")

    def test_dump_bad_address(self):
        with self.assertRaises(SystemExit):
            self.dump("ram", "0x1000", "4")     # below RAM
        with self.assertRaises(SystemExit):
            self.dump("rom", "0x2000", "4")     # above ROM


if __name__ == "__main__":
    unittest.main()
