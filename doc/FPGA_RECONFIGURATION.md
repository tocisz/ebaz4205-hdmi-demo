# FPGA Runtime Reconfiguration on EBAZ4205

## Status Overview

The kernel now has all the infrastructure needed for runtime FPGA reconfiguration via
the Linux FPGA Manager framework and Device Tree Overlays:

| Config option | Purpose | Enabled |
|---|---|---|
| `CONFIG_FPGA_MGR_ZYNQ_FPGA` | PCAP bitstream loading (Zynq devcfg) | ✅ (always had) |
| `CONFIG_FPGA_BRIDGE` | Bridge framework for AXI isolation | ✅ (always had) |
| `CONFIG_FPGA_REGION` | Region abstraction for FPGA areas | ✅ (always had) |
| `CONFIG_OF_OVERLAY` | Dynamic DT modification at runtime | ✅ (new — kernel #3) |
| `CONFIG_OF_CONFIGFS` | `/sys/kernel/config/device-tree/overlays/` interface | ✅ (new — kernel #3) |
| `CONFIG_OF_FPGA_REGION` | Overlay → FPGA programming orchestration | ✅ (new — kernel #3) |

### Verified at runtime (kernel #3, 2026-07-15)

```
# fpga-region DT node:
$ cat /proc/device-tree/fpga-region/compatible
fpga-region

# of-fpga-region driver probed:
$ ls /sys/class/fpga_region/
region0

# Driver binding:
$ cat /sys/class/fpga_region/region0/device/uevent
DRIVER=of-fpga-region
OF_NAME=fpga-region
OF_COMPATIBLE_0=fpga-region

# FPGA manager:
$ cat /sys/class/fpga_manager/fpga0/state
operating

# ConfigFS overlay interface:
$ ls /sys/kernel/config/device-tree/overlays/
(empty — no active overlays)

# ConfigFS persisted across reboots via /etc/fstab
```

The configfs mount is now part of `/etc/fstab` (added in `post-build.sh` for future builds):

```
configfs  /sys/kernel/config  configfs  defaults  0  0
```

---

## The Ethernet PHY Clock Problem

### Hardware path (solved — PHY crystal installed)

**Original (FPGA-supplied clock, now removed):**

```
PS IO PLL (999 MHz)
  → FCLK_CLK1 divider (÷40 = 25 MHz)
  → PL clock routing fabric (pass-through, no logic)
  → OBUF primitive
  → FPGA pin U18 (clk_25m)
  → Ethernet PHY XTAL_IN
  → PHY generates GMII_rx_clk, GMII_tx_clk → back to FPGA pins U14, U15
```

**Current (dedicated oscillator, installed):**

```
┌─────────────┐
│  25 MHz     │  VCC ── 3.3V
│  Oscillator │  GND ── GND
│  (XO)       │  OUT ── PHY XTAL_IN (PCB trace from FPGA pin U18 cut)
└─────────────┘
```

The EBAZ4205 board originally had **no 25 MHz quartz crystal** for the Ethernet PHY.
The PHY relied entirely on the FPGA to supply its master reference clock. This worked
fine as long as the PL remained configured, but broke during any runtime reconfiguration.

**The 25 MHz crystal oscillator has been added to the hardware** (2025-07-17). The
FPGA-based clock generation has been removed from the PL design:

| Change | Details |
|---|---|
| `output clk_25m` port removed from `system_top.v` | No longer drives FPGA pin U18 |
| OBUF `ext_clk_25m_obuf` removed | No output buffer for PHY clock |
| FCLK_CLK1 disabled in PS (`PCW_EN_CLK1_PORT=0`) | PS no longer generates 25 MHz |
| `create_bd_port -dir O clk_25m` removed from BD | No longer part of block design |
| `clk_25m` constraints removed from XDC | Pin U18 freed up |
| `fclk-enable` changed to `<0x1>` in DT | Only FCLK_CLK0 enabled (AXI fabric) |

With the local oscillator, the PHY is **completely independent** of the PL state.
Ethernet survives full PL reconfiguration — the `macb` driver never sees link drop.
No serial console or script workarounds are needed; SSH over `eth0` stays up.

### Simple hot-reload via SSH (no workaround needed)

Now that the PHY has its own clock, you can do a full PL reconfiguration over SSH
without any prep work:

```bash
# Simply load the new bitstream — Ethernet stays up the entire time
fpgautil -b new_design.bit.bin -f Full
```

No need to unbind `macb`, no dropped SSH session, no UART console required.

---

## Device Tree Overlay Flow (Conceptual)

### The intended kernel flow

When you apply a DT overlay targeting the `fpga-region`, the kernel performs this
atomic sequence:

```
┌──────────────────────────────────────────────────────────┐
│  1. Find the target fpga-region                          │
│     └─ Matches on compatible = "fpga-region"             │
│                                                          │
│  2. Read firmware-name from overlay                      │
│     └─ e.g. firmware-name = "top.bit.bin"               │
│                                                          │
│  3. Find and freeze FPGA bridges for this region         │
│     └─ Disables AXI traffic to/from PL peripherals       │
│     └─ (Zynq-7000 PCAP may handle this transparently)    │
│                                                          │
│  4. Program the FPGA via fpga-mgr                        │
│     └─ Uses PCAP (devcfg@f8007000)                       │
│     └─ Loads the .bit.bin file from /lib/firmware/       │
│                                                          │
│  5. Thaw bridges                                         │
│     └─ Re-enables AXI traffic                            │
│                                                          │
│  6. Accept overlay into live tree                        │
│     └─ New child nodes appear under the fpga-region      │
│                                                          │
│  7. Probe child devices                                  │
│     └─ Platform drivers bind to new peripherals          │
│     └─ /dev/ nodes appear, modules load                  │
└──────────────────────────────────────────────────────────┘
```

### Overlay removal

```
rmdir /sys/kernel/config/device-tree/overlays/my_region
```

The kernel:
1. Unbinds drivers for overlay-added devices
2. Removes overlay nodes from the live tree
3. Freezes bridges
4. (Leaves FPGA in whatever state it was programmed to — does NOT clear it)

### Limitations on the current setup

The EBAZ4205 board has PL peripheral nodes (mwipcore, LCD, GPIO EMIO, PWM beeper)
**hardcoded in the base device tree**. This means:

1. **Drivers bind at boot** — before any overlay exists
2. **Applying an overlay that reconfigures the PL** would change hardware
   underneath already-running drivers → bus errors or hangs
3. **The overlay isn't self-consistent** — the base DT assumes the current bitstream;
   replacing it means the base DT nodes no longer describe the hardware

For full runtime reconfiguration to work cleanly, the base DT should contain **only the
PS (hard IP)** plus the `fpga-region` node — no PL peripheral nodes. All PL device nodes
should be in the overlay alongside `firmware-name`. This way:

- Base DT: PS peripherals (UART, SD card, NAND, Ethernet), fpga-region
- Overlay: firmware-name + child nodes for PL devices

Alternatively, use **partial reconfiguration** (see below) to keep the static peripherals
running while swapping only the dynamic region.

---

## Partial Reconfiguration

### Concept

Partial Reconfiguration (PR), also called **Dynamic Function eXchange (DFX)** in Xilinx
terminology, allows reprogramming only a *region* of the PL while the rest of the FPGA
continues operating uninterrupted.

```
┌──────────────────────────────────────────┐
│                PL Fabric                 │
│                                          │
│  ┌────────────┐    ┌──────────────────┐  │
│  │  Static    │    │  Dynamic (PRR)   │  │
│  │  Region    │    │                  │  │
│  │            │    │  HDMI Generator  │  │
│  │  clk_25m   │    │  AXI DMAC        │  │
│  │  GPIO EMIO │    │  Video Pipeline  │  │
│  │  SPI0 EMIO │    │                  │  │
│  │  TTC0 EMIO │    └──────────────────┘  │
│  │            │          ▲               │
│  │  (always   │          │ AXI bridges   │
│  │   running) │    can be frozen here    │
│  └────────────┘                          │
└──────────────────────────────────────────┘
```

Key terms:

| Term | Meaning |
|---|---|
| **Static region** | Part of the PL that never changes. Clock routing, EMIO pass-through, basic I/O. |
| **Partially Reconfigurable Region (PRR)** | A fixed-area, fixed-boundary region that can be independently reprogrammed. Also called a "reconfigurable partition". |
| **Persona** | One specific bitstream designed to fit into a PRR. You can have multiple personas for one PRR and swap between them. |
| **FPGA Bridge** | Hardware (or soft logic) that gates the AXI bus to a PRR. Frozen during reconfiguration to prevent bus stalls. |
| **Floorplanning** | The process of assigning physical FPGA resources (LUTs, BRAM, DSP) to a PRR using Pblocks in Vivado. |

### How it relates to the kernel

In the device tree, each PRR gets its own `fpga-region` node with a `fpga-bridge`:

```dts
// Static region overlay (loaded once at boot):
fpga-bridge@4400 {
    compatible = "altr,freeze-bridge-controller";  // or custom AXI freeze bridge
    reg = <0x4400 0x10>;

    fpga_region_hdmi: fpga-region-hdmi {
        compatible = "fpga-region";
        // fpga-mgr is inherited from parent fpga-region
        #address-cells = <1>;
        #size-cells = <1>;
    };
};

// Persona overlay (loaded to swap HDMI design at runtime):
/dts-v1/;
/plugin/;

&fpga_region_hdmi {
    firmware-name = "hdmi_persona_v2.bit.bin";
    partial-fpga-config;                    // ← tells kernel this is partial

    // Nodes for what's in this persona:
    my_hdmi_dma: dma@43000000 {
        compatible = "adi,axi-dmac-1.00.a";
        reg = <0x43000000 0x10000>;
        ...
    };
};
```

### Benefits for the EBAZ4205

| Benefit | Details |
|---|---|
| **Ethernet survives** | PHY has its own crystal → always has a clock source |
| **LCD/GPIO continue** | SPI0 EMIO, GPIO EMIO, TTC0 PWM are in static region |
| **HDMI hot-swap** | Swap video processing pipelines (different resolutions, effects) without rebooting |
| **Safer AXI** | The bridge freezes AXI traffic → no bus hangs during reconfig |
| **Faster** | PR bitstream is much smaller (only the PRR area) → reconfiguration in tens of ms instead of hundreds |

### Learning path

Partial reconfiguration requires both **HDL tooling changes** and **kernel setup**:

**HDL side (Vivado):**
1. **Floorplan the design**: Define Pblocks for the static region and each PRR
2. **Create PR projects**: Vivado PR flow generates the static image and persona images
3. **Add freeze bridges**: A soft-logic AXI freeze bridge IP in the static region, controlled by a GPIO register the kernel can access
4. **Verify DRC**: Vivado validates boundary connections, timing closure for each persona

**Key Vivado documentation:**
- [UG909: Vivado Design Suite — Partial Reconfiguration](https://docs.amd.com/r/en-US/ug909-vivado-partial-reconfiguration)
- [UG947: Vivado Design Suite Tutorial — Partial Reconfiguration](https://docs.amd.com/r/en-US/ug947-vivado-partial-reconfiguration-tutorial)

**Kernel side:**
1. `CONFIG_OF_OVERLAY`, `CONFIG_OF_FPGA_REGION` — already enabled ✅
2. DT nodes for `fpga-bridge` (soft logic freeze bridge) and PR `fpga-region`
3. DT overlay `.dtbo` for each persona with `partial-fpga-config` flag
4. Copy `persona.bit.bin` to `/lib/firmware/`
5. Apply: `mkdir /sys/kernel/config/device-tree/overlays/pr_hdmi; echo pr_hdmi.dtbo > .../path`
6. Remove: `rmdir /sys/kernel/config/device-tree/overlays/pr_hdmi`

**Community resources:**
- [Xilinx Wiki: Solution Zynq PL Programming With FPGA Manager](https://xilinx-wiki.atlassian.net/wiki/spaces/A/pages/18841645)
- [ikwzm/ZynqMP-FPGA-Linux-Example](https://github.com/ikwzm/ZynqMP-FPGA-Linux-Example-0-UltraZed) — working PR examples on Zynq
- [Linux kernel docs: fpga-region.txt](https://www.kernel.org/doc/Documentation/devicetree/bindings/fpga/fpga-region.txt)
- [ControlPaths: Configuring PL from PS in Zynq](https://www.controlpaths.com/2023/04/08/configuring-pl-from-ps-in-zynq-mpsoc/)

---

## Practical Improvements (Priority Order)

### 1. ✅ Add PHY crystal (DONE — 2025-07-17)

**Status:** ✅ Hardware mod complete. 25 MHz CMOS oscillator soldered to the PHY,
PCB trace from FPGA pin U18 cut.

**HDL changes:** `clk_25m` output port, OBUF, FCLK_CLK1 connection, and XDC constraints
removed from the FPGA design. FCLK_CLK1 disabled in PS. `fclk-enable` in DT set to `<0x1>`
(only FCLK_CLK0 for AXI fabric).

**Impact:** The `macb` driver never needs to be unbound. Ethernet and SSH survive
full PL reconfiguration without any workarounds.

### 2. 🟡 Restructure base device tree

Move all PL peripheral nodes out of `zynq-ebaz4205.dts` and into a `pl.dtsi` overlay.
The base DT would only have:

```dts
/ {
    // PS hard IP only: UART, SD card, NAND, Ethernet, TTC0, SPI0, GPIO
    // fpga-region with fpga-mgr = <&devcfg>, no children, no firmware-name
};
```

Then a boot-time overlay adds the PL devices:

```dts
/dts-v1/;
/plugin/;
&fpga-region {
    firmware-name = "ebaz4205_top.bit.bin";
    mwipcore: axi:mwipcore@0 { ... };
    // LCD, HDMI, GPIO-EMIO routing, etc.
};
```

This matches the overlay design pattern: bitstream + device nodes travel together.

**Note:** With the PHY crystal installed, you can boot with a blank PL and let the
overlay load everything — the `clk_25m` stub bitstream is no longer required.

### 3. 🟢 Implement partial reconfiguration

After steps 1-2, implement a PR design:

1. Floorplan the static region: clk_25m routing, GPIO EMIO (12:0), SPI0 EMIO, TTC0 PWM
2. Define a PRR for the HDMI video pipeline (AXI DMAC, hdmi_generator, hdmi core)
3. Add an AXI freeze bridge to gate the PRR's AXI bus
4. Build different HDMI personas (different resolutions, test patterns, video effects)
5. Swap them at runtime via DT overlays

This is the "final form" — Ethernet stays up, the LCD and beeper keep working, and only
the video pipeline gets swapped.

---

## References

- [Linux kernel: fpga-region DT binding](https://www.kernel.org/doc/Documentation/devicetree/bindings/fpga/fpga-region.txt)
- [Linux kernel: fpga-region driver API](https://docs.kernel.org/6.16/driver-api/fpga/fpga-region.html)
- [Xilinx Wiki: Zynq PL Programming With FPGA Manager](https://xilinx-wiki.atlassian.net/wiki/spaces/A/pages/18841645)
- [Xilinx UG909: Partial Reconfiguration](https://docs.amd.com/r/en-US/ug909-vivado-partial-reconfiguration)
- [ikwzm: Zynq FPGA Linux Examples](https://github.com/ikwzm/ZynqMP-FPGA-Linux-Example-0-UltraZed)
- [ARCHITECTURE.md — main architecture document](ARCHITECTURE.md)