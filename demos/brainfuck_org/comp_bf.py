#!/usr/bin/env python3
"""Compile ASCII Brainfuck source to bf1 run-length bytecode.

Vendored/adapted from brainfuck_machine compile_simulate/comp-bf.py.
Ignores non-command characters (comments/whitespace).

Usage:
  python3 comp_bf.py program.b [-o program.bin] [-q]
  python3 -c 'from comp_bf import compile_file; compile_file("x.b","x.bin")'
"""

from __future__ import annotations

import argparse
import pprint
import sys
from pathlib import Path

NEUTRAL = ["[", "]", ",", "."]
NEGATIVE = ["<", "-"]
POSITIVE = [">", "+"]
OPS = set(NEUTRAL + NEGATIVE + POSITIVE)

CODE_RAM_SIZE = 8192


class Node:
    def __init__(self, l: int, r: int):
        assert l < r
        self.l = l
        self.r = r
        self.child: list[Node] = []

    def insert(self, n: "Node") -> None:
        assert n.l > self.l and n.r < self.r
        if not self.child:
            self.child.append(n)
            return

        li = None
        x = n.l
        if x < self.child[0].l:
            li = -1
        else:
            li = len(self.child) - 1
            for j, e in reversed(list(enumerate(self.child))):
                if x > e.l:
                    li = j
                    break

        ri = None
        x = n.r
        if x > self.child[-1].r:
            ri = len(self.child)
        else:
            ri = 0
            for j, e in enumerate(self.child):
                if x < e.r:
                    ri = j
                    break

        if li == ri:
            self.child[li].insert(n)
        elif ri == li + 1:
            self.child.insert(ri, n)
        else:
            n.child = self.child[li + 1 : ri]
            del self.child[li + 1 : ri]
            self.child.insert(li + 1, n)

    def repr(self):
        return (self.l, [c.repr() for c in self.child], self.r)


def encode(count: int, op: str, out_bin: bytearray) -> int:
    if op in [">", "<"]:
        opc = 0b00000000
    elif op in ["+", "-"]:
        opc = 0b01000000
    elif op == "]":
        opc = 0b10000000
    elif op == "[":
        opc = 0b10111111
    elif op == ",":
        opc = 0b11000000
    elif op == ".":
        opc = 0b11100000
    else:
        raise SyntaxError(f"Unknown opcode {op!r}")

    if op in NEUTRAL:
        out_bin.append(opc)
        return count - 1

    if op in POSITIVE:
        cnt = min(31, count)
        rest = count - cnt
    else:
        cnt = max(-32, -count)
        rest = count + cnt
    opc |= cnt & 0b00111111
    out_bin.append(opc)
    return rest


def emit_run(count: int, op: str, out_bin: bytearray) -> None:
    while count > 0:
        count = encode(count, op, out_bin)


def calc_jmp(
    out_bin: bytearray, tree: Node, offset: int, depth: int, stats: dict
) -> int:
    if depth > stats["max_depth"]:
        stats["max_depth"] = depth
    inserted = 0
    for t in tree.child:
        inserted += calc_jmp(out_bin, t, offset + inserted, depth + 1, stats)
    if tree.l >= 0:
        length = tree.r - tree.l + inserted + 1
        if length >= 0b00100000:
            high = length >> 8
            low = length & 0xFF
            if high > 0b00111111:
                raise SyntaxError(
                    f"jump too long at {tree.l} (length = {length})"
                )
            out_bin[tree.l + offset] = 0b10100000 | high
            out_bin.insert(tree.l + offset + 1, low)
            inserted += 1
            stats["long_jumps"] += 1
        else:
            out_bin[tree.l + offset] = 0b10000000 | length
    return inserted


def calculate_jumps(out_bin: bytearray) -> dict:
    loop_stack: list[int] = []
    tree = Node(-1, len(out_bin))
    for ptr, opc in enumerate(out_bin):
        if opc == 0b10111111:
            loop_stack.append(ptr)
        if opc == 0b10000000:
            if not loop_stack:
                raise SyntaxError(f"unmatched close loop at {ptr}")
            sptr = loop_stack.pop()
            tree.insert(Node(sptr, ptr))
    if loop_stack:
        raise SyntaxError(f"unclosed loops at {loop_stack}")

    stats = {"max_depth": 0, "long_jumps": 0, "tree": tree.repr()}
    calc_jmp(out_bin, tree, 0, 1, stats)
    return stats


def compile_source(source: str) -> tuple[bytes, dict]:
    """Compile Brainfuck source text to bf1 bytecode.

    Returns (bytecode, stats) where stats includes size, long_jumps, max_depth.
    """
    out_bin = bytearray()
    last_opcode = None
    count = 0
    for ch in source:
        if ch not in OPS:
            continue
        if ch == last_opcode:
            count += 1
        else:
            if last_opcode is not None:
                emit_run(count, last_opcode, out_bin)
            count = 1
            last_opcode = ch
    if count > 0 and last_opcode is not None:
        emit_run(count, last_opcode, out_bin)

    if not out_bin:
        raise SyntaxError("no Brainfuck commands found in source")

    stats = calculate_jumps(out_bin)
    stats["size"] = len(out_bin)
    return bytes(out_bin), stats


def compile_file(
    src_path: str | Path,
    out_path: str | Path | None = None,
    *,
    quiet: bool = False,
    check_size: bool = True,
) -> tuple[Path, bytes, dict]:
    """Compile a .b file to bytecode. Writes out_path if given (or src stem .bin)."""
    src_path = Path(src_path)
    source = src_path.read_text(encoding="utf-8", errors="replace")
    code, stats = compile_source(source)

    if check_size and len(code) > CODE_RAM_SIZE:
        raise ValueError(
            f"bytecode is {len(code)}B but code RAM is only {CODE_RAM_SIZE}B"
        )

    if out_path is None:
        out_path = src_path.with_suffix(".bin")
    else:
        out_path = Path(out_path)
    out_path.parent.mkdir(parents=True, exist_ok=True)
    out_path.write_bytes(code)

    if not quiet:
        print(f"compiled {src_path} -> {out_path} ({len(code)}B)", file=sys.stderr)
        print(
            f"  long_jumps={stats['long_jumps']}  max_depth={stats['max_depth']}",
            file=sys.stderr,
        )
    return out_path, code, stats


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Compile Brainfuck source to bf1 bytecode"
    )
    p.add_argument("source", help="Brainfuck source file (.b)")
    p.add_argument(
        "-o",
        "--output",
        help="Output bytecode path (default: <source>.bin)",
    )
    p.add_argument(
        "-q",
        "--quiet",
        action="store_true",
        help="Suppress stats on stderr",
    )
    p.add_argument(
        "--dump-tree",
        action="store_true",
        help="Pretty-print loop interval tree",
    )
    args = p.parse_args(argv)

    try:
        out_path, code, stats = compile_file(
            args.source, args.output, quiet=args.quiet
        )
    except (OSError, SyntaxError, ValueError) as e:
        print(f"ERROR: {e}", file=sys.stderr)
        return 1

    if args.dump_tree:
        pprint.PrettyPrinter(indent=2).pprint(stats["tree"])

    if not args.quiet:
        print(out_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
