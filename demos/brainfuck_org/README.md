# Brainfuck on EBAZ4205 (bf1 CPU)

Run plain Brainfuck programs on the FPGA `bf1` CPU.

## On the board (logged in via SSH) — recommended day-to-day

Install once from your PC:

```bash
./demos/brainfuck_org/install_to_board.sh          # default host: ebaz
# or:  EBAZ_HOST=root@192.168.x.x ./demos/brainfuck_org/install_to_board.sh
```

Then on the board:

```bash
ssh ebaz

# plain source — compiles, loads FPGA, prints UART output
bf1 hello.b
bf1 /root/bf1-examples/sierpinski.b -n 1552

# interactive program (live keyboard ↔ UART)
bf1 /root/bf1-examples/ghost.b -i
# from your PC in one shot:
ssh -t ebaz bf1 /root/bf1-examples/ghost.b -i

bf1 --help
```

Installed files:

| Path | Role |
|------|------|
| `/root/bf1` | compile + run tool |
| `/root/comp_bf.py` | BF → bf1 bytecode compiler (imported by `bf1`) |
| `/root/bf1-examples/` | sample `.b` programs |

**Capture bounds:** Brainfuck does not halt the CPU. In batch mode use `-n` /
`--max-bytes`, or rely on `--max-time` (default 30s) / UART idle detection.
Interactive mode (`-i`) runs until you quit with **Ctrl-C** or **Ctrl-]**.

---

## From your PC (no login shell on the board)

```bash
# One command: compile → deploy → run → print UART output
python3 demos/brainfuck_org/bf1.py demos/brainfuck_org/src/sierpinski.b

# Interactive (allocates a TTY via ssh -t)
python3 demos/brainfuck_org/bf1.py demos/brainfuck_org/src/ghost.b -i
```

Environment overrides:

| Variable       | Default   | Meaning                          |
|----------------|-----------|----------------------------------|
| `EBAZ_HOST`    | `ebaz`    | SSH target                       |
| `EBAZ_BF1_DIR` | `/tmp/bf1`| Remote working directory         |

## Manual steps (if you prefer)

### 1. Compile

```bash
python3 demos/brainfuck_org/comp_bf.py hello.b -o hello.bin
```

- Input: ASCII Brainfuck (non-command chars are comments).
- Output: bf1 run-length bytecode (must fit in **8 KiB** code RAM).
- Bracket comments like `[this is a comment with dots.com]` are normal BF
  dead-loops — they compile fine and are skipped when the current cell is 0
  (true after data-RAM clear). Unmatched `[` / `]` still fail.

### 2. Optional: simulate on the host

```bash
python3 demos/brainfuck_org/bf_interpret.py hello.b
python3 demos/brainfuck_org/bf_interpret.py prog.b -i $'input\n'
```

Semantics match the hardware: 8-bit cells, 32 KiB tape, EOF → 0.

### 3. Run on the board

```bash
python3 demos/brainfuck_org/bf1.py run hello.b -n 256 -o hello.out
python3 demos/brainfuck_org/bf1.py run ghost.b -i
```

## CLI reference

### `bf1` (on board) / `run_bf1_program.py`

```
bf1 <program.b|program.bin> [options]
```

| Flag | Meaning |
|------|---------|
| `-i` / `--interactive` | Live console: keyboard → UART, UART → screen |
| `-n` / `--max-bytes N` | Stop batch capture after N output bytes |
| `--max-time SEC` | Wall-clock limit (default 30s batch / none interactive) |
| `--input DATA` | Batch: feed DATA to `,` (or `@file`) |
| `-o` / `--output FILE` | Also save captured bytes to FILE |
| `--no-clear-data-ram` | Skip ~1–2s full 32K data RAM clear |
| `-q` / `--quiet` | Less status noise |

Fast BF-based clear (nested riding counters, ~101B): see [`CLEAR_DATA_RAM.md`](CLEAR_DATA_RAM.md).

### `bf1.py` (host)

```
bf1.py <program.b|program.bin> [options]   # shorthand for run
bf1.py run <program> [options]
bf1.py compile <source.b> [-o out.bin]
bf1.py sim <source.b> [-o out.txt] [-i input]
```

Same run flags as above, plus `--host` / `--remote-dir`.

## Included demos

| Program | Source | Notes |
|---------|--------|-------|
| Hello | `src/hello.b` | Classic "Hello World!" |
| Sierpinski | `src/sierpinski.b` | Finite ASCII art (1552 B) |
| Squares | `src/squares.b` | Finite number list |
| Ghost | `src/ghost.b` | **Interactive** game — `bf1 ghost.b -i` (wasd + Enter) |
| Xmas tree | `src/xmastree.b` | Needs `,` input |

## Layout

```
demos/brainfuck_org/
  bf1.py                 # host entry point (compile / sim / run)
  comp_bf.py             # BF → bf1 bytecode compiler
  bf_interpret.py        # host reference interpreter
  run_bf1_program.py     # on-board loader + UART I/O (installed as /root/bf1)
  install_to_board.sh    # deploy tools + examples to the board
  src/                   # Brainfuck sources
  bin/                   # precompiled bytecode (optional)
  expected/              # golden outputs from earlier bring-up
```

## Tips & pitfalls

1. **Finite output first.** Infinite printers need `--max-bytes` or `--max-time`.
2. **No self-halt.** Batch capture is always bounded (idle / max-time / max-bytes).
3. **Interactive programs.** Use `-i`. From a PC: `ssh -t ebaz bf1 ghost.b -i`
   (the `-t` is required so the board sees a real keyboard). Quit with Ctrl-C
   or Ctrl-]. Enter is sent as LF (what most BF programs expect).
4. **Comments.** Non-ops are ignored. Cristofani-style `[comment with . and ,]`
   blocks are fine (dead loops at cell 0). Unmatched brackets fail compile.
5. **Batch input.** `bf1 prog.b --input $'w\n' -n 200` feeds `,` without `-i`.
6. **Stale board process.** If a previous run left `/dev/mem` busy:
   ```bash
   ssh ebaz 'pkill -f "/root/bf1|run_bf1_program"; python3 - <<"PY"
   import mmap,struct,os
   fd=os.open("/dev/mem",os.O_RDWR|os.O_SYNC)
   m=mmap.mmap(fd,0x1000,offset=0x7C440000)
   for off in (0x404,0x444,0x484):
       m[off:off+4]=struct.pack("<I",0)
   m.close(); os.close(fd)
   PY'
   ```
7. **Data RAM** is uncleared BRAM after boot — default is to zero all 32K for
   deterministic results (~1–2 s). Needed so leading `[comment]` loops stay dead.
