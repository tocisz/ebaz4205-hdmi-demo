# Fork Chain Trace

This document traces the origin and fork chain for each repository used in this project.

---

## Legend

Each entry follows this structure:

```
upstream repo
  └── X/tag-or-branch: base point used
       └── m1nl/fork: what m1nl forked, what they changed
            └── tocisz/fork: what we forked, what we changed
```

---

## 1. Main repo: `ebaz4205-hdmi-demo`

| Remote | URL | Branch |
|--------|-----|--------|
| `origin` | `git@github.com:tocisz/ebaz4205-hdmi-demo.git` | `master` |
| `m1nl` | `https://github.com/m1nl/ebaz4205-hdmi-demo.git` | `master` |

**Chain:**
```
m1nl/ebaz4205-hdmi-demo (created by m1nl, latest tag v0.0.5)
  └── tocisz/ebaz4205-hdmi-demo (forked at v0.0.5)
       └── +2 commits: our local changes
            - v0.0.5-2-g13bc209 (current HEAD)
```

**Description:**  
This is the **top-level project** that ties all submodules together via `Makefile`, `scripts/`, and `.gitmodules`. It has no upstream beyond m1nl's repo.

**Our changes (1 commit past m1nl's state):**
- Updated `.gitmodules` URLs to point to `tocisz` forks
- Updated `Makefile` (rm→rm -rf, mkdir→mkdir -p)
- Updated `scripts/boot.cmd` (added console bootargs)
- Updated `scripts/create_fsbl_project.tcl` (new Xilinx tools API)
- Added `doc/ARCHITECTURE.md`, `doc/BEEPER_ANALYSIS.md`, `doc/FPGA_RECONFIGURATION.md`
- Updated submodule pointers for `buildroot` and `linux`

---

## 2. `u-boot-xlnx`

| Remote | URL | Branch |
|--------|-----|--------|
| `origin` | `git@github.com:tocisz/u-boot-xlnx.git` | `ebaz4205` |
| `m1nl` | `https://github.com/m1nl/u-boot-xlnx.git` | `ebaz4205` |
| `xilinx` | `https://github.com/Xilinx/u-boot-xlnx.git` | — |

**Chain:**
```
Xilinx/u-boot-xlnx
  ├── tag xilinx-v2025.2
  │    └── m1nl/ebaz4205: +4 commits (ebaz4205 patches)
  │         └── current tip: 5f994aeba41 (xilinx-v2025.2-4-g5f994aeba41)
  │
  └── tag xlnx_rebase_v2025.01_2025.1_update1
       └── tocisz/ebaz4205: +4 commits (same ebaz4205 patches, rebased)
            └── current tip: 6d71ce15965 (xlnx_rebase_v2025.01_2025.1_update1-4-g6d71ce15965)
```

**The 4 ebaz4205 commits** (present in both, different hashes due to different base):

| Purpose | m1nl's hash (on xilinx-v2025.2) | Our hash (on xlnx_rebase_v2025.01...) |
|---------|--------------------------------|--------------------------------------|
| Define config & DTS for ebaz4205 | `98c1943b631` | `47cbb1ab329` |
| HDMI DMA transfers & SPI display | `c10806ab21e` | `4e566b0c810` |
| NAND flash support | `c3e3e21c65b` | `6e7460b0ab9` |
| Remove unneeded config options | `5f994aeba41` | `6d71ce15965` |

**Key difference:**  
m1nl based his `ebaz4205` branch on Xilinx tag `xilinx-v2025.2` (5367 additional upstream commits).  
We based our branch on `xlnx_rebase_v2025.01_2025.1_update1` (a newer Xilinx tag) with only the 4 ebaz4205 commits on top — a much cleaner history.

---

## 3. `linux`

| Remote | URL | Branch |
|--------|-----|--------|
| `origin` | `git@github.com:tocisz/analogdevicesinc-linux.git` | `ebaz4205` |
| `m1nl` | `https://github.com/m1nl/analogdevicesinc-linux.git` | `ebaz4205` |
| `adi` | `https://github.com/analogdevicesinc/linux.git` | — |
| `xilinx` | `https://github.com/Xilinx/linux-xlnx.git` | — |

**Chain:**
```
kernel.org (mainline Linux v6.12)
  └── Xilinx/linux-xlnx (Xilinx SoC/FPGA support)
       └── analogdevicesinc/linux (ADI drivers on top of Xilinx tree)
            ├── commit 90b13e116180 (Merge tag 'v6.12.40' into xlnx_rebase_v6.12_LTS_2025.1_update)
            │    └── m1nl/ebaz4205: +4518 commits (ADI tree + ebaz4205 patches)
            │         └── current tip: 76ebe531aa3b (older ADI snapshot)
            │
            └── same commit 90b13e116180
                 └── tocisz/ebaz4205: +707 commits (different ADI snapshot + 2 ebaz4205 commits)
                      └── current tip: def799f71349 (xlnx_rebase_v6.12_LTS_2025.1_update_merge_6.12.40-707-gdef799f71349)
```

**The 2 ebaz4205 commits:**

| Commit | Message |
|--------|---------|
| `859c3fafeba3` | define defconfig for ebaz4205 board |
| `def799f71349` | ebaz4205: enable OF overlay and FPGA configfs support |

**Key difference:**  
Both branches fork from the same Xilinx merge commit `90b13e`.  
m1nl's branch has 4518 commits (a different set of ADI tree history).  
Our branch has 707 commits on top (a newer ADI snapshot with the 2 ebaz4205 patches).  
The 705 non-ebaz4205 commits include ADI drivers (CI, drivers, devicetrees, docs).

---

## 4. `buildroot`

| Remote | URL | Branch |
|--------|-----|--------|
| `origin` | `git@github.com:tocisz/buildroot.git` | `ebaz4205` |
| `m1nl` | `https://github.com/m1nl/buildroot.git` | `ebaz4205` |

**Chain:**
```
buildroot/buildroot (upstream)
  └── tag 2025.05.2
       └── m1nl/ebaz4205: +149 commits (ebaz4205 board support + packages)
            └── current tip: 22bc98017c (2025.05.2-149-g22bc98017c)
            │
            └── tocisz/ebaz4205: +150 commits (= m1nl's 149 + our 1)
                 └── current tip: 044e07b59d (2025.05.2-150-g044e07b59d)
```

**Our additional commit:**
```
044e07b59d ebaz4205: update board config and build scripts
```

**Description:**  
m1nl's `ebaz4205` branch adds 149 commits on top of upstream Buildroot `2025.05.2`. These include:
- Board config (`configs/zynq_ebaz4205_defconfig`)
- Board files (`board/ebaz4205/`)
- Packages for MTD, UBIFS, JFFS2, etc.

We added 1 commit on top (`044e07b`) with config/build updates.

---

## 5. `hdl`

| Remote | URL | Branch |
|--------|-----|--------|
| `origin` | `git@github.com:tocisz/analogdevicesinc-hdl.git` | `ebaz4205` |
| `m1nl` | `https://github.com/m1nl/analogdevicesinc-hdl.git` | `ebaz4205` |

**Chain:**
```
analogdevicesinc/hdl (upstream ADI HDL)
  └── tag 2023_R2_p1
       └── m1nl/ebaz4205 & tocisz/ebaz4205: +5 commits (identical)
            └── current tip: a12184174 (2023_R2_p1-5-ga12184174)
```

**The 5 ebaz4205 commits** (identical in both forks):

| Commit | Message |
|--------|---------|
| `7d7755786` | add ebaz4205 project |
| `3f03f3434` | add HDMI libraries |
| `941d4dc0d` | add support for HDMI DMA transfers |
| `0b9c3b102` | rename hdmi_source_dma to hdmi_sink_dma |
| `a12184174` | add license |

**Key difference:**  
None — both forks point to the exact same commit. No local changes.

---

## Summary Diagram

```
kernel.org (v6.12)
  └── Xilinx/linux-xlnx
       └── analogdevicesinc/linux ──────────────────────┐
            ├─ 90b13e (v6.12.40 merge)                  │
            │  ├── m1nl/analogdevicesinc-linux +4518    │
            │  └── tocisz/analogdevicesinc-linux +707   │
            │                                           │
Xilinx/u-boot-xlnx                                     │
  ├─ tag xilinx-v2025.2                                │
  │  └── m1nl/u-boot-xlnx +4 (ebaz4205)                │
  └─ tag xlnx_rebase_v2025.01_2025.1_update1            │
     └── tocisz/u-boot-xlnx +4 (ebaz4205)               │
                                                        │
buildroot/buildroot ────────────────────────────────────┤
  └─ tag 2025.05.2                                      │
     └── m1nl/buildroot +149 ─── tocisz/buildroot +150  │
                                                        │
analogdevicesinc/hdl ───────────────────────────────────┘
  └─ tag 2023_R2_p1
     └── m1nl/hdl +5 = tocisz/hdl +5 (identical)
```

All tied together by:

```
m1nl/ebaz4205-hdmi-demo (v0.0.5)
  └── tocisz/ebaz4205-hdmi-demo (v0.0.5-2-g13bc209)
```

## Note on "ahead/behind" counts on GitHub

When GitHub shows e.g. "707 ahead of and 4518 behind m1nl/ebaz4205" for the linux repo, it means:

- **707 ahead:** commits in our branch that m1nl's branch doesn't have
- **4518 behind:** commits in m1nl's branch that our branch doesn't have

Both branches share a common ancestor (the Xilinx v6.12.40 merge commit `90b13e`), but after that point they contain **different sets of commits** from the ADI tree, plus the ebaz4205 patches. This is not a linear relationship — they are parallel forks.
