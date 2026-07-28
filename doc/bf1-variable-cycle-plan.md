# bf1 Variable-Cycle FSM — Implementation Plan

## 1. Motivation

The current `bf1` core is a single-cycle design that survives 100 MHz through two timing workarounds:

- `bf1_ce` — a free-running clock enable that halts register updates every other cycle
- `set_multicycle_path -setup 2` in the XDC — relaxes timing to 20 ns for all paths

This works but is a blunt instrument. The combinational critical path from BRAM output
through the ALU and back to BRAM input still exists — it's just given extra time.

The longest path is the **long jump** ALU (`lj`), which performs a full 13-bit addition:
`pc + $signed({lj_offset[4:0], insn[7:0]})`. All other instructions use a sign-extended
6-bit offset that resolves much faster.

**Key observation**: The long jump is a two-instruction sequence (prefix byte + jump byte).
The prefix's EXEC cycle currently does nothing useful for the ALU — it just sets `ljN = 1`
and throws away ALU result. We can **repurpose this free cycle** to pre-compute the lower
part of the addition, splitting the 13-bit carry chain across two instructions.

**Goal**: Replace the monolithic timing workaround with a **pipelined long jump** that
reduces the critical path from a 13-bit addition to an 8-bit addition (the mid computation),
so all instructions (including long jump) run in 2 cycles per instruction (CPI=2) without
multicycle constraints.

---

## 2. Carry Chain Analysis

### 2.1 The Fundamental Problem

Addition's critical path is carry propagation from LSB to MSB. To split across two cycles the
**lower bits must be computed first**, because their carry-out feeds the upper bits.

```
Full addition:     pc[12:0] + offset[12:0]

Carry chain:       bit0 → bit1 → ... → bit11 → bit12
                                          ↑ needs carry from bit11
                               bit11 → bit12
                                          ↑ needs carry from bit10
                               ...
                                          ↑ needs carry from bit0   ← start here first!
```

### 2.2 Current Encoding — Backwards for Pipelining

The current long-jump encoding splits the 13-bit offset as:

```
offset bits: [12][11][10][9][8] [7][6][5][4][3][2][1][0]
              ← prefix (5) →     ←──── jump insn (8) ────→
              ↑ upper             ↑ lower (carry from bit7 needed for bit8)
```

The prefix gives **upper** bits but those can't resolve until the carry from the **lower**
bits (in the jump byte) arrives — exactly backwards for pipelining.

### 2.3 Re-encoded Format — Lower Bits in Prefix

Swap the placement within the 13-bit offset:

```
offset bits: [12][11][10][9][8][7][6][5][4][3] [2][1][0]
              ←──── jump insn (8) ──────────→   ←prefix(5)→
              ↑ upper (step 2)                   ↑ lower (step 1)
```

| Byte | Field | Current encoding | New encoding |
|---|---|---|---|
| N (prefix) | `insn[7:5]` | `101` | `101` (unchanged) |
| N (prefix) | `insn[4:0]` | offset[12:8] **(upper 5)** | offset[4:0] **(lower 5)** |
| N+1 (jump) | `insn[7:0]` | offset[7:0] (lower 8) | offset[12:5] **(upper 8)** |

Both are valid 13-bit signed offsets; only the bit assignment between the two bytes changes.

```verilog
// Current:
offset[12:0] = {prefix_insn[4:0],  jump_insn[7:0]};

// New:
offset[12:0] = {jump_insn[7:0],    prefix_insn[4:0]};
```

### 2.4 Two-Step Pipelined Addition

**Step 1 — prefix EXEC** (5-bit addition, resolves in <2 ns at 100 MHz):

```verilog
wire [5:0] low_sum = pc[4:0] + prefix_insn[4:0];   // 5-bit → 1 carry bit
```

Stored in pipeline registers:

```verilog
pj_low     <= low_sum[4:0];    // low 5 bits of result
pj_carry5  <= low_sum[5];      // carry into bit 5
pj_pc_high <= pc[12:5];        // upper 8 bits of PC, saved for step 2
pj_valid   <= 1;               // pipeline data is ready
```

**Step 2 — jump EXEC** (8-bit addition with known carry-in, ~4-5 ns):

```verilog
wire [7:0] mid_sum = pc[12:5] + jump_insn + pj_carry5;

// Assemble full 13-bit result
pcN = {mid_sum[7:0], pj_low[4:0]};
```

**Why no sign extension?** The 13-bit offset `{jump_insn[7:0], prefix_insn[4:0]}` is treated
as a signed value by `$signed()` in the reference, but the pipelined computation treats
`jump_insn` (the upper 8 bits) as unsigned. This introduces an "error" of `256 × sign_bit`,
which is exactly ±8192. In 13-bit modular arithmetic (`2¹³ = 8192`), this error wraps to zero:

```
When jump_insn[7]=0 (positive offset):
  unsigned:    pc[12:5] + jump_insn + carry
  signed ref:  pc[12:5] + unsigned(jump_insn) + carry   [same — no error]

When jump_insn[7]=1 (negative offset):
  unsigned:    pc[12:5] + jump_insn + carry
  signed ref:  pc[12:5] + (jump_insn - 256) + carry    [error = 256]
  But result[12:0] discards bit 13, so 256 = 2⁸ wraps in 13-bit space
  via {jump_insn - 256, prefix} = {jump_insn, prefix} - 8192 (mod 8192)
```

No explicit sign extension needed — modular arithmetic handles it.

### 2.5 Carry Chain Comparison

| Approach | Step 1 (prefix EXEC) | Step 2 (jump EXEC) |
|---|---|---|
| **Current** (single-cycle) | — | 13-bit carry from GND |
| **Pipelined** (new encoding) | 5-bit carry (resolves in <2 ns, free cycle) | **8-bit carry from known carry-in** |

The 5-bit carry in step 1 resolves essentially immediately (one CARRY4 tile = 4 bits).
The 8-bit carry in step 2 starts from a valid carry-in at bit 5, so it only needs to
propagate through bits 5..12 — about 8 CARRY4 carry-mux delays (~120 ps in 7-series).
Even with input LUTs and output MUX, this easily fits in 10 ns at 100 MHz.

---

## 3. Architecture

### 3.1 Two-State FSM for All Instructions

Since the pipelined long jump reduces the critical path sufficiently, the 3-cycle
EXTRA state is unnecessary. A simple 2-state FSM serves all instructions:

```
FETCH ──→ EXEC ──→ FETCH    (every instruction, CPI=2)
```

| State | Purpose |
|---|---|
| `FETCH` | Addresses stable (`code_addr = pc`, `mem_addr = maddr`). BRAM outputs settle. Registers hold. |
| `EXEC` | ALU computes from stable BRAM outputs. Registers capture and BRAM prefetches at end. |

### 3.2 Key Signal: `complete`

```verilog
localparam FETCH = 1'b0;
localparam EXEC  = 1'b1;

reg state, stateN;
assign stateN = (state == FETCH) ? EXEC : FETCH;
assign complete = (state == EXEC);
```

Gates **all** register captures and BRAM prefetches:

```verilog
// bf1.v: register capture
always @(posedge clk) begin
  if (cpu_active && complete) begin
    // capture ALU results, update pipeline registers, etc.
  end
end

// bf1_soc.v: BRAM output capture
always @(posedge clk_i) begin
  if (prefetch || (cpu_active && complete))
    code_ra_dout <= code_ram[...];
end
```

### 3.3 CPI per Instruction

| Instructions | Cycles | Notes |
|---|---|---|
| `< > - + , . [ ]` | 2 | Unchanged from current effective CPI (with `bf1_ce`) |
| Long jump prefix | 2 | Prefix EXEC computes 5-bit addition (was doing nothing) |
| Long jump jump | 2 | Jump EXEC computes 8-bit addition with known carry-in |
| Any long jump pair | 4 | Same total as current effective CPI |

**No instruction takes more than 2 cycles.**

---

## 4. Changes to `bf1.v`

### 4.1 New Port

```verilog
output wire complete    // high during EXEC; gates BRAM prefetch in bf1_soc
```

### 4.2 Pipeline Registers

```verilog
reg [4:0]  pj_low;         // low 5 bits of jump target
reg        pj_carry5;       // carry out of low 5-bit addition
reg [7:0]  pj_pc_high;     // pc[12:5] at time of prefix
reg        pj_valid;        // pipeline data is valid
```

Written during **prefix** EXEC, read during **jump** EXEC.
Total: 5 + 1 + 8 + 1 = 15 flops.

No `pj_offset_high` register — the jump byte (`insn`) is read directly from the BRAM
output during jump EXEC. The BRAM output is stable throughout EXEC because `complete=0`
during FETCH prevents the output register from toggling.

### 4.3 State Machine

Replace the current single-cycle (always execute) with the 2-state FSM:

```verilog
localparam FETCH = 1'b0;
localparam EXEC  = 1'b1;
reg state, stateN;

assign stateN = (state == FETCH) ? EXEC : FETCH;
assign complete = (state == EXEC);
```

### 4.4 Address Outputs

```verilog
assign code_addr = (state == EXEC) ? pcN : pc;
assign mem_addr  = (state == EXEC) ? maddrN : maddr;
```

### 4.5 Write Strobe Gating

In the "after ALU" casez block:

```verilog
4'b0_01?: begin mem_dout = alu_c[7:0]; mem_wr = complete; end
4'b0_110: begin  mem_wr = complete; io_rd = complete; end
4'b0_111: begin   io_wr = complete; end
4'b0_100: begin
  do_jump_or_ret = 1;
  do_jump = |insn[4:0];
end
4'b1_???: begin
  do_jump_or_ret = 1;
  do_jump = 1;
end
```

No write ever fires during FETCH (`complete=0`) or during an unstable ALU computation.

### 4.6 ALU — Long Jump Removed from Shared Path

The long jump no longer uses the shared ALU. The `3'b1_??` branch in the "before ALU"
casez becomes a no-op (or is simply removed — the default `X` propagation suffices).

Optionally, the shared ALU can be **reused** to compute the prefix's low 5-bit addition
instead of using a standalone adder (see §4.6a).

### 4.6a ALU Reuse for Prefix Step (Optional)

Instead of a standalone `wire [5:0] low_sum = pc[4:0] + insn[4:0]`, the existing ALU
can compute the same result. Set up the ALU with zero-extended prefix (Style B):

```verilog
// In "before ALU", inside the existing casez:
3'b0_10: begin
  if (!insn[5]) begin
    // [ (insn[7:5]=100): full jump computation
    alu_a = {2'b0, pc};
    alu_b = $signed({insn[5:0], 7'b0}) >>> 7;
  end else begin
    // prefix (insn[7:5]=101): low 5-bit addition for pipeline step 1
    alu_a = {2'b0, pc};
    alu_b = {8'b0, insn[4:0]};   // zero-ext prefix_low to 13 bits
  end
end
```

Then extract the carry by XOR-unwinding the full-adder equation:

```verilog
pj_low    <= alu_c[4:0];              // = (pc[4:0] + insn[4:0]) mod 32
pj_carry5 <= alu_c[5] ^ pc[5];        // = carry out of bit 4
```

The XOR recovers the hidden carry-in because `alu_c[5] = pc[5] + alu_b[5] + carry5`,
and `alu_b[5] = 0` for the zero-ext prefix. Adding LUTs: zero (reuses existing ALU).

### 4.7 PC Calculation — Pipelined Long Jump

```verilog
always @ (do_jump_or_ret, do_jump, pc, mem_din, rsp, rst0, alu_c,
          state, lj, pj_low, pj_carry5, pj_pc_high, pj_valid, insn)
begin
  pcN   = pc + 1'b1;   // default: next instruction
  rspN  = rsp;
  rstkW = 0;

  if (state == FETCH) begin
    pcN = pc;           // freeze during fetch
  end else begin
    // ── Pipelined long jump (lj=1, pj_valid=1) ──
    if (lj && pj_valid) begin
      wire [7:0] mid = pj_pc_high + insn + pj_carry5;
      pcN = {mid[7:0], pj_low[4:0]};
    end
    // ── Normal branch ([ ]) ──
    else if (do_jump_or_ret) begin
      if (do_jump) begin // [
        if (mem_din != 0) begin
          rspN = rsp + 1'b1;
          rstkW = 1;
        end else begin
          pcN = alu_c[12:0];
        end
      end else begin // ]
        if (mem_din != 0) pcN = rst0;
        else rspN = rsp - 1'b1;
      end
    end
    // else: < > - + , . → pcN stays at pc+1 (default)
  end
end
```

The `lj && pj_valid` guard ensures the pipelined path fires only during the one instruction
after a prefix. The `lj` flag is set by the prefix's `ljN=1` and cleared (via `ljN=0`
default) during the very next EXEC — so it's high for exactly one instruction.

No `mid[9]` or `mid[8]` bits to inspect: the 13-bit result truncates naturally, and
modular arithmetic guarantees correctness (§2.4).

### 4.8 Sequential Block — Pipeline + Main Registers

```verilog
always @(negedge resetq or posedge clk) begin
  if (!resetq) begin
    state <= FETCH;
    pj_low <= 0; pj_carry5 <= 0; pj_pc_high <= 0; pj_valid <= 0;
    { pc, rsp, maddr, lj, lj_offset } <= 0;
  end else if (ctrl_reset_i) begin
    state <= FETCH;
    pj_low <= 0; pj_carry5 <= 0; pj_pc_high <= 0; pj_valid <= 0;
    { pc, rsp, maddr, lj, lj_offset } <= 0;
  end else if (cpu_active) begin
    state <= stateN;
    if (complete) begin
      // ── Long jump pipeline step 1 (prefix) ──
      if (!lj && (insn[7:5] == 3'b101)) begin
        wire [5:0] low = pc[4:0] + insn[4:0];
        pj_low     <= low[4:0];
        pj_carry5  <= low[5];
        pj_pc_high <= pc[12:5];
        pj_valid   <= 1;
      end else if (lj) begin
        // Pipeline data consumed by jump instruction
        pj_valid <= 1'b0;
      end

      // ── Main register capture ──
      { pc, rsp, maddr, lj, lj_offset }
      <= { pcN, rspN, maddrN, ljN, lj_offsetN };
    end
  end
end
```

**Pipeline timing**: `pj_valid` is set to 1 during the prefix's EXEC posedge. During the
immediately following instruction (the jump), `lj=1` and `pj_valid=1`, triggering the
pipelined path. After the jump's EXEC posedge, `pj_valid` clears. If the instruction
after the prefix is not the jump (malformed program), the `lj` flag is cleared by the
default `ljN = 0` in the "after ALU" block, limiting damage to one bad PC computation.

### 4.9 Sensitivity List Updates

| Block | Add to sensitivity list |
|---|---|
| `before ALU` | `state` |
| `after ALU` | `state` |
| `calculate pc` | `state`, `pj_low`, `pj_carry5`, `pj_pc_high`, `pj_valid` |

### 4.10 Complete New Port List

```verilog
module bf1 (
   input  wire clk,
   input  wire resetq,
   input  wire cpu_active,       // clock enable: 0 = stall
   input  wire ctrl_reset_i,     // synchronous reset from PS

   output wire [`DADDR_WIDTH-1:0] mem_addr,
   output reg  mem_wr,
   output reg  [`DATA_WIDTH-1:0] mem_dout,
   input  wire [`DATA_WIDTH-1:0] mem_din,

   output reg  io_wr,
   output reg  io_rd,
   input  wire [`DATA_WIDTH-1:0] io_din,
   output wire [`DATA_WIDTH-1:0] io_dout,

   output wire [`CADDR_WIDTH-1:0] code_addr,
   input  wire [7:0] insn,

   output wire [`DEPTH-1:0] _rsp,
   output wire [`CADDR_WIDTH-1:0] pc_debug,

   output wire complete            // NEW
);
```

---

## 5. Changes to `bf1_soc.v`

### 5.1 Wire the `complete` Signal

```verilog
wire complete;

bf1 #() bf1_inst (
    // ... existing ports ...
    .complete(complete)            // NEW
);
```

### 5.2 Remove `bf1_ce` Clock Divider

```verilog
// OLD: assign cpu_active = cpu_active_raw && !prefetch && bf1_ce;

// NEW: no half-speed divider; FSM handles 2-cycle pipelining
assign cpu_active = cpu_active_raw && !prefetch;
```

### 5.3 Gate BRAM Output Registers with `complete`

```verilog
// Code RAM Port A — capture during EXEC
always @(posedge clk_i) begin
  if (prefetch || (cpu_active && complete))
    code_ra_dout <= code_ram[prefetch ? 13'd0 : code_addr];
end

// Data RAM Port A
always @(posedge clk_i) begin
  if (prefetch || (cpu_active && complete))
    data_ra_dout <= data_ram[mem_addr];
  if (mem_wr)
    data_ram[mem_addr] <= mem_dout;
end
```

This ensures:
- **FETCH** (`complete=0`): BRAM outputs hold → ALU sees stable values throughout FETCH
- **EXEC** (`complete=1`): BRAM prefetches at computed `pcN`/`maddrN` for next cycle

The BRAM output is captured at the EXEC→FETCH transition and held stable for the
entire FETCH cycle, giving the ALU a full 20 ns to settle before its own results
are captured at the end of EXEC.

### 5.4 Remove the Global Multicycle Constraint

In `bf1_soc_constr.xdc`, delete the global set_multicycle_path. Replace with nothing,
or keep as comments. Optionally add a targeted safety net for only the bf1_inst
hierarchy (§6.2).

### 5.5 IO Stall Interaction

Already correct — `io_stall_rx`/`io_stall_tx` gate `cpu_active_raw`, which drops
`cpu_active`, freezing the FSM via `if (cpu_active)` in the sequential block.

---

## 6. Timing Analysis

### 6.1 Register-to-Register Paths

All sequential elements are clock-enabled by the same `complete` signal, active every 2nd
edge. Both launch and capture happen at EXEC edges, giving 20 ns between them at 100 MHz.

| Path | Available |
|---|---|
| BRAM dout → CPU register | 20 ns |
| BRAM dout → BRAM address | 20 ns |
| CPU register → BRAM address | 20 ns |
| CPU register → CPU register | 20 ns |

### 6.2 Targeted Multicycle Safety Net

If Vivado fails to correlate the `complete` enables, add:

```tcl
set_multicycle_path -setup 2 \
  -from [get_cells -hier -filter {NAME =~ *bf1_inst* && IS_SEQUENTIAL}] \
  -to   [get_cells -hier -filter {NAME =~ *bf1_inst* && IS_SEQUENTIAL}]
set_multicycle_path -hold 1 \
  -from [get_cells -hier -filter {NAME =~ *bf1_inst* && IS_SEQUENTIAL}] \
  -to   [get_cells -hier -filter {NAME =~ *bf1_inst* && IS_SEQUENTIAL}]
```

Constrains only `bf1_inst`, not PS interface logic.

---

## 7. Verification

### 7.1 Arithmetic Test Bench

A dedicated test bench (`tb_longjump_pipeline.sv`) verifies the pipelined computation
against a direct `$signed()` reference. It covers:

- **1017 tests**: 1000 random (full 13-bit PC range × 32 prefix values × 256 jump values)
  + 17 corner cases (zero, max, sign transitions, carry chains, overflow/underflow)
- All four calculations match: reference, standalone-adder pipeline, and both ALU-reuse
  variants (Style A and Style B)

The test bench confirmed that `mid_sum = pc[12:5] + jump_insn + carry5` (8-bit, unsigned)
gives the same 13-bit result as `pc + $signed({jump_insn, prefix_insn[4:0]})` — the
modular wraparound cancels the sign-extension error exactly.

### 7.2 Simulation Checklist

1. **Reset**: After `prefetch`, FSM enters FETCH. First EXEC executes `code_ram[0]`.
2. **Normal instructions**: CPI=2 for `< > - + , .`.
3. **Branch `[ ]`**: Forward/backward loops, CPI=2.
4. **Long jump**: Prefix EXEC sets `pj_low`/`pj_carry5`/`pj_pc_high`. Jump EXEC computes
   correct `pcN`. CPI=2 for both (4 cycles total).
5. **Pipeline clear**: After long jump, `pj_valid=0`; next instruction uses normal path.
6. **IO stalls**: `cpu_active` drops → FSM freezes → resumes correctly.
7. **`ctrl_reset_i`**: Clean reset from any state.

### 7.3 Implementation Targets

- **Resources**: +15 flops (pipeline) + 1 flop (FSM). Negligible.
- **Timing**: All paths meet 100 MHz.
- **If pipelined path fails**: The 8-bit addition is extremely unlikely to fail at 100 MHz.
  If it does, add one pipeline register between the adder and `pcN` MUX, or use the
  3-cycle EXTRA fallback (§10).

---

## 8. Re-encoding Compatibility

The software assembler must emit the new bit assignment:

```python
# Old:
prefix_byte = 0b101_00000 | (offset >> 8)   # upper 5
jump_byte   = offset & 0xFF                  # lower 8

# New:
prefix_byte = 0b101_00000 | (offset & 0x1F) # lower 5
jump_byte   = (offset >> 5) & 0xFF           # upper 8
```

The total offset value and range are unchanged.

---

## 9. Future Work

### 9.1 Pre-computed ±1

Replace ALU for `< > - +` with dedicated increment/decrement logic to eliminate the
adder from ~90% of instructions.

### 9.2 Branch Jump Table

Pre-compute matching-bracket addresses into a small BRAM, eliminating the ALU + stack
from `[ ]`.

### 9.3 Rollback

Three files to revert: `bf1.v`, `bf1_soc.v`, `bf1_soc_constr.xdc`.

---

## 10. Appendix: 3-Cycle EXTRA Fallback

If the 8-bit mid addition somehow fails timing, add a 3rd state:

```verilog
localparam [1:0] FETCH = 2'b00, EXEC = 2'b01, EXTRA = 2'b10;

always @(*) begin
  case (state)
    FETCH: stateN = EXEC;
    EXEC:  stateN = is_long_jump ? EXTRA : FETCH;
    EXTRA: stateN = FETCH;
    default: stateN = FETCH;
  endcase
end

assign complete = (state == EXEC && !is_long_jump) || (state == EXTRA);
```

CPI for long jump becomes 3 (pair=5), still competitive with current effective CPI of 4
under `bf1_ce`.
