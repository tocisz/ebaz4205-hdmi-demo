#!/usr/bin/env bash
# Install the on-board bf1 tool to /root on the EBAZ4205.
#
# Usage:
#   ./demos/brainfuck_org/install_to_board.sh          # host: ebaz
#   ./demos/brainfuck_org/install_to_board.sh root@192.168.1.10
#
# After install, on the board:
#   bf1 hello.b
#   bf1 /root/bf1-examples/sierpinski.b -n 1552
#   bf1 /root/bf1-examples/ghost.b -i

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

echo "Installing bf1 tools to $HOST:/root ..."

put "$HERE/run_bf1_program.py" /root/bf1
put "$HERE/comp_bf.py"         /root/comp_bf.py

ssh_cmd 'mkdir -p /root/bf1-examples'
put "$HERE/src/hello.b"      /root/bf1-examples/hello.b
put "$HERE/src/sierpinski.b" /root/bf1-examples/sierpinski.b
put "$HERE/src/squares.b"    /root/bf1-examples/squares.b
put "$HERE/src/ghost.b"      /root/bf1-examples/ghost.b

# Make `bf1` work in login shells AND non-interactive ssh commands.
ssh_cmd 'chmod 755 /root/bf1
ln -sfn /root/bf1 /usr/bin/bf1
cat > /root/bf1-examples/README.txt << "EOF"
bf1 — run Brainfuck on the FPGA CPU

  bf1 hello.b
  bf1 hello.b -n 64 -o /tmp/out.txt
  bf1 /root/bf1-examples/sierpinski.b -n 1552
  bf1 /root/bf1-examples/ghost.b -i     # interactive game (wasd + Enter)

  Quit interactive with Ctrl-C or Ctrl-].
  From a PC:  ssh -t HOST bf1 /root/bf1-examples/ghost.b -i

  bf1 --help

Files: /root/bf1  /root/comp_bf.py  /root/bf1-examples/
EOF
ls -la /root/bf1 /usr/bin/bf1 /root/comp_bf.py /root/bf1-examples/'

echo
echo "Done. On the board:"
echo "  ssh $HOST"
echo "  bf1 /root/bf1-examples/hello.b -n 32"
echo "  bf1 /root/bf1-examples/sierpinski.b -n 1552"
echo "  bf1 /root/bf1-examples/ghost.b -i"
echo "From PC interactive: ssh -t $HOST bf1 /root/bf1-examples/ghost.b -i"
