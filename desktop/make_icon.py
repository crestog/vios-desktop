"""
desktop/make_icon.py — draw the application icon, from source, with no image library.

`python -m desktop.make_icon` writes `desktop/vios.png` (what pywebview hands the
window, `__main__.py:260`) and `desktop/vios.ico` (what a Windows shortcut and the
taskbar want). Both are committed, so nothing has to run this to launch the app;
it exists so the icon has a *source*. A binary checked in with no way to
regenerate it is a file nobody can ever change by two pixels.

**Why no Pillow.** It is not in `requirements.txt` and the icon is not worth adding
a dependency for. PNG is a zlib stream with four length-prefixed chunks, and an ICO
since Vista is a directory of whole PNG files — both are short enough to write
directly, and `zlib` is in the standard library.

The mark is the app's own palette, not a new one: `--bg-deep` through `--surface-2`
for the tile, `--border-default` for its edge, `--accent-primary` for the play
triangle. Legibility at 16 px is the only real constraint on a Windows icon, which
rules out the graph-and-nodes drawing this application would otherwise deserve —
three connected dots at 16 px is three grey pixels. A play triangle survives, and
it is the honest subject: the thing this application is for is watching reels.

Antialiasing is supersampling, because there is no rasteriser here to ask for it.
Every shape is a signed distance function, so the *same* code rounds the tile's
corners and the triangle's, and a sample is inside when its distance is negative.
"""

from __future__ import annotations

import math
import os
import struct
import zlib

_HERE = os.path.dirname(os.path.abspath(__file__))

# From web/src/styles/tokens.css. Duplicated rather than parsed: this runs once
# by hand, and a CSS parser here would be more code than the icon.
TILE_TOP = (0x15, 0x15, 0x20)
TILE_BOT = (0x07, 0x07, 0x0B)
EDGE = (0x2E, 0x2E, 0x3F)
ACCENT = (0x81, 0x8C, 0xF8)

# Every size Windows asks for. 16 is the one that matters — Explorer's list view
# and the taskbar's small-icon mode both use it, and an icon that only works at
# 256 is an icon nobody sees.
SIZES = (16, 32, 48, 64, 128, 256)


def _rrect(px: float, py: float, hw: float, hh: float, r: float) -> float:
    """Signed distance to a rounded rectangle centred on the origin."""
    qx = abs(px) - (hw - r)
    qy = abs(py) - (hh - r)
    outside = math.hypot(max(qx, 0.0), max(qy, 0.0))
    inside = min(max(qx, qy), 0.0)
    return outside + inside - r


# The play triangle, in the unit square. Nudged right of centre because a
# triangle's visual weight sits behind its apex, so a geometrically centred one
# looks like it is falling off the left edge.
_TRI = ((0.365, 0.245), (0.365, 0.755), (0.775, 0.500))
_TRI_R = 0.035          # corner rounding, in the same units


def _tri_edges():
    """Outward normals and offsets for the triangle's three half-planes."""
    out = []
    n = len(_TRI)
    # Signed area decides the winding, which decides which way the normals face.
    area = sum(_TRI[i][0] * _TRI[(i + 1) % n][1] - _TRI[(i + 1) % n][0] * _TRI[i][1]
               for i in range(n)) / 2.0
    sign = 1.0 if area > 0 else -1.0
    for i in range(n):
        ax, ay = _TRI[i]
        bx, by = _TRI[(i + 1) % n]
        ex, ey = bx - ax, by - ay
        length = math.hypot(ex, ey) or 1.0
        # Rotate the edge to get a normal, and flip it outward by the winding.
        nx, ny = (ey / length) * sign, (-ex / length) * sign
        out.append((nx, ny, nx * ax + ny * ay))
    return tuple(out)


_TRI_EDGES = _tri_edges()


def _tri(px: float, py: float, r: float = _TRI_R) -> float:
    """Distance to the rounded triangle. Negative inside.

    `max` over the half-planes is the standard convex-polygon distance, and
    subtracting a radius rounds every corner at once — the same trick as the tile,
    which is the reason both shapes are written as distances at all.
    """
    return max(nx * px + ny * py - off for nx, ny, off in _TRI_EDGES) - r


def _lerp(a: tuple, b: tuple, t: float) -> tuple:
    return tuple(a[i] + (b[i] - a[i]) * t for i in range(3))


def _over(dst: tuple, src: tuple, alpha: float) -> tuple:
    """Source-over, on straight (non-premultiplied) colour."""
    return tuple(dst[i] + (src[i] - dst[i]) * alpha for i in range(3))


def render(size: int) -> bytes:
    """One RGBA image, top row first — the order PNG wants."""
    # 16 samples per pixel below 64, where a jagged edge is the whole difference
    # between a mark and a mess; 9 above, where it is already invisible.
    ss = 4 if size <= 64 else 3
    inv = 1.0 / (ss * ss)
    step = 1.0 / (size * ss)
    half = step / 2.0

    # A hairline at any size: 1 device pixel at 16, a little more at 256, never
    # the thick frame a fixed fraction of the canvas would draw on a small icon.
    edge_w = (1.0 if size <= 32 else 1.5 if size <= 64 else 2.0) / size
    hw = hh = 0.5 - (1.0 / size)        # a pixel of breathing room in the corners

    # Both radii shrink on the small sizes, and this is the one place the drawing
    # is not scale-invariant. A radius that is the right *fraction* at 256 eats
    # three of sixteen pixels per corner at 16 and the tile stops reading as a
    # square; the same for the triangle, whose rounded apex at 16 px is simply a
    # blunt one. Type designers call this optical sizing and it is the same
    # problem: a shape that is correct large is not correct small.
    radius = 0.215 if size >= 48 else 0.185 if size >= 32 else 0.155
    tri_r = _TRI_R if size >= 48 else 0.018 if size >= 32 else 0.0

    rows = bytearray()
    for y in range(size):
        row = bytearray()
        for x in range(size):
            acc_r = acc_g = acc_b = acc_a = 0.0
            for sy in range(ss):
                py = (y * ss + sy) * step + half
                for sx in range(ss):
                    px = (x * ss + sx) * step + half
                    d = _rrect(px - 0.5, py - 0.5, hw, hh, radius)
                    if d > 0.0:
                        continue                     # outside the tile: transparent
                    col = _lerp(TILE_TOP, TILE_BOT, py)
                    if d > -edge_w:
                        col = EDGE
                    if _tri(px, py, tri_r) <= 0.0:
                        col = ACCENT
                    acc_r += col[0]
                    acc_g += col[1]
                    acc_b += col[2]
                    acc_a += 1.0
            if acc_a <= 0.0:
                row += b"\x00\x00\x00\x00"
                continue
            # Average the *covered* samples only, then let coverage be the alpha.
            # Averaging over all of them instead would darken every edge toward
            # black — the classic halo, and it shows most at 16 px.
            a = acc_a * inv
            row += bytes((round(acc_r / acc_a), round(acc_g / acc_a),
                          round(acc_b / acc_a), round(a * 255)))
        rows += b"\x00" + row            # filter byte 0: this row is stored raw
    return bytes(rows)


def _chunk(kind: bytes, payload: bytes) -> bytes:
    body = kind + payload
    return struct.pack(">I", len(payload)) + body + struct.pack(
        ">I", zlib.crc32(body) & 0xFFFFFFFF)


def png(size: int) -> bytes:
    """A minimal RGBA PNG: signature, IHDR, IDAT, IEND."""
    ihdr = struct.pack(">IIBBBBB", size, size, 8, 6, 0, 0, 0)
    return (b"\x89PNG\r\n\x1a\n"
            + _chunk(b"IHDR", ihdr)
            + _chunk(b"IDAT", zlib.compress(render(size), 9))
            + _chunk(b"IEND", b""))


def ico(images: dict) -> bytes:
    """An ICO wrapping whole PNGs — one directory entry each.

    Every Windows since Vista reads PNG-compressed entries, and writing BMP
    entries instead would mean an upside-down bottom-up scanline order and a
    separate 1-bit AND mask for something already carrying an alpha channel.
    """
    sizes = sorted(images)
    header = struct.pack("<HHH", 0, 1, len(sizes))
    offset = len(header) + 16 * len(sizes)
    entries = bytearray()
    blobs = bytearray()
    for s in sizes:
        data = images[s]
        # 256 is written as 0: the field is one byte and 256 does not fit.
        entries += struct.pack("<BBBBHHII", s % 256, s % 256, 0, 0, 1, 32,
                               len(data), offset)
        blobs += data
        offset += len(data)
    return bytes(header + entries + blobs)


def main() -> int:
    images = {s: png(s) for s in SIZES}
    png_path = os.path.join(_HERE, "vios.png")
    ico_path = os.path.join(_HERE, "vios.ico")
    # 256 for the PNG: pywebview scales it down, and every other consumer of a
    # loose PNG (a .desktop file, a README) wants the big one too.
    with open(png_path, "wb") as f:
        f.write(images[256])
    with open(ico_path, "wb") as f:
        f.write(ico(images))
    print(f"{png_path}  {len(images[256])} bytes")
    print(f"{ico_path}  {os.path.getsize(ico_path)} bytes  "
          f"({', '.join(str(s) for s in SIZES)})")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
