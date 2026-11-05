"""Write ../nvmon.ico: the nvmon glyph (blue hexagon, teal beam, white
beacon) on a dark disc, at 16..256 px. Pure Python (zlib PNG entries), so
it runs on the bundled embeddable interpreter:

    python\\python.exe bin\\nvmon-window\\src\\make_icon.py
"""

from __future__ import annotations

import math
import struct
import zlib
from pathlib import Path

SIZES = (16, 20, 24, 32, 40, 48, 64, 128, 256)
SS = 4  # supersamples per axis

BG = (16, 18, 22)
RING = (38, 42, 48)
BLUE = (74, 144, 226)
TEAL = (45, 212, 191)
WHITE = (255, 255, 255)

# Glyph geometry in a 180x180 box (same as the header SVG).
HEX = [(90, 28), (144, 59), (144, 121), (90, 152), (36, 121), (36, 59)]
BEAM = [(90, 90), (138, 70), (138, 110)]


def _inside(poly, x, y):
    c = False
    j = len(poly) - 1
    for i in range(len(poly)):
        xi, yi = poly[i]
        xj, yj = poly[j]
        if (yi > y) != (yj > y) and x < (xj - xi) * (y - yi) / (yj - yi) + xi:
            c = not c
        j = i
    return c


def _seg_dist(px, py, a, b):
    ax, ay = a
    bx, by = b
    dx, dy = bx - ax, by - ay
    t = max(0.0, min(1.0, ((px - ax) * dx + (py - ay) * dy) / (dx * dx + dy * dy)))
    return math.hypot(px - (ax + t * dx), py - (ay + t * dy))


def _sample(u, v):
    """Colour + alpha at (u, v) in 0..1 icon space."""
    x, y = u * 180, v * 180
    d = math.hypot(x - 90, y - 90)
    if d > 88:
        return None
    col = RING if d > 82 else BG
    # hexagon outline, stroke ~9 units (reads at 16 px)
    edge = min(_seg_dist(x, y, HEX[i], HEX[(i + 1) % 6]) for i in range(6))
    if edge < 5.5:
        col = BLUE
    elif _inside(BEAM, x, y):
        col = tuple(int(c * 0.6 + b * 0.4) for c, b in zip(TEAL, BG))
    if math.hypot(x - 90, y - 90) < 13:
        col = WHITE
    return col


def render(size: int) -> bytes:
    rows = []
    for py in range(size):
        row = bytearray([0])  # PNG filter: none
        for px in range(size):
            r = g = b = a = 0
            for sy in range(SS):
                for sx in range(SS):
                    c = _sample((px + (sx + 0.5) / SS) / size, (py + (sy + 0.5) / SS) / size)
                    if c:
                        r += c[0]; g += c[1]; b += c[2]; a += 1
            n = SS * SS
            if a:
                row += bytes((r // a, g // a, b // a, 255 * a // n))
            else:
                row += b"\0\0\0\0"
        rows.append(bytes(row))
    raw = zlib.compress(b"".join(rows), 9)

    def chunk(tag, data):
        return struct.pack(">I", len(data)) + tag + data + struct.pack(">I", zlib.crc32(tag + data))

    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", ihdr) + chunk(b"IDAT", raw) + chunk(b"IEND", b"")


def main() -> None:
    pngs = [render(s) for s in SIZES]
    out = bytearray(struct.pack("<HHH", 0, 1, len(SIZES)))
    offset = 6 + 16 * len(SIZES)
    for s, png in zip(SIZES, pngs):
        out += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32, len(png), offset)
        offset += len(png)
    for png in pngs:
        out += png
    dest = Path(__file__).resolve().parent.parent / "nvmon.ico"
    dest.write_bytes(bytes(out))
    print(f"wrote {dest} ({len(out)} bytes)")


if __name__ == "__main__":
    main()
