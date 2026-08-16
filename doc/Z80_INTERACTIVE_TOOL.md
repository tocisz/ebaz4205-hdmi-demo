# Z80 Interactive Board Tool — Implementation Plan

## Goal

Turn `/root/z80` (`demos/z80_asm/run_z80_program.py`) from a **one-shot load-and-run** into a **session-style control tool** for a live `z80_soc`. The FPGA CPU, BRAMs, and AXI-Stream FIFO stay up across invocations. Each `z80 <cmd>` should do one well-defined thing; a short chain of verbs is allowed when it stays unambiguous.

This is a **software-only** change. No RTL or block-design work is required. Existing GP0/GP1/GP2 and FIFO helpers already implement halt/run/reset, byte ROM/RAM R/W, and FIFO reset.

### Who this is for

Day-to-day use on the board (and later a thin host wrapper) when iterating on NASCOM/RC2014 images from `~/repos/z80/RC2014-nascom/` (`rom_ebaz.bin`, `hello.hex` / `tests.hex` via `z80-unknown-coff-objcopy -O ihex`).

---

## Current behaviour (what we are replacing)

`z80 program.bin` always:

1. `reset` (CPU halted)
2. load ROM (boot `JP 0x2000` and/or `--rom-image`)
3. load RAM
4. reset AXI FIFO both directions + drain character device
5. `reset` + `run`
6. either capture `-n` / `--max-time` or `-i` terminal
7. **`halt` on exit**

That last step is why the SoC cannot stay “alive” between commands. The host helper `demos/z80_asm/z80.py` only uploads the runner + images and invokes the same one-shot path.

Hardware facts the new tool must keep:

| Resource | Map | Notes |
|---|---|---|
| CPU control | `axi_gpreg` `@0x7C440000` GP0 | `_HALT`, `_RESET`, `_RUN`, step, status bit 0 |
| RAM | GP1, Z80 `0x2000–0xFFFF` | PS offset = Z80 addr − `0x2000` |
| ROM | GP2, Z80 `0x0000–0x1FFF` | 8 KiB |
| I/O stream | `/dev/axis_fifo_0x7c450000` | not cleared by CPU reset; use PG080 TDFR/RDFR `0xA5` |
| Safe mem access | CPU **halted** | load/dump while running races the Z80 |

`halt`/`run` are **pulses**, not sticky bits. There is no “leave RUN asserted”. After `run`, the CPU executes until it hits a Z80 `HALT` or we pulse `halt`/`reset`. `status()` only reports the core halt flag.

---

## Proposed CLI

Board binary stays `/usr/bin/z80` → `/root/z80`.

```text
z80 [--quiet] <command> [args] [<command> [args] ...]
```

### Commands

| Command | Meaning |
|---|---|
| `halt` / `stop` | Pulse GP0 halt. Idempotent if already halted. |
| `run` / `start` | Pulse GP0 run. Does **not** reset PC. |
| `reset` | Pulse GP0 reset (PC = 0). Leaves CPU **halted**. |
| `status` | Print `halted=yes/no` (and later optional extra bits). |
| `load rom FILE [ADDR]` | Write image into ROM. Default ADDR = `0` or from IHEX. |
| `load ram FILE [ADDR]` | Write image into RAM. Default ADDR = `0x2000` or from IHEX. |
| `dump rom [ADDR [LEN]] [-o FILE]` | Read ROM. Default `0 0x2000`. Hex to stdout unless `-o`. |
| `dump ram [ADDR [LEN]] [-o FILE]` | Read RAM. Default `0x2000 256`. |
| `term` / `connect` | Attach stdin/stdout to the FIFO. Last command only. |
| `flush` | Reset + drain AXI FIFO (both directions). |

Aliases: `stop`=`halt`, `start`=`run`, `connect`=`term`.

### Image formats

Detect by content + suffix:

1. **Intel HEX** (`.hex` / `.ihx`, or first non-empty line starts with `:`).  
   Records carry load addresses. Optional `[ADDR]` is an **added base** (normally 0).  
   Skip records outside the selected space (ROM vs RAM) with a warning.  
   This is what `z80-unknown-coff-objcopy -O ihex -j.ram` produces in RC2014-nascom.
2. **Raw binary** (`.bin`, `.out` stripped, or anything else).  
   Contiguous blob at `[ADDR]` (defaults above).  
   `z80-unknown-coff-ld` + `objcopy -O binary` is this path (`rom_ebaz.bin`).
3. **Not in v1:** COFF `.out` / ELF. User runs `objcopy` first.

Sparse IHEX: only write listed bytes. Do **not** zero the whole bank unless `--fill 0x00` is given.

`--verify` (default on for `load`): read back first/last N bytes and a few interior samples. Full verify optional (`--verify-all`) because GP1/GP2 is one-byte-per-handshake and 56 KiB is slow.

`--vector ADDR` on `load rom`: write `C3 lo hi` at `0x0000` after the image (same as current `--rom-org`).

### Terminal

```text
z80 term
z80 term --flush          # discard FIFO before attach (default: keep buffer)
z80 term --no-flush       # explicit keep
```

- Raw TTY, same quit keys as today: **Ctrl-]** or restore on SIGINT.
- `--flush` is the “discard buffer” option. Default **keep** so a just-started program’s banner is not lost.
- `term` must be the **last** verb in a chain (it is interactive and blocking).
- Do **not** halt on exit. The CPU keeps running; the next `z80` invocation can dump RAM or re-attach.

### Command chaining (keep it small)

Allow a **sequence of already-parsed commands** on one argv:

```text
z80 halt
z80 load rom rom_ebaz.bin
z80 load ram hello.hex
z80 flush reset run
z80 term --flush          # or: z80 flush term
```

Useful one-liners:

```text
z80 halt load ram hello.hex flush reset run
z80 halt dump ram 0x2000 64
z80 reset run term
```

Rules that keep parsing simple:

- Subcommand names are reserved words. Everything after a verb belongs to that verb until the next reserved word or end.
- `load` / `dump` take a required target (`rom`|`ram`) then optional file/addr/len/`-o`.
- Flags that apply to one verb sit next to it (`term --flush`, `load ram f.hex --verify-all`).
- Global flags (`-q`) may appear before the first verb.
- If chaining feels messy in review, ship **one verb per invocation** first; chaining is a thin loop over the same handlers.

Do **not** build a REPL in v1. SSH + repeated `z80` is enough.

### Compatibility shim

Keep a deprecated one-shot for existing scripts and `z80.py run`:

```text
z80 run FILE [legacy flags]     # halt, load ram FILE @0x2000, optional boot ROM,
                                # flush, reset, run, capture or -i, halt on exit
```

Print a one-line stderr hint pointing at the new verbs. After the host helper is updated, mark `run` as compatibility-only in `--help`.

---

## Architecture

Split the current 700-line script into modules on the board (still one installable tree):

```text
demos/z80_asm/
  z80_board/                 # library used by the CLI
    hw.py                    # Z80Board, FIFO open/reset/read/write (moved as-is)
    images.py                # IHEX + binary parse → list[(addr, bytes)]
    cli.py                   # argparse + chain dispatcher
  run_z80_program.py         # thin wrapper: python3 -m or exec cli
  z80.py                     # host: scp/ssh each verb (or a remote chain)
```

Install: `install_to_board.sh` copies the package to `/root/z80_board/` and a `/root/z80` entry point. PATH symlink unchanged.

### Load/dump algorithm

```text
require_halted():
    if not status():
        error("CPU running; z80 halt first")   # or --force-halt

load:
    require_halted()
    segs = parse_image(file)
    for (addr, blob) in segs:
        if space == rom: clip/check 0x0000–0x1FFF; rom_write
        if space == ram: clip/check 0x2000–0xFFFF; ram_write(addr-0x2000)
    optional vector
    sample verify
```

Refuse to load/dump while running unless `--force-halt` (then halt, do I/O, leave halted). Never auto-`run` after load.

### Host helper (`z80.py`) after v1

Keep `assemble` / `sim`. Change `run` to:

1. assemble if needed
2. `scp` images
3. `ssh ebaz 'z80 halt load ram … flush reset run'`
4. optional `ssh -t ebaz z80 term`

Later: `python3 demos/z80_asm/z80.py halt`, `… load ram hello.hex`, etc., as pass-through.

---

## Implementation phases

### Phase A — Split + verbs without I/O stream

1. Extract `Z80Board` + FIFO helpers unchanged.
2. Implement `halt`, `start`/`run`, `reset`, `status`.
3. Implement binary-only `load`/`dump` (reuse `load_rom`/`load_ram`/`rom_read`/`ram_read`).
4. Add hexdump formatter (reuse the existing 16-byte dump).
5. Unit-test image address translation on the host (no board): RAM `0x2000` → offset 0, reject `0x1FFF` for RAM, reject overflow past `0xFFFF` / `0x1FFF`.
6. Manual on board: load `counter.bin`, dump 16 bytes, `reset run`, confirm `status` not halted.

### Phase B — Intel HEX

1. Parse types 00 (data), 01 (EOF), 02/04 (extended address). Reject 03/05 or ignore with warning.
2. Golden tests from `~/repos/z80/RC2014-nascom/hello.hex` / `tests.hex` (addresses in `.ram`, typically `0x8400` on EBAZ memory map — confirm against `memory_ebaz.ld` at implement time).
3. `load ram hello.hex` then `dump ram 0x8400 32` must match objcopy binary of `.ram`.

### Phase C — Terminal + flush

1. Move current `-i` loop to `term`.
2. `--flush` / `--no-flush`.
3. **Do not halt on detach.**
4. Document: leftover RX in the ACIA/bridge is why `--flush` exists; NASCOM banners need `--no-flush` after `run`.

### Phase D — Chaining + install + host

1. Argv splitter + reserved-word scan.
2. Update `install_to_board.sh` and `demos/z80_asm/README.md`.
3. Host `z80.py` pass-through + keep legacy `run` as a composed chain with old “halt at end” only if `--oneshot` is set.
4. Compatibility: `z80 FILE.bin` without a verb → error with hint, **or** treat as legacy `run` for one release.

### Phase E — Optional later (out of scope unless needed)

- `step` (already in `Z80Board`).
- Fill/clear: `z80 fill ram 0x2000 0x100 0x00`.
- Faster block load if we ever add a burst GP protocol (RTL).
- REPL.
- Load COFF/ELF directly.

---

## Recommended defaults (decisions)

| Topic | Decision | Why |
|---|---|---|
| Auto-halt on load | No; require `halt` or `--force-halt` | Makes state explicit |
| Halt after `term` | No | SoC stays interactive |
| Chain verbs | Yes, simple reserved-word scan | `halt load ram x reset run` is the common flow |
| Default `term` flush | Keep buffer | Don’t eat startup text |
| Default dump length | 256 RAM / full ROM if ADDR omitted for rom | ROM is only 8K; dumping all RAM is huge |
| Legacy positional `z80 file.bin` | One-release shim then error | Avoid silent dual personality |

---

## Verification

On-board checklist after install:

```text
z80 status
z80 halt
z80 load rom /root/z80-examples/boot.bin
z80 load ram /root/z80-examples/counter.bin
z80 dump ram 0x2000 16
z80 flush reset run
z80 status                 # halted=no
# in another shell or after a short sleep:
z80 halt
z80 dump ram 0x2000 16     # still the program
```

RC2014:

```text
# on host
scp ~/repos/z80/RC2014-nascom/rom_ebaz.bin ~/repos/z80/RC2014-nascom/hello.hex ebaz:/tmp/
# on board
z80 halt
z80 load rom /tmp/rom_ebaz.bin          # image includes its own vectors
z80 load ram /tmp/hello.hex
z80 flush reset run term --no-flush
```

Regression: existing `python3 demos/z80_asm/z80.py demos/z80_asm/src/counter.s -n 64` still works via the compatibility `run` chain (including final halt).

Host: `python3 -m py_compile` on the new package; IHEX unit tests without the FPGA.

---

## Risks

- **Byte-wise GP load is slow.** 8K ROM + a few K of HEX is fine. Filling 56K will take seconds; print progress every 1K if not `--quiet`.
- **Running CPU vs dump.** Document corruption risk; default refuse.
- **FIFO vs ACIA.** `flush` clears PS FIFO + bridge, not the ACIA 1-byte RX holding register. A leftover `rx_valid` can still deliver one stale character after `term --flush`. If that bites NASCOM, add a later “discard one dummy IN” or an RTL peek; do not block v1 on it.
- **`run` after `reset` vs `run` without `reset`.** Both are valid; chaining should not imply reset.
- **IHEX segments in ROM space while `load ram`.** Warn and skip; do not abort the whole file unless `--strict`.

---

## Implementation status (2026-08-16)

Implemented (software only, `demos/z80_asm/`):

- `z80_board/` package: `hw.py` (register/FIFO layer, unchanged logic),
  `images.py` (Intel HEX + binary), `cli.py` (verb dispatcher, chaining,
  legacy compatibility path).
- Board verbs: `halt/stop`, `run/start`, `reset`, `status`, `load rom|ram`
  (HEX + binary, `--vector`/`--fill`/`--verify-all`/`--no-verify`/
  `--force-halt`/`--strict`), `dump rom|ram` (`-o`/`--binary`), `flush`,
  `term/connect` (`--flush`/`--no-flush`; no halt on detach).
- Host `z80.py`: `halt/reset/status/flush/load/dump/term` pass-through
  (auto-upload of local files) + raw `board` subcommand; legacy `run`
  unchanged but now uploads the whole package; `_flag_tokens` keeps
  explicit 0 values (`--fill 0`) while omitting False bool flags.
- Install (`install_to_board.sh`) deploys `/root/z80` + `/root/z80_board/`.
- Unit tests in `demos/z80_asm/tests/` — run without the board:

  ```bash
  ./demos/z80_asm/run_z80_tests.sh
  ```

  (79 tests: images, chain splitting, load/dump handlers against a
  persistent `MockBoard`, end-to-end `cli.main()` invocations, host
  pass-through helpers.)

Not done (needs the board):

- Live verification on hardware (status/load/dump/run/term on EBAZ4205).
- RC2014 flow on the board: `z80 halt; z80 load rom rom_ebaz.bin;
  z80 load ram hello.hex; z80 flush reset run; z80 term --no-flush`.
  Load addresses confirmed off-board: plain `hello.hex`/`tests.hex` carry
  `.ram` at 0x8400 (0x8000-based rc2014.ld), which lies inside the EBAZ
  0x2000–0xFFFF window — the extra 24 KB is simply unused, so no
  EBAZ-specific hex variants were added to the RC2014 Makefile.
- ACIA leftover-RX note: `flush` clears the AXI FIFO + bridge but not a
  stale byte held in the ACIA 1-byte RX register (known caveat in cmds).

Review fixes (same session, before boarding): `term --fifo` removed (the
FIFO device is fixed; the option was parsed but never honoured); new-style
`main()` now reports "need root" on /dev/mem PermissionError instead of
crashing; host `remote_tokens()` only uploads path-like or known-suffix
files (a cwd file named `status` can't shadow a verb); host `term` gained
`--no-flush`; host `load` flags are emitted exactly once; default load
verification samples interior quartiles (not just ends); an image that
writes nothing into the selected space errors instead of "succeeding";
`term` exits on FIFO POLLHUP/POLLERR instead of hanging until Ctrl-].

---

## File touch list

| File | Change |
|---|---|
| `demos/z80_asm/run_z80_program.py` | Become entry / re-export; logic moves out |
| `demos/z80_asm/z80_board/` | New package (hw, images, cli) |
| `demos/z80_asm/z80.py` | Pass-through verbs + `board` raw subcommand |
| `demos/z80_asm/install_to_board.sh` | Install package + examples |
| `demos/z80_asm/run_z80_tests.sh` | Unit test runner (host, no board) |
| `demos/z80_asm/tests/` | Self-contained unittest suite + MockBoard |
| `demos/z80_asm/README.md` | New command table + test section |
| `doc/Z80_INTERACTIVE_TOOL.md` | This plan |

No Makefile / Vivado / DT changes.

---

## Suggested first PR slice

Phase A only: halt/run/reset/status + binary load/dump + “must halt” guard. Leave current one-shot `main()` callable as `z80 run …` so nothing else breaks. Then B (hex), C (term), D (chain + host).
