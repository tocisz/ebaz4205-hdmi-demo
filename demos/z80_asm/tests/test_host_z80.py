"""Host-side helper (z80.py) pure-function tests (no SSH, no hardware)."""

import contextlib
import io
import pathlib
import unittest
from types import SimpleNamespace
from unittest import mock

import z80


class FlagTokensTest(unittest.TestCase):

    def test_bool_flags(self):
        args = SimpleNamespace(verify_all=True, no_verify=False,
                               force_halt=False, strict=True,
                               vector=None, fill=None)
        toks = z80._flag_tokens(args, [
            ("vector", "--vector", True),
            ("fill", "--fill", True),
            ("verify_all", "--verify-all", False),
            ("no_verify", "--no-verify", False),
            ("force_halt", "--force-halt", False),
            ("strict", "--strict", False),
        ])
        self.assertEqual(toks, ["--verify-all", "--strict"])

    def test_value_flags_hex(self):
        args = SimpleNamespace(vector=0x100, fill=0)
        toks = z80._flag_tokens(args, [
            ("vector", "--vector", True),
            ("fill", "--fill", True),
        ])
        self.assertEqual(toks, ["--vector", "0x100", "--fill", "0x0"])

    def test_none_values_skipped(self):
        args = SimpleNamespace(vector=None, fill=None, strict=False)
        toks = z80._flag_tokens(args, [
            ("vector", "--vector", True),
            ("fill", "--fill", True),
            ("strict", "--strict", False),
        ])
        self.assertEqual(toks, [])


class PackageLayoutTest(unittest.TestCase):

    def test_board_pkg_files_exist(self):
        for fn in z80.TOOL_FILES:
            self.assertTrue((z80.BOARD_PKG / fn).is_file(),
                            f"missing {fn} in {z80.BOARD_PKG}")
        self.assertTrue(z80.BOARD_RUNNER.is_file())


class UploadTokenTest(unittest.TestCase):
    """_is_upload_token: verbs can never be shadowed by same-named files."""

    class _AnyFile:
        """Path stand-in: every token 'exists' as a relative regular file."""

        def __init__(self, tok: str):
            self._tok = tok

        def exists(self):
            return True

        def is_file(self):
            return True

        def is_absolute(self):
            return self._tok.startswith("/")

        @property
        def suffix(self):
            return pathlib.Path(self._tok).suffix

    def test_verb_words_never_upload(self):
        with mock.patch.object(z80, "Path", self._AnyFile):
            for tok in ("status", "halt", "run", "reset", "flush",
                        "load", "dump", "term", "all", "max",
                        "--fill", "-n"):
                self.assertFalse(z80._is_upload_token(tok), tok)

    def test_existing_files_upload_including_absolute(self):
        with mock.patch.object(z80, "Path", self._AnyFile):
            for tok in ("counter.bin", "hello.hex", "app.ihx",
                        "src/app.out", "./prog.s", "nested/x.z80",
                        "/tmp/hello.hex", "/home/u/rom_ebaz.bin"):
                self.assertTrue(z80._is_upload_token(tok), tok)

    def test_no_file_no_upload(self):
        self.assertFalse(z80._is_upload_token("definitely-not-here.hex"))


class TokenBuilderTest(unittest.TestCase):
    """Host load/dump/term token builders (flags emitted exactly once)."""

    def _args(self, **kw) -> SimpleNamespace:
        base = dict(args=[], vector=None, fill=None, verify_all=False,
                    no_verify=False, force_halt=False, strict=False,
                    output=None)
        base.update(kw)
        return SimpleNamespace(**base)

    def test_load_tokens_emit_flags_once(self):
        args = self._args(args=["app.bin", "0x8400"],
                          vector=0x100, strict=True)
        self.assertEqual(z80._load_tokens("ram", args),
                         ["load", "ram", "app.bin", "0x8400",
                          "--vector", "0x100", "--strict"])

    def test_load_tokens_reject_flag_in_positionals(self):
        args = self._args(args=["--vector", "app.bin"])
        err = io.StringIO()
        with contextlib.redirect_stderr(err), self.assertRaises(SystemExit):
            z80._load_tokens("ram", args)
        self.assertIn("unexpected token '--vector'", err.getvalue())

    def test_dump_tokens(self):
        args = self._args(args=["0x8400", "64"], force_halt=True)
        toks, remote_out = z80._dump_tokens("ram", args)
        self.assertEqual(toks,
                         ["dump", "ram", "0x8400", "64", "--force-halt"])
        self.assertIsNone(remote_out)

    def test_dump_tokens_with_output(self):
        args = self._args(args=["0x8400", "64"], output="out.bin")
        toks, remote_out = z80._dump_tokens("ram", args)
        self.assertEqual(toks[:5], ["dump", "ram", "0x8400", "64", "-o"])
        self.assertIsNotNone(remote_out)

    def test_term_tokens(self):
        self.assertEqual(z80._term_tokens(SimpleNamespace(flush=True)),
                         ["term", "--flush"])
        self.assertEqual(z80._term_tokens(SimpleNamespace(flush=False)),
                         ["term", "--no-flush"])
        self.assertEqual(z80._term_tokens(SimpleNamespace(flush=None)),
                         ["term"])


if __name__ == "__main__":
    unittest.main()
