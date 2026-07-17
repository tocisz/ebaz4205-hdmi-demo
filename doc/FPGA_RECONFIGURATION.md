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

### Verified at runtime (2026-07-17, tested end-to-end)

The overlay is now applied at boot by `/etc/init.d/S20fpgaregion` via configfs
(instead of U-Boot `fdt apply`). This gives the kernel full overlay lifecycle
tracking — the overlay can be removed and re-applied cleanly at runtime.

```
# fpga-region DT node:
$ cat /proc/device-tree/fpga-region/compatible
fpga-region

# of-fpga-region driver probed:
$ ls /sys/class/fpga_region/
region0

# FPGA manager:
$ cat /sys/class/fpga_manager/fpga0/state
operating

# ConfigFS overlay interface — "pl" overlay active (applied by init script):
$ ls /sys/kernel/config/device-tree/overlays/
pl

# All PL devices present after overlay applied:
$ ls /sys/class/leds/
led0:green  led0:red  led1:aux0  led1:aux1  led1:aux2  mmc0::

$ ls /dev/fb0
/dev/fb0

$ ls /sys/bus/iio/devices/
iio:device0  iio:device1        # xadc + mwipcore0:hdmi_sink

# Overlay can be removed and re-applied at runtime:
$ rmdir /sys/kernel/config/device-tree/overlays/pl
$ ls /sys/class/leds/
mmc0::                           # only MMC LED remains

$ mkdir -p /sys/kernel/config/device-tree/overlays/pl
$ cat /mnt/pl-ebaz4205.dtbo > /sys/kernel/config/device-tree/overlays/pl/dtbo
$ ls /sys/class/leds/
led0:green  led0:red  led1:aux0  led1:aux1  led1:aux2  mmc0::   # all back
```

The configfs mount is part of `/etc/fstab` (added in `post-build.sh`):

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

### Simple hot-reload via SSH (tested, working)

Now that the PHY has its own clock and the overlay is managed by the kernel
(via configfs, not U-Boot), you can do a full PL reconfiguration over SSH:

```bash
# 1. Remove current PL overlay (unbinds drivers, removes DT nodes)
rmdir /sys/kernel/config/device-tree/overlays/pl

# 2. Program new bitstream (Ethernet stays up throughout)
fpgautil -b /mnt/new_design.bit.bin -f Full

# 3. Apply new overlay (adds DT nodes for new design, probes drivers)
mkdir -p /sys/kernel/config/device-tree/overlays/pl
cat /mnt/new_overlay.dtbo > /sys/kernel/config/device-tree/overlays/pl/dtbo
```

No need to unbind `macb`, no dropped SSH session, no UART console required.
The kernel tracks the overlay lifecycle, so removal/re-application is clean.

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

### ✅ Limitations resolved

**The base DT has been restructured (2025-07-17).** All PL peripheral nodes have been
moved out of `zynq-ebaz4205.dts` and into the `pl-ebaz4205.dtso` overlay. The base DT
now contains only PS hard IP plus the `fpga-region` node (inherited from `zynq-7000.dtsi`).

**Overlay application at boot:** The overlay is applied by the `/etc/init.d/S20fpgaregion`
init script via configfs (`/sys/kernel/config/device-tree/overlays/`). This is critical —
because the kernel tracks configfs-applied overlays, they can be **removed cleanly at
runtime** (`rmdir`), which is not possible when overlays are merged by U-Boot before boot.

**U-Boot change:** U-Boot now uses `CONFIG_OF_SEPARATE=y` (instead of `CONFIG_OF_BOARD=y`),
so it loads the base DTB (`devicetree.dtb`, PS-only) from the FAT partition and passes it
to the kernel. The `boot.cmd` does **not** apply any DT overlay — that is deferred to
userspace init for proper lifecycle tracking.

**Benefits of the new structure:**

1. **Drivers bind only after overlay is applied** — PL devices appear when configfs
   overlay is created, not at kernel probe time
2. **Full overlay lifecycle** — remove (`rmdir`) and re-apply works; the kernel tracks
   which nodes were added and can unbind drivers / delete nodes cleanly
3. **Hot-reload ready** — swap the PL design by removing the old overlay, reprogramming
   the FPGA via `fpgautil`, and applying a new overlay with matching device nodes

**What the overlay covers:**

| Node | Why it's in the overlay |
|---|---|
| `leds` (GPIO 54-58) | EMIO GPIOs → need PL routing |
| `emio_keys` (GPIO 59-63) | EMIO GPIOs → need PL routing |
| `beeper` | TTC0 PWM output via EMIO |
| `ext_clk_50m` | PL reference clock |
| `st7789v@0` (LCD) | SPI0 routed through EMIO |
| `hdmi_sink_dma` | AXI DMAC in PL |
| `mwipcore0` | PL IP core |

**Remaining in base DTS:** All PS peripherals (MIO-based), the restart key on GPIO 20,
and the `fpga-region` container from the SoC DTSI.

For full details on the restructuring, see [section 2](#2--restructure-base-device-tree-done--2025-07-17).

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

### 2. ✅ Restructure base device tree (DONE — 2025-07-17)

**Status:** ✅ Base DT restructured. All PL-dependent nodes moved from `zynq-ebaz4205.dts`
into a separate DT overlay `pl-ebaz4205.dtso`. Only PS peripherals remain in the base DTS.

**Changes to `u-boot-xlnx/arch/arm/dts/zynq-ebaz4205.dts`:**

| Removed from base DTS | Reason | New location |
|---|---|---|
| `leds` node (GPIO 54-58) | EMIO → needs PL routing | `pl-ebaz4205.dtso` inside `&amba` |
| `key0`–`key4` (GPIO 59-63) | EMIO → needs PL routing | `pl-ebaz4205.dtso` inside `&amba` |
| `beeper` (TTC0 PWM) | EMIO output needs PL routing | `pl-ebaz4205.dtso` inside `&amba` |
| `ext_clk_50m` | PL reference clock | `pl-ebaz4205.dtso` inside `&amba` |
| `st7789v@0` (LCD) | SPI0 routed via EMIO | `pl-ebaz4205.dtso` fragment targeting `&spi0` |
| `amba: axi` bus + children | PL AXI peripherals (DMAC, mwipcore) | `pl-ebaz4205.dtso` inside `&amba` |

**Kept in base DTS (PS hard IP, always works):**
- `&gem0` — Ethernet (MIO)
- `&sdhci0` — SD card (MIO)
- `&uart0`, `&uart1` — UART (MIO)
- `&gpio0` — PS GPIO controller
- `&spi0` — SPI controller (children moved to overlay)
- `&smcc`, `&nfc0` — NAND (MIO)
- `&ttc0` — TTC0 timer (PWM property kept for framework)
- `&clkc` — Clock controller
- `keys` node with restart key only (GPIO 20 = MIO)
- `fpga_full: fpga-region` — inherited from `zynq-7000.dtsi`

**New file — `pl-ebaz4205.dtso`:**

The overlay targets `&amba` (the AXI bus) and `&spi0`, adding all PL peripheral
nodes. It is compiled with `cpp -U linux` + `dtc -@` to produce `pl-ebaz4205.dtbo`.
The `-U linux` flag prevents the CPP preprocessor from expanding `linux` to `1`,
which would mangle property names like `linux,default-trigger`.

```dts
/dts-v1/;
/plugin/;

&amba {
    // Add firmware-name here to trigger FPGA programming at overlay apply:
    // firmware-name = "system_top.bit.bin";

    leds { ... };          // EMIO GPIO 54-58
    emio_keys { ... };     // EMIO GPIO 59-63
    beeper { ... };        // TTC0 PWM via EMIO
    ext_clk_50m { ... };   // PL reference clock
    hdmi_sink_dma: dma@7c420000 { ... };  // AXI DMAC
    mwipcore0: mwipcore@0 { ... };        // MathWorks IIO wrapper
};

&spi0 {
    st7789v@0 { ... };     // LCD via EMIO SPI
};
```

The overlay is compiled from `u-boot-xlnx/arch/arm/dts/pl-ebaz4205.dtso` by the
project Makefile using `cpp -U linux` + `dtc -@` and copied to `build_sdimg/`.
`build/system_top.bit.bin` is also generated (via `bootgen -process_bitstream bin`)
from `system_top.bit` and placed on the SD card for use by `fpgautil` or the
overlay's `firmware-name`.

**Boot-time overlay application (`/etc/init.d/S20fpgaregion`):**

The init script runs at boot (order S20) and applies the overlay via configfs:

```sh
mount /dev/mmcblk0p1 /mnt 2>/dev/null || true
mkdir -p /sys/kernel/config/device-tree/overlays/pl
cat /mnt/pl-ebaz4205.dtbo > /sys/kernel/config/device-tree/overlays/pl/dtbo
```

Because the overlay is applied by the **kernel** (not U-Boot), the kernel tracks
it in its overlay list. This means it can later be removed with:

```bash
rmdir /sys/kernel/config/device-tree/overlays/pl
```

U-Boot merged overlays would appear as native DT nodes and couldn't be removed.

**SD card boot partition contents** (all files from `build_sdimg/`):

| File | Purpose |
|---|---|
| `BOOT.bin` | FSBL + bitstream + U-Boot + base DTB |
| `boot.scr` | U-Boot boot script (loads kernel + base DTB, no overlay) |
| `devicetree.dtb` | Base device tree (PS peripherals only) |
| `uImage` | Linux kernel |
| `pl-ebaz4205.dtbo` | PL device tree overlay (applied by init script) |
| `system_top.bit.bin` | Full bitstream for runtime reconfiguration |

**U-Boot configuration change:**

`CONFIG_OF_BOARD=y` was replaced with `CONFIG_OF_SEPARATE=y` in
`zynq_ebaz4205_defconfig`. This forces U-Boot to use its own appended DTB
(compiled from `zynq-ebaz4205.dts`) for its control FDT, and properly pass
the DTB loaded from the FAT partition (`devicetree.dtb`) to the kernel via
`bootm ${kernel_addr_r} - ${fdt_addr_r}`.

**Applying the overlay at runtime** (only needed when swapping designs):

```bash
# Copy bitstream to firmware directory
scp build/system_top.bit.bin root@ebaz:/lib/firmware/

# Apply overlay via configfs
mkdir -p /sys/kernel/config/device-tree/overlays/pl
cat pl-ebaz4205.dtbo > /sys/kernel/config/device-tree/overlays/pl/dtbo

# Check that PL devices appeared
ls /sys/class/fpga_manager/fpga0/state        # "operating"
ls /sys/class/fpga_region/region0/devices/     # PL devices listed
```

**Note:** The FPGA is programmed at boot by BOOT.bin (FSBL stage). The init script
only adds the DT overlay for device nodes — `firmware-name` is intentionally **not**
in the overlay. For runtime reconfiguration, you can either:
- Use `fpgautil -b system_top.bit.bin -f Full` directly (Ethernet stays up), or
- Add `firmware-name` to the overlay, copy the `.bit.bin` to `/lib/firmware/`,
  and apply the modified overlay via configfs to program FPGA + add nodes atomically.

**Known issues fixed during implementation:**

| Issue | Fix |
|---|---|
| `linux,default-trigger` mangled to `1,default-trigger` by CPP | `-U linux` added to CPP flags in Makefile |
| `mwipcore0` missing from overlay (removed from base DTS but not added to `.dtso`) | Added `mwipcore@0` node with `stream-channel` and `data-channel` to overlay |
| Heartbeat LED showed `[none]` due to mangled property name | Resolved by `-U linux` fix above |

**Removal:**

```bash
rmdir /sys/kernel/config/device-tree/overlays/pl
```

This unbinds the PL device drivers and removes the overlay nodes, but leaves the
FPGA in its programmed state.

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