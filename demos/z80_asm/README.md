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

# Load separate ROM and RAM images.  A nonzero --rom-org generates
# JP --rom-org at ROM address 0; --ram-org is a Z80 RAM address.
python3 demos/z80_asm/z80.py run \
    --rom demos/z80_asm/src/boot.s \
    --ram demos/z80_asm/src/counter.s --ram-org 0x2000 \
    -n 3
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
| Counter | `src/counter.s` | `00 01 02 03 … FF 00 01 …` | Uses raw I/O bridge (any port) |
| Echo | `src/echo.s` | Echoes back any byte sent | Interactive, raw I/O |
| Walk | `src/walk.s` | Pattern 00–FF written to RAM, then output | Raw I/O |
| Memtest | `src/memtest.s` | Full RAM test; outputs error count and pass/fail | Run from ROM |
| Boot | `src/boot.s` | `jp 0x2000` (3 bytes) | ROM bootstrap |
| ACIA Echo | `src/acia_echo.s` | Echoes back any byte sent via ACIA | Interactive, polled ACIA protocol |
| ACIA Counter | `src/acia_counter.s` | `00 01 02 03 … FF 00 01 …` via ACIA | ACIA TDRE-gated output |
| ACIA IRQ Test | `src/acia_irq_test.s` + `bin/acia_irq_rom.bin` | Echoes an input byte via an IM 1 ACIA ISR | RX data is consumed explicitly by the ISR |
| RC2014 NASCOM ROM | `~/repos/z80/RC2014-nascom/rom.bin` | NASCOM BASIC with buffered serial I/O | IM 1 ACIA interrupts; load as a ROM image |

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

1. **Finite capture**: Infinite-loop programs need `-n N` or `--max-time SEC`.
2. **Boot ROM**: Written once per FPGA boot. The runner checks and skips if present.
3. **FIFO state between runs**: The AXI FIFO and bridge staging register are
   independent of the Z80 ctrl reset. The runner resets both FIFO data paths,
   then drains any remaining packets before starting a new program.
4. **Interactive**: `ssh -t ebaz z80 echo.bin -i` (`-t` is required for TTY).
   Quit with Ctrl-C or Ctrl-].
5. **Image origins**: `--ram-org` is a Z80 address in `0x2000–0xFFFF`.
   If `--rom-org` is omitted, the ROM image is loaded at `0x0000` unchanged
   and must contain its own reset vector. If `--rom-org 0x100` is given,
   the runner loads the image at `0x0100` and generates `jp 0x0100` at reset.
6. **Out of RAM**: 56 KB limit. Assemble first to check binary size.
7. **Stale board process**: Reset with `pkill -f "/root/z80|run_z80"; echo "done"`.
8. **Memory map**: Legacy code loaded to RAM address `0x2000`; the boot ROM
   contains `jp 0x2000` unless an explicit ROM image is supplied.

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
