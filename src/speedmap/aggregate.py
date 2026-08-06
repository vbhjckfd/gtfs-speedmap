"""Aggregate one day of archived snapshots into per-cell average bus speed.

Output: data/agg/YYYY-MM-DD.parquet, keyed by (month, hour, cx, cy) with
running sums, so days can be merged later without revisiting R2.

Run:
    python -m speedmap.aggregate 2026-07-15
    python -m speedmap.aggregate --all
"""

from __future__ import annotations

import argparse
import sys
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timezone
from zoneinfo import ZoneInfo

import pandas as pd

from . import r2
from .config import (
    AGG_DIR,
    CELL_SIZE_M,
    HIST_BIN_KMH,
    HIST_DIR,
    SPEED_MAX_MPS,
    STALE_MAX_S,
    TERMINAL_RADIUS_M,
    TZ,
    WORKERS,
)
from .depots import load_sites
from .grid import cell_of, in_bbox, near_trip_stop, project
from .snapshots import VehicleRow, parse_feed
from .static_feed import StaticFeed, load_for_date
from .utm import project_xy

_LOCAL_TZ = ZoneInfo(TZ)
MPS_TO_KMH = 3.6

# Accumulator layout: [n, sum_speed_mps, sum_lat, sum_lon]
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
        key = (local.strftime("%Y-%m"), local.hour, cx, cy)
        bucket = acc.get(key)
        if bucket is None:
            acc[key] = [1, speed, lat, lon]
        else:
            bucket[0] += 1
            bucket[1] += speed
            bucket[2] += lat
            bucket[3] += lon

        if hist is not None:
            bin_index = int(speed * MPS_TO_KMH / HIST_BIN_KMH)
            hist_key = (*key, bin_index)
            hist[hist_key] = hist.get(hist_key, 0) + 1

        stats["kept"] += 1


def aggregate_day(
    client, date_str: str, workers: int = WORKERS
) -> tuple[pd.DataFrame, pd.DataFrame, DayStats]:
    feed = load_for_date(client, date_str)
    keys = r2.snapshot_keys(client, date_str)
    if not keys:
        return pd.DataFrame(), pd.DataFrame(), DayStats()

    acc: dict[tuple[str, int, int, int], list] = {}
    hist: dict[tuple[str, int, int, int, int], int] = {}
    seen: set[tuple[str, int]] = set()
    stats = DayStats()
    stats["snapshots"] = len(keys)
    zones = depot_zones()

    def fetch(key: str) -> list[VehicleRow]:
        try:
            return parse_feed(r2.get_bytes(client, key))
        except Exception:
            # A single unreadable object must not sink a whole day.
            stats["snapshot_errors"] += 1
            return []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for rows in pool.map(fetch, keys):
            accumulate(rows, feed, acc, seen, stats, zones, hist)

    cells = pd.DataFrame(
        [
            (month, hour, cx, cy, n, s_speed, s_lat, s_lon)
            for (month, hour, cx, cy), (n, s_speed, s_lat, s_lon) in acc.items()
        ],
        columns=["month", "hour", "cx", "cy", "n", "sum_speed", "sum_lat", "sum_lon"],
    )
    bins = pd.DataFrame(
        [(month, hour, cx, cy, b, n) for (month, hour, cx, cy, b), n in hist.items()],
        columns=["month", "hour", "cx", "cy", "bin", "n"],
    )
    return cells, bins, stats


def write_day(client, date_str: str, force: bool = False, workers: int = WORKERS) -> bool:
    AGG_DIR.mkdir(parents=True, exist_ok=True)
    HIST_DIR.mkdir(parents=True, exist_ok=True)
    out = AGG_DIR / f"{date_str}.parquet"
    hist_out = HIST_DIR / f"{date_str}.parquet"
    if out.exists() and hist_out.exists() and not force:
        print(f"{date_str}  skip (already aggregated)")
        return False

    started = time.monotonic()
    cells, bins, stats = aggregate_day(client, date_str, workers=workers)
    if cells.empty:
        print(f"{date_str}  no data")
        return False

    cells.to_parquet(out, index=False)
    bins.to_parquet(hist_out, index=False)
    elapsed = time.monotonic() - started
    print(
        f"{date_str}  {stats['snapshots']} snapshots  {len(cells)} cells  "
        f"{len(bins)} bins  {elapsed:.0f}s  {stats.render()}"
    )
    return True


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("date", nargs="?", help="YYYY-MM-DD")
    ap.add_argument("--all", action="store_true", help="every day present in R2")
    ap.add_argument("--force", action="store_true", help="re-aggregate days already on disk")
    ap.add_argument("--workers", type=int, default=WORKERS)
    args = ap.parse_args(argv)

    client = r2.make_client()
    if args.all:
        dates = r2.raw_dates(client)
    elif args.date:
        dates = [args.date]
    else:
        ap.error("pass a date or --all")

    print(f"cell={CELL_SIZE_M:g}m  {len(depot_zones())} depot zone(s)  {len(dates)} day(s)")
    for date_str in dates:
        write_day(client, date_str, force=args.force, workers=args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
