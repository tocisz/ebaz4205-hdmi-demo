#!/usr/bin/env bash
# Build the SD image and deploy it to the EBAZ4205 board.
#
# Usage:
#   ./scripts/ebaz_deploy.sh                # make sdimg + deploy to ${EBAZ_HOST:-ebaz}
#   ./scripts/ebaz_deploy.sh --skip-build   # deploy existing build_sdimg/ artifacts
#   ./scripts/ebaz_deploy.sh root@192.168.1.203
#
# Steps:
#   1. make sdimg                     -> build_sdimg/ (BOOT.bin, uImage, dtb, dtbo, bitstream)
#   2. scp -O the 6 boot files to <host>:/mnt/   (boot partition; SFTP missing -> -O)
#   3. sync + reboot the board
#   4. wait for it to come back (ping loop, then sshd poll) and print uname

set -euo pipefail

HOST="${EBAZ_HOST:-ebaz}"
SKIP_BUILD=0

for arg in "$@"; do
  case "$arg" in
    --skip-build) SKIP_BUILD=1 ;;
    -h|--help)
      grep '^#' "$0" | sed 's/^# \{0,1\}//'
      exit 0
      ;;
    -*) echo "unknown option: $arg" >&2; exit 1 ;;
    *) HOST="$arg" ;;
  esac
done

HERE="$(cd "$(dirname "$0")" && pwd)"
ROOT="$(cd "$HERE/.." && pwd)"
SDIMG="$ROOT/build_sdimg"

FILES=(BOOT.bin boot.scr devicetree.dtb pl-ebaz4205.dtbo system_top.bit.bin uImage)  # boot partition (/mnt/)

if [ "$SKIP_BUILD" -eq 0 ]; then
  echo "==> make sdimg ..."
  (cd "$ROOT" && make sdimg)
fi

echo "==> Verifying artifacts ..."
for f in "${FILES[@]}"; do
  [ -f "$SDIMG/$f" ] || { echo "missing $SDIMG/$f (run without --skip-build)" >&2; exit 1; }
  echo "    $f ($(du -h "$SDIMG/$f" | cut -f1))"
done

echo "==> Checking board $HOST is online ..."
ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" 'echo "    board ok: $(uname -n) $(uname -r)"' || {
  echo "board $HOST not reachable before deploy" >&2
  exit 1
}

echo "==> Copying boot files to $HOST:/mnt/ ..."
scp -O -o ConnectTimeout=10 "${FILES[@]/#/$SDIMG/}" "$HOST":/mnt/

echo "==> Syncing and rebooting ..."
ssh "$HOST" 'sync; reboot' || true   # ssh drops while rebooting; that is expected

IP="$(ssh -G "$HOST" 2>/dev/null | awk '/^hostname /{print $2; exit}')"
IP="${IP:-192.168.1.203}"

echo "==> Waiting for $HOST ($IP) to respond to ping ..."
deadline=$((SECONDS + 180))
while [ $SECONDS -lt $deadline ]; do
  if ping -c 1 -W 1 "$IP" >/dev/null 2>&1; then
    break
  fi
  sleep 2
done
if ! ping -c 1 -W 1 "$IP" >/dev/null 2>&1; then
  echo "board did not respond to ping within 180s" >&2
  exit 1
fi

echo "==> ICMP up, waiting for sshd ..."
n=0
until ssh -o BatchMode=yes -o ConnectTimeout=5 -o StrictHostKeyChecking=accept-new "$HOST" 'true' 2>/dev/null; do
  n=$((n + 1))
  if [ "$n" -ge 60 ]; then
    echo "sshd did not come up within 300s" >&2
    exit 1
  fi
  sleep 5
done

echo "==> Board is back online:"
ssh "$HOST" 'uname -a; ls /mnt/'
echo "==> Done."
