"""Image parsing for the Z80 board tool.

Two formats are supported for ``load rom|ram``:

1. **Intel HEX** (``.hex``/``.ihx``) — records carry their own addresses.
   This is what ``z80-unknown-coff-objcopy -O ihex -j.ram`` produces in
   ~/repos/z80/RC2014-nascom/.
2. **Raw binary** (everything else) — a contiguous blob placed at a single
   address supplied on the command line (or the space default).

Detection is content-based: a file whose first non-whitespace byte is ':'
is treated as Intel HEX; anything else is binary.
"""

from pathlib import Path


class ImageError(ValueError):
    """Raised when an image file is malformed."""


# ---------------------------------------------------------------------------
# Intel HEX
# ---------------------------------------------------------------------------

def parse_intel_hex(text: str) -> list[tuple[int, bytes]]:
    """Parse Intel HEX text into a list of (address, bytes) segments.

    Records in the file are coalesced into contiguous segments; gaps
    between records create separate segments.  Handles record types:
      00 data, 01 EOF, 02 (8086 segment base), 04 (extended linear base).
    Types 03 and 05 are rejected as unsupported.
    """
    segments: list[tuple[int, bytes]] = []
    run_addr: int | None = None
    run_buf = bytearray()
    base = 0          # extended linear/segment base added to record addresses

    for lineno, line in enumerate(text.splitlines(), 1):
        line = line.strip()
        if not line:
            continue
        if not line.startswith(":"):
            raise ImageError(f"line {lineno}: expected ':' record, got: {line[:40]!r}")
        try:
            raw = bytes.fromhex(line[1:])
        except ValueError:
            raise ImageError(f"line {lineno}: invalid hex digits") from None
        if len(raw) < 5:
            raise ImageError(f"line {lineno}: record too short")
        count = raw[0]
        addr = (raw[1] << 8) | raw[2]
        rtype = raw[3]
        if len(raw) != 5 + count:
            raise ImageError(f"line {lineno}: payload length mismatch")
        if (sum(raw[:4 + count]) + raw[4 + count]) & 0xFF != 0:
            raise ImageError(f"line {lineno}: checksum error")
        data = raw[4:4 + count]

        if rtype == 0x01:                       # EOF
            break
        elif rtype == 0x00:                     # data
            full = base + addr
            if run_addr is None:
                run_addr, run_buf = full, bytearray()
            elif full != run_addr + len(run_buf):
                segments.append((run_addr, bytes(run_buf)))
                run_addr, run_buf = full, bytearray()
            run_buf.extend(data)
        elif rtype == 0x02:                     # segment base (8086)
            if len(data) != 2:
                raise ImageError(f"line {lineno}: type 02 needs 2 data bytes")
            base = ((data[0] << 8) | data[1]) << 4
        elif rtype == 0x04:                     # extended linear base
            if len(data) != 2:
                raise ImageError(f"line {lineno}: type 04 needs 2 data bytes")
            base = ((data[0] << 8) | data[1]) << 16
        elif rtype in (0x03, 0x05):             # start address records
            raise ImageError(
                f"line {lineno}: record type {rtype:02X} (start address) "
                "is not supported")
        else:
            raise ImageError(f"line {lineno}: unknown record type {rtype:02X}")

    if run_addr is not None:
        segments.append((run_addr, bytes(run_buf)))
    return segments


# ---------------------------------------------------------------------------
# Format detection
# ---------------------------------------------------------------------------

def parse_image(path: str | Path) -> tuple[str, object]:
    """Read ``path`` and detect raw binary vs Intel HEX.

    Returns:
        ("bin", raw_bytes)                     — for raw binary images
        ("hex", [(addr, bytes), ...])           — for Intel HEX images
    """
    raw = Path(path).read_bytes()
    first_line = raw.lstrip().split(b"\n", 1)[0].strip()
    if first_line[:1] == b":":                 # Intel HEX always starts with ':'
        text = raw.decode("ascii", errors="replace")
        return "hex", parse_intel_hex(text)
    return "bin", raw


# ---------------------------------------------------------------------------
# Output formatting
# ---------------------------------------------------------------------------

def format_hexdump(data: bytes, base: int = 0):
    """Yield hexdump lines (16 bytes each) for ``data`` starting at ``base``."""
    for i in range(0, len(data), 16):
        chunk = data[i:i + 16]
        hex_str = " ".join(f"{b:02x}" for b in chunk)
        ascii_str = "".join(chr(b) if 0x20 <= b < 0x7f else "." for b in chunk)
        yield f"{base + i:04x}: {hex_str:<48s}  {ascii_str}"
