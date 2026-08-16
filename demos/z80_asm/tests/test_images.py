"""Intel HEX / binary parsing and hexdump format tests (no hardware)."""

import os
import tempfile
import unittest
from pathlib import Path

from z80_board.images import (ImageError, format_hexdump,
                              parse_image, parse_intel_hex)


def mkhex(addr: int, data: bytes, rtype: int = 0x00) -> str:
    """Build one Intel HEX record with a correct checksum."""
    rec = bytes([len(data)]) + addr.to_bytes(2, "big") + bytes([rtype]) + data
    csum = (-sum(rec)) & 0xFF
    return ":" + (rec + bytes([csum])).hex().upper()


def eof() -> str:
    return mkhex(0, b"", rtype=0x01)


class ParseIntelHexTest(unittest.TestCase):

    def test_single_contiguous_segment(self):
        data = bytes(range(0x20))
        text = mkhex(0x0100, data) + "\n" + eof()
        segs = parse_intel_hex(text)
        self.assertEqual(segs, [(0x0100, data)])

    def test_gap_splits_segments(self):
        text = (mkhex(0x2000, bytes([0x11]) * 4) + "\n"
                + mkhex(0x2010, bytes([0x22]) * 4) + "\n"
                + eof())
        self.assertEqual(parse_intel_hex(text),
                         [(0x2000, bytes([0x11]) * 4),
                          (0x2010, bytes([0x22]) * 4)])

    def test_extended_linear_address(self):
        # type 04 record carries the upper 16 bits as *data*
        text = (mkhex(0, b"\x00\x01", rtype=0x04) + "\n"
                + mkhex(0x0080, bytes([0xAA, 0xBB, 0xCC])) + "\n"
                + eof())
        self.assertEqual(parse_intel_hex(text),
                         [(0x10080, bytes([0xAA, 0xBB, 0xCC]))])

    def test_segment_address_record(self):
        # type 02 record: value is an 8086 segment (<< 4)
        text = (mkhex(0, b"\x10\x00", rtype=0x02) + "\n"
                + mkhex(0x0000, bytes([0x01, 0x02])) + "\n"
                + eof())
        self.assertEqual(parse_intel_hex(text),
                         [(0x10000, bytes([0x01, 0x02]))])

    def test_eof_optional(self):
        # Files that stop after the last data record are tolerated.
        self.assertEqual(parse_intel_hex(mkhex(0, b"\x01\x02\x03")),
                         [(0, bytes([1, 2, 3]))])

    def test_blank_lines_ignored(self):
        text = "\n\n" + mkhex(0x10, bytes([9])) + "\n\n" + eof()
        self.assertEqual(parse_intel_hex(text), [(0x10, bytes([9]))])

    def test_checksum_error_rejected(self):
        # 1 data byte 0x42, then a (wrong) checksum of 0x00.
        rec = ":" + (bytes([1, 0, 0, 0, 0x42]) + bytes([0])).hex()
        with self.assertRaises(ImageError) as cm:
            parse_intel_hex(rec)
        self.assertIn("checksum", str(cm.exception))

    def test_bad_hex_digits_rejected(self):
        with self.assertRaises(ImageError):
            parse_intel_hex(":NOTHEX\n")

    def test_non_colon_lines_rejected(self):
        with self.assertRaises(ImageError):
            parse_intel_hex("101000 00 11\n")

    def test_unsupported_start_address_rejected(self):
        text = mkhex(0, b"\x00\x00\x00\x00", rtype=0x05) + "\n" + eof()
        with self.assertRaises(ImageError) as cm:
            parse_intel_hex(text)
        self.assertIn("05", str(cm.exception))
        text = mkhex(0, b"\x00\x00\x00\x00", rtype=0x03) + "\n" + eof()
        with self.assertRaises(ImageError):
            parse_intel_hex(text)

    def test_unknown_record_type_rejected(self):
        with self.assertRaises(ImageError):
            parse_intel_hex(mkhex(0, b"\x01", rtype=0x06) + "\n" + eof())

    def test_realistic_nascom_golden(self):
        # Equivalent of `z80-unknown-coff-objcopy -O ihex -j.ram` for the
        # RC2014 hello program: two 16-byte records at 0x8400 + EOF.
        text = (
            ":10840000060A211784CDE4013E0B90CD8E1E211E5D\n"
            ":1084100084CDE40110ECC948656C6C6F20000A0043\n"
            ":00000001FF\n"
        )
        segs = parse_intel_hex(text)
        self.assertEqual(len(segs), 1)
        addr, data = segs[0]
        self.assertEqual(addr, 0x8400)
        self.assertEqual(data[:8], b"\x06\x0A\x21\x17\x84\xCD\xE4\x01")
        # 'Hello' begins at offset 23 of the merged 32-byte blob
        self.assertEqual(data[23:28], b"Hello")
        self.assertEqual(data[29:], b"\x00\x0a\x00")


class ParseImageTest(unittest.TestCase):

    def _write(self, suffix: str, content: bytes) -> str:
        fd, path = tempfile.mkstemp(suffix=suffix)
        with os.fdopen(fd, "wb") as f:
            f.write(content)
        self.addCleanup(os.unlink, path)
        return path

    def test_binary_detection(self):
        path = self._write(".bin", b"\xC3\x00\x20\x01\x02\x03")
        kind, payload = parse_image(path)
        self.assertEqual((kind, payload), ("bin", b"\xC3\x00\x20\x01\x02\x03"))

    def test_hex_detection_by_content(self):
        text = mkhex(0x8400, bytes([0xC3, 0x00, 0xFF])) + "\n" + eof() + "\n"
        path = self._write(".hex", text.encode())
        kind, payload = parse_image(path)
        self.assertEqual(kind, "hex")
        self.assertEqual(len(payload), 1)
        self.assertEqual(payload[0], (0x8400, bytes([0xC3, 0x00, 0xFF])))

    def test_leading_blank_lines_before_hex(self):
        text = "\n\n" + mkhex(0, b"\x01") + "\n" + eof() + "\n"
        path = self._write(".hex", text.encode())
        kind, payload = parse_image(path)
        self.assertEqual(kind, "hex")


class FormatHexdumpTest(unittest.TestCase):

    def test_hexdump_lines(self):
        lines = list(format_hexdump(b"\xC3\x00\x20" + b"." * 13, base=0x2000))
        self.assertEqual(len(lines), 1)
        line = lines[0]
        self.assertTrue(line.startswith("2000: c3 00 20 2e 2e"))
        self.assertTrue(line.endswith("..."))

    def test_base_is_address_not_offset(self):
        lines = list(format_hexdump(b"AB", base=0xFFF0))
        self.assertTrue(lines[0].startswith("fff0: 41 42"))

    def test_multi_line(self):
        data = bytes(range(17))
        lines = list(format_hexdump(data))
        self.assertEqual(len(lines), 2)
        self.assertTrue(lines[1].startswith("0010:"))


if __name__ == "__main__":
    unittest.main()
