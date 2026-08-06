"""Render web/og.png — the social preview — from the aggregated data itself.

Deliberately dependency-free: a 1200x630 RGB buffer written out as a PNG by
hand, so the project keeps its `pip install` down to the pipeline's needs. No
basemap, just the dots on a dark ground, which is what the map is recognisable
by anyway.

Run: python -m speedmap.make_og
"""

from __future__ import annotations

import json
import math
import struct
import sys
import zlib

from .config import SCALE_HIGH_KMH, SCALE_LOW_KMH, WEB_DATA_DIR

WIDTH, HEIGHT = 1200, 630
MARGIN = 28
BACKGROUND = (14, 20, 28)
DOT_RADIUS = 2
SOURCE = "all-08"  # 08:00 — the busiest hour, and the most red/green contrast

# Same ramp as web/app.js.
RAMP = ((0.0, (214, 40, 40)), (0.5, (240, 166, 32)), (1.0, (46, 158, 74)))


def ramp(t: float) -> tuple[int, int, int]:
    x = min(1.0, max(0.0, t))
    for i in range(1, len(RAMP)):
        p1, c1 = RAMP[i - 1]
        p2, c2 = RAMP[i]
        if x <= p2:
            f = (x - p1) / (p2 - p1)
            return tuple(round(c1[j] + (c2[j] - c1[j]) * f) for j in range(3))
    return RAMP[-1][1]


def _clip(values: list[float], tail: float = 0.01) -> tuple[float, float]:
    """The value range holding all but the outermost `tail` at each end."""
    ordered = sorted(values)
    last = len(ordered) - 1
    return ordered[int(last * tail)], ordered[int(last * (1 - tail))]


def mercator_y(lat: float) -> float:
    return math.log(math.tan(math.pi / 4 + math.radians(lat) / 2))


def write_png(path, pixels: bytearray, width: int, height: int) -> None:
    raw = bytearray()
    stride = width * 3
    for row in range(height):
        raw.append(0)  # filter type 0 (None)
        raw += pixels[row * stride : (row + 1) * stride]

    def chunk(tag: bytes, data: bytes) -> bytes:
        return (
            struct.pack(">I", len(data))
            + tag
            + data
            + struct.pack(">I", zlib.crc32(tag + data) & 0xFFFFFFFF)
        )

    png = b"\x89PNG\r\n\x1a\n"
    png += chunk(b"IHDR", struct.pack(">IIBBBBB", width, height, 8, 2, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(bytes(raw), 9))
    png += chunk(b"IEND", b"")
    path.write_bytes(png)


def build() -> None:
    source = WEB_DATA_DIR / f"{SOURCE}.json"
    if not source.exists():
        raise SystemExit(f"{source} missing — run `make build` first")
    data = json.loads(source.read_text())
    lats, lons, speeds = data["lat"], data["lon"], data["v"]
    if not lats:
        raise SystemExit(f"{source} has no cells")

    # Fit the drawing to the data, keeping the Mercator aspect honest so the
    # city is not stretched. A handful of long suburban routes reach far past
    # the city; framing on the full extent shrinks Lviv to a blob in the
    # middle, so clip the frame to the bulk of the cells.
    lat_lo, lat_hi = _clip(lats)
    lon_lo, lon_hi = _clip(lons)
    y_lo, y_hi = mercator_y(lat_lo), mercator_y(lat_hi)
    span_x = math.radians(lon_hi - lon_lo)
    span_y = y_hi - y_lo
    scale = min((WIDTH - 2 * MARGIN) / span_x, (HEIGHT - 2 * MARGIN) / span_y)
    off_x = (WIDTH - span_x * scale) / 2
    off_y = (HEIGHT - span_y * scale) / 2

    pixels = bytearray(BACKGROUND * (WIDTH * HEIGHT))

    for lat, lon, kmh in zip(lats, lons, speeds):
        px = int(off_x + (math.radians(lon) - math.radians(lon_lo)) * scale)
        py = int(off_y + (y_hi - mercator_y(lat)) * scale)
        r, g, b = ramp((kmh - SCALE_LOW_KMH) / (SCALE_HIGH_KMH - SCALE_LOW_KMH))
        for dy in range(-DOT_RADIUS, DOT_RADIUS + 1):
            for dx in range(-DOT_RADIUS, DOT_RADIUS + 1):
                if dx * dx + dy * dy > DOT_RADIUS * DOT_RADIUS:
                    continue
                x, y = px + dx, py + dy
                if 0 <= x < WIDTH and 0 <= y < HEIGHT:
                    i = (y * WIDTH + x) * 3
                    pixels[i : i + 3] = bytes((r, g, b))

    out = WEB_DATA_DIR.parent / "og.png"
    write_png(out, pixels, WIDTH, HEIGHT)
    print(f"{out} — {len(lats):,} cells, {out.stat().st_size / 1024:.0f} KB")


def main(argv: list[str] | None = None) -> int:
    build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
