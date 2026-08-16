"""Chain parsing logic (z80_board.cli._split_chain) tests."""

import unittest

from z80_board import cli


class ChainSplitTest(unittest.TestCase):

    def test_simple_chain(self):
        quiet, actions = cli._split_chain(
            ["halt", "load", "ram", "x.bin", "reset", "run"])
        self.assertFalse(quiet)
        self.assertEqual(actions, [
            ("halt", []),
            ("load", ["ram", "x.bin"]),
            ("reset", []),
            ("run", []),
        ])

    def test_aliases_resolved(self):
        _, actions = cli._split_chain(["stop", "connect", "--flush"])
        self.assertEqual(actions, [("halt", []), ("term", ["--flush"])])
        _, actions = cli._split_chain(["start"])
        self.assertEqual(actions, [("run", [])])

    def test_verbs_with_dash_options(self):
        _, actions = cli._split_chain(
            ["load", "rom", "rom.bin", "--vector", "0x100", "--verify-all"])
        self.assertEqual(actions, [
            ("load", ["rom", "rom.bin", "--vector", "0x100", "--verify-all"]),
        ])

    def test_global_quiet_popped(self):
        quiet, actions = cli._split_chain(["-q", "status"])
        self.assertTrue(quiet)
        self.assertEqual(actions, [("status", [])])
        quiet, actions = cli._split_chain(["--quiet", "halt", "run"])
        self.assertTrue(quiet)

    def test_legacy_positional_form(self):
        # z80 counter.bin -n 64  → the whole line is the legacy one-shot.
        quiet, actions = cli._split_chain(["counter.bin", "-n", "64"])
        self.assertEqual(actions, [("__legacy__", ["counter.bin", "-n", "64"])])

    def test_legacy_positional_with_quiet(self):
        quiet, actions = cli._split_chain(["-q", "counter.bin", "-i"])
        self.assertTrue(quiet)
        self.assertEqual(actions,
                         [("__legacy__", ["-q", "counter.bin", "-i"])])

    def test_legacy_run_first_verb(self):
        _, actions = cli._split_chain(["run", "counter.bin", "-i"])
        self.assertEqual(actions,
                         [("__legacy__", ["counter.bin", "-i"])])

    def test_bare_run_is_new_verb(self):
        _, actions = cli._split_chain(["reset", "run"])
        self.assertEqual(actions, [("reset", []), ("run", [])])
        _, actions = cli._split_chain(["run"])
        self.assertEqual(actions, [("run", [])])

    def test_run_with_args_mid_chain_rejected(self):
        # Must not silently drop 'halt' and take a different code path.
        import contextlib
        import io
        stderr = io.StringIO()
        with contextlib.redirect_stderr(stderr), \
                self.assertRaises(SystemExit) as cm:
            cli._split_chain(["halt", "run", "x.bin"])
        self.assertEqual(cm.exception.code, 1)
        self.assertIn("must be alone", stderr.getvalue())
        # ...but a leading run-with-args is the legacy path.
        _, actions = cli._split_chain(["run", "x.bin", "reset"])
        self.assertEqual(actions[0][0], "__legacy__")

    def test_empty_argv(self):
        quiet, actions = cli._split_chain([])
        self.assertEqual((quiet, actions), (False, []))

    def test_help_is_handled(self):
        import contextlib
        import io
        with contextlib.redirect_stdout(io.StringIO()), \
                self.assertRaises(SystemExit) as cm:
            cli._split_chain(["--help"])
        self.assertEqual(cm.exception.code, 0)

    def test_verb_without_args_then_bare_verbs(self):
        _, actions = cli._split_chain(
            ["flush", "reset", "run", "term", "--no-flush"])
        self.assertEqual(actions, [
            ("flush", []), ("reset", []), ("run", []),
            ("term", ["--no-flush"]),
        ])

    def test_load_hex_target_as_first_token(self):
        # "rom"/"ram" are not verbs, so they stay with their load/dump verb.
        _, actions = cli._split_chain(["load", "ram", "prog.hex"])
        self.assertEqual(actions, [("load", ["ram", "prog.hex"])])


if __name__ == "__main__":
    unittest.main()
