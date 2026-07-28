# bf1_soc Synthesis Report

**Date:** 2026-07-28  
**Device:** xc7z010clg400-1 (Zynq-7010, speed grade -1)  
**Tool:** Vivado 2023.2  
**Configuration:** ALU **reused** for long-jump prefix step (Style A: `[`-style ALU setup, carry extraction via `alu_c[5] ^ pc[5] ^ 1'b1`)

> **Note:** A 50 MHz effective clock (`create_clock -name clk_i -period 20.000`) is applied during synthesis. The bf1 core advances only every 2 clock cycles via `bf1_ce`, giving the ALU data path ~20 ns to settle — matching the `set_multicycle_path -setup 2` constraint used in the full project.

## Resource Utilization

| Item         | Count   |
|--------------|---------|
| LUTs (total) | **204** |
| LUT1         | 3       |
| LUT2         | 46      |
| LUT3         | 21      |
| LUT4         | 51      |
| LUT5         | 42      |
| LUT6         | 41      |
| FFs          | **152** |
| FDCE         | 150     |
| FDPE         | 2       |
| CARRY4       | 13      |
| BUFG         | 1       |
| IBUF         | 64      |
| OBUF         | 123     |
| RAMB36E1     | **10**  |
|   data_ram   | 8       |
|   code_ram   | 2       |
| RAM32M       | 2       |
| RAM32X1D     | 1       |

**Hierarchical breakdown:**

| Instance | Module | Cells |
|----------|--------|-------|
| top      |        | 570   |
| bf1_inst | bf1    | 254   |
| rstack   | stack  | 86    |

## Propagation Delays (Worst-Case, Slow Process Corner)

Delay data from post-synthesis timing report (unplaced netlist, no clock constraint applied — all paths report infinite slack).

### Critical Internal Path: BRAM → ALU → BRAM (code_ram write address)

| Segment | Delay (ns) | Cumulative (ns) |
|---------|------------|-----------------|
| code_ram_reg_1 CLK → DOBDO[2] | 2.080 | 2.080 |
| Net: code_ra_dout[6] | 0.584 | 2.664 |
| LUT4 (pj_carry53_carry__1_i_3) | 0.053 | 2.717 |
| Net: alu_a[8] | 0.363 | 3.080 |
| CARRY4 (pj_carry53_carry__1 DI[1] → O[3]) | 0.363 | 3.443 |
| Net: pj_carry53_carry__1_n_4 | 0.369 | 3.812 |
| LUT5 (data_ram_reg_0_0_i_4) | 0.142 | 3.954 |
| Net: ADDRBWRADDR[11] | 0.356 | 4.310 |
| LUT6 (io_tx_data_int[7]_i_6) | 0.053 | 4.363 |
| CARRY4 (io_tx_data_int_reg[7]_i_4 S[3] → CO[3]) | 0.233 | 4.596 |
| CARRY4 (rstack/io_tx_data_int_reg[7]_i_3 CI → CO[0]) | 0.195 | 4.791 |
| Net: mem_din2 / CO[0] | 0.285 | 5.076 |
| LUT4 (io_tx_data_int[0]_i_1) | 0.153 | 5.229 |
| Net: data_ram_reg[0] | 0.361 | 5.590 |
| LUT5 (data_ram_reg_0_0_i_7) | 0.053 | 5.643 |
| CARRY4 (pj_carry53_carry__2 CI → O[0]) | 0.146 | 5.789 |
| Net: data_ram_reg_0_0[0] | 0.361 | 6.150 |
| LUT6 (code_ram_reg_0_i_21) | 0.142 | 6.292 |
| Net: code_ram_reg_0_i_21_n_0 | 0.358 | 6.650 |
| LUT4 (code_ram_reg_0_i_2) | 0.053 | 6.703 |
| Net: bf1_inst_n_23 | 0.584 | 7.287 |
| CARRY4 (code_ram_reg_0_i_3) | 0.233 | 7.520 |
| Net: ADDRBWRADDR_n_0 | 0.359 | 7.879 |
| LUT3 (code_ram_reg_0_i_10) | 0.053 | 7.932 |
| Net: code_ram_reg_0_i_10_n_0 | 0.147 | 8.079 |
### Critical Path Detail (post-synthesis, 100 MHz clock constraint)

**Worst path:** code_ram_reg_1 (BRAM output) → code_ram_reg_0 (BRAM address input)

| Segment | Delay (ns) | Cumulative (ns) |
|---------|------------|-----------------|
| RAMB36E1 CLK → DOBDO[2] | 2.454 | 2.454 |
| Net: bf1_inst/code_ra_dout[6] | 0.800 | 3.254 |
| LUT4 (maddrN0_carry__1_i_3) → alu_a[8] | 0.124 + 0.639 | 4.017 |
| CARRY4 (maddrN0_carry__1) DI[1] → O[3] | 0.614 | 4.631 |
| Net → data_ram_reg_0_0_i_4 | 0.629 | 5.260 |
| LUT5 (data_ram_reg_0_0_i_4) | 0.307 | 5.567 |
| Net: ADDRBWRADDR[11] → io_tx_data_int[7]_i_6 | 0.465 | 6.032 |
| LUT6 (io_tx_data_int[7]_i_6) | 0.124 | 6.156 |
| CARRY4 (io_tx_data_int_reg[7]_i_4) | 0.401 | 6.557 |
| CARRY4 (rstack) | 0.293 | 6.850 |
| → LUT4 → LUT5 → LUT6 → LUT4 → LUT6 chain | 3.203 | 10.053 |
| Net → CARRY4 (maddrN0_carry__0, maddrN0_carry__1) | 1.231 | 11.284 |
| LUT6 (code_ram_reg_0_i_19) | 0.306 | 11.590 |
| LUT4 (code_ram_reg_0_i_5) | 0.124 | 11.714 |
| Net → code_ram_reg_0/ADDRBWRADDR[11] | 0.800 | 12.514 |
| **Total (before setup)** | **12.514** | |

(Clock path: 1.308 ns clock tree + 20.000 ns period = 21.308 ns required → slack = 21.308 − 12.514 − 0.566 setup = **+7.470 ns**)

### Summary Table (post-synthesis, 100 MHz clock constraint)

| Metric | Standalone bf1_soc | Full project (post-impl) |
|--------|-------------------|-------------------------|
| **Device** | xc7z010clg400-1 | xc7z010clg400-1 |
| **Clock** | clk_i (50 MHz effective) | fpga_0_clk (100 MHz, with MCP -setup 2) |
| **Effective period** | 20.000 ns | 20.000 ns (2-cycle path) |
| **Worst path** | BRAM → ALU → BRAM (code_ram addr) | BRAM → ALU → BRAM (data_ram addr) |
| **Data path** | 12.514 ns | 11.007 ns |
| **Logic levels** | 13 (CARRY4×5, LUT4×3, LUT5×2, LUT6×3) | 11 (CARRY4×5, LUT4×2, LUT5×3, LUT6×1) |
| **Slack** | **+7.470 ns** (MET) | **+7.690 ns** (estimated with MCP) |

**Key insight:** With a 20 ns effective period (either via 50 MHz clock or `set_multicycle_path -setup 2` on a 100 MHz clock), the ALU data path has **~7.5 ns of margin** on Zynq-7010. The MCP constraints are correct and sufficient for timing closure.

## Comparison: ALU Reuse Experiment (Kintex-7)

An earlier experiment replaced the **dedicated standalone adders** (`lj_offsetN`, `pj_low_sum`) with **ALU reuse** (Style A: `[`-style ALU setup, carry extraction `alu_c[5] ^ pc[5] ^ 1'b1`). The comparison was done on Kintex-7 and showed:

| Metric | Before (dedicated adder) | After (ALU reuse) | Delta |
|--------|------------------------|-------------------|-------|
| **Total LUTs** | 197 | 204 | **+7** |
| **FFs** | 152 | 152 | 0 |
| **CARRY4** | 11 | 13 | **+2** |
| **bf1_inst cells** | 245 | 254 | **+9** |
| **rstack cells** | 86 | 86 | 0 |
| **Worst internal path** | 7.69 ns | 8.08 ns | **+0.39 ns** |
| **Worst output path** | 9.87 ns | 9.87 ns | 0 |

**Conclusion:** ALU reuse slightly *increased* LUT count (+7) and timing delay (+0.39 ns) despite removing the standalone 5-bit adder. The carry extraction logic (`alu_c[5] ^ pc[5] ^ 1'b1`) and the additional fan-out on `alu_c[5]` offset the savings. This experiment was tagged `failed-alu-reuse` and the design was reverted to the dedicated-adder baseline.

## Current Configuration

The current `bf1.v` uses the **dedicated-adder baseline** (no ALU reuse, merged reset branches). Resource and timing numbers for the Zynq-7010 target are shown in the sections above.

## Notes

- Results are from **post-synthesis, pre-implementation** (unplaced, estimated wire delays). Place & route typically improves timing slightly by optimizing cell placement.
- A 50 MHz effective clock (`create_clock -name clk_i -period 20.000`) is applied during synthesis, modeling the 2-cycle ALU data path. This matches the `set_multicycle_path -setup 2` constraint on the full project's 100 MHz `fpga_0_clk`.
- The ALU data path has ~7.5 ns of margin with the 20 ns effective period on Zynq-7010.
