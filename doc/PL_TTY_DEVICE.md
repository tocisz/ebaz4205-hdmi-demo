# PL TTY Device — Legacy UART Lite Path (Replaced)

> **Status: historical reference.** This path has been replaced by the
> AXI-Stream FIFO bridge (`doc/AXIS_FIFO_BRIDGE.md` Phase 2). The document
> is retained for its echo_char/uart_phy test history and as a record of the
> backpressure gap that motivated the replacement.

## What it was

A bidirectional TTY character device exposed to Linux as `/dev/ttyUL1`, built
from a Xilinx AXI UART Lite IP core. The PS side was a stock Linux serial
driver; the PL side was the `uart_phy` PHY feeding the `bf2_soc` softcore CPU
over its parallel byte-stream interface — i.e. Linux talked to the softcore's
UART **as if it were an external UART on a wire**.

```
PS /dev/ttyUL1 (uartlite) ──AXI4-Lite @0x7C430000, IRQ 57──▶ axi_uartlite_0
axi_uartlite_0/tx ──serial wire──▶ uart_phy_0/uart_rx_i ──parallel──▶ bf2_soc_0/io_rx_*
bf2_soc_0/io_tx_* ──parallel──▶ uart_phy_0 ──serial wire──▶ axi_uartlite_0/rx
```

## Why it was replaced

The PS→PL direction had **no end-to-end flow control** — `uart_phy`'s 16-byte
RX FIFO filled in ~1.4 ms at 115200 baud, after which incoming bytes were
dropped silently. Wire-level flow control (RTS/CTS via UART16550) was
considered but the AXI-Stream FIFO bridge (`doc/AXIS_FIFO_BRIDGE.md`) was
chosen instead: it provides true backpressure in both directions, uses an
in-tree staging driver, and runs at ~100 MB/s class throughput — ~10⁴× the
uartlite wire rate.

## Current solution

See `doc/AXIS_FIFO_BRIDGE.md`. `bf2_soc` now connects to `axis_byte_bridge`
v1 (drop-24) instead of `uart_phy`; `axi_uartlite_0`, `uart_phy_0`, and the
Phase-1 `case_toggle` test module are removed from the block design. The PS
console stays on UART1 (`ttyPS0`, MIO 24-25).

## Remaining reference material

### uart_phy / echo_char

The `uart_phy` library module (`hdl/library/uart_phy/`) remains available for
projects that need a serial-PHY-to-parallel bridge (echo_char/bf1_soc
simulations still use it). Its parallel interface contract is documented in
`doc/UART_PHY.md`.

The `echo_char` loopback module (`hdl/library/echo_char/`) served as the
original verification vehicle and uncovered three RTL bugs whose fixes are
now baked into `uart_phy` itself. The test history is retained below.

### Kernel module

`CONFIG_SERIAL_UARTLITE=m` remains in `zynq_ebaz4205_defconfig` — the module
still builds and can be loaded on any board that has a UART Lite IP in its
design. With the DT node removed from `pl-ebaz4205.dtso`, it simply won't
auto-load on this design (no modalias match).

---

## History: echo_char loopback (original verification vehicle)

### What it was

`echo_char.v` — a PL module composing two `uart_phy` instances around a `+1`
transform: RX (8× oversampling) → byte+1 → TX (8N1). It proved the
PS↔PL↔PS data path end-to-end and served as the smoke test for the subsequent
`bf2_soc` wiring, which used an identical uartlite ↔ wire ↔ PHY structure.

### Hardware test — passed

Tested on the EBAZ4205 board via `pyserial`:

```python
ser = serial.Serial('/dev/ttyUL1', 115200, timeout=2)
for c in [b'A', b'z', b'0', b'!', b'\n']:
    ser.write(c)
    assert ser.read(1) == bytes([(c[0] + 1) & 0xFF])
```

**Full sweep of all 256 byte values (0x00–0xFF): 261/261 checks passed.** Shell
tools also confirmed: `echo -n "A" > /dev/ttyUL1` produces `B` via
`cat /dev/ttyUL1`.

### Bugs found and fixed (echo_char RTL)

Three RTL bugs in `echo_char.v` were discovered via simulation. All three
lessons are baked into `uart_phy` itself today.

**Bug 1: TX baud counter not reset on new transmission** — corrupted output
bytes because the free-running baud counter phase could truncate the start bit.
*Fix:* reset the counter on `tx_start` so every transmission begins with a
full-duration start bit (today: `uart_tx.sv` phase-resets its divider on
`tx_start`).

**Bug 2: RX shift register not cleared between receptions** — stale bits from
the previous byte corrupted the next one. *Fix:* clear the shift register in
the idle state (today: `uart_rx.sv`).

**Bug 3: No buffering between RX and TX (back-to-back byte loss)** — the single
`tx_byte` register dropped bytes that arrived while the TX was still
serializing. *Fix:* a 16-entry synchronous FIFO between RX and TX (today:
`uart_phy`'s `fifo_sync`, depth 16 matching the AXI UART Lite's own FIFOs).

### Simulation test suite

**File:** `hdl/library/echo_char/tb_echo_char.v` — self-checking Verilog
testbench (10 tests: known values, wraparound, back-to-back, idle, exhaustive
0x00–0xFF sweep, timing drift ×100, FIFO bursts 8/32/64/17). **490/490
passed.** The same suite structure now lives in `tb_uart_phy.sv`
(`hdl/library/uart_phy/`).

```bash
make -C hdl/library/echo_char sim     # run simulation
make -C hdl/library/echo_char wave    # open waveforms in Vivado
make -C hdl/library/echo_char sim-clean  # remove artifacts
```

### Historic BD wiring (for reference)

The block design before Phase 2 of `doc/AXIS_FIFO_BRIDGE.md` wired:

```tcl
ad_ip_instance axi_uartlite axi_uartlite_0
ad_ip_parameter axi_uartlite_0 CONFIG.C_BAUDRATE 115200
ad_ip_parameter axi_uartlite_0 CONFIG.C_DATA_BITS 8
ad_ip_parameter axi_uartlite_0 CONFIG.C_USE_PARITY 0
ad_ip_parameter axi_uartlite_0 CONFIG.C_ODD_PARITY 0
ad_cpu_interconnect 0x7C430000 axi_uartlite_0
ad_cpu_interrupt ps-13 mb-13 axi_uartlite_0/interrupt

ad_ip_instance uart_phy uart_phy_0
ad_connect sys_cpu_clk uart_phy_0/clk
ad_connect sys_cpu_reset uart_phy_0/reset
ad_connect axi_uartlite_0/tx uart_phy_0/uart_rx_i
ad_connect axi_uartlite_0/rx uart_phy_0/uart_tx_o

ad_ip_instance bf2_soc bf2_soc_0
ad_connect sys_cpu_clk bf2_soc_0/clk_i
ad_connect sys_cpu_resetn bf2_soc_0/resetq
ad_connect uart_phy_0/rx_data   bf2_soc_0/io_rx_data
ad_connect uart_phy_0/rx_valid  bf2_soc_0/io_rx_valid
ad_connect bf2_soc_0/io_rx_ready  uart_phy_0/rx_accept_i
ad_connect bf2_soc_0/io_tx_data   uart_phy_0/tx_data
ad_connect bf2_soc_0/io_tx_valid  uart_phy_0/tx_start
ad_connect uart_phy_0/tx_ready    bf2_soc_0/io_tx_ready
```

## Reference

- **AXI UART Lite registers:** RX (0x00), TX (0x04), STATUS (0x08), CONTROL
  (0x0C)
- **UART wire format:** 8N1 (start low, 8 data bits LSB-first, stop high),
  115200 baud
- **Doc cross-ref:** `doc/AXIS_FIFO_BRIDGE.md` — current PS↔PL bridge solution
- **Doc cross-ref:** `doc/UART_PHY.md` — `uart_phy` contract (module still in
  library for reuse)
