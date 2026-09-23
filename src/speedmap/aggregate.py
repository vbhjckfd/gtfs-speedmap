"""Aggregate one day of archived snapshots into per-cell average bus speed.

Output: data/agg/YYYY-MM-DD.parquet, keyed by (month, hour, cx, cy, dir) with
running sums, so days can be merged later without revisiting R2.

`dir` is the heading bin: a cell holds both directions of its street, and the
approach to a junction and the departure from it are nowhere near the same
speed, so the two are counted apart.

Run through ingest.py, which feeds this pass and segments.py from one download.
"""

from __future__ import annotations

import math
import time
from collections import Counter
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .config import (
    AGG_DIR,
    HIST_BIN_KMH,
    HIST_DIR,
    SPEED_MAX_MPS,
    STALE_MAX_S,
    TERMINAL_RADIUS_M,
    TZ,
)
from .depots import load_sites
from .files import write_atomic
from .grid import cell_of, heading_bin, in_bbox, near_trip_stop, project
from .snapshots import VehicleRow
from .static_feed import StaticFeed
from .utm import project_xy

_LOCAL_TZ = ZoneInfo(TZ)
MPS_TO_KMH = 3.6

# Accumulator layout: [n, sum_speed_mps, sum_lat, sum_lon, sum_sin, sum_cos]
# The last two are the bearing as a unit vector, summed. Averaging degrees
# directly is wrong across the 359°/0° seam; the vector sum is not.
Cell = list


class DayStats(Counter):
    """Per-filter-stage row counts, printed after each day."""

    ORDER = (
        "rows_parsed",
        "drop_not_bus",
        "drop_stale",
        "drop_duplicate",
        "drop_speed",
        "drop_bbox",
        "drop_no_stops",
        "drop_depot",
        "drop_at_terminal",
        "drop_at_stop",
        "kept",
    )

    def render(self) -> str:
        return "  ".join(f"{k}={self[k]}" for k in self.ORDER)


def depot_zones() -> list[tuple[float, float, float]]:
    """Discovered depots as (easting, northing, radius²) for cheap testing."""
    return [
        (*project_xy(site["lon"], site["lat"]), site["radius_m"] ** 2)
        for site in load_sites()
    ]


def in_depot(x: float, y: float, zones: list[tuple[float, float, float]]) -> bool:
    for zx, zy, r2_limit in zones:
        if (zx - x) ** 2 + (zy - y) ** 2 <= r2_limit:
            return True
    return False


def accumulate(
    rows: list[VehicleRow],
    feed: StaticFeed,
    acc: dict[tuple[str, int, int, int], list],
    seen: set[tuple[str, int]],
    stats: DayStats,
    zones: list[tuple[float, float, float]] | None = None,
    hist: dict[tuple[str, int, int, int, int], int] | None = None,
) -> None:
    """Apply the filter chain to one snapshot's rows and fold survivors into `acc`."""
    bus_routes = feed.bus_route_ids
    zones = zones if zones is not None else []
    stats["rows_parsed"] += len(rows)

    for row in rows:
        if row.route_id not in bus_routes:
            stats["drop_not_bus"] += 1
            continue

        # The feed republishes vehicles whose own timestamp is hours old.
        if row.feed_ts - row.veh_ts > STALE_MAX_S:
            stats["drop_stale"] += 1
            continue

        # The collector polls every 10 s and the upstream feed repeats
        # unchanged entities, so without this a parked bus is counted many
        # times over and drags its cell's average down.
        key = (row.vehicle_id, row.veh_ts)
        if key in seen:
            stats["drop_duplicate"] += 1
            continue
        seen.add(key)

        speed = row.speed
        if not (0.0 <= speed <= SPEED_MAX_MPS):
            stats["drop_speed"] += 1
            continue

        lat, lon = row.lat, row.lon
        if not in_bbox(lat, lon):
            stats["drop_bbox"] += 1
            continue

        stops = feed.stops_for(row.trip_id, row.route_id)
        if stops is None:
            stats["drop_no_stops"] += 1
            continue

        x, y = project(lon, lat)

        # A depot or off-street layover yard: parked buses, not slow traffic.
        if in_depot(x, y, zones):
            stats["drop_depot"] += 1
            continue

        # End of a run. Buses idle here for minutes, and often stand past the
        # terminal stop rather than at it, so the exclusion is wider.
        terminals = feed.terminals_for(row.trip_id, row.route_id)
        if terminals and near_trip_stop(x, y, terminals, feed, radius_m=TERMINAL_RADIUS_M):
            stats["drop_at_terminal"] += 1
            continue

        if near_trip_stop(x, y, stops, feed):
            stats["drop_at_stop"] += 1
            continue

        local = datetime.fromtimestamp(row.veh_ts, tz=timezone.utc).astimezone(_LOCAL_TZ)
        cx, cy = cell_of(x, y)
        radians = math.radians(row.bearing)
        sin_b, cos_b = math.sin(radians), math.cos(radians)
        key = (local.strftime("%Y-%m"), local.hour, cx, cy, heading_bin(row.bearing))
        bucket = acc.get(key)
        if bucket is None:
            acc[key] = [1, speed, lat, lon, sin_b, cos_b]
        else:
            bucket[0] += 1
            bucket[1] += speed
            bucket[2] += lat
            bucket[3] += lon
            bucket[4] += sin_b
            bucket[5] += cos_b

        if hist is not None:
            bin_index = int(speed * MPS_TO_KMH / HIST_BIN_KMH)
            hist_key = (*key, bin_index)
            hist[hist_key] = hist.get(hist_key, 0) + 1

        stats["kept"] += 1


class SpeedDay:
    """One day of the speed map, folded in a snapshot at a time."""

    def __init__(self, feed: StaticFeed) -> None:
        self.feed = feed
        self.acc: dict[tuple[str, int, int, int], list] = {}
        self.hist: dict[tuple[str, int, int, int, int], int] = {}
        self.seen: set[tuple[str, int]] = set()
        self.stats = DayStats()
        self.zones = depot_zones()

    def add(self, rows: list[VehicleRow]) -> None:
        accumulate(rows, self.feed, self.acc, self.seen, self.stats, self.zones, self.hist)

    def frames(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        cells = pd.DataFrame(
            [(*key, *sums) for key, sums in self.acc.items()],
            columns=[
                "month",
                "hour",
                "cx",
                "cy",
                "dir",
                "n",
                "sum_speed",
                "sum_lat",
                "sum_lon",
                "sum_sin",
                "sum_cos",
            ],
        )
        bins = pd.DataFrame(
            [(*key, n) for key, n in self.hist.items()],
            columns=["month", "hour", "cx", "cy", "dir", "bin", "n"],
        )
        return cells, bins


def _outputs(date_str: str) -> tuple[Path, Path]:
    return AGG_DIR / f"{date_str}.parquet", HIST_DIR / f"{date_str}.parquet"


def is_done(date_str: str) -> bool:
    return all(path.exists() for path in _outputs(date_str))


def save(date_str: str, day: SpeedDay | None, started: float) -> bool:
    cells, bins = day.frames() if day else (pd.DataFrame(), pd.DataFrame())
    if cells.empty:
        print(f"{date_str}  no data", flush=True)
        return False

    AGG_DIR.mkdir(parents=True, exist_ok=True)
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    out, hist_out = _outputs(date_str)
    write_atomic(out, lambda p: cells.to_parquet(p, index=False))
    write_atomic(hist_out, lambda p: bins.to_parquet(p, index=False))
    print(
        f"{date_str}  {day.stats['snapshots']} snapshots  {len(cells)} cells  "
        f"{len(bins)} bins  {time.monotonic() - started:.0f}s  {day.stats.render()}",
        flush=True,
    )
    return True
