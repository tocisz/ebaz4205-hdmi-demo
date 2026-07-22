# bf1 Hardware Test Failure — Hypothesis

## Summary

Test 1 (`+.[]` increment-and-loop) passes on the EBAZ4205, but Test 2 (`,.` UART echo)
fails: no response from the UART, CPU reported at PC=3, halted=0.  This document
collects everything we know and what we think the root causes are.

> **UPDATE 2026-07-22 (evening): ROOT CAUSE FOUND — see §5.**
> The deployed H1 fix (`io_rx_ready = io_rd && !halted`) was itself broken:
> it lets `uart_phy` consume the RX byte on a `bf1_ce=0` cycle when the CPU
> does not capture it (~50% per byte → CPU stuck at `,`, PC=0, halted=0 —
> exactly the observed symptom).  Two further latent bugs were found in the
> same audit (§5.2, §5.3).  All fixed, proven by a new integration testbench
> that reproduces the exact hardware symptom for every broken variant.

---

## 1. Facts (known from observation)

| # | Fact | Source |
|---|------|--------|
| F1 | Test 1 (`+.[]`) passes: `data_ram[0]` reads back 1 | `test_bf1.sh` / `test_bf1.py` run on board |
| F2 | Test 2 (`,.` UART echo) fails: no byte appears on `/dev/ttyUL1` after sending `A` | same scripts, manual UART read after 2s timeout |
| F3 | After the failed Test 2 attempt, `GP0_IN` reports `halted=0, PC=3` | `test_bf1.sh` diagnostic output |
| F4 | The bitstream on the board (`/tmp/system_top.bit.bin`, 2 083 744 B) is the bootgen-converted version of the local `build/system_top.bit` (2 083 846 B, MD5 `a2122ca0…`) | `ls -la` + `md5sum` comparison; 102 B difference = BIT header |
| F5 | No uncommitted RTL changes exist — `git diff HEAD` is clean for all source files | `git diff HEAD -- hdl/library/bf1_soc/ hdl/library/uart_phy/ hdl/projects/ebaz4205/` |
| F6 | The local bitstream was built on **2026-07-22 10:17** (`stat system_top.bit`). No RTL changes after that date (`git log --since` returns nothing). | stat + git log |
| F7 | Simulation (xsim, `tb_bf1_soc.sv`) passes **all 6 tests**, including `,.` and `,+.` | earlier branch summary |
| F8 | In simulation, `bf1_soc`'s `io_rx_data`/`io_rx_valid` are **driven directly** by the testbench, **not** through `uart_phy`'s FIFO. The testbench asserts `io_rx_valid=1` before the CPU's `,` executes. | `tb_bf1_soc.sv` structure |

---

## 2. Hypothesis (what we think is wrong)

### H1 — `io_rx_ready` deadlock (RTL bug)  ★ **primary cause**

**Claim:** The signal `io_rx_ready` in `bf1_soc.v` is gated by `cpu_active`, creating a
deadlock when the CPU waits for UART input.

**Evidence (signal trace):**

```
bf1_soc.v line 248:  assign io_rx_ready = io_rd && cpu_active;
system_bd.tcl line 89:
  ad_connect bf1_soc_0/io_rx_ready  uart_phy_0/rx_accept_i
```

When the CPU executes instruction `,` (opcode 0xC0):

1. `io_rd = 1` (CPU decoding `,`)
2. `io_rx_valid = 0` (UART FIFO empty — no byte received yet)
3. `io_stall_rx = io_rd && !io_rx_valid = 1`
4. `cpu_active_raw = !halted && !io_stall_rx && !io_stall_tx = 0`
5. `cpu_active = cpu_active_raw && !prefetch && bf1_ce = 0`
6. **`io_rx_ready = 1 && 0 = 0`**  →  `uart_phy.rx_accept_i = 0`

Meanwhile in `uart_phy.v` (FIFO read logic, lines ~219–237):

```verilog
if (rx_valid && rx_accept_i) begin        // advance to next entry
    ...
end else if (!rx_valid && !fifo_empty && rx_accept_i) begin
    // Present data from FIFO
    rx_data  <= fifo_mem[...];
    rx_valid <= 1'b1;
    ...
end
// rx_accept_i=0 → neither branch fires → rx_valid stays 0 forever
```

When the real UART byte arrives:
7. RX FSM writes byte to `fifo_mem[]`
8. `fifo_empty` drops to 0
9. But `rx_accept_i = 0` (from step 6) → **the condition `!rx_valid && !fifo_empty && rx_accept_i` never triggers**
10. `rx_valid` stays 0 → `io_rx_valid` stays 0 → `io_stall_rx` stays 1 → **deadlock**

**Why simulation passes (F7):** The testbench asserts `io_rx_valid=1` directly on
`bf1_soc` before the `,` executes.  Step 3 never happens (`io_stall_rx=0`), so
the deadlock path is never exercised.

**Status — FIX APPLIED (2026-07-22):**

Changed to `assign io_rx_ready = io_rd && !halted;` (line 328 of `bf1_soc.v`).

Gating by `io_rd` allows the FIFO to present data when the CPU is executing
`,` (regardless of `cpu_active`).  Gating by `!halted` prevents FIFO data loss
if the CPU is halted mid-instruction (BRAM freezes → `io_rd` stays high →
without `!halted` the FIFO would accept and discard bytes while halted).

Full signal trace after fix:
1. CPU executes `,`: `io_rd=1`, `io_rx_valid=0`
2. `io_stall_rx=1`, `cpu_active=0` (CPU stalls)
3. **`io_rx_ready = 1 && !0 = 1`** → `rx_accept_i=1`
4. UART data arrives, RX FSM writes to FIFO
5. FIFO read block: `!rx_valid && !fifo_empty && rx_accept_i` → TRUE
6. `rx_valid=1`, next cycle: `io_rx_valid=1`, `io_stall_rx=0`
7. `cpu_active_raw=1`, CPU runs, consumes byte

The `!halted` gating also prevents a secondary issue: if the CPU is halted
at a `,` instruction (BRAM frozen, `io_rd` stuck high), the FIFO will not
advance past the received byte — it stays in the holding register until
the CPU resumes.

---

### H2 — PC not reset between test programs (test procedure bug) ★ **contributing factor**

**Claim:** After Test 1 halts the CPU and new code is loaded for Test 2, the
PS tool does not issue a RESET pulse.  The CPU therefore resumes execution
from wherever the PC was when HALT was asserted — NOT from address 0.

**Evidence:**

The test scripts do this between Test 1 and Test 2 (from `test_bf1.py`):

```python
w(GP0_OUT, 1); w(GP0_OUT, 0)     # HALT (edge on bit 0)
for i in range(4): cw(i, 0)      # clear code_ram[0..3]
cw(0, 0xC0); cw(1, 0xE0)        # load `,.` program
w(GP0_OUT, 8); w(GP0_OUT, 0)     # RUN (edge on bit 3)
# — NO RESET pulse (bit 1) —
```

After Test 1, the CPU was executing `[]` (infinite loop at addresses 2–3).
When HALT arrives, PC is in the range 3–4 (exact value depends on cycle timing).

When RUN is pulsed:

- `halted` clears to 0
- `prefetch` was already 0 (cleared once during initial reset)
- BRAM begins reading from `code_addr` (= `pcN`, the next-pc computed by the
  bf1 core from the frozen state) → `code_ra_dout` gets `code_ram[pcN]`
- Since all addresses ≥ 2 were cleared to 0, the CPU executes 0x00
  (a no-op: `>>>` with offset 0) forever
- **It never reaches address 0** where the `,.` program lives

This matches F3: after the timeout the diagnostic reads `PC=3`, which is where
the CPU was looping in the zeros.

**Why Test 1 passes:** After initial PL configuration, the PS7 `FCLK_RESET0_N`
properly resets the `bf1_soc` block.  `pc=0`, `halted=1`, `prefetch=1`.  The
test zeroes BRAMs, loads code, hits RUN.  The BRAM pre-fetches address 0
during prefetch.  CPU starts executing at PC=0.  All good for the first
program.

**Root cause — deeper than test scripts: `ctrl_reset` never reached the bf1 core.**

The `ctrl_reset` signal (edge-detected from PS register bit 1) in `bf1_soc` only
sets `halted <= 1` and re-arms `prefetch`.  It does **not** reset the bf1 core's
internal registers — PC, RSP, maddr, lj, lj_offset.  These registers are only
cleared by the hardware `resetq` pin (driven by `FCLK_RESET0_N` via the PS7),
which cannot be toggled from software (no `/dev/mem`-accessible GPIO drives it).

So even if the test script sends a RESET pulse (GP0 bit 1), the bf1 core's PC
stays at whatever value it had the moment HALT was asserted.  When RUN is
later pulsed, the CPU resumes from that old PC, not from address 0.

**Fix applied (2026-07-22):** Added a `ctrl_reset_i` input to `bf1.v` that
synchronously resets the core registers (same zeroing as the hardware reset)
independently of `cpu_active`.  Connected to the edge-detected `ctrl_reset`
signal from `bf1_soc`.

```verilog
// bf1.v register block — ctrl_reset_i is a 1-cycle pulse from the PS
if (!resetq) begin
    { pc, rsp, maddr, lj, lj_offset } <= 0;
end else if (ctrl_reset_i) begin
    { pc, rsp, maddr, lj, lj_offset } <= 0;   // ← new
end else if (cpu_active) begin
    { pc, rsp, maddr, lj, lj_offset } <= { pcN, rspN, maddrN, ljN, lj_offsetN };
end
```

**Why two reset signals?**

*   `resetq` — hardware pin (async, active low).  Fires once after PL
    configuration.  Resets everything unconditionally.
*   `ctrl_reset_i` — PS register pulse (sync, active high).  Triggerable
    from software via `devmem` writes to GP0 bit 1.  Resets the same
    registers but leaves other state (BRAM contents, UART FIFO) intact.

This is the cleanest approach because the PS cannot re-assert the hardware
`resetq` pin — `FCLK_RESET0_N` is managed by the PS7 boot ROM and has no
software-accessible toggle.

With this fix, test scripts must use the full sequence:
```python
w(GP0_OUT, 1); w(GP0_OUT, 0)    # HALT (bit 0)
w(GP0_OUT, 2); w(GP0_OUT, 0)    # RESET (bit 1) — now also resets PC
# load code
w(GP0_OUT, 8); w(GP0_OUT, 0)    # RUN (bit 3)
```

---

### H3 — uart_phy synchronizer not reset after PL config (RTL bug) ★ **auxiliary**

**Claim:** The double-flop synchronizer in `uart_phy` (`uart_in_sync0`/`uart_in_sync1`)
has no reset. If the registers power up as `0` instead of `1` after PL configuration,
the RX FSM detects a false falling edge, reads garbage, and the CPU misses real UART data.

**Evidence:**

```verilog
// uart_phy.v (before fix)
reg uart_in_sync0, uart_in_sync1;
always @(posedge clk) begin
    uart_in_sync0 <= uart_rx_i;
    uart_in_sync1 <= uart_in_sync0;
end
```

On 7-series FPGAs, register power-up state is unpredictable. The UART line is
idle-high (`1`), but if the synchroniser registers power up as `0`, the FSM sees
a falling edge on the first clock cycle and initiates a spurious reception.

**Fix applied (2026-07-22):**

```verilog
always @(posedge clk) begin
    if (reset) begin
        uart_in_sync0 <= 1'b1;  // UART idle state = high
        uart_in_sync1 <= 1'b1;
    end else begin
        uart_in_sync0 <= uart_rx_i;
        uart_in_sync1 <= uart_in_sync0;
    end
end
```

**Why this matters for hardware:** In simulation, all registers initialise to `X`
or `0` deterministically — the testbench accounts for this. On real hardware
after PL reconfiguration (via `fpgautil`), BRAM content is undefined and
flip-flops may power up in any state. The synchronizer fix ensures the RX FSM
starts in a known idle state regardless of power-up values.

---

### H4 — Interaction between H1, H2, and H3

All three must be fixed for Test 2 to pass on hardware:

| Fix | If missing | Symptom |
|-----|-----------|---------|
| H1 (`io_rx_ready`) | CPU deadlocks on first `,` after UART byte arrives | PC stuck at address 0 (the `,` instruction) |
| H2 (PC reset) | CPU executes old/zeroed code from wrong address | PC advancing through cleared memory, never reaching address 0 |
| H3 (synchronizer reset) | False start bit detected after PL config | FIFO gets garbage byte, `rx_valid` stuck at 1, CPU reads junk |

---

## 5. Root-cause audit (2026-07-22, evening)

Trigger: the user pointed out that UART was proven working on this board with
`echo_char` (266/266 HW tests) and asked whether some changes were unnecessary.
Full re-audit of the uncommitted diff vs. commit `6fc5bfc0d`:

### 5.1 H1 fix was wrong — the actual root cause of the current failure ★★★

`uart_phy`'s RX FIFO is a **holding-register** design:

```verilog
if (rx_valid && rx_accept_i) begin ... end              // consume + advance
else if (!rx_valid && !fifo_empty && rx_accept_i) ...   // present (rx_valid<=1)
```

It **consumes the presented byte at ANY posedge where `rx_valid && rx_accept_i`**,
but the bf1 core captures `io_din` only at posedges where `cpu_active=1` — and
`cpu_active = cpu_active_raw && !prefetch && bf1_ce` with `bf1_ce` toggling every
cycle (half-speed enable).  The H1 gate `io_rx_ready = io_rd && !halted` is high
during the *whole* `,` instruction, including `bf1_ce=0` cycles:

| Cycle | bf1_ce | cpu_active | rx_valid | rx_accept_i | Result |
|-------|--------|-----------|----------|-------------|--------|
| N   | — | 0 (stalled) | 0 | 1 | byte arrives → presented at posedge N+1 |
| N+1 | 0 | 0 | 1 | **1** | **uart_phy consumes byte; CPU does NOT capture → BYTE LOST** |
| N+2 | — | 0 | 0 | 1 | `io_stall_rx=1` again → CPU stuck at `,` forever |

~50% probability per byte, set by the free-running `bf1_ce` phase at arrival —
explains the intermittent HW behaviour (sometimes 0x41 echoed, usually stuck at
PC=0, halted=0, data_ram unchanged).

**Fix:** `assign io_rx_ready = io_rd && !halted && (!io_rx_valid || cpu_active);`
- `!io_rx_valid` term: keeps presentation possible while stalled (breaks the
  H1 deadlock — that part of the H1 analysis was correct),
- `cpu_active` term: consumption only at the actual capture edge (no race),
- `!halted`: byte held while halted (unchanged).

### 5.2 Latent: registered `io_stall_tx` drops back-to-back `.` bytes

```verilog
// OLD: stall engages one cycle AFTER the execute edge — too late
end else if (io_wr && cpu_active && !io_tx_ready) io_stall_tx <= 1;
```

With the (correct) single-cycle `io_tx_valid` strobe, a `.` executing while
`uart_phy` is busy fired the strobe into a busy FSM (ignored) and advanced the
PC anyway — the byte vanished.  Any two `.` within one byte-time (~8680 clk)
→ second byte lost (`+..`, Hello-World-style programs).  Never seen: unit TB
hardwires `io_tx_ready=1`; HW tests space `.` by human-speed input.

**Fix:** combinational `assign io_stall_tx = io_wr && !io_tx_ready;` — the CPU
holds at `.` while TX is busy; the strobe can only fire into an idle uart_phy.

### 5.3 Latent: inverted `tx_busy → io_tx_ready` in system_bd.tcl

`ad_connect uart_phy_0/tx_busy bf1_soc_0/io_tx_ready` — `io_tx_ready` is
ready-when-high (`!io_tx_ready` = stall), `tx_busy` is busy-when-high.
Masked so far because the registered `io_stall_tx` never blocked the strobe;
would deadlock with §5.2's fix.

**Fix:** added `tx_ready` output to `uart_phy` (= `!tx_busy`), connected
`uart_phy_0/tx_ready → bf1_soc_0/io_tx_ready`.

### 5.4 Necessity assessment of earlier changes (user's question)

| Change | Verdict |
|--------|---------|
| H2 `ctrl_reset_i` + prefetch re-arm | **NECESSARY** — the actual cause of the *original* PC=3 failure: CPU resumed from stale PC in zeroed memory, never ran the new program |
| `io_tx_valid` single-cycle strobe | **NECESSARY** — old level-based version re-armed `tx_start` when TX completed → every byte transmitted twice |
| H1 `io_rx_ready` change | **WRONG GATE** — deadlock premise correct; `!halted` alone introduced the §5.1 race. Replaced by the 3-term version |
| H3 synchroniser reset (uart_phy) | **UNNECESSARY in practice** — RX FSM needs a baud tick (108 clk) to leave IDLE; the synchroniser settles to idle-high within 2 clk of config. Harmless; kept as defensive RTL |
| `bf1_ce` half-speed enable | **NECESSARY** — ALU path ~11.5–12.5 ns > 10 ns clock |

### 5.5 Proof: new integration testbench `tb_bf1_soc_uart.sv`

`bf1_soc` + `uart_phy` wired exactly as `system_bd.tcl`, real 115200-baud
serial frames (the unit TB drives `io_rx_valid` directly — F8 — and cannot
see any of these bugs).  Tests: `,.` echo + no-spurious-TX, `,[.,]` echo loop
with phase-varying gaps, `+..` back-to-back output.

| RTL variant | Result |
|-------------|--------|
| Original (`io_rx_ready = io_rd && cpu_active`) | **all echo tests timeout, PC=0** (deadlock) |
| H1 (`io_rd && !halted`) — the deployed one | **phase-dependent timeouts, PC=0, halted=0 — exact HW symptom** |
| Fixed RX + registered `io_stall_tx` | **`+..` second byte dropped (timeout, PC=3)** |
| All fixes | **8/8 PASS** |

Regression: `bf1_soc` unit 6/6, `uart_phy` 490/490, `echo_char` 490/490 — all pass.

---

## 3. Applied Fixes

### RTL Fixes (all applied 2026-07-22)

| Bug | File | Change |
|-----|------|--------|
| H1 — `io_rx_ready` deadlock | `bf1_soc.v` | `io_rx_ready = io_rd && cpu_active` → `io_rd && !halted` → **`io_rd && !halted && (!io_rx_valid \|\| cpu_active)`** (§5.1) |
| H1 — prefetch re-arm | `bf1_soc.v` | `ctrl_reset` also re-arms `prefetch <= 1` |
| H2 — PC reset via PS | `bf1.v` | Added `ctrl_reset_i` port; resets PC, RSP, maddr, lj, lj_offset synchronously |
| H2 — connection | `bf1_soc.v` | Wired `ctrl_reset` → `bf1_inst.ctrl_reset_i` |
| H3 — synchronizer reset | `uart_phy.v` | Initialise `uart_in_sync0/1` to 1'b1 on reset (defensive; §5.4) |
| §5.2 — TX drop | `bf1_soc.v` | Registered `io_stall_tx` → combinational `io_wr && !io_tx_ready` |
| §5.3 — inverted ready | `uart_phy.v`, `system_bd.tcl` | New `tx_ready` output; `tx_ready → io_tx_ready` |
| TB — handshake | `tb_bf1_soc.sv` | `uart_send` waits for the accept edge (valid && ready at posedge) |
| TB — integration | `tb_bf1_soc_uart.sv` | **New** — bf1_soc+uart_phy over real serial (§5.5) |

### Build Status

| Step | Status |
|------|--------|
| uart_phy IP rebuild | ✅ Done (2026-07-22 12:09) |
| bf1_soc simulation | ✅ All 6 tests pass |
| uart_phy simulation | ✅ All 490 checks pass |
| echo_char simulation | ✅ All 490 checks pass |
| Full project bitstream | ✅ Built (2026-07-22) |

### Hardware verification (2026-07-22, evening) — ALL PASS ✅

Deployed via **boot image** (`BOOT.bin` + `system_top.bit.bin` replaced on the
SD boot partition, `make sdimg`, reboot) instead of runtime `fpgautil` — after
runtime reconfig the uartlite driver cannot re-bind (IRQ mapping is stale,
`error -ENXIO: IRQ index 0 not found`), while a boot-time FSBL configuration
+ boot-time overlay apply + boot-time driver probe is clean.

`test_bf1_v4.py` — **7/7 PASS, three consecutive runs:**

| Test | Result |
|------|--------|
| `+.[]` increment-and-loop | ✅ data_ram[0]==1 |
| `,.` echo ×3 ('A','B','C') — *the previously failing case* | ✅ all echoed |
| `,+.` increment echo (0x42→0x43) | ✅ |
| `+..` back-to-back output (§5.2 TX-busy stall) | ✅ two bytes 0x01,0x01 |
| `,[.,]` echo loop "Hello" with varied inter-byte timing (bf1_ce phases) | ✅ all echoed |

Note: tests zero `data_ram[0]` via GP1 before `+`-based programs — BRAM
content is uninitialised after boot-time PL configuration.

### Remaining

| What | Status |
|------|--------|
| Rebuild IPs (uart_phy, echo_char, bf1_soc) | ✅ Done |
| Full project bitstream (clean timing, WNS +2.38 ns) | ✅ Done |
| `make sdimg`, replace BOOT.bin + bit.bin on SD, reboot | ✅ Done |
| HW test suite (`test_bf1_v4.py`) | ✅ 7/7 ×3 runs |
| Commit the fixes (hdl submodule + docs) | ⏳ awaiting user |

---

## 4. References

- `hdl/library/bf1_soc/bf1_soc.v` — `io_rx_ready` assignment (line 248), clock gating, prefetch
- `hdl/library/uart_phy/uart_phy.v` — FIFO read logic (lines 219–237)
- `hdl/projects/ebaz4205/system_bd.tcl` — `ad_connect` of `io_rx_ready` → `rx_accept_i` (line 89)
- `/tmp/test_bf1.py` on board — test script sequence (no RESET between tests)
- `/tmp/test_bf1.sh` on board — same sequence in shell
- `tb_bf1_soc.sv` — simulation testbench (direct RX drive, bypasses uart_phy)
