#!/usr/bin/env bash
# Install the Z80 on-board tools to /root on the EBAZ4205.
#
# Usage:
#   ./demos/z80_asm/install_to_board.sh          # host: ebaz
#   ./demos/z80_asm/install_to_board.sh root@192.168.1.10
#
# After install, on the board:
#   z80 counter.bin -n 64
#   z80 echo.bin -i
#   z80 walk.bin -n 256

set -euo pipefail

HOST="${1:-${EBAZ_HOST:-ebaz}}"
HERE="$(cd "$(dirname "$0")" && pwd)"

ssh_cmd() {
  ssh -o BatchMode=yes -o ConnectTimeout=10 "$HOST" "$@"
}

put() {
  local src="$1" remote="$2"
  ssh_cmd "cat > $(printf %q "$remote")" < "$src"
}

echo "Installing Z80 tools to $HOST:/root ..."

put "$HERE/run_z80_program.py" /root/z80
ssh_cmd 'chmod 755 /root/z80
mkdir -p /root/z80_board'
put "$HERE/z80_board/__init__.py" /root/z80_board/__init__.py
put "$HERE/z80_board/hw.py"        /root/z80_board/hw.py
put "$HERE/z80_board/images.py"    /root/z80_board/images.py
put "$HERE/z80_board/cli.py"       /root/z80_board/cli.py

# Pre-assemble binaries and upload them
cd "$HERE"

echo "  Assembling boot ROM..."
python3 assemble_z80.py src/boot.s --org 0x0000 -o bin/boot.bin -q

echo "  Assembling counter..."
python3 assemble_z80.py src/counter.s -o bin/counter.bin -q

echo "  Assembling echo..."
python3 assemble_z80.py src/echo.s -o bin/echo.bin -q

echo "  Assembling walk..."
python3 assemble_z80.py src/walk.s -o bin/walk.bin -q

ssh_cmd 'mkdir -p /root/z80-examples'
put bin/counter.bin /root/z80-examples/counter.bin
put bin/echo.bin    /root/z80-examples/echo.bin
put bin/walk.bin    /root/z80-examples/walk.bin
put bin/boot.bin    /root/z80-examples/boot.bin

# Symlink /root/z80 into PATH
ssh_cmd 'ln -sfn /root/z80 /usr/bin/z80
cat > /root/z80-examples/README.txt << "EOF"
z80 — interactive control of the Z80 SoC on the FPGA

The CPU stays alive between invocations.  Commands run left to right:

  z80 status                        # halted / running
  z80 halt                          # pause the CPU
  z80 load ram counter.bin          # write image into RAM @0x2000
  z80 load rom boot.bin             # write image into ROM @0x0000
  z80 dump ram 0x2000 64            # read back RAM as hex
  z80 flush reset run               # discard FIFO, PC=0, start
  z80 term                          # attach terminal (Ctrl-] to detach)
  z80 term --flush                  # same, but discard buffered output first

  One-liners:  z80 halt load ram counter.bin flush reset run
               ssh -t HOST z80 term

Legacy one-shot (halts CPU at end, kept for scripts):
  z80 counter.bin -n 64
  z80 echo.bin -i

  z80 --help

Files: /root/z80  /root/z80_board/  /root/z80-examples/
EOF
ls -la /root/z80 /usr/bin/z80 /root/z80-examples/'

echo
echo "Done. On the board:"
echo "  ssh $HOST"
echo "  z80 status"
echo "  z80 halt load ram /root/z80-examples/counter.bin flush reset run"
echo "  z80 term"
echo "  z80 /root/z80-examples/counter.bin -n 64   (legacy one-shot)"
echo "From PC: python3 demos/z80_asm/z80.py board halt load ram app.hex reset run"
