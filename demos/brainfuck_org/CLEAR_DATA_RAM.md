# Fast data-RAM clear (BF program)

Zeroing 32 KiB data RAM via PS register pokes (`data_write` × 32768) takes
**>1 s**. Running a small Brainfuck clear on the bf1 CPU does it in tens of ms.

## Constraints

| Resource   | Size        | Notes                          |
|------------|-------------|--------------------------------|
| Data RAM   | 32 KiB      | 8-bit cells, ptr wraps at 32K  |
| Code RAM   | 8 KiB       | unrolled `[-]>` × 32K ≈ 128 KB — will not fit |
| Cell width | 8-bit       | loop counters ≤ 255            |

## Algorithm — nested riding counters

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

Compiled with `comp_bf.py`: **~103 bytes** bytecode (depth 5).

## Host workflow (intended)

1. Load clear bytecode → `reset` → `run`
2. Poll `data_read(0) == 0xFF` (or halt-on-`0x00` once wired in HW)
3. `halt` → load user program → `reset` → `run`

## The code

```python
def _clear_data_ram_source() -> str:
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
