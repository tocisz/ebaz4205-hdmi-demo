#!/usr/bin/env bash
# FULL INSTALL: flash the SD card attached to THIS machine (boot + rootfs).
#
# Usage:  sudo ./scripts/flash_rootfs.sh /dev/mmcblk0
#
# Rebuilds a complete bootable SD card from build_sdimg/:
#   - p1 (boot, FAT): refresh with the 6 boot files (BOOT.bin, boot.scr,
#     devicetree.dtb, pl-ebaz4205.dtbo, system_top.bit.bin, uImage)
#   - p2 (rootfs):    dd build_sdimg/rootfs.ext4 (kernel modules + depmod data)
#
# Only needed for rootfs *userspace* changes (busybox config, packages,
# inittab), a brand-new card, or to resync a board that drifted after live
# updates. For kernel/HDL iteration use ./scripts/ebaz_deploy.sh instead
# (live update over ssh, no dd).
#
# Safety:
#   - run as root (sudo); the SD card must be in THIS machine's card reader
#   - the board must be powered OFF / the card removed from the board
#   - refuses the machine's own disk (nvme/sd* patterns are checked; /dev/sda
#     explicitly refused), refuses any mounted partition
#   - requires you to type the partition name to confirm
#   - p1 is refreshed in place (existing FAT preserved); if p1 has no
#     recognized filesystem the script refuses rather than mkfs

set -euo pipefail

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
IMG="$ROOT/build_sdimg/rootfs.ext4"
BOOTDIR="$ROOT/build_sdimg"

[ -f "$IMG" ] || { echo "ERROR: $IMG missing -- run 'make sdimg' first" >&2; exit 1; }

[ "$(id -u)" -eq 0 ] || { echo "ERROR: run with sudo (dd/mount need root): sudo $0 $*" >&2; exit 1; }

DEV="${1:-}"
case "$DEV" in
  /dev/sd[a-z]|/dev/mmcblk[0-9]) : ;;
  *) echo "usage: sudo $0 /dev/sdX   or   sudo $0 /dev/mmcblk0   (whole device, see lsblk)" >&2; exit 1 ;;
esac

if [[ "$DEV" == /dev/mmcblk* ]]; then
  P1="${DEV}p1"; P2="${DEV}p2"
else
  P1="${DEV}1"; P2="${DEV}2"
fi

BOOT_FILES=(BOOT.bin boot.scr devicetree.dtb pl-ebaz4205.dtbo system_top.bit.bin uImage)
for f in "${BOOT_FILES[@]}"; do
  [ -f "$BOOTDIR/$f" ] || { echo "ERROR: missing $BOOTDIR/$f (run 'make sdimg' first)" >&2; exit 1; }
done

# --- safety checks ---------------------------------------------------------
for d in "$DEV" "$P1" "$P2"; do
  if findmnt -n -o SOURCE "$d" >/dev/null 2>&1; then
    echo "ERROR: $d is mounted -- unmount it first (umount $d)" >&2
    exit 1
  fi
done
if [ "$DEV" = "/dev/sda" ]; then
  echo "ERROR: refusing to touch /dev/sda (looks like this machine's own disk)" >&2
  echo "       check lsblk for the SD card device (e.g. /dev/sdb, /dev/mmcblk0)" >&2
  exit 1
fi
for d in "$P1" "$P2"; do
  if [ ! -b "$d" ]; then
    echo "ERROR: $d is not a block device -- is the SD card present? (lsblk)" >&2
    exit 1
  fi
done

# --- confirm ---------------------------------------------------------------
IMG_SIZE="$(du -h "$IMG" | cut -f1)"
echo "Target device: $DEV"
lsblk -o NAME,SIZE,FSTYPE,LABEL "$DEV"
echo
echo "This will:"
echo "  1. OVERWRITE $P2 with build_sdimg/rootfs.ext4 ($IMG_SIZE)"
echo "  2. refresh $P1 with: ${BOOT_FILES[*]}"
echo "The SD card must be in this machine and the board powered OFF."
read -r -p "Type '$P2' to confirm: " ans
[ "$ans" = "$P2" ] || { echo "aborted"; exit 1; }

# --- 1. rootfs -------------------------------------------------------------
echo "==> dd $IMG -> $P2 ..."
dd if="$IMG" of="$P2" bs=16M conv=fsync status=progress
sync

# --- 2. boot partition -----------------------------------------------------
FSTYPE="$(blkid -o value -s TYPE "$P1" 2>/dev/null || true)"
if [ -z "$FSTYPE" ]; then
  echo "ERROR: no filesystem detected on $P1 -- refusing to guess." >&2
  echo "       (if this is a brand-new card, format it first:" >&2
  echo "        mkfs.vfat -F 32 -n BOOT $P1, then re-run this script)" >&2
  exit 1
fi
[ "$FSTYPE" = "vfat" ] || [ "$FSTYPE" = "msdos" ] || {
  echo "ERROR: $P1 is $FSTYPE, expected vfat -- refusing to touch it" >&2; exit 1
}

MNT="$(mktemp -d /tmp/ebaz-boot.XXXXXX)"
trap 'umount "$MNT" 2>/dev/null || true; rmdir "$MNT" 2>/dev/null || true' EXIT
echo "==> Refreshing $P1 ($FSTYPE) ..."
mount "$P1" "$MNT"
for f in "${BOOT_FILES[@]}"; do
  cp -f "$BOOTDIR/$f" "$MNT/$f"
done
sync
umount "$MNT"
echo "==> Boot files written to $P1: ${BOOT_FILES[*]}"

echo "==> Done. Eject the card, reinsert it into the board, power on."
