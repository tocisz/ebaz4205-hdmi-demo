# Byte-FIFO Migration — PL + Kernel + Userspace

**Goal:** Replace the 32-bit packet transport (32-bit `TDATA` + `TLAST` per word, 24 bits dropped) with a true **byte-stream FIFO** — one `char` per `TDATA` beat, no packet length. **DEPTH=1024 bytes** each direction (user decision). Saves 3/4 PL BRAM and 3/4 driver bounce, simplifies `hw.py`/`cli.py`.

**Status:** `PL_DONE` + `DRIVER_DONE` + `HOST_DONE` + `INTEGRATION_DONE` + `VERIFICATION_DONE` + `POLL_DONE` — Phase 1 (PL 8-bit) committed; Phase 2 (driver fork, poll) committed 2026-08-27; Phase 3 (host byte shim) committed 2026-08-27; Phase 4 (integration wiring/docs) committed 2026-08-27; Phase 5 (verification) DONE 2026-08-27 (HDL+kernel rebuilt + deployed, `todos2.f` clean, 100 OK); **Phase 6 (host poll switch) DONE 2026-08-27** (FIFO `POLLIN` wakeups, pipe linger preserved, 100 OK); **RX IRQ wake fix** DONE 2026-08-27 (sparse output now wakes `poll()` after FIFO idle).

**Update 2026-08-27:** Name **`axi_byte_fifo` confirmed** — AXI is correct: PS talks **AXI4-Lite** (`s_axi_aclk`, `s_axi_awaddr/wdata/araddr` @ `0x7C450000`, `axis_fifo.ko` does `iowrite32(TDFD)/ioread32(RDFD)`) to the FIFO IP; the IP's PL side then emits **AXIS** (`axi_str_txd/rxd`, now 8-bit) to `axis_byte_bridge`/`z80_soc`. Xilinx IP is `axi_fifo_mm_s` = *AXI-MM to Stream* — `axi_` names the SW-visible MM side. Bridge keeps `axis_` because its ports are pure stream.

**Naming decision:** Rename PL library to match kernel driver. Proposed unified name **`axi_byte_fifo`**:

| Today | After |
|-------|-------|
| `hdl/library/axi_fifo_lite` (PL) | `hdl/library/axi_byte_fifo` |
| `linux/drivers/staging/axis-fifo/axis-fifo.c` → `axis_fifo.ko`, device `/dev/axis_fifo_0x7c450000` | `linux/drivers/staging/axi-byte-fifo/axi-byte-fifo.c` → `axi_byte_fifo.ko`, device `/dev/axi_byte_fifo_0x7c450000`, DT `compatible="xlnx,axi-byte-fifo-1.0"` |
| `hdl/library/axis_byte_bridge` | **keeps name** (bridge stays; its stream ports shrink `32b → 8b`, `TLAST` deleted) |

Rationale: `axi_` (AXI-MM side) matches Xilinx `axi_fifo_mm_s`; `axis_byte_bridge` already describes the stream side — only the FIFO IP/driver need the shared `axi_byte_fifo` name so `find . -name '*byte_fifo*'` hits both. **Confirmed 2026-08-27.**

Related docs: `doc/AXIS_FIFO_BRIDGE.md`, `doc/Z80_FIFO_WEDGE_INVESTIGATION.md §5b-§5d`, `hdl/library/axi_fifo_lite/README.md`.

---

## Current waste (why it matters)

* `axi_fifo_lite.sv`: `rx_mem[1024][31:0]` + `tx_mem[1024][31:0]` = 8 KiB BRAM, only `rx_mem[*][7:0]` used → 75% dead. At `DEPTH=1024×8b` → 2 KiB.
* `axis_byte_bridge.sv`: `m_axis_tdata[31:0]` → `rx_data=m_axis_tdata[7:0]` (upper 24 dropped), `s_axis_tdata={24'd0,byte}` → `TLAST=1` per byte.
* `axis-fifo.c`: `READ_BUF_SIZE 128U` words = 512 B bounce, word loops `iowrite32(TDFD)` + `iowrite32(TLR=len)`, `ioread32(RLR)` + `RDFO` checks, `len%4` guards.
* `demos/z80_asm/z80_board/hw.py`: `write_byte = bytes([b,0,0,0])`, `_low_bytes_from_words()` extracts `word[0]`.

All of `TLR`/`RLR`/`TLAST`/`TDR`/`RDR` exist only to emulate packets that never carry >1 byte.

---

## Phases

### Phase 0 — Planning & rename decision (this file) — Status: `DONE` 2026-08-27

- [x] Write this plan to `doc/AXI_BYTE_FIFO_PLAN.md` (tracks progress)
- [x] Confirm unified name `axi_byte_fifo` — **ACK 2026-08-27** (`axi_` = AXI-MM side is correct; PL side is AXIS but SW-visible port is `s_axi`, hence `axi_fifo_mm_s` heritage)
- [x] Confirm device path & compatible change is OK (breaking — bitstream + `.ko` + `hw.py` must upgrade atomically) — implied by name ACK
- [x] Confirm `DEPTH=1024` fixed (not 4096 for same BRAM as old 32-bit) — per prompt

### Phase 1 — PL: `axi_byte_fifo` + `axis_byte_bridge` 8-bit — Status: `DONE` 2026-08-27 (hdl fd8f113e5)

PL is the source of truth — driver and userspace shrink to match it.

- [x] `git mv hdl/library/axi_fifo_lite hdl/library/axi_byte_fifo`
  - [x] Rename `axi_fifo_lite.sv` → `axi_byte_fifo.sv`, module `axi_fifo_lite` → `axi_byte_fifo`
  - [x] `axi_byte_fifo.sv`: `logic [7:0] rx_mem[DEPTH]`, `tx_mem[DEPTH]`; ports `axi_str_{txd,rxd}_tdata[7:0]`; delete `*_tlast`; `tx_pending_cnt` coalesce removed (no `TLR` commit window); `TDFD(0x10)` = immediate push to `tx_mem`, `RDFD(0x20)` = immediate pop; `TDFV(0x0C)=DEPTH-tx_cnt`, `RDFO(0x1C)=rx_cnt`; `TLR(0x14)`/`RLR(0x24)` return `0` (or trap) — no packet commit; delete `TDR`/`RDR` handling; keep offsets `0x00/04/08/18/28` identical (see map below). Keep `single always_ff` merged (MDRV-1) and coincident `AR+RDFD`/`tvalid` handling.
  - [x] `axi_byte_fifo_ip.tcl` / `component.xml` / `xgui/axi_byte_fifo_v1_0.tcl`: `adi_ip_create axi_byte_fifo`, `TDATA` width `32→8`, delete `TLAST` from both `axi_str_*` buses (component.xml patched: TLAST portMaps + ports removed, TDATA 31→7)
  - [x] Update `hdl/library/axi_byte_fifo/README.md` (new width, no `TLAST`/`TLR`/`RLR`, `DEPTH=1024` fixed)
- [x] `hdl/library/axis_byte_bridge/axis_byte_bridge.sv`: ports `m_axis_tdata[7:0]`, `s_axis_tdata[7:0]`; delete `m_axis_tlast`/`s_axis_tlast`; delete `24'd0` padding + `UNUSEDSIGNAL` guards; keep RTS/CTS (`rts_n` gating `rx_valid`+`m_axis_tready`, `cts_n=!io_tx_ready`) and 1-deep `tx_stage` for strobe-vs-`tready`
  - [x] `axis_byte_bridge_ip.tcl`: stream buses `TDATA 8`, delete `TLAST` (component.xml patched)
- [x] `hdl/projects/ebaz4205/system_bd.tcl`: `ad_ip_instance axi_byte_fifo axi_byte_fifo_0` (was `axi_fifo_lite axi_fifo_mm_s_0`) @`0x7C450000`, re-wire `AXI_STR_TXD/RXD` (now 8-bit) — instance renamed to `axi_byte_fifo_0`, DT `compatible="xlnx,axi-byte-fifo-1.0"` pending
- [x] Testbenches: `fifo_wedge_tb/axi_fifo_lite_sim.sv` wrapper now 8-bit (TLAST deleted) mapping `axi_fifo_mm_s_sim`→`axi_byte_fifo`; `run.tcl` updated to `../axi_byte_fifo/axi_byte_fifo.sv`; `tb_axis_byte_bridge.sv` width `8`, TLAST checks removed (full tb_fifo_wedge byte semantics deferred)
- [x] Lint: `verilator --lint-only -Wall` axis_byte_bridge **PASS**, `verilator --lint-only -Wno-SYMRSVDWORD axi_byte_fifo.sv` 3× UNUSEDSIGNAL (awaddr/wstrb/araddr, expected) — no error, z80_soc lint SYNCASYNCNET warnings (tv80) — no error
- [x] Sims: `make -C hdl/library/z80_soc sim-acia` **PASS (15 checks, RTS/CTS)**, `make -C hdl/library/axis_byte_bridge sim` **30 checks PASS incl. TEST4 RTS stalls m_axis only**

Register map after (from base `0x7C450000`):

| Off | Name | Access | New meaning |
|-----|------|--------|-------------|
| `0x00` | `ISR` | RO/W1C | unchanged (`0x01D00000` idle, `W1C`); `RC` (bit 26) latches on RX empty→non-empty |
| `0x04` | `IER` | RW | enables the latched RX-data (`RC`, bit 26) interrupt; `interrupt = ISR & IER` |
| `0x08` | `TDFR` | WO | `0xA5` resets TX (`tx_cnt/wptr/rptr`) |
| `0x0C` | `TDFV` | RO | `DEPTH - tx_cnt` bytes free |
| `0x10` | `TDFD` | WO | **one byte** `wdata[7:0]` → TX FIFO |
| `0x14` | `TLR` | RO/WO | **deleted** — reads `0`, writes ignored (kept for `axis-fifo.c` compat if driver not yet cut) |
| `0x18` | `RDFR` | WO | `0xA5` resets RX |
| `0x1C` | `RDFO` | RO | `rx_cnt` bytes occupied |
| `0x20` | `RDFD` | RO | **one byte** pop (`rx_cnt--`) |
| `0x24` | `RLR` | RO | **deleted** — reads `0` (was `4`) |
| `0x28` | `SRR` | WO | `0xA5` resets both |

### Phase 2 — Kernel: `axi-byte-fifo` driver — Status: `DONE` 2026-08-27

Mirror PL deletion — no length register, no word bounce, no `%4` guards. Old driver kept (Option A fork).

- [x] Fork driver: `cp -a linux/drivers/staging/axis-fifo linux/drivers/staging/axi-byte-fifo` + new `Kconfig` `AXI_BYTE_FIFO`, `Makefile` `obj-$(CONFIG_AXI_BYTE_FIFO) += axi-byte-fifo.o` — old driver stays for rollback (Option B rejected)
- [x] `axi-byte-fifo.c` (from `axis-fifo.c` 470 LOC, 19 KiB):
  - [x] `DRIVER_NAME "axi_byte_fifo"`, `of_match "xlnx,axi-byte-fifo-1.0"` (`xlnx,axi-fifo-mm-s-4.1` removed)
  - [x] Deleted `READ_BUF_SIZE`/`WRITE_BUF_SIZE`, `tmp_buf[128]` word bounce, `TDR`/`RDR`/`TLR`/`RLR` sysfs (kept `isr/ier/tdfr/tdfv/tdfd/rdfr/rdfo/rdfd/srr` only)
  - [x] `axi_byte_fifo_read`: `wait_event(read_queue, RDFO>0)`, `min(len,RDFO)` bytes, `kbuf[i]=ioread32(RDFD)&0xFF` → `copy_to_user`, return `to_read` (no `RLR` read, no `bytes_available%4`, no `words/4`)
  - [x] `axi_byte_fifo_write`: `wait_event(write_queue, TDFV>=len)`, `kmalloc(len)` → `copy_from_user` → `for (i<len) iowrite32(byte, TDFD)` — **no `TLR` commit**, return `len` (no `len%4` guard, `len>tx_fifo_depth` check now bytes → `len>DEPTH`)
  - [x] `axi_byte_fifo_parse_dt`: accept `xlnx,axi-str-*-tdata-width == 8` (was `32`), error message updated to "only supports 8 bits (byte FIFO)"
  - [x] `fops.poll` (new): `poll_wait(read_queue/write_queue)`, `mask|=POLLIN|POLLRDNORM if RDFO`, `POLLOUT|POLLWRNORM if TDFV>=1` — fixes historic `f_op->poll==NULL` (§5c); added `#include <linux/poll.h>`; RX IRQ wake is supplied by the PL `RC` latch
  - [x] Kept `reset_ip_core` (`SRR/TDFR/RDFR` + `ISR/IER`), `misc_register` name `axi_byte_fifo_%pa` → `/dev/axi_byte_fifo_0x7c450000`, sysfs `ip_registers/{isr,ier,tdfr,tdfv,tdfd,rdfr,rdfo,rdfd,srr}` (no `tdr/rdlr/rlr`); added `wake_up_interruptible` after read/write for poll waiters; `MODULE_DESCRIPTION` updated to byte-stream
  - [x] Renamed internal symbols `axis_fifo*` → `axi_byte_fifo*` (`struct axi_byte_fifo`, `axi_byte_fifo_irq/open/close/read/write/poll/probe/remove/init/exit`, `axi_byte_fifo_attrs`, `DETECTION`)
- [x] DTS: no manual edit — PL-generated DT from `system_bd.tcl` (`axi_byte_fifo_0 @0x7C450000`) will emit `compatible="xlnx,axi-byte-fifo-1.0"`, `xlnx,rx-fifo-depth=<1024>` (bytes) automatically on next `make sdimg`
- [x] Build: verified `axi-byte-fifo.c` compiles clean with `arm-buildroot-linux-gnueabihf-gcc` (same flags as `axis-fifo.c`, exit 0, no warnings) — full `make modules` / `modinfo` deferred to board deploy
- [x] Keep old `axis_fifo.ko` until cutover — `linux/drivers/staging/axis-fifo/` untouched (verified still builds, `axis-fifo.c` unchanged)

### Phase 3 — Userspace: `demos/z80_asm/z80_board` — Status: `DONE` 2026-08-27

Delete the Python drop-24 shim. **No `.poll()` switch yet** — the new
`axi_byte_fifo` driver does expose `f_op->poll` (Phase 2), but Phase 3
keeps the host on timeout-poll (stdin-only `select.poll` + drain on
20 ms timeout + `EAGAIN` retry).  Switching `cli._term_session` to
`POLLIN`/`POLLOUT` on the FIFO fd is deferred to **Phase 6** (after
Phase 5 verification) per user request so the byte shim can be
verified first and rollback stays trivial (old `axis_fifo` still works
via fallback).

- [x] `demos/z80_asm/z80_board/hw.py`:
  - [x] `FIFO_DEV="/dev/axi_byte_fifo_0x7c450000"` + `FIFO_DEV_LEGACY="/dev/axis_fifo_0x7c450000"` fallback, `FIFO_BASE=0x7C450000` unchanged
  - [x] `write_byte(fd,b)` → `os.write(fd, bytes([b & 0xFF]))` (was `bytes([b,0,0,0])`)
  - [x] Deleted `_low_bytes_from_words()` (no `word[0]` extraction)
  - [x] `read_available(fd, max_bytes=4096)` → `os.read(fd,4096)` returns `list(chunk)` directly; keep `EAGAIN/EINVAL` transient handling
  - [x] `read_byte`, `flush_fifo`, `capture_fifo_output` unchanged except they now see bytes not words; `reset_fifo_buffers()` still `mmap` `TDFR/RDFR=0xA5`
  - [x] `open_fifo(device=None)` falls back `FIFO_DEV → FIFO_DEV_LEGACY` on `FileNotFoundError` (atomic cutover until Phase 4)
  - [x] Docstring: "byte bridge, no packet" + fallback note; `flush_fifo` comment notes poll deferred
- [x] `demos/z80_asm/z80_board/cli.py`: no logic change — `hw.write_byte`/`read_available` now byte-correct, `EAGAIN` retry (`deadline 1.0s + drain RX +5ms`) is still RTS backpressure, `_INPUT_TRANSLATE={0x08:0x7F,0x09:0x20,0x0A:0x0D}` + CRLF collapse + `0.5s` pipe linger kept; `_term_session` docstring updated to note poll deferred
- [x] `demos/brainfuck_org/run_bf1_program.py`: same `FIFO_DEV` + fallback, `write_byte`/`read_byte` byte-stream
- [x] `demos/z80_asm/tests/test_fifo.py`: `_packet(*bytes_)` → `bytes(bytes_)`, removed `LowBytesTest` (word-alignment), kept drain/term tests, added `test_multiple_writes_coalesce` + `test_write_byte_is_single_byte`; `run_z80_tests.sh` **100 OK**
- [x] `demos/z80_asm/install_to_board.sh`: (no change needed for Phase 3 — `z80_board/hw.py`+`cli.py` already uploaded; kernel `.ko` install deferred to Phase 4 atomic deploy)

### Phase 4 — Integration wiring — Status: `DONE` 2026-08-27

- [x] Choose cutover strategy: **Atomic** (selected) — one `./scripts/ebaz_deploy.sh` run programs new bitstream + `rmmod axis_fifo; insmod axi_byte_fifo` + `install_to_board.sh` — old device path disappears on next reboot. Keep this plan as rollback reference. Dual (both `0x7C450000` 8-bit + `0x7C460000` 32-bit alias) rejected — doubles BRAM for no benefit.
- [x] `hdl/projects/ebaz4205/system_bd.tcl`: finalized instance name `axi_byte_fifo_0` (was `axi_fifo_mm_s_0`) @`0x7C450000` — done in Phase 1 (`ad_ip_instance axi_byte_fifo axi_byte_fifo_0`, `axi_byte_fifo_0/s_axi_aclk`, `ad_cpu_interconnect 0x7C450000`, `AXI_STR_TXD/RXD` 8-bit, `ad_connect rts_n→rx_rts_n`); DT `compatible="xlnx,axi-byte-fifo-1.0"` will auto-emit on next `make sdimg`
- [x] `doc/ARCHITECTURE.md` updated: FIFO instance/table → `axi_byte_fifo_0`/`axi_byte_fifo`, 8-bit TDATA no TLAST, `/dev/axi_byte_fifo_0x7c450000` (legacy fallback `/dev/axis_fifo_0x7c450000`), Mermaid 32b→8b, `axis_byte_bridge` 8-bit byte-stream, `axi_byte_fifo` project IP row, resource table BRAM note; PL_TTY historical section → `axi_byte_fifo`
- [x] `ADDRESS_MAP.md` — no file in tree (address map lives in `hdl/projects/ebaz4205/system_bd.tcl` + `doc/AXI_BYTE_FIFO_PLAN.md` register map); nothing to update
- [x] Host fallback verified: `demos/z80_asm/z80_board/hw.py` + `demos/brainfuck_org/run_bf1_program.py` `open_fifo` falls back `FIFO_DEV→FIFO_DEV_LEGACY` so new host tree runs against old board image until atomic cutover

### Phase 5 — Verification (byte FIFO, timeout-poll) — Status: `DONE` 2026-08-27

Verify the atomic cutover works **without** yet switching to `poll()`:
`cli._term_session` stays on the Phase-3 timeout-poll loop so Phase 5
proves the byte FIFO + RTS + fallback in isolation.

- [x] PL lint+sims: `verilator --lint-only` (`axi_byte_fifo` + `axis_byte_bridge` clean), `make -C hdl/library/z80_soc sim-acia` **15 checks PASS (RTS/CTS)**, `sim-verilator` PASS, `make -C hdl/library/axis_byte_bridge sim` **30 checks PASS incl. TEST4 `rts_n` stalls PS→PL only** — re-ran 2026-08-27 after `hdl/projects/ebaz4205/Makefile` `axi_fifo_lite→axi_byte_fifo` fix
- [x] Module load: `ssh ebaz 'ls -l /dev/axi_byte_fifo_*'` → `crw------- /dev/axi_byte_fifo_0x7c450000`, `lsmod` → `axi_byte_fifo`, `devmem 0x7C45000C` → `0x400` after `SRR` (1024 bytes free), `rdfo` `0x0`; DT `compatible="xlnx,axi-byte-fifo-1.0"`, `tdata-width 8`, `rx/tx-depth 1024`; **Note:** `cat /sys/.../tdfv` currently returns garbled `0x78305f6f` (driver sysfs reads device-name string, not MMIO) — devmem proves HW correct; sysfs bug to fix in follow-up, does not affect FIFO data path
- [x] Host tests: `./demos/z80_asm/run_z80_tests.sh` → **100 OK** (Phase 3 updated, 2026-08-27)
- [x] Hardware byte-stream: `z80 halt; z80 load rom/ram (TC2014-FORTH) flush reset run` + `printf 'C\\r' | ssh ebaz z80 term` → `* Z80 FORTH * OK`, `printf 'words\\r' | z80 term` → word list clean, interactive `z80 term` works via byte stream (`hw.py` fallback `axi_byte_fifo→axis_fifo` not needed)
- [x] Burst that previously needed RTS: `cat ~/repos/z80/TC2014-FORTH/scripts/todos2.f | ssh ebaz z80 term` → **clean `; OK` lines (no `? MSG #0` / `variaoetN1u`)** after proper cold-start `C` handshake; `cat` throughput now honest byte rate (no 4× write amplification); RTS (`rts_n=(cr_tx_ctrl==2'b10)`) gates `rx_valid`+`m_axis_tready` so EAGAIN throttles at `serBuf≥48`
- [x] `make sdimg` **WNS +0.683 ns (TNS 0, WHS 0.023)** — no timing violation, `report_utilization` **Block RAM Tile 19.5/60 (32.5%)** — `axi_byte_fifo` 2 KiB (was 8 KiB, -75% BRAM, 1024×8b vs 1024×32b) — DT overlay `pl-ebaz4205.dtbo` 4.5K now `axi-byte-fifo@7C450000` `xlnx,axi-byte-fifo-1.0` 8-bit
- [x] Delete old driver/PL alias after verification (keep until Phase 6 if poll switch wants fallback) — **kept**: `linux/drivers/staging/axis-fifo/` untouched, `hdl` `axi_byte_fifo` only; no dual `0x7C460000` alias

### Phase 6 — Host `poll()` switch (after verification) — Status: `DONE` 2026-08-27

Switch the host from timeout-poll to honest `f_op->poll` **after**
Phase 5 proved the byte FIFO.  The terminal now waits on both stdin and
FIFO readiness; the old EAGAIN retry remains the write-side backpressure
fallback, and the pipe-EOF grace period retains its finite timer.

- [x] `demos/z80_asm/z80_board/cli.py:_term_session`: register FIFO fd
  with `select.poll()` for `POLLIN` (`RDFO>0`) alongside stdin
  `POLLIN`; drain only on descriptor events instead of a periodic 20 ms
  timeout.  Keep stdin `POLLHUP/POLLERR/POLLNVAL` handling and 0.5 s pipe
  linger.  The finite timeout is used only during pipe-EOF grace so a
  non-interactive command can terminate after output goes quiet.
- [x] `demos/z80_asm/z80_board/hw.py`: comments updated for the active
  FIFO `poll()` path; `flush_fifo` retains short settle sleeps because it
  is also used during startup and supports the legacy fallback.
- [x] `demos/z80_asm/tests/test_fifo.py`: poll-aware test verifies FIFO
  registration, FIFO-only output wake, and indefinite interactive poll.
- [x] Verification: `run_z80_tests.sh` **100 OK**; `py_compile` clean;
  on-board `strace -f -e poll` shows `poll([{fd=stdin}, {fd=fifo}], 2,
  -1)` and `cat todos2.f | ssh ebaz z80 term` remains clean.  The normal
  TTY path no longer wakes at 50 Hz while idle.

---

## Decisions log

| Decision | Rationale |
|----------|-----------|
| `DEPTH=1024` bytes | User fixed; keeps latency identical, saves 75% BRAM. Growing to 4096 would be free (same BRAM as old 32b×1024) but deferred. |
| Unified name `axi_byte_fifo` | `git mv`-able, matches Xilinx `axi_fifo_mm_s` (`s_axi` MM side SW-visible; PL side is AXIS `axi_str_*` but named from SW view). **Confirmed 2026-08-27.** |
| Delete `TLR`/`RLR`/`TLAST` entirely | Atomic value = 1 byte, per prompt. Keep reads as `0` for one-release compat if needed. |
| Add `f_op->poll` in new driver | Historic missing `poll` forced timeout-poll in `cli.py:_term_session`; now can `POLLIN`/`POLLOUT` honestly. |
| Atomic cutover (bitstream+`.ko`+`hw.py`) | Changing `TDATA` width breaks old driver; dual mapping wastes BRAM — single reboot cutover is cleanest. Plan file is the rollback record. |

## Errors encountered

| Error | Attempt | Resolution |
|-------|---------|------------|
| `hdl/projects/ebaz4205/Makefile` still `LIB_DEPS += axi_fifo_lite` after `git mv` | `make sdimg` 2026-08-27 failed `flock ... axi_fifo_lite/component.xml` | Fixed to `axi_byte_fifo`, `make clean && make sdimg` rebuild OK (XSA 454K, bit 2.0M) |
| `u-boot-xlnx/arch/arm/dts/pl-ebaz4205.dtso` still `axi-fifo@7C450000 xlnx,axi-fifo-mm-s-4.1 0x20` after PL rename | `ebaz_deploy.sh` deployed new bitstream but overlay still probed `axis_fifo` | Updated dtso to `axi-byte-fifo@7C450000 xlnx,axi-byte-fifo-1.0 0x08`, rebuilt `pl-ebaz4205.dtbo`, `scp` + `reboot` → `axi_byte_fifo` probe, `/dev/axi_byte_fifo_0x7c450000` |
| `cat /sys/.../tdfv` shows `0x78305f6f` ("o_0x") not `0x400` | Phase 5 verification | HW correct (`devmem 0x7C45000C` = `0x400`); driver sysfs reads device-name string ("". `axi_byte_fifo` data path works (`todos2.f` clean) — sysfs bug to fix in follow-up |
| `make sdimg` killed early (pid check) | Background `nohup make sdimg &` | Ran synchronous `make sdimg` to completion (BOOT 2.6M, uImage 4.1M, rootfs 1G) |
| Interactive `term` stopped showing sparse Z80 output until a keypress | Phase 6 used `poll(-1)`, but PL `interrupt` was hardwired to `0`; the driver's `poll_wait()` queue was never woken after RX became non-empty | Added a latched RX `RC` event (`empty→non-empty`), `interrupt = ISR & IER`, and ISR-W1C handling in `axi_byte_fifo.sv`; deployed bitstream and verified `words`/`todos2.f` plus IRQ count on hardware |

## Notes

* This plan is the progress tracker — check boxes as phases complete; do not create a second `task_plan.md` without updating this file.
* Rename is `git mv` so history follows; Vivado IP cache `*.hw/*.cache/*.ip_user_files` regenerates.
* Old docs `doc/Z80_FIFO_WEDGE_INVESTIGATION.md §5b-§5d` remain valid context (packet wedge + RTS fix) but this migration obsoletes `AXI_STR_* 32b` + packet language — update `doc/AXIS_FIFO_BRIDGE.md` when PL lands.

