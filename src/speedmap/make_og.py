"""Render web/og.png — the social preview — from the built map files themselves.

Deliberately dependency-free: a 1200x630 RGB buffer written out as a PNG by
hand, so the project keeps its `pip install` down to the pipeline's needs. No
basemap, just every leg on a dark ground, coloured as the map colours it.

Run: make og
"""

from __future__ import annotations

import json
import math
import struct
import sys
import zlib

from .config import WEB_DATA_DIR

WIDTH, HEIGHT = 1200, 630
MARGIN = 28
BACKGROUND = (14, 20, 28)
LINE_RADIUS = 2.2
# Weekday 08:00 — the busiest hour, and the most red/green contrast.
DAYTYPE, HOUR = "wd", 8
METRIC = "med"
# Same as web/app.js.
DELAY_HIGH = 1.0
OFFPEAK_MIN_RIDES = 10
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


def leg_delays(routes: list[dict], hourly: dict[int, dict], hour: int) -> dict:
    """Delay per stop pair at `hour`, as the viewer computes it.

    Routes sharing a stop pair are pooled, weighted by rides; the off-peak is
    the quickest hour with at least OFFPEAK_MIN_RIDES pooled rides.
    """
    pooled: dict[tuple[str, str], dict[int, list[float]]] = {}
    for r in routes:
        key = f"{r['route']}|{r['dir']}"
        for h, data in hourly.items():
            row = data.get(key)
            if not row:
                continue
            for i, (a, b) in enumerate(zip(r["path"], r["path"][1:])):
                t, n = row[METRIC][i], row["n"][i]
                if t is None or not n:
                    continue
                acc = pooled.setdefault((a, b), {}).setdefault(h, [0.0, 0])
                acc[0] += t * n
                acc[1] += n
    delays = {}
    for pair, by_hour in pooled.items():
        if hour not in by_hour:
            continue
        busy = [s / n for s, n in by_hour.values() if n >= OFFPEAK_MIN_RIDES]
        if busy:
            s, n = by_hour[hour]
            delays[pair] = s / n / min(busy) - 1
    return delays


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


def _clip(values: list[float], tail: float = 0.15) -> tuple[float, float]:
    """The value range holding all but the outermost `tail` at each end."""
    ordered = sorted(values)
    last = len(ordered) - 1
    return ordered[int(last * tail)], ordered[int(last * (1 - tail))]


def _shape_legs(route: dict, stops: dict):
    """(leg index, start point, end point) for each segment of the route's shape.

    A route whose stops its shape cannot account for has none; it is drawn as
    the chain of its own stops, as the viewer does.
    """
    if "shape" not in route:
        points = [stops[s] for s in route["path"]]
        yield from ((i, p, q) for i, (p, q) in enumerate(zip(points, points[1:])))
        return
    shape, dist = route["shape"], route["dist"]
    along, leg = 0.0, 0
    for p, q in zip(shape, shape[1:]):
        while leg < len(dist) - 2 and along >= dist[leg + 1]:
            leg += 1
        yield leg, p, q
        dy = (q[0] - p[0]) * 111_320
        dx = (q[1] - p[1]) * 111_320 * math.cos(math.radians(p[0]))
        along += math.hypot(dx, dy)


def build() -> None:
    index = json.loads((WEB_DATA_DIR / "index.json").read_text())
    full = [m["key"] for m in index["months"] if not m["part"]]
    month = (full or [m["key"] for m in index["months"]])[-1]
    paths = json.loads((WEB_DATA_DIR / f"paths-{month}.json").read_text())
    hourly = {}
    for h in index["hours"]:
        f = WEB_DATA_DIR / f"rides-{month}-{DAYTYPE}-{h:02d}.json"
        if f.exists():
            hourly[h] = json.loads(f.read_text())
    delays = leg_delays(paths["routes"], hourly, HOUR)
    if not delays:
        raise SystemExit(f"{month}: no timed legs — run `make build` first")

    # Frame on the bulk of the stops: a few suburban routes reach far past the
    # city and would shrink Lviv to a blob in the middle.
    stops = paths["stops"].values()
    lat_lo, lat_hi = _clip([s[0] for s in stops])
    lon_lo, lon_hi = _clip([s[1] for s in stops])
    y_hi = mercator_y(lat_hi)
    span_x = math.radians(lon_hi - lon_lo)
    span_y = y_hi - mercator_y(lat_lo)
    scale = min((WIDTH - 2 * MARGIN) / span_x, (HEIGHT - 2 * MARGIN) / span_y)
    off_x = (WIDTH - span_x * scale) / 2
    off_y = (HEIGHT - span_y * scale) / 2

    def px(p):
        return (
            off_x + (math.radians(p[1]) - math.radians(lon_lo)) * scale,
            off_y + (y_hi - mercator_y(p[0])) * scale,
        )

    segments = []
    for r in paths["routes"]:
        for leg, p, q in _shape_legs(r, paths["stops"]):
            d = delays.get((r["path"][leg], r["path"][leg + 1]))
            if d is not None:
                segments.append((d, px(p), px(q)))
    segments.sort(key=lambda s: s[0])  # red on top, where the story is

    pixels = bytearray(BACKGROUND * (WIDTH * HEIGHT))
    reach = math.ceil(LINE_RADIUS)
    for d, (x0, y0), (x1, y1) in segments:
        colour = bytes(ramp(1 - d / DELAY_HIGH))
        steps = max(1, int(math.hypot(x1 - x0, y1 - y0)))
        for k in range(steps + 1):
            cx, cy = x0 + (x1 - x0) * k / steps, y0 + (y1 - y0) * k / steps
            for y in range(int(cy) - reach, int(cy) + reach + 1):
                for x in range(int(cx) - reach, int(cx) + reach + 1):
                    if 0 <= x < WIDTH and 0 <= y < HEIGHT and (x - cx) ** 2 + (y - cy) ** 2 <= LINE_RADIUS**2:
                        i = (y * WIDTH + x) * 3
                        pixels[i : i + 3] = colour

    out = WEB_DATA_DIR.parent / "og.png"
    write_png(out, pixels, WIDTH, HEIGHT)
    print(f"{out} — {month}, {len(delays):,} legs, {out.stat().st_size / 1024:.0f} KB")


def main(argv: list[str] | None = None) -> int:
    build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
