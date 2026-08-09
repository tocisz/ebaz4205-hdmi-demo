# Z80 on EBAZ4205 (z80_soc)

Run Z80 assembly programs on the FPGA `z80_soc` (tv80 core via Wishbone bridge).

## Quick start

### On the board (logged in via SSH)

Install once from your PC:

```bash
./demos/z80_asm/install_to_board.sh          # default host: ebaz
# or:  EBAZ_HOST=root@192.168.x.x ./demos/z80_asm/install_to_board.sh
```

Then on the board:

```bash
ssh ebaz

# Counter — outputs incrementing bytes
z80 /root/z80-examples/counter.bin -n 64

# Interactive echo — type characters, see them echoed back
z80 /root/z80-examples/echo.bin -i

# Memory walker — writes pattern to RAM, outputs via I/O
z80 /root/z80-examples/walk.bin -n 256

z80 --help
```

Installed files:

| Path | Role |
|------|------|
| `/root/z80` | load + run tool |
| `/root/z80-examples/` | sample `.bin` programs |

### From your PC

```bash
# One command: assemble → deploy → run → print output
python3 demos/z80_asm/z80.py demos/z80_asm/src/counter.s -n 64

# Interactive (allocates a TTY via ssh -t)
python3 demos/z80_asm/z80.py demos/z80_asm/src/echo.s -i
```

Environment overrides:

| Variable | Default | Meaning |
|---|---|---|
| `EBAZ_HOST` | `ebaz` | SSH target |
| `EBAZ_Z80_DIR` | `/tmp/z80` | Remote working directory |

## Manual steps

### 1. Assemble

```bash
python3 demos/z80_asm/assemble_z80.py src/counter.s -o /tmp/counter.bin
python3 demos/z80_asm/assemble_z80.py src/counter.s               # auto: counter.bin
```

Requires `binutils-z80` (`apt install binutils-z80`).

### 2. Run on the board

```bash
python3 demos/z80_asm/z80.py run src/counter.bin -n 64
python3 demos/z80_asm/z80.py run src/echo.bin -i
```

### 3. Simulate (view binary contents)

```bash
python3 demos/z80_asm/z80.py sim src/counter.s
python3 demos/z80_asm/z80.py sim bin/counter.bin
```

## Programs

| Program | Source | Expected output | Notes |
|---|---|---|---|
| Counter | `src/counter.s` | `00 01 02 03 … FF 00 01 …` | Infinite loop |
| Echo | `src/echo.s` | Echoes back any byte sent | Interactive |
| Walk | `src/walk.s` | Pattern 00–FF written to RAM, then output | Infinite loop |
| Boot | `src/boot.s` | `jp 0x2000` (3 bytes) | ROM bootstrap |

## Architecture

The FPGA design replaces the old `bf2_soc` (Brainfuck CPU) with a Z80-compatible
SoC. The PS (ARM A9) communicates with the Z80 through:

- **`ctrl_gp0`** (axi_gpreg @0x7C440000): CPU control — halt, run, step, reset
- **`ctrl_gp1`** (axi_gpreg @0x7C440044): RAM access (8 KB, Z80 address 0x2000+)
- **`ctrl_gp2`** (axi_gpreg @0x7C440084): ROM access (8 KB, Z80 address 0x0000-0x1FFF)
- **`/dev/axis_fifo_0x7c450000`**: byte stream I/O (Z80 `IN`/`OUT` port 0)

### Register protocol

Same as bf2_soc — see `doc/Z80_SOC_PLAN.md` or `doc/AXIS_FIFO_BRIDGE.md`.

## Tips & pitfalls

1. **Finite capture**: Infinite-loop programs need `-n N` or `--max-time SEC`.
2. **Boot ROM**: Written once per FPGA boot. The runner checks and skips if present.
3. **Stale FIFO bytes**: The v1 axis_byte_bridge has no reset and can hold a byte
   from a previous run. The runner flushes before starting.
4. **Interactive**: `ssh -t ebaz z80 echo.bin -i` (`-t` is required for TTY).
   Quit with Ctrl-C or Ctrl-].
5. **Out of RAM**: 8 KB limit. Assemble first to check binary size.
6. **Stale board process**: Reset with `pkill -f "/root/z80|run_z80"; echo "done"`.
7. **Memory map**: Code loaded to RAM offset 0 → Z80 sees it at 0x2000.
   The boot ROM (0x0000) must contain `jp 0x2000`.

## Layout

```
demos/z80_asm/
  z80.py                 # host entry point (assemble / sim / run)
  assemble_z80.py        # .s → .bin wrapper (uses binutils-z80)
  run_z80_program.py     # on-board loader + I/O (installed as /root/z80)
  install_to_board.sh    # deploy tools + examples to the board
  src/                   # Z80 assembly sources
  bin/                   # pre-assembled binaries
  expected/              # golden output references
```
