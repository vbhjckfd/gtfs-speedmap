"""Route shapes redrawn from where the buses actually drove.

The feed's shapes are sparse — vertices up to a kilometre apart — so a leg drawn
along one cuts across blocks, and routes without a shape are drawn stop to stop.
The vehicles themselves report where they are every few seconds, which over a
month traces each street many times over.

Every timed leg has a reference line: its stretch of the feed's shape, or the
straight line between its stops. Each sample a bus sent while driving that leg
is placed along the line and to its left or right, and the placings are summed
in TRACE_BIN_M bins (the ingest side). At build time a bin's mean placing is a
point of the street, and the bins in order are the leg's new line:

    data/trace/YYYY-MM-DD.parquet  (route, direction, pair, ref_m, bin) -> n, sum_s, sum_off

Along and off rather than coordinates, because UTM cannot be turned back into
lat/lon here, and an offset from a line is the same length in either.
"""

from __future__ import annotations

import math
from bisect import bisect_left, bisect_right

import pandas as pd

from .config import SHAPE_TOLERANCE_M, SPEED_MAX_MPS, TRACE_BIN_M, TRACE_MIN_N
from .geometry import cumulative, simplify
from .grid import project

# ref_m is the length of the reference line the bins were placed on: the feed
# re-cuts a leg's shape when its timetable changes, and metres along one line
# mean nothing on another.
TRACE_KEYS = ["route_id", "direction", "from_stop", "to_stop", "ref_m"]
# Bins match the month's line within this; its shape went through JSON rounding.
REF_MATCH_M = 2
TRACE_COLUMNS = [*TRACE_KEYS, "bin", "n", "sum_s", "sum_off"]

M_PER_DEG = 111320


def metres(a, b) -> float:
    """Ground distance between two [lat, lon] points, flat-earth, fine across a city."""
    dy = (a[0] - b[0]) * M_PER_DEG
    dx = (a[1] - b[1]) * M_PER_DEG * math.cos(math.radians(a[0]))
    return math.hypot(dx, dy)


def along(points: list, cum: list[float], at: float) -> list[float]:
    """The point `at` metres into a polyline whose vertices sit `cum` metres in."""
    for (p, q), (c0, c1) in zip(zip(points, points[1:]), zip(cum, cum[1:])):
        if c1 >= at:
            t = (at - c0) / (c1 - c0) if c1 > c0 else 0
            return [p[0] + (q[0] - p[0]) * t, p[1] + (q[1] - p[1]) * t]
    return list(points[-1][:2])


def leg_line(route: dict, i: int, stops: dict) -> list[list[float]]:
    """Leg i of a route as the viewer draws it: its shape between the two stops.

    Mirrors legLine in web/app.js; a route without a shape is drawn stop to stop.
    `stops` maps stop id to [lat, lon, ...].
    """
    a, b = route["path"][i], route["path"][i + 1]
    straight = [list(stops[s][:2]) for s in (a, b) if s in stops]
    shape, dist = route.get("shape"), route.get("dist")
    if not shape or not dist or not dist[i + 1] > dist[i]:
        return straight
    cum = [0.0]
    for p, q in zip(shape, shape[1:]):
        cum.append(cum[-1] + metres(p, q))
    start, end = dist[i], dist[i + 1]
    inside = [list(p[:2]) for p, c in zip(shape, cum) if start < c < end]
    return [along(shape, cum, start), *inside, along(shape, cum, end)]


def _placed(xy: list, cum: list[float], x: float, y: float) -> tuple[float, float]:
    """(metres along, metres to the left) of the nearest point of the line."""
    best = (math.inf, 0.0, 0.0)
    for i, ((ax, ay), (bx, by)) in enumerate(zip(xy, xy[1:])):
        dx, dy = bx - ax, by - ay
        length2 = dx * dx + dy * dy
        t = 0.0 if length2 == 0 else max(0.0, min(1.0, ((x - ax) * dx + (y - ay) * dy) / length2))
        d = math.hypot(x - (ax + t * dx), y - (ay + t * dy))
        if d < best[0]:
            side = 1.0 if dx * (y - ay) - dy * (x - ax) >= 0 else -1.0
            best = (d, cum[i] + t * math.sqrt(length2), side * d)
    return best[1], best[2]


def utm_line(line: list) -> tuple[list[tuple[float, float]], list[float]]:
    xy = [project(lon, lat) for lat, lon in line]
    return xy, cumulative(xy)


def trace_leg(
    run: list[tuple[int, float, float]],
    times: list[int],
    start: int,
    end: int,
    line: tuple[list, list[float]],
    max_off: float,
    key: tuple,
    trace: dict,
) -> None:
    """Fold the samples a run sent between two stop passes into the leg's bins."""
    xy, cum = line
    if len(xy) < 2:
        return
    for _, x, y in run[bisect_left(times, start) : bisect_right(times, end)]:
        s, off = _placed(xy, cum, x, y)
        if abs(off) > max_off:
            continue  # a diversion or a GPS jump, not this street
        bucket = trace.setdefault((*key, round(cum[-1]), int(s // TRACE_BIN_M)), [0, 0.0, 0.0])
        bucket[0] += 1
        bucket[1] += s
        bucket[2] += off


def _point(line: list, xy: list, cum: list[float], s: float, off: float) -> list[float]:
    """`s` metres along the line and `off` metres to its left, as [lat, lon].

    The fraction along a segment carries over from UTM to lat/lon, and so does
    "perpendicular": both are conformal, they differ by a small rotation.
    """
    i = max(0, min(bisect_right(cum, s) - 1, len(cum) - 2))
    t = (s - cum[i]) / (cum[i + 1] - cum[i]) if cum[i + 1] > cum[i] else 0.0
    (lat0, lon0), (lat1, lon1) = line[i][:2], line[i + 1][:2]
    lat, lon = lat0 + (lat1 - lat0) * t, lon0 + (lon1 - lon0) * t
    k = M_PER_DEG * math.cos(math.radians(lat))
    north, east = (lat1 - lat0) * M_PER_DEG, (lon1 - lon0) * k
    length = math.hypot(north, east)
    if length == 0:
        return [lat, lon]
    # Left of travel is (-north, east) in (east, north).
    return [lat + off * east / length / M_PER_DEG, lon - off * north / length / k]


def dense_shapes(
    paths: list[dict], stops: dict, trace: pd.DataFrame, seconds: dict[tuple, float]
) -> list[dict]:
    """The routes with each traced leg redrawn through its buses' mean positions.

    A leg with no bin of TRACE_MIN_N samples on its current reference line keeps
    that line, unless covering it in the leg's mean `seconds` would take more
    than SPEED_MAX_MPS: then the feed's dist points at the wrong pass of the
    shape and the leg is drawn stop to stop. A route with nothing redrawn is
    returned as it came. Each leg is simplified on its own, so every stop stays
    a vertex and `dist` stays exact at the stops.
    """
    bins: dict[tuple, dict[int, list[tuple[float, float]]]] = {}
    if not trace.empty:
        trace = trace.groupby([*TRACE_KEYS, "bin"], sort=False)[["n", "sum_s", "sum_off"]].sum()
        trace = trace[trace["n"] >= TRACE_MIN_N].sort_index()
        for (*key, ref_m, _), (n, sum_s, sum_off) in zip(trace.index, trace.to_numpy()):
            by_ref = bins.setdefault(tuple(key), {})
            by_ref.setdefault(int(ref_m), []).append((sum_s / n, sum_off / n))

    return [_redrawn(route, stops, bins, seconds) or route for route in paths]


def _placings(by_ref: dict[int, list], length: float) -> list:
    return next((p for ref_m, p in by_ref.items() if abs(ref_m - length) <= REF_MATCH_M), [])


def _redrawn(route: dict, stops: dict, bins: dict, seconds: dict) -> dict | None:
    legs, changed = [], False
    for i, (a, b) in enumerate(zip(route["path"], route["path"][1:])):
        line = leg_line(route, i, stops)
        if len(line) < 2:
            return None  # a stop without coordinates: no way to place the rest
        key = (route["route"], route["dir"], a, b)
        xy, cum = utm_line(line)
        placings = _placings(bins.get(key, {}), cum[-1])
        if placings:
            changed = True
            line = [line[0], *(_point(line, xy, cum, s, off) for s, off in placings), line[-1]]
        elif len(line) > 2 and cum[-1] > SPEED_MAX_MPS * seconds.get(key, math.inf):
            changed = True
            line = [line[0], line[-1]]
        keep = simplify(utm_line(line)[0], SHAPE_TOLERANCE_M)
        legs.append([line[k] for k in keep])
    if not changed:
        return None

    shape, dist, total = [legs[0][0]], [0], 0.0
    for leg in legs:
        for p in leg[1:]:
            total += metres(shape[-1], p)
            shape.append(p)
        dist.append(round(total))
    return {
        **route,
        "shape": [[round(c, 5) for c in p] for p in shape],
        "dist": dist,
    }
