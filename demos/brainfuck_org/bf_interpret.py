#!/usr/bin/env python3
"""Small source-level Brainfuck interpreter used as an independent reference.

Semantics match the bf1 hardware configuration:
- 8-bit wrapping cells
- 32 KiB data tape by default
- EOF on input reads as 0
- comments/non-command bytes are ignored
"""
import argparse
import sys

OPS = b"><+-.,[]"


def parse_args():
    p = argparse.ArgumentParser()
    p.add_argument("source", help="Brainfuck source file")
    p.add_argument("-t", "--tape-size", type=int, default=32768)
    p.add_argument("-m", "--max-steps", type=int, default=200_000_000)
    p.add_argument("-i", "--input", default="", help="input bytes for ',' (text; use @file to read a file)")
    return p.parse_args()


def main():
    args = parse_args()
    src = open(args.source, "rb").read()
    code = bytes(c for c in src if c in OPS)

    jumps = {}
    stack = []
    for i, c in enumerate(code):
        if c == ord('['):
            stack.append(i)
        elif c == ord(']'):
            if not stack:
                raise SyntaxError(f"unmatched ] at {i}")
            j = stack.pop()
            jumps[i] = j
            jumps[j] = i
    if stack:
        raise SyntaxError(f"unclosed [ at {stack}")

    if args.input.startswith('@'):
        inp = open(args.input[1:], 'rb').read()
    else:
        inp = args.input.encode()

    tape = bytearray(args.tape_size)
    ptr = 0
    pc = 0
    ipos = 0
    out = bytearray()
    steps = 0

    while pc < len(code):
        if steps >= args.max_steps:
            raise TimeoutError(f"step limit {args.max_steps} exceeded at pc={pc}")
        c = code[pc]
        if c == ord('>'):
            ptr += 1
            if ptr >= len(tape):
                raise IndexError(f"tape right overrun at pc={pc}")
        elif c == ord('<'):
            ptr -= 1
            if ptr < 0:
                raise IndexError(f"tape left overrun at pc={pc}")
        elif c == ord('+'):
            tape[ptr] = (tape[ptr] + 1) & 0xFF
        elif c == ord('-'):
            tape[ptr] = (tape[ptr] - 1) & 0xFF
        elif c == ord('.'):
            out.append(tape[ptr])
        elif c == ord(','):
            tape[ptr] = inp[ipos] if ipos < len(inp) else 0
            ipos += 1
        elif c == ord('['):
            if tape[ptr] == 0:
                pc = jumps[pc]
        elif c == ord(']'):
            if tape[ptr] != 0:
                pc = jumps[pc]
        pc += 1
        steps += 1

    sys.stdout.buffer.write(out)
    print(f"\n[bf_interpret: {steps} steps, {len(out)} output bytes]", file=sys.stderr)


if __name__ == "__main__":
    main()
