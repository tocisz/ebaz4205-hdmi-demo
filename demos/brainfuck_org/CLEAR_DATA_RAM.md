# Fast data-RAM clear (BF program)

Zeroing 32 KiB data RAM via PS register pokes (`data_write` × 32768) takes
**>1 s**. Running a small Brainfuck clear on the bf1 CPU does it in tens of ms.

| Variant | Bytecode | Cycles (rel) |
|---------|----------|-------------|
| Nested riding | 103 B | 1.00× |
| Binary counter | **93 B** | 1.23× |

## Constraints

| Resource   | Size        | Notes                          |
|------------|-------------|--------------------------------|
| Data RAM   | 32 KiB      | 8-bit cells, ptr wraps at 32K  |
| Code RAM   | 8 KiB       | unrolled `[-]>` × 32K ≈ 128 KB — will not fit |
| Cell width | 8-bit       | loop counters ≤ 255            |

## Algorithm 1 — nested riding counters (103 B)

```
32768 = 128 × 256
  256 =  16 ×  16
   16 =   4 ×   4
```

Same skeleton at every level (counter sits at block base **B**):

```text
set counter = N
while counter ≠ 0:
    counter--
    clear the next STRIDE cells     ← child level, or leaf >[-]×4
    walk back to counter
    move counter forward by STRIDE  ← [->×S+<×S]
    advance to new base B+STRIDE
```

- **Leaf:** `>[-]>[-]>[-]>[-]` clears 4 cells.
- Pointer wraps at 32K, so the last block finishes at cell 0.
- After the outer loop: `-` sets **m[0] = 0xFF** as a done flag for the host.
- Trailing `[]` spins forever so the CPU never falls into residual code-RAM
  bytes left past the clear program (`load_program` only clears the prefix
  it overwrites).

Compiled with `comp_bf.py`: **103 bytes** bytecode (depth 5).

## Algorithm 2 — binary riding counter (93 B)

Compact binary counter (N=11 bits, K=16 block, 2^N = 2048 iterations). Each
iteration clears the next 16 tape cells (2048 × 16 = 32768 total) and
relocates the 12 counter cells via a looped right-to-left copy (not unrolled).

```text
Loop 2048 times:
    clear next 16 cells
    relocate 12 counter cells +16 (looped RTL)
    binary increment once: [>]+<[-<]>+
    if overflow → exit
m[0] = 0xFF; []
```

Hardware runtime ~1.23× nested-riding cycles. Smaller bytecode, slightly
slower — a size–speed trade-off.

See `try_binary_clear.py` for the source generator and full comparison.

## Host workflow (intended)

1. Load clear bytecode → `reset` → `run`
2. Poll `data_read(0) == 0xFF` (or halt-on-`0x00` once wired in HW)
3. `halt` → load user program → `reset` → `run`

## The code

### Algorithm 1

```python
def clear_data_ram_source() -> str:
    """Brainfuck source that zeros all data cells and sets m[0] = 0xFF."""

    def move(n: int) -> str:
        return "[-" + (">" * n) + "+" + ("<" * n) + "]"

    return (
        "[-]" + ("+" * 128)  # m[0] = 128
        + "["  # ===== outer: 128 blocks of 256 =====
        + "-"
        + ">"
        + "[-]" + ("+" * 16)  # mid = 16
        + "["  # ----- mid: 16 groups of 16 -----
        + "-"
        + ">"
        + "[-]" + ("+" * 4)  # inner = 4
        + "["  # ..... inner: 4 groups of 4 .....
        + "-"
        + ">[-]>[-]>[-]>[-]"  # clear next 4 cells
        + "<<<<"
        + "[->>>>+<<<<]"  # move inner counter +4
        + ">>>>"
        + "]"
        + "<"  # adjust after inner
        + ("<" * 16)  # back to mid counter
        + move(16)  # move mid counter +16
        + (">" * 16)  # advance +16
        + "]"
        + "<"  # adjust after mid
        + ("<" * 256)  # back to outer counter
        + move(256)  # move outer counter +256
        + (">" * 256)  # advance +256
        + "]"
        + "-"  # m[0] = 0xFF (done)
        # Spin forever so we never fall into residual code-RAM bytes left
        # past this program (load_program only clears the prefix it overwrites).
        + "[]"
    )
```

### Algorithm 2

```python
TAPE_DEFAULT = 32768
BITS_DEFAULT = 11

def binary_clear_source(n_bits: int = BITS_DEFAULT, tape: int = TAPE_DEFAULT) -> str:
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
```
