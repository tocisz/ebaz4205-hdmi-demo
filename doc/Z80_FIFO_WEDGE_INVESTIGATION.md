# Z80 `term` Output Wedge — Investigation Record

**Date:** 2026-07 (investigation session)
**Symptom:** During sustained guest output bursts (NASCOM BASIC banner, FORTH
`words` listing) on the EBAZ4205 z80_soc, terminal output stops mid-burst. The
CPU keeps running but spins on transmit. Pressing keys releases more pending
output (sometimes one char per keypress). The state **survives detaching and
reattaching `z80 term`**. `z80 term --flush` (or `z80 flush`) restores normal
operation — though after the RX cut-through experiment (§5a) flush alone may
no longer suffice in deep-stall states.

This document records established **facts** (each with references/evidence),
**lemmas** (things deduced from facts), the **hypotheses** (with per-hypothesis
status after testing — see §4 and §5a), and open
questions — so the investigation can be resumed or explained fresh.

---

## 1. System under test

Data path host ↔ Z80:

```
Linux user  ──/dev/axis_fifo_0x7c450000──  axis_fifo.ko  ──  axi_fifo_mm_s IP
                                                                │ AXI_STR_TXD (PS→PL) / AXI_STR_RXD (PL→PS)
                                                           axis_byte_bridge (32-bit word ↔ byte)
                                                                │ io_rx_* / io_tx_* handshake
                                                           z80_soc: raw I/O ports + acia68b50 (ports 0x80/0x81)
                                                                │
                                                              tv80 CPU (guest BASIC / FORTH)
```

Key configuration (`hdl/projects/ebaz4205/system_bd.tcl`, lines 299–330):

* `axi_fifo_mm_s`: TX and RX depth **1024 words each**
  (`C_TX_FIFO_DEPTH`/`C_RX_FIFO_DEPTH` = 1024)
* **Store-and-forward both directions**: `C_USE_TX_CUT_THROUGH 0`,
  `C_USE_RX_CUT_THROUGH 0`
* Base address `0x7C450000`; z80_soc control regs at `0x7C440000`

Relevant sources:

| File | Role |
|---|---|
| `hdl/library/axis_byte_bridge/axis_byte_bridge.sv` | word↔byte adapter; TLAST per word |
| `hdl/library/z80_soc/rtl/acia68b50/acia68b50.sv` | ACIA model (TDRE/RDRF semantics) |
| `hdl/library/z80_soc/z80_soc.sv` | wishbone ack rules, io mux |
| `linux/drivers/staging/axis-fifo/axis-fifo.c` | kernel chardev (no poll!) |
| `demos/z80_asm/z80_board/hw.py` | register defs, `read_available`, flush |
| `demos/z80_asm/z80_board/cli.py` | `_term_session`, `_drain_fifo_to` |
| `~/repos/z80/RC2014-nascom/int32k.asm` | guest serial driver (polled TX) |
| `~/repos/z80/TC2014-FORTH/forth.asm` | FORTH using int32k serial |

PG080 register offsets used (offset from `0x7C450000`):

| Off | Reg | Meaning |
|---|---|---|
| 0x00 | ISR | interrupt status (write 1s to clear) |
| 0x04 | IER | interrupt enable |
| 0x08 | TDFR | TX FIFO reset (write 0xA5) |
| 0x0C | TDFV | TX vacancy (words) |
| 0x14 | TFW/TX LEN | commit host→PL packet |
| 0x18 | RDFR | RX FIFO reset (write 0xA5) |
| 0x1C | RDFO | RX occupancy (words) |
| 0x20 | RLR | receive packet length |

---

## 2. Facts (measured / read from source)

### F1 — Back-pressure design per layer

* **Guest:** polls ACIA status; `TXA` reads SR, checks `TDRE`, writes DR only
  when set (`int32k.asm` TXA, line 216ff). Same I/O handling in BASIC and
  FORTH.
* **ACIA RTL:** write to DR goes straight through if `tx_ready`, else into a
  1-byte `tx_hold`; `TDRE` stays 0 until the hold drains
  (`acia68b50.sv`, `wr_data`, deferred-drain block).
* **Bridge:** `tx_ready` mirrors `s_axis_tready` combinationally; PL→PS side
  is a 1-deep staging reg asserting **TLAST on every word** (one word = one
  packet) (`axis_byte_bridge.sv`).
* **z80_soc:** ACIA access acks in 1 cycle regardless of TX handshake;
  raw-port OUT waits for `io_tx_ready` (`z80_soc.sv` ack section).
* **Kernel driver:** has **no `f_op->poll`**; `read()` returns exactly ONE
  packet per call (RLR bytes); non-blocking read = trylock mutex → check RDFO
  → read RLR → EINVAL *only if* `bytes_available > len`
  (`axis-fifo.c`, `axis_fifo_read()`).
* **Python tool:** `_term_session` polls stdin only, wakes every 20 ms,
  drains with `read_available()` (loop `os.read(fd, 4096)` until EAGAIN)
  (`cli.py`, `hw.py`). 4096 ≥ max possible RLR (depth 1024 words = 4096 B),
  so the driver's EINVAL guard cannot fire.

### F2 — Deployment verified current

md5sums of `/root/z80_board/{hw,cli,images,__init__}.py` matched the repo
exactly at time of testing; `__pycache__` regenerated at install. The hang is
**not** stale deployed code.

### F3 — Wedge signature on the registers (while hung)

* `RDFO = 0` — kernel-visible RX queue genuinely empty; every userspace
  EAGAIN was honest.
* `TDFV = 0x3FC` — exactly 4 TX words parked, unconsumed, **persistent across
  healthy idle periods too** (never returns to 0x400 after first damage).
* `ISR = 0x01f80000` — only threshold/reset-complete bits latched; no RC
  storm, no error bits (RPUE/RPORE/RPURE/TPOE/TSE all clear).
* CPU `status` reports *running*, but ignores queued keystrokes → alive but
  stuck on the transmit side.

### F4 — Reproduction without any terminal attached

Typing `words\r` into the FIFO with **no reader**: burst fills the RX FIFO to
capacity in ≈47 ms and output stops at `RDFO ≈ 1026`.
Note: **1026 > configured depth 1024** — occupancy above physical capacity.

### F5 — Drain-from-full works

A tight read loop recovered all 2228 bytes of a complete `words` listing from
a FIFO at capacity; CPU resumed mid-drain and pushed the rest. Fabric + kernel
handle the "full then drained" case correctly at least sometimes.

### F6 — The wedge happens WITHOUT the FIFO ever being full

With an aggressive drainer (pure read loop, no sampling overhead), peak
`RDFO` was measured between **37 and 90 words** — yet bursts still wedged at
~1.3–1.4 KB received, tail e.g. `'p) 0branch branch execut'`, post-state
`RDFO=0`. So FIFO-full back-pressure is **not** the trigger.

### F7 — A phantom zero byte is inserted into the stream

Captured streams contain a spurious `NUL` where the guest emitted none:

```
...'rd pad hold blanks erase fill '[NUL]' query expect ." ...
```

Observed at the **same offset (633)** in two independent runs. Text after the
NUL is the correct continuation → this is an *insertion* of one zero-data
packet, not a pointer shift/desync of payload order.

### F8 — A host→guest keystroke kick partially releases the wedge

While wedged (RDFO=0), writing a single keystroke packet made the stalled
burst resume: `RDFO` jumped 0 → 769 within 1 s. This matches the field report
of "each keypress shows more characters".

### F9 — Reset semantics observed

* RX-FIFO-only reset (`RDFR = 0xA5`) did **not** release a wedge.
* Full `--flush` (TDFR + RDFR + settle + drain) reliably restores health.
* PS CPU reset pulse leaves the CPU **halted**; a RUN pulse (GP0 bit 3) is
  required afterwards (`z80_soc.sv`: reset sets `halted<=1`).
* This FORTH image boots to a `Cold | Warm start (C|W) ?` prompt and needs a
  key before accepting commands.

### F10 — Tooling quirks on the board

* `mmap(PROT_READ)`-only mapping of `0x7C450000` faults with SIGBUS;
  `PROT_READ|PROT_WRITE` maps and reads fine (why `reset_fifo_buffers()`
  works but naive probes crashed).
* Reading `RLR` (offset 0x20) reproducibly raised SIGBUS during wedged-state
  probes (unresolved oddity; do single-offset-per-process probes).
* Board dropbear has no SFTP: use `scp -O` or pipe via ssh cat.
* `/dev/mem` mmap of the FIFO base is the only way to observe RDFO/TDFV/ISR;
  reading TDFD consumes a TX word (avoid).

---

## 3. Lemmas (deduced)

* **L1 — Userspace is honest.** Driver non-blocking read reports empty iff
  RDFO=0 (F1, F3). All Python-level "no data" answers were true; the missing
  output was never sitting readable anywhere.
* **L2 — Kernel datapath is functional.** Reads return committed packets
  correctly, including from a completely full FIFO (F5). Packet size never
  exceeds 4 B (TLAST per word, F1), so the EINVAL path can't trigger at
  len=4096.
* **L3 — Guest software is not implicated.** Identical failure with two
  independent guests (BASIC, FORTH) sharing only the int32k polled-TDRE
  serial code (F1); failure occurs with no guest involvement at all beyond
  emitting (F4, F6).
* **L4 — The CPU stall message chain is faithful.** CPU spinning on TDRE ⇒
  ACIA hold undrained ⇒ `io_tx_ready` low ⇒ bridge `s_axis_tready` low ⇒ the
  RX side of axi_fifo_mm_s stopped accepting, even though RDFO=0 (F3).
* **L5 — The wedge state is fabric-resident, not process-resident.**
  Survives closing/reopening the chardev (field report + F3 measurements from
  fresh processes).
* **L6 — The wedge state lives inside axi_fifo_mm_s.** `--flush` fixes it
  without touching CPU/ACIA/bridge resets (flush resets only the FIFO IP's
  internal TX/RX paths, F9; bridge reset is wired to `sys_cpu_reset` and ACIA
  reset to `cpu_nrst` — neither is asserted by flush).
  **Undercut by §5a result 5:** on the cut-through bitstream a deep-stall state
  was revived only by FIFO reset **plus** CPU reset — flush alone failed. So
  either L6 holds only in milder states, or the stuck state spans FIFO +
  bridge/ACIA. Re-test before relying on it.
* **L7 — Two distinct misbehaviors exist:**
  (a) *capacity overflow*: RDFO can exceed physical depth (F4) — corruption
  triggered at the full boundary;
  (b) *commit desync*: phantom zero-data packet (F7) followed later by total
  stop of packet visibility despite tready deassertion (F3, F6) — occurs
  without ever filling (F6).

---

## 4. Hypotheses

* **H1 (original form REFUTED 2026-07, see §5a): race inside the axi_fifo_mm_s
  RX store-and-forward packet-commit logic.** With TLAST-always-high, every
  word commits as its own packet; occasionally the length/accounting commits
  before (or without) the data-RAM write becoming part of the entry → a phantom
  zero-data packet becomes visible (explained F7) → repeated events eventually
  desync the commit FSM so far that it stops publishing packets entirely while
  keeping `s_axis_tready` low (explained F3/F6 and L4's stall chain).
  **Status:** the RX cut-through experiment (H3/§5a) bypasses exactly this
  machinery, yet both the aligned phantom NUL (same `fill [NUL] query` context)
  and the stall persist — so this specific commit-race mechanism cannot be the
  cause. What survives is only a weakened variant: a bug in the RX-side logic
  **shared by both modes** (word storage / `tready` generation / RLR tracking),
  or outside the FIFO IP entirely (`axis_byte_bridge`, or the z80_soc io-ack ↔
  ACIA TX-deferral chain). The deterministic NUL offset across runs/bitstreams
  fits "a specific stream pattern triggers it" better than a random race.
  Next discriminator: ILA/xsim on `AXI_STR_RXD` at the bridge↔FIFO boundary
  (open question 2) — `tready` stuck low with RDFO < depth points back at the
  FIFO; a dropped/unaccepted `tvalid` pulse from the bridge side exonerates it.
* **H2: the full-boundary overflow (F4) and the phantom packet are the same
  underlying counter bug**, seen at different operating points. Not proven:
  F6 shows wedging without full.
* **H3 (tested 2026-07, REFUTED): enabling `C_USE_RX_CUT_THROUGH 1` removes the
  store-and-forward commit machinery** where the race was thought to live.
  Built (timing met, `C_USE_RX_CUT_THROUGH VALUE="1"` confirmed in
  `ebaz4205.gen/.../hw_handoff/system.hwh`), deployed via
  `./scripts/ebaz_deploy.sh --bitstream-only`, and exercised with
  10-burst `words` tests plus a raw-packet capture. **The wedge survives**;
  see §5a for full results. The parameter is left in `system_bd.tcl` for now
  (with a comment); revert to 0 if the corruption symptoms below are judged
  worse than the original behavior.
* **H4: keystroke kicks advance the wedged FSM** because host→PL activity
  shares internal scheduling/memory-port arbitration with the stuck RX path
  (explains F8; mechanism unverified). Partially undercut by §5a result 5:
  in one deep-stall state the kick had no effect, so kick effectiveness is
  state-dependent.
* **H5 (tested 2026-07, REFUTED): the persistent 4 parked TX words
  (TDFV=0x3FC) are residual corruption from earlier wedged sessions.** On the
  cut-through bitstream TDFV reads 0x3FC **immediately after TDFR+RDFR reset +
  CPU reset with zero host writes**, and it is *frozen*: six 4-byte writes to
  TDFD do not decrement it, and repeated `TDFR = 0xA5` does not restore 0x400.
  Either the vacancy counter never initializes correctly in this design or it
  is damaged by guest boot traffic alone (the only activity between reset and
  measurement). TDFV must not be used as a diagnostic signal.

---

## 5. Open questions / next steps

1. ~~Rebuild bitstream with `C_USE_RX_CUT_THROUGH 1` and rerun the
   reproduction scripts.~~ **Done 2026-07 — did not fix the wedge; see §5a.**
2. ~~Next probe: ILA (or xsim) on `AXI_STR_RXD` between `axis_byte_bridge` and
   `axi_fifo_mm_s` during a burst — check for `tvalid` pulses with `tready`
   stuck low while RDFO < depth; also watch bridge staging-reg state at stall.~~
   **Done 2026-07 — xsim reproduction, see §5b. `axis_accepts` proves the bridge
   delivered every `tvalid` (2300/2300), so the wedge is inside `axi_fifo_mm_s`.
   A behavioral FIFO with the same register map passes.**
3. Re-test L6 on the cut-through bitstream: confirm whether *any*
   FIFO-register-only recovery exists, or whether CPU reset is now always
   required (§5a result 5).
4. Confirm/refute dropped-byte corruption (§5a result 4) by diffing raw
   captures against a known-good `words` listing instead of eyeballing.
5. Explain RLR-read SIGBUS (F10) — possibly related to the same IP state.
6. ~~Decide whether to keep `C_USE_RX_CUT_THROUGH 1` or revert to 0
   (`system_bd.tcl`) — neither mode fixes the wedge; cut-through additionally
   showed possible byte drops but survives longer before stalling.~~
   **Superseded by §5b — both modes wedge identically; the fix is `axi_fifo_lite`
   (see §5b), not a cut-through toggle. Leave `system_bd.tcl` at 1 until the
   Lite swap lands, then remove the parameter.**
7. Optional UX hardening in `z80_board.cli`: detect the wedge signature
   (CPU running + no output + RDFO=0 + TDFV frozen for > N seconds) and hint
   `z80 term --flush` — note that flush alone may no longer suffice; suggest
   `z80 halt load rom <img> reset run` style full restart when it fails.
8. Swap `axi_fifo_mm_s` for `axi_fifo_lite` (see §5b) — pending user call.
   Verify on hardware: `TDFV=0x400` after reset, 10× `words` bursts, no phantom
   NUL, `make sdimg` timing, and `axis_fifo.ko` ABI unchanged.

---

## 5a. H3 experiment record (RX cut-through bitstream, 2026-07)

Change: `C_USE_RX_CUT_THROUGH 0 -> 1` in `hdl/projects/ebaz4205/system_bd.tcl`;
full Vivado rebuild (timing met, WNS ≥ 0); deployed live via
`./scripts/ebaz_deploy.sh --bitstream-only` (no reboot; PL reconfiguration wipes
GP2-loaded ROM contents, so the FORTH image was reloaded with
`z80 halt / load rom rom.bin / board run`).

Test scripts (board-side, pattern per §6):

| Script | What it does |
|---|---|
| `/tmp/peak.py` | clean slate → boot → cold start → one `words` burst, aggressive drainer |
| `/tmp/h3_test2.py` | clean slate → **10 consecutive** `words` bursts; on stall tries keystroke kick then FIFO-only reset |
| `/tmp/rawcap.py` | clean slate → one `words` burst, keeps RAW packet bytes (`/tmp/raw.bin`) |

Results:

1. **The stall/wedge still occurs — H3 refuted.** No run completed a full
   `words` listing (~2297 B on the old bitstream) in one piece: bursts stall at
   anywhere from ~350 B to ~1660 B. Because each *new command* acts as a kick
   (F8), back-to-back bursts look deceptively like "short but complete"
   listings (348–680 B each) — the pending tail of burst *i* is flushed by the
   keystrokes of burst *i+1*. A single isolated burst (rawcap) stalls cleanly
   mid-dictionary (tail `...ey key `, stable >1.5 s, RDFO=0).
2. **The phantom NUL persists without store-and-forward — H1 refuted as the
   NUL source.** Aligned NULs appeared at the same context as F7
   (`...erase fill [NUL] query...`) in two independent runs on the cut-through
   bitstream. The insertion therefore does NOT come from the store-and-forward
   packet-commit machinery.
3. **Raw packet capture shows healthy framing.** Every `os.read` returned
   exactly 4 bytes (one word per packet, TLAST-per-word intact); payload bytes
   are word-aligned (`w\0\0\0 o\0\0\0 r\0\0\0 ...`). So the driver↔IP packet
   protocol itself is working at stall time; the loss happens below packet
   formation (bridge/ACIA handshake) or inside the IP datapath.
4. **Possible new symptom under cut-through: dropped output bytes.** Two runs
   showed garbled dictionary text (`query`→`qut`, `blanks`→`blans`,
   `mod blanks`→`modanks`) in extracted streams — consistent with whole bytes
   missing from the middle of the stream. Not yet confirmed against raw
   captures (extraction-alignment artifact not fully ruled out); rawcap's own
   burst was clean apart from the early stall.
5. **Recovery recipe changed (worse).** In one deep-stall state neither the
   keystroke kick nor `z80 flush` (TDFR+RDFR+drain, no CPU touch) revived
   output; only FIFO reset **plus CPU reset+run** re-booted the guest. On the
   old bitstream flush alone sufficed (F9). Kick effectiveness is now
   state-dependent.
6. **TDFV frozen at 0x3FC from the start** (see H5). Never observed 0x400 on
   this bitstream, including immediately after FIFO resets with no traffic.
7. Sanity checks done: timing met on rebuild; `C_USE_RX_CUT_THROUGH VALUE="1"`
   verified in the generated hardware handoff (`system.hwh`); deployment
   verified (bitstream programmed, overlay reapplied, `/dev/axis_fifo_*`
   present).

Conclusions:

* The wedge lives **below the store-and-forward commit layer** — candidates are
  now (i) the axi_fifo_mm_s RX *datapath/handshake* proper (still shared by
  both modes), (ii) `axis_byte_bridge` (staging-reg handshake, TLAST
  generation), or (iii) the z80_soc io ack rules interacting with ACIA TX
  deferral (L4 chain). L6's "state inside axi_fifo_mm_s" needs re-testing: the
  new "flush alone doesn't revive" data point suggests the stuck state may
  span FIFO **and** bridge/ACIA.
* Since TLAST-per-word packets are formed fine (result 3) yet bytes go
  missing/stall, a promising next probe is ILA (or xsim) on the
  `axis_byte_bridge` ↔ `axi_fifo_mm_s` `AXI_STR_RXD` handshake during a burst,
  watching for `tvalid` pulses the FIFO never accepts (`tready` stuck low
  while RDFO < depth).

---

## 5b. Xsim reproduction + `axi_fifo_lite` proof (2026-07, EBAZ4205 not rebooted)

**Harness:** `hdl/library/fifo_wedge_tb/` — real `axi_fifo_mm_s` IP (same
`C_TX_DEPTH=1024/C_RX_DEPTH=1024`, tested both `C_USE_RX_CUT_THROUGH 0` and `1`)
+ real `axis_byte_bridge` + real `acia68b50`, driven by a `int32k.asm`-faithful
polled-TX guest (`SER_TDRE=$02` = `SR[1]`) and an `axis_fifo.c`-faithful host BFM
(`RDFO→RLR→RDFD*N`, `TDFV→TDFD→TLR`). Fixes a TB bug: guest had polled
`SR[4]` (FE) and never progressed; now correctly polls `SR[1]`.

**With the Xilinx IP (both cut-through modes):**

```
BYTES=2300 GAP_POLL=2 GAP_BYTE=8 PACING=0
[peek] post-hard-rst TDFV=0x3FC RDFO=0 ISR=0x01D00000   // hard reset already wrong
[peek] post-soft-rst TDFV=0x3FC ...                     // SRR/TDFR/RDFR=0xA5
sent=2300 axis_accepts=2300 recv=1917
ANOMALY: RX word upper bits nonzero: 0x85D80000
PHANTOM-NUL at offset 1917 (expected 0x85)
WEDGE: TDFV=0x3FC RDFO=1532 ISR=0x85D80000 (RPURE) stall 2.5s
```

* `axis_accepts=2300` proves every `s_axis_tvalid/tready` handshake from the
  bridge was accepted — the bridge is exonerated; the loss is **inside the IP**.
* Phantom NUL at the same offset (1917) in both `RX_CUT=0` and `1` confirms §5a
  conclusion (not the store-and-forward commit). `RDFO=1532` afterwards is
  > remaining `383` bytes, and `ISR` shows `RPURE` — the length-queue has
  desynced and the AXI-Lite register file aliases `RDFO`/`ISR`/`RLR`.
* `TDFV=0x3FC` after *hard* `s_axi_aresetn` (before any `SRR`) reproduces H5 in
  simulation — the vacancy counter never initializes to `0x400`. This is an IP
  reset bug, not a driver sequence bug. Spec init `poll TRC|RRC`
  (`RESET_MODE=settle`: `SRR→poll 0x01800000`) was tested and **still wedges at
  1917**.
* Larger burst `BYTES=4000` wedges later at `recv=3232` with `BYTE DROPPED` at
  2973 — packet count matters, not wall time.

**With a behavioral FIFO (`USE_BEHAV=1`, `hdl/library/fifo_wedge_tb/axi_fifo_behav.sv`
/ `hdl/library/axi_fifo_lite/axi_fifo_lite.sv`):**

```
[peek] post-hard-rst TDFV=0x400 RDFO=0 ISR=0x01D00000
BYTES=2300  → sent=2300 axis_accepts=2300 recv=2300 anomalies=0 max_occ=1 PASS
BYTES=10000 → sent=10000 recv=10000 anomalies=0 max_occ=1 PASS
```

Same bridge/ACIA/guest/host, only the FIFO swapped. `TDFV` is correct, no
phantom, no wedge. **The wedge is an `axi_fifo_mm_s` bug for single-word
packets (`TLAST=1` per word) at 1024 depth** — the length-queue / `tready`
generation desyncs after ~1.9k packets even at low occupancy (`max_occ` ~400
tight-drain, `37–90` on board). Upgrading Vivado (2023.2 → 2024.x) keeps IP
`v4.3` (same `xpm_fifo` subcore, PG080 only adds ECC), so no fix expected.

**What was *not* the cause:**

* `axis_byte_bridge` overwrite (held `tx_stage_valid` while `tready=0` is
  protected by `tx_ready` stalling the ACIA — confirmed by `axis_accepts`
  matching `sent`).
* `acia68b50` deferred-TX (`tx_hold`) — correct by `TDRE` poll; TB now matches
  hardware `int32k.asm` `SER_TDRE=$02`.
* Host `read_available`/`RLR` handling — TB mirrors `axis_fifo.c` exactly.
* Reset sequence — hard, `driver` (10 cycles), and spec `settle` (poll
  `TRC|RRC`) all wedge identically.

**Fix wired (2026-08-26):** `hdl/library/axi_fifo_lite/` — ~120-line drop-in
with the same `s_axi`/`axi_str_txd`/`axi_str_rxd` ports and
`0x00/0x04/0x08/0x0C/0x10/0x14/0x18/0x1C/0x20/0x24/0x28` map, so
`axis_fifo.ko` and `z80_board/hw.py` are unchanged (instance keeps name
`axi_fifo_mm_s_0`, base `0x7C450000`). `hdl/projects/ebaz4205/system_bd.tcl`
now instantiates `axi_fifo_lite axi_fifo_mm_s_0` (no `C_USE_RX_CUT_THROUGH`
parameter). Limitations vs PG080 are intentional and documented in
`hdl/library/axi_fifo_lite/README.md`: fixed 1024 depth, packet = 1 word
(`TLR=4*N`, `RLR=4`), `C_USE_*` / `TDEST` / thresholds / `TLAST≠1` /
multi-word packets / bursts >128 without `TLR` / ECC / CDC / IRQ not supported
(see that README for full table). Alternative zero-RTL dodge is bridge v2
packing (4 B → 1 word, packet rate 4× lower → wedge beyond FORTH `words`
size).

## 6. Reproduction toolkit (as used in this session)

All experiments ran over `ssh ebaz` as plain python3; pattern:

```python
fd = os.open("/dev/axis_fifo_0x7c450000", os.O_RDWR | os.O_NONBLOCK)
m = mmap.mmap(os.open("/dev/mem", os.O_RDWR | os.O_SYNC), 0x1000,
              mmap.MAP_SHARED, mmap.PROT_READ | mmap.PROT_WRITE,
              offset=0x7C450000)          # RW map mandatory (see F10)
def rd(off): struct.unpack("<I", m[off:off+4])[0]   # 0x0C=TDFV 0x1C=RDFO 0x00=ISR
os.write(fd, bytes([ch, 0, 0, 0]))          # send one keystroke (drop-24 word)
```

Drainer variants compared: tight loop vs `poll(..., 20 ms)` + drain (exact
copy of `_term_session` semantics). CPU reset+run and FIFO resets done via
`/dev/mem` writes at `0x7C440000` GP0 (`bit1=reset pulse, bit3=run`) and
TDFR/RDFR (`0xA5`).

Remember when re-running: reset ⇒ must pulse RUN; wait out boot banner;
answer the Cold/Warm prompt before sending commands; nobody draining ⇒ you
are measuring capacity behavior, not the wedge.
