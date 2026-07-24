#!/usr/bin/env python3
"""Binary-counter clear of data RAM vs nested riding counters.

Idea
-----------------------------------------
Layout within each block of K cells (K · 2^N = TAPE, both powers of 2):

  rel 0      backstop (always 0) — wall for <[-<]
  rel 1      anchor / main-loop flag
  rel 2..N+1 counter bits 0..N-1  (0 or 1)
  rel N+2    overflow bit (2^N) — fires after 2^N increments
  rel N+3..  padding (kept 0)

  K is the smallest power of 2 that fits the counter (N+3 cells).

Each main-loop iteration (pointer starts on anchor):
  1. Clear the *next* block of K cells (dirty RAM safe).
  2. Move anchor+bits forward by K (old cells zeroed by the moves).
     Overflow is always 0 during the move — it is only set by the
     increment that ends the loop, after the move.
  3. Advance pointer by K onto the new anchor.
  4. Single binary increment: [>]+<[-<]>+
     (the infinite form +[[>]+<[-<]>+] is the free-running counter;
      one step drops the outer brackets.)
  5. If overflow bit set → clear anchor (exit) and overflow.

After 2^N iters the counter sits back at absolute cell 0/1 (wrap) and the
overflow-exit path has zeroed it. Set m[0]=0xFF done-flag.

K must be a power of 2 so that K·2^N = 2^15 lands on the same absolute
address after wrap. That forces K ∈ {16,32,...,2048} and N = 15 − log2(K).

Run:
  python3 try_binary_clear.py           # size table + full 32K sim compare
  python3 try_binary_clear.py --quick   # correctness on tape=256
  python3 try_binary_clear.py --size-only
"""
from __future__ import annotations

import argparse
import sys
import time
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent))
from comp_bf import compile_source  # noqa: E402

OPS = set("><+-.,[]")
TAPE_DEFAULT = 32768


# ---------------------------------------------------------------------------
# Sources
# ---------------------------------------------------------------------------
def nested_riding_source() -> str:
    """Current production clear (see CLEAR_DATA_RAM.md)."""

    def move(n: int) -> str:
        return "[-" + (">" * n) + "+" + ("<" * n) + "]"

    return (
        "[-]" + ("+" * 128)
        + "["
        + "-"
        + ">"
        + "[-]" + ("+" * 16)
        + "["
        + "-"
        + ">"
        + "[-]" + ("+" * 4)
        + "["
        + "-"
        + ">[-]>[-]>[-]>[-]"
        + "<<<<"
        + "[->>>>+<<<<]"
        + ">>>>"
        + "]"
        + "<"
        + ("<" * 16)
        + move(16)
        + (">" * 16)
        + "]"
        + "<"
        + ("<" * 256)
        + move(256)
        + (">" * 256)
        + "]"
        + "-"
        + "[]"
    )


def binary_clear_source(n_bits: int, tape: int = TAPE_DEFAULT) -> str:
    """Binary-counter clear. N bits → 2^N iters, block K = tape / 2^N."""
    if tape <= 0 or (tape & (tape - 1)) != 0:
        raise ValueError("tape must be a power of 2")
    if not (1 <= n_bits < 16):
        raise ValueError("n_bits out of range")
    k = tape // (1 << n_bits)
    if k * (1 << n_bits) != tape:
        raise ValueError("tape not divisible by 2^n_bits")
    need = n_bits + 3  # backstop + anchor + n_bits + overflow
    if need > k:
        raise ValueError(f"counter needs {need} cells, block is only {k}")
    # Riding-clear uses one 8-bit cell as the loop count, so K must fit.
    if k > 255:
        raise ValueError(f"block K={k} exceeds 8-bit cell (use N>=8 so K<=128)")

    def R(n: int) -> str:
        return ">" * n

    def L(n: int) -> str:
        return "<" * n

    def move(n: int) -> str:
        return "[-" + R(n) + "+" + L(n) + "]"

    def riding_clear(count: int) -> str:
        """Zero `count` cells starting at ptr; also zeros the cell at +count.
        Ends with ptr at start+count (value 0). ``count`` must be 1..255."""
        if not (1 <= count <= 255):
            raise ValueError(f"riding_clear count {count} out of 1..255")
        return (
            "[-]" + ("+" * count)
            + "[-"
            + ">[-]<"
            + move(1)
            + ">"
            + "]"
        )

    ov = n_bits + 1          # anchor → overflow
    n_move = n_bits + 1      # anchor + bits (overflow always 0 at move time)
    parts: list[str] = []

    # Phase 0: zero first block so the counter starts clean.
    parts.append(riding_clear(k))
    parts.append(L(k))  # back to 0

    # Phase 1: backstop=0, anchor=1
    parts.append(">+")

    # Phase 2: main loop (while anchor)
    parts.append("[")

    # 2a. Clear next block of K cells.
    parts.append(R(k - 1))             # → start of next block
    parts.append(riding_clear(k))
    parts.append(L(2 * k - 1))         # → anchor

    # 2b. Move anchor+bits forward by K, then advance to new anchor.
    #
    # Unrolled move(K)×n_move is ~6B each (since >K is 1 RLE byte for K≤31)
    # but ×12 ≈ 84B. Instead: drop a riding count just past the block and
    # walk right-to-left — one move(K) in the body, ~23B total.
    #
    #   cells [anchor .. anchor+n_move): payload to relocate
    #   cell  [anchor+n_move]          : temp count (overflow slot; always 0
    #                                    at move time, safe to reuse)
    parts.append(R(n_move))            # → temp just past bits
    parts.append("[-]" + ("+" * n_move))
    parts.append(
        "["
        "-"                            # dec count
        "<"                            # next payload cell (RTL)
        + move(k) +                    # relocate it +K
        ">[-<+>]"                      # slide count left onto cleared cell
        "<"                            # sit on count for loop test
        "]"
    )
    # Ends on anchor (count rides down to 0 there).
    parts.append(R(k))                 # new anchor

    # 2c. One binary-increment step (ends on anchor).
    parts.append("[>]+<[-<]>+")

    # 2d. Overflow check → clear anchor to exit.
    parts.append(R(ov))
    parts.append("[" + L(ov) + "-" + R(ov) + "-" + "]")
    parts.append(L(ov))

    parts.append("]")

    # Phase 3: at anchor (abs 1 after wrap), set m[0]=0xFF, spin.
    parts.append("<-[]")

    return "".join(parts)


# ---------------------------------------------------------------------------
# Source-level interpreter (wrapping tape, like bf1 data RAM)
# ---------------------------------------------------------------------------
def run_bf_source(
    src: str,
    tape_size: int,
    *,
    init_dirty: bool = True,
    seed: int = 1,
    max_steps: int = 2_000_000_000,
):
    code = [c for c in src if c in OPS]
    jumps: dict[int, int] = {}
    stack: list[int] = []
    for i, c in enumerate(code):
        if c == "[":
            stack.append(i)
        elif c == "]":
            if not stack:
                raise SyntaxError(f"unmatched ] at {i}")
            j = stack.pop()
            jumps[i] = j
            jumps[j] = i
    if stack:
        raise SyntaxError(f"unclosed [ at {stack[-1]}")

    if init_dirty:
        tape = bytearray((i * 17 + seed * 31) & 0xFF for i in range(tape_size))
    else:
        tape = bytearray(tape_size)

    # Terminal [] spin index (if present)
    spin_at = None
    for i in range(len(code) - 1):
        if code[i] == "[" and code[i + 1] == "]" and jumps.get(i) == i + 1:
            spin_at = i

    ptr = 0
    pc = 0
    steps = 0
    n = len(code)
    while pc < n:
        if steps >= max_steps:
            raise TimeoutError(f"step limit at pc={pc} ptr={ptr}")
        if pc == spin_at and tape[ptr] != 0:
            break
        c = code[pc]
        if c == ">":
            ptr = (ptr + 1) % tape_size
        elif c == "<":
            ptr = (ptr - 1) % tape_size
        elif c == "+":
            tape[ptr] = (tape[ptr] + 1) & 0xFF
        elif c == "-":
            tape[ptr] = (tape[ptr] - 1) & 0xFF
        elif c == "[":
            if tape[ptr] == 0:
                pc = jumps[pc]
        elif c == "]":
            if tape[ptr] != 0:
                pc = jumps[pc]
        pc += 1
        steps += 1
    return tape, ptr, steps


# ---------------------------------------------------------------------------
# bf1-accurate bytecode cycle counter (1 cycle per insn fetch)
# ---------------------------------------------------------------------------
def run_bf1_bytecode(
    code: bytes,
    tape_size: int = TAPE_DEFAULT,
    *,
    init_dirty: bool = True,
    seed: int = 1,
    max_cycles: int = 10**11,
):
    """Match hdl/library/bf1_soc/bf1.v semantics closely enough for cycle counts."""
    if init_dirty:
        tape = bytearray((i * 17 + seed * 31) & 0xFF for i in range(tape_size))
    else:
        tape = bytearray(tape_size)

    def s6(v: int) -> int:
        v &= 0x3F
        return v - 64 if v >= 32 else v

    pc = 0
    ptr = 0
    rsp: list[int] = []
    lj = False
    lj_offset = 0
    cycles = 0
    n = len(code)

    while 0 <= pc < n:
        cycles += 1
        if cycles > max_cycles:
            raise TimeoutError(f"cycle limit at pc={pc} ptr={ptr}")
        insn = code[pc]

        if lj:
            offset = ((lj_offset & 0x1F) << 8) | insn
            if tape[ptr] != 0:
                rsp.append(pc + 1)
                pc = pc + 1
            else:
                pc = pc + offset
            lj = False
            continue

        if (insn & 0b11000000) == 0b00000000:
            ptr = (ptr + s6(insn)) % tape_size
            pc += 1
        elif (insn & 0b11000000) == 0b01000000:
            tape[ptr] = (tape[ptr] + s6(insn)) & 0xFF
            pc += 1
        elif (insn & 0b11100000) == 0b10000000:
            if insn & 0b11111:  # short [
                offset = s6(insn)
                if tape[ptr] != 0:
                    rsp.append(pc + 1)
                    pc = pc + 1
                else:
                    pc = pc + offset
            else:  # ]
                if tape[ptr] != 0:
                    pc = rsp[-1]
                else:
                    rsp.pop()
                    pc = pc + 1
        elif (insn & 0b11100000) == 0b10100000:  # long-jump prefix
            lj = True
            lj_offset = insn & 0x1F
            pc += 1
        elif (insn & 0b11100000) == 0b11000000:  # ,
            tape[ptr] = 0
            pc += 1
        elif (insn & 0b11100000) == 0b11100000:  # .
            pc += 1
        else:
            raise RuntimeError(f"bad opcode 0x{insn:02X} at {pc}")

    return tape, ptr, cycles


# ---------------------------------------------------------------------------
# Reporting
# ---------------------------------------------------------------------------
def _ops(src: str) -> int:
    return sum(1 for c in src if c in OPS)


def analyze_size(name: str, src: str) -> dict:
    bc, st = compile_source(src if not src.endswith("[]") else src)
    return {
        "name": name,
        "ops": _ops(src),
        "bc": st["size"],
        "depth": st["max_depth"],
        "long_j": st["long_jumps"],
    }


def analyze_full(name: str, src: str, tape: int) -> dict:
    info = analyze_size(name, src)
    # Drop terminal [] so the bytecode runner finishes; keep '-' done flag.
    assert src.endswith("[]"), "source should end with done-flag + spin"
    bc, _ = compile_source(src[:-2])
    t0 = time.perf_counter()
    tape_out, ptr, cycles = run_bf1_bytecode(bc, tape_size=tape)
    dt = time.perf_counter() - t0
    nz = [i for i, v in enumerate(tape_out) if v]
    ok = nz == [0] and tape_out[0] == 0xFF
    info.update(
        cycles=cycles,
        sim_s=dt,
        ptr=ptr,
        m0=tape_out[0],
        ok=ok,
        nz=nz[:8],
        bc_nosspin=len(bc),
    )
    return info


def print_info(info: dict, *, cycles: bool = True) -> None:
    print(f"=== {info['name']} ===")
    print(
        f"  source ops: {_ops_from_info(info):6d}    "
        f"bytecode: {info['bc']:4d} B    "
        f"depth={info['depth']}  long_jumps={info['long_j']}"
    )
    if cycles and "cycles" in info:
        print(f"  bf1 cycles: {info['cycles']:>10,}    sim {info['sim_s']:.2f}s")
        print(f"  @100 MHz / 1 cyc: {info['cycles']/100e6*1e3:6.2f} ms")
        status = "OK" if info["ok"] else f"FAIL nz={info['nz']}"
        print(f"  m[0]=0x{info['m0']:02X}  ptr={info['ptr']}  {status}")
    print()


def _ops_from_info(info: dict) -> int:
    return info.get("ops", 0)


def main() -> None:
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    ap.add_argument("-t", "--tape", type=int, default=TAPE_DEFAULT)
    ap.add_argument("--size-only", action="store_true")
    ap.add_argument("--quick", action="store_true",
                    help="correctness only on tape=256 (source-level)")
    args = ap.parse_args()

    if args.quick:
        tape = 256
        print(f"QUICK correctness (source-level) tape={tape}\n")
        for n in range(1, 8):
            k = tape // (1 << n)
            if k == 0 or (n + 3) > k:
                continue
            src = binary_clear_source(n, tape)
            bc, st = compile_source(src)
            t0 = time.perf_counter()
            out, ptr, steps = run_bf_source(src, tape)
            dt = time.perf_counter() - t0
            nz = [i for i, v in enumerate(out) if v]
            ok = nz == [0] and out[0] == 0xFF
            print(
                f"  N={n} K={k:3d}  bc={st['size']:3d}B  steps={steps:7d}  "
                f"{dt:.3f}s  {'OK' if ok else 'FAIL '+str(nz[:6])}"
            )
        return

    tape = args.tape
    print(f"tape = {tape}\n")

    print(f"--- bytecode size by N (K = {tape}/2^N) ---")
    print(f"  {'N':>4}  {'K':>5}  {'ops':>7}  {'bc':>5}  notes")
    rows = []
    for n in range(4, 15):
        k = tape // (1 << n) if (1 << n) <= tape else 0
        if k == 0 or k * (1 << n) != tape:
            continue
        need = n + 3
        if need > k:
            print(f"  {n:4d}  {k:5d}  {'':>7}  {'':>5}  SKIP (need {need} > K)")
            continue
        if k > 255:
            print(f"  {n:4d}  {k:5d}  {'':>7}  {'':>5}  SKIP (K>255, 8-bit cell)")
            continue
        src = binary_clear_source(n, tape)
        info = analyze_size(f"N={n}", src)
        rows.append((n, k, info))
        print(
            f"  {n:4d}  {k:5d}  {info['ops']:7d}  {info['bc']:4d}B"
        )
    if rows:
        best_n, best_k, best = min(rows, key=lambda r: r[2]["bc"])
        print(f"\n  best binary: N={best_n} K={best_k} at {best['bc']} B")
    nested_src = nested_riding_source() if tape == TAPE_DEFAULT else None
    if nested_src:
        ni = analyze_size("nested riding", nested_src)
        print(f"  nested riding:          {ni['bc']} B  (production)\n")
    else:
        print()

    if args.size_only:
        return

    print("--- full simulation (bf1 bytecode cycles, dirty tape) ---\n")
    results = []
    if nested_src:
        info = analyze_full("nested riding (production)", nested_src, tape)
        print_info(info)
        results.append(info)

    # Simulate the more interesting binary widths (K must be <= 255).
    for n in (11, 10, 8):
        k = tape // (1 << n)
        if k == 0 or n + 3 > k or k > 255:
            continue
        info = analyze_full(
            f"binary counter N={n} K={k}", binary_clear_source(n, tape), tape
        )
        print_info(info)
        results.append(info)

    if len(results) >= 2 and results[0]["name"].startswith("nested"):
        base = results[0]["cycles"]
        print("--- summary vs nested riding ---")
        print(f"  {'version':<32} {'bc':>5}  {'cycles':>12}  {'rel':>6}  ok")
        for r in results:
            rel = r["cycles"] / base
            print(
                f"  {r['name']:<32} {r['bc']:4d}B  {r['cycles']:12,}  "
                f"{rel:5.2f}x  {'Y' if r['ok'] else 'N'}"
            )
        print()
        print(
            "Conclusion: with a looped multi-cell move, binary N=11 K=16 is "
            "SMALLER than nested riding (93 B vs 103 B) but ~1.23x more "
            "bf1 cycles. RLE already makes >K a single byte for K<=31; the "
            "win came from not unrolling move(K) across all bit cells."
        )


if __name__ == "__main__":
    main()
