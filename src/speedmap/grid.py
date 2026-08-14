"""Metric binning and trip-scoped stop proximity.

Positions are projected to UTM 35N so a cell is a true square of
`CELL_SIZE_M` metres and stop distances are true metres.
"""

from __future__ import annotations

import math

from .config import BBOX, CELL_SIZE_M, HEADING_BINS, STOP_BUCKET_M, STOP_RADIUS_M
from .static_feed import StaticFeed
from .utm import project_xy


def _neighbourhood(reach: int) -> tuple[tuple[int, int], ...]:
    span = range(-reach, reach + 1)
    return tuple((dx, dy) for dx in span for dy in span)


_NEIGHBOURHOODS = {reach: _neighbourhood(reach) for reach in (1, 2, 3)}


def in_bbox(lat: float, lon: float) -> bool:
    lat_min, lat_max, lon_min, lon_max = BBOX
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def cell_of(x: float, y: float, cell_size: float = CELL_SIZE_M) -> tuple[int, int]:
    return int(x // cell_size), int(y // cell_size)


def heading_bin(bearing: float, bins: int = HEADING_BINS) -> int:
    """Which heading bin a bearing in degrees falls in, 0 = north, clockwise.

    Bins are centred on their heading rather than starting at it, so bin 0 is
    due north give or take half a bin instead of north-to-north-east — the
    labels a reader expects of a compass.
    """
    width = 360.0 / bins
    return int((bearing % 360.0 + width / 2) // width) % bins


def near_trip_stop(
    x: float,
    y: float,
    stops: frozenset[str],
    feed: StaticFeed,
    radius_m: float = STOP_RADIUS_M,
    bucket_m: float = STOP_BUCKET_M,
) -> bool:
    """True if (x, y) is within `radius_m` of a stop **on this trip**.

    Stops belonging only to other trips — including the stop across the street,
    which carries a different stop_id and is served by the opposite direction —
    are ignored, so they never blank out the running lane.

    The bucket sweep widens with the radius, so a terminal's larger exclusion
    still sees every stop that could be in range.
    """
    bx, by = int(x // bucket_m), int(y // bucket_m)
    buckets = feed.stop_buckets
    stop_xy = feed.stop_xy
    r2_limit = radius_m * radius_m
    reach = max(1, math.ceil(radius_m / bucket_m))
    for dx, dy in _NEIGHBOURHOODS.get(reach) or _neighbourhood(reach):
        for stop_id in buckets.get((bx + dx, by + dy), ()):
            if stop_id not in stops:
                continue
            sx, sy = stop_xy[stop_id]
            if (sx - x) ** 2 + (sy - y) ** 2 <= r2_limit:
                return True
    return False


def stops_near(
    x: float,
    y: float,
    wanted: dict[str, int] | frozenset[str],
    feed: StaticFeed,
    radius_m: float,
    bucket_m: float = STOP_BUCKET_M,
):
    """Yield (stop_id, distance²) for stops of `wanted` within `radius_m`.

    The same bucket sweep as `near_trip_stop`, but it reports which stop and how
    far rather than answering yes or no — segments.py needs the nearest approach
    to each stop, not the fact that there was one.
    """
    bx, by = int(x // bucket_m), int(y // bucket_m)
    buckets = feed.stop_buckets
    stop_xy = feed.stop_xy
    r2_limit = radius_m * radius_m
    reach = max(1, math.ceil(radius_m / bucket_m))
    for dx, dy in _NEIGHBOURHOODS.get(reach) or _neighbourhood(reach):
        for stop_id in buckets.get((bx + dx, by + dy), ()):
            if stop_id not in wanted:
                continue
            sx, sy = stop_xy[stop_id]
            d2 = (sx - x) ** 2 + (sy - y) ** 2
            if d2 <= r2_limit:
                yield stop_id, d2


def project(lon: float, lat: float) -> tuple[float, float]:
    return project_xy(lon, lat)
