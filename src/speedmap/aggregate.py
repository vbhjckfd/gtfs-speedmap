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
from .config import AGG_DIR, CELL_SIZE_M, SPEED_MAX_MPS, STALE_MAX_S, TZ, WORKERS
from .grid import cell_of, in_bbox, near_trip_stop, project
from .snapshots import VehicleRow, parse_feed
from .static_feed import StaticFeed, load_for_date

_LOCAL_TZ = ZoneInfo(TZ)

# Accumulator layout: (n, sum_speed_mps, sum_lat, sum_lon)
Cell = tuple[int, float, float, float]


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
        "drop_at_stop",
        "kept",
    )

    def render(self) -> str:
        return "  ".join(f"{k}={self[k]}" for k in self.ORDER)


def accumulate(
    rows: list[VehicleRow],
    feed: StaticFeed,
    acc: dict[tuple[str, int, int, int], list],
    seen: set[tuple[str, int]],
    stats: DayStats,
) -> None:
    """Apply the filter chain to one snapshot's rows and fold survivors into `acc`."""
    bus_routes = feed.bus_route_ids
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
        if near_trip_stop(x, y, stops, feed):
            stats["drop_at_stop"] += 1
            continue

        local = datetime.fromtimestamp(row.veh_ts, tz=timezone.utc).astimezone(_LOCAL_TZ)
        cx, cy = cell_of(x, y)
        bucket = acc.get((local.strftime("%Y-%m"), local.hour, cx, cy))
        if bucket is None:
            acc[(local.strftime("%Y-%m"), local.hour, cx, cy)] = [1, speed, lat, lon]
        else:
            bucket[0] += 1
            bucket[1] += speed
            bucket[2] += lat
            bucket[3] += lon
        stats["kept"] += 1


def aggregate_day(client, date_str: str, workers: int = WORKERS) -> tuple[pd.DataFrame, DayStats]:
    feed = load_for_date(client, date_str)
    keys = r2.snapshot_keys(client, date_str)
    if not keys:
        return pd.DataFrame(), DayStats()

    acc: dict[tuple[str, int, int, int], list] = {}
    seen: set[tuple[str, int]] = set()
    stats = DayStats()
    stats["snapshots"] = len(keys)

    def fetch(key: str) -> list[VehicleRow]:
        try:
            return parse_feed(r2.get_bytes(client, key))
        except Exception:
            # A single unreadable object must not sink a whole day.
            stats["snapshot_errors"] += 1
            return []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for rows in pool.map(fetch, keys):
            accumulate(rows, feed, acc, seen, stats)

    df = pd.DataFrame(
        [
            (month, hour, cx, cy, n, s_speed, s_lat, s_lon)
            for (month, hour, cx, cy), (n, s_speed, s_lat, s_lon) in acc.items()
        ],
        columns=["month", "hour", "cx", "cy", "n", "sum_speed", "sum_lat", "sum_lon"],
    )
    return df, stats


def write_day(client, date_str: str, force: bool = False, workers: int = WORKERS) -> bool:
    AGG_DIR.mkdir(parents=True, exist_ok=True)
    out = AGG_DIR / f"{date_str}.parquet"
    if out.exists() and not force:
        print(f"{date_str}  skip (already aggregated)")
        return False

    started = time.monotonic()
    df, stats = aggregate_day(client, date_str, workers=workers)
    if df.empty:
        print(f"{date_str}  no data")
        return False

    df.to_parquet(out, index=False)
    elapsed = time.monotonic() - started
    print(
        f"{date_str}  {stats['snapshots']} snapshots  {len(df)} cells  "
        f"{elapsed:.0f}s  {stats.render()}"
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

    print(f"cell={CELL_SIZE_M:g}m  {len(dates)} day(s)")
    for date_str in dates:
        write_day(client, date_str, force=args.force, workers=args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
