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

# Control verbs — the CPU stays alive between invocations
z80 status                       # halted / running
z80 halt                         # pause the CPU
z80 load ram counter.bin         # write image into RAM @ 0x2000 (Intel HEX or .bin)
z80 dump ram 0x2000 64           # read RAM back as a hexdump
z80 flush reset run              # discard FIFO, PC=0, start

# Terminal
z80 term                         # attach stdin/stdout to the Z80 I/O stream
z80 term --flush                 # discard buffered output before attaching
                                  # (Ctrl-] detaches; CPU keeps running)

# One-liners — commands run left to right, 'term' must be last
z80 halt load ram counter.bin flush reset run

# Legacy one-shot (halts the CPU on exit; kept for scripts)
z80 /root/z80-examples/counter.bin -n 64
z80 /root/z80-examples/echo.bin -i

z80 --help
```

Installed files:

| Path | Role |
|------|------|
| `/root/z80` | board tool entry point |
| `/root/z80_board/` | implementation package (`hw.py`, `images.py`, `cli.py`) |
| `/root/z80-examples/` | sample `.bin` programs |

### From your PC

```bash
# One command: assemble → deploy → run → print output (legacy one-shot)
python3 demos/z80_asm/z80.py demos/z80_asm/src/counter.s -n 64

# Interactive (allocates a TTY via ssh -t)
python3 demos/z80_asm/z80.py demos/z80_asm/src/echo.s -i

# Load separate ROM and RAM images.  A nonzero --rom-org generates
# JP --rom-org at ROM address 0; --ram-org is a Z80 RAM address.
python3 demos/z80_asm/z80.py run \
    --rom demos/z80_asm/src/boot.s \
    --ram demos/z80_asm/src/counter.s --ram-org 0x2000 \
    -n 3

# New-style: pass individual commands through to the board tool.
# Local files are uploaded automatically; --host selects the board.
python3 demos/z80_asm/z80.py status
python3 demos/z80_asm/z80.py load ram demos/z80_asm/bin/counter.bin
python3 demos/z80_asm/z80.py dump ram 0x2000 16
python3 demos/z80_asm/z80.py board halt load ram app.hex flush reset run
python3 demos/z80_asm/z80.py term --flush          # ssh -t is allocated
python3 demos/z80_asm/z80.py term --no-flush       # explicit keep-buffer
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
python3 demos/z80_asm/z80.py run src/counter.bin -n 64   # legacy one-shot
python3 demos/z80_asm/z80.py run src/echo.bin -i

# New-style session (CPU stays alive between commands)
python3 demos/z80_asm/z80.py board halt load ram src/counter.bin flush reset run
python3 demos/z80_asm/z80.py term
```

### Intel HEX loading

`load` accepts both raw binaries and Intel HEX (as produced by
`z80-unknown-coff-objcopy -O ihex`).  HEX records carry their own addresses,
so a NASCOM image can be loaded straight into RAM (e.g. `.ram` at `0x8400`
on the EBAZ memory map):

```bash
ssh ebaz
z80 halt
z80 load rom /tmp/rom_ebaz.bin      # binary, addresses from the linker script
z80 load ram /tmp/hello.hex         # hex, addresses from the records
z80 flush reset run term --no-flush
```

### 3. Simulate (view binary contents)

```bash
python3 demos/z80_asm/z80.py sim src/counter.s
python3 demos/z80_asm/z80.py sim bin/counter.bin
```

## Programs

| Program | Source | Expected output | Notes |
|---|---|---|---|
| Counter | `src/counter.s` | `00 01 02 03 … FF 00 01 …` | Uses raw I/O bridge (any port) |
| Echo | `src/echo.s` | Echoes back any byte sent | Interactive, raw I/O |
| Walk | `src/walk.s` | Pattern 00–FF written to RAM, then output | Raw I/O |
| Memtest | `src/memtest.s` | Full RAM test; outputs error count and pass/fail | Run from ROM |
| Boot | `src/boot.s` | `jp 0x2000` (3 bytes) | ROM bootstrap |
| ACIA Echo | `src/acia_echo.s` | Echoes back any byte sent via ACIA | Interactive, polled ACIA protocol |
| ACIA Counter | `src/acia_counter.s` | `00 01 02 03 … FF 00 01 …` via ACIA | ACIA TDRE-gated output |
| ACIA IRQ Test | `src/acia_irq_test.s` + `bin/acia_irq_rom.bin` | Echoes an input byte via an IM 1 ACIA ISR | RX data is consumed explicitly by the ISR |
| RC2014 NASCOM ROM | `~/repos/z80/RC2014-nascom/rom.bin` | NASCOM BASIC with buffered serial I/O | IM 1 ACIA interrupts; load as a ROM image |

## Unit tests (no board required)

The board tool and image parsers run (and are tested) on the host without
any FPGA access.  The hardware layer is replaced by an in-memory `MockBoard`
whose state persists across invocations, exactly like the live FPGA does
between `z80` commands.

```bash
./demos/z80_asm/run_z80_tests.sh
# or from the repo root:
python3 -m unittest discover -s demos/z80_asm/tests -t demos/z80_asm -v
```

Coverage: Intel HEX parsing (records, types 02/04, gaps, EOF, checksums),
binary detection, chain splitting, `load`/`dump` handlers (boundaries,
clipping, vector, fill, verify, force-halt, strict), and end-to-end `z80`
invocations via `cli.main()` (state persistence, term-not-last rule, legacy
compatibility, clean TTY error).

## Architecture

The FPGA design replaces the old `bf2_soc` (Brainfuck CPU) with a Z80-compatible
SoC. The PS (ARM A9) communicates with the Z80 through:

- **`ctrl_gp0`** (axi_gpreg @0x7C440000): CPU control — halt, run, step, reset
- **`ctrl_gp1`** (axi_gpreg @0x7C440044): RAM access (56 KB, Z80 address 0x2000-0xFFFF; GP address is a zero-based offset)
- **`ctrl_gp2`** (axi_gpreg @0x7C440084): ROM access (8 KB, Z80 address 0x0000-0x1FFF)
- **`/dev/axis_fifo_0x7c450000`**: byte stream I/O — shared between the raw bridge (any port except 0x80-0x81) and the MC68B50 ACIA (ports 0x80-0x81)

### Z80 I/O port map

| Port(s) | Peripheral | Protocol |
|---|---|---|
| Any except 0x80-0x81 | Raw byte bridge | IN/OUT pass-through to FIFO (original behaviour) |
| 0x80 | ACIA Control (write) / Status (read) | MC68B50 register protocol |
| 0x81 | ACIA Data (write/read) | MC68B50 byte transfer |

### MC68B50 ACIA protocol

The FPGA implements an MC68B50-compatible ACIA on I/O ports 0x80-0x81.
The Z80 firmware may use either the polled protocol or maskable interrupts:

1. **Initialise**: Write control register (port 0x80) with non-reset value
   (e.g. `0x17` = ÷1 clock, 8 bits, 1 stop bit, RTS low, no IRQ)
2. **Transmit**: Poll status until TDRE (bit 1) = 1, then write data reg (port 0x81)
3. **Receive**: Poll status until RDRF (bit 0) = 1, then read data reg (port 0x81)
4. **Interrupt mode**: Set CR[7] to enable receive IRQs and/or CR[6:5] to
   `01` to enable transmit-empty IRQs, then use `IM 1` and `EI`.  The SoC
   connects the ACIA IRQ to the TV80 INT input; IM 1 acknowledges vector
   `RST 38h` at ROM address `0x0038`.  RX data remains in the shared bridge
   until the ISR explicitly reads port `0x81` (there is no RX prefetch).

Status register format:

| Bit | Name | Meaning |
|---|---|---|
| 0 | RDRF | Receive data register full |
| 1 | TDRE | Transmit data register empty |
| 2 | DCD | Data carrier detect (tied to 0) |
| 3 | CTS | Clear to send (tied to 0) |
| 4 | FE | Framing error (always 0 in FIFO mode) |
| 5 | OVRN | Overrun (byte lost when RDRF was still set) |
| 6 | PE | Parity error (always 0 in FIFO mode) |
| 7 | IRQ | Interrupt request (1 when enabled IRQ pending) |

The ACIA shares the AXI-Stream FIFO with the raw bridge.  Only one consumer
is active per Z80 I/O cycle, so both paths coexist without conflict.  Interrupt
acknowledge cycles are handled internally by the Z80 wrapper and are not
mistaken for raw FIFO reads.

### Interrupt test

Build the split RAM/ROM images, then send one byte through the ACIA:

```bash
python3 demos/z80_asm/assemble_z80.py \
    demos/z80_asm/src/acia_irq_test.s \
    --org 0x2000 -o demos/z80_asm/bin/acia_irq_test.bin
python3 demos/z80_asm/make_acia_irq_rom.py
python3 demos/z80_asm/z80.py run \
    --rom demos/z80_asm/bin/acia_irq_rom.bin \
    --ram demos/z80_asm/bin/acia_irq_test.bin --ram-org 0x2000 \
    --input Z -n 1
```

Expected output is one byte, `5a` (the echoed `Z`).  The ROM image contains
`JP 0x2000` at reset and the IM 1 handler at `0x0038`; the RAM source only
contains the ACIA setup and `HALT` loop.

### Register protocol

Same as bf2_soc — see `doc/Z80_SOC_PLAN.md` or `doc/AXIS_FIFO_BRIDGE.md`.

## Tips & pitfalls

1. **Finite capture** (legacy `run`): Infinite-loop programs need `-n N` or
   `--max-time SEC`.
2. **Load/dump require a halted CPU**: `z80 load ...` or `z80 dump ...` while
   the CPU is running fail unless you add `--force-halt` (which halts first).
3. **FIFO state between programs**: The AXI FIFO and bridge staging register
   are independent of the Z80 ctrl reset. Use `z80 flush` to reset both FIFO
   data paths and drain stale packets before a `reset run`.
4. **Interactive**: `ssh -t ebaz z80 term` (`-t` is required for TTY).
   Detach with Ctrl-]; the CPU keeps running. `term --flush` discards
   buffered output first. Note: `flush` cannot clear a stale byte already
   held in the ACIA RX register.
5. **New-style `run` ≠ legacy `run`**: on the board, `z80 run` resumes the
   CPU (no reset, no load). To restart from PC=0 use `z80 reset run`.
   `z80 run FILE.bin [options]` is the legacy one-shot compatibility path
   (halts the CPU on exit).
6. **Image origins**: `--ram-org` is a Z80 address in `0x2000–0xFFFF` (legacy
   flags; new-style `load ram FILE 0xADDR` takes the same address).
   If `--rom-org` is omitted, a ROM image is loaded at `0x0000` unchanged and
   must contain its own reset vector. `--rom-org 0x100` generates `jp 0x0100`
   at reset; the new-style equivalent is `load rom FILE --vector 0x100`.
7. **Intel HEX sparse loads**: only bytes listed in the file are written;
   nothing is zeroed. Use `load ram FILE --fill 0x00` to clear the loaded
   region first (mimics the old one-shot behaviour).
8. **Out of RAM**: 56 KB limit (0x2000–0xFFFF). Assemble first to check size.
9. **Load vs space**: if every segment of an image falls outside the target
   space, `load` errors with “nothing was written” (warnings are printed for
   each skipped/clipped segment first). Partial images warn and load the
   in-range part.
10. **Stale board process**: Reset with `pkill -f "/root/z80|run_z80"; echo "done"`.
11. **Memory map**: Legacy code loads to RAM address `0x2000`; the boot ROM
    contains `jp 0x2000` unless an explicit ROM image is supplied.

## Layout

```
demos/z80_asm/
  z80.py                 # host entry point (assemble / sim / run / passthrough)
  assemble_z80.py        # .s → .bin wrapper (uses binutils-z80)
  run_z80_program.py     # on-board entry point (installed as /root/z80)
  z80_board/             # board-side implementation package
    hw.py                #   /dev/mem register access + FIFO helpers
    images.py            #   Intel HEX / binary parsing, hexdump
    cli.py               #   command dispatcher (halt/run/reset/load/dump/term/flush)
  tests/                 # host-side unit tests (no board needed)
    mock_board.py        #   in-memory board + run_main() harness
    test_*.py            #   images / chain split / load-dump / dispatcher / host
  run_z80_tests.sh       # unit test runner
  install_to_board.sh    # deploy tools + examples to the board
  src/                   # Z80 assembly sources
  bin/                   # pre-assembled binaries
  expected/              # golden output references
```
