"""Metric binning and trip-scoped stop proximity.

Positions are projected to UTM 35N so a cell is a true square of
`CELL_SIZE_M` metres and stop distances are true metres.
"""

from __future__ import annotations

from .config import BBOX, CELL_SIZE_M, STOP_BUCKET_M, STOP_RADIUS_M
from .static_feed import StaticFeed
from .utm import project_xy

_NEIGHBOURS = tuple((dx, dy) for dx in (-1, 0, 1) for dy in (-1, 0, 1))


def in_bbox(lat: float, lon: float) -> bool:
    lat_min, lat_max, lon_min, lon_max = BBOX
    return lat_min <= lat <= lat_max and lon_min <= lon <= lon_max


def cell_of(x: float, y: float, cell_size: float = CELL_SIZE_M) -> tuple[int, int]:
    return int(x // cell_size), int(y // cell_size)


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

    Requires bucket_m >= radius_m: the 3x3 neighbourhood then covers every stop
    that could be in range.
    """
    bx, by = int(x // bucket_m), int(y // bucket_m)
    buckets = feed.stop_buckets
    stop_xy = feed.stop_xy
    r2_limit = radius_m * radius_m
    for dx, dy in _NEIGHBOURS:
        for stop_id in buckets.get((bx + dx, by + dy), ()):
            if stop_id not in stops:
                continue
            sx, sy = stop_xy[stop_id]
            if (sx - x) ** 2 + (sy - y) ** 2 <= r2_limit:
                return True
    return False


def project(lon: float, lat: float) -> tuple[float, float]:
    return project_xy(lon, lat)
