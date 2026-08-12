"""Time real vehicles between consecutive stops, one day at a time.

The speed map answers "how fast do buses move along this street". It cannot
answer "how long does this ride take": positions near a stop on the vehicle's
own trip are dropped by design, so every dwell is a hole, and a speed field
integrated along a line is not a journey anyway.

This pass answers the other question. It reassembles each vehicle's day into
runs, finds the moment each run came nearest to every stop on its route's path,
and keeps the elapsed time between adjacent stops — dwell, lights, queues and
all. The output is keyed by the stop **pair**, not by the trip, so every variant
of a route contributes to the same leg:

    data/seg/YYYY-MM-DD.parquet      (month, hour, route, direction, pair) -> n, sum_s
    data/seghist/YYYY-MM-DD.parquet  the same key plus a SEG_BIN_S-second bin

A leg is timed against the canonical path of its route and direction — the
longest stop list that route runs. A short-turn variant simply never approaches
the stops it does not serve and contributes nothing there; an express that
drives past a stop it does not serve does contribute, which is correct, because
what is being measured is how long that route takes to get from one point to
the next, not whether it opened its doors.

Run:
    python -m speedmap.segments 2026-07-15
    python -m speedmap.segments --all
"""

from __future__ import annotations

import argparse
import json
import sys
import time
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timezone
from concurrent.futures import ThreadPoolExecutor
from zoneinfo import ZoneInfo

import pandas as pd

from . import r2
from .config import (
    PATHS_FILE,
    RUN_GAP_MAX_S,
    SEG_BIN_S,
    SEG_DIR,
    SEG_GAP_MAX_S,
    SEG_HIST_DIR,
    SEG_MAX_S,
    STALE_MAX_S,
    STOP_PASS_RADIUS_M,
    TZ,
    WORKERS,
)
from .grid import in_bbox, project, stops_near
from .snapshots import VehicleRow, parse_feed
from .static_feed import StaticFeed, load_for_date

_LOCAL_TZ = ZoneInfo(TZ)

# One run's samples: (timestamp, easting, northing).
Sample = tuple[int, float, float]


class SegStats(Counter):
    ORDER = (
        "rows_parsed",
        "drop_not_bus",
        "drop_stale",
        "drop_duplicate",
        "drop_bbox",
        "drop_no_path",
        "runs",
        "runs_short",
        "passes",
        "leg_gap",
        "leg_backwards",
        "leg_too_long",
        "legs",
    )

    def render(self) -> str:
        return "  ".join(f"{k}={self[k]}" for k in self.ORDER)


def collect_runs(
    rows_by_snapshot, feed: StaticFeed, stats: SegStats
) -> dict[tuple[str, str, str], list[Sample]]:
    """Every vehicle's day, grouped by (vehicle, trip, route) and projected.

    The stop and depot filters the speed map applies are deliberately absent:
    standing at a stop is the part of a ride this pass is here to measure.
    """
    runs: dict[tuple[str, str, str], list[Sample]] = defaultdict(list)
    seen: set[tuple[str, int]] = set()
    bus_routes = feed.bus_route_ids

    for rows in rows_by_snapshot:
        stats["rows_parsed"] += len(rows)
        for row in rows:
            if row.route_id not in bus_routes:
                stats["drop_not_bus"] += 1
                continue
            if row.feed_ts - row.veh_ts > STALE_MAX_S:
                stats["drop_stale"] += 1
                continue
            key = (row.vehicle_id, row.veh_ts)
            if key in seen:
                stats["drop_duplicate"] += 1
                continue
            seen.add(key)
            if not in_bbox(row.lat, row.lon):
                stats["drop_bbox"] += 1
                continue
            x, y = project(row.lon, row.lat)
            runs[(row.vehicle_id, row.trip_id, row.route_id)].append((row.veh_ts, x, y))
    return runs


def split_runs(samples: list[Sample]) -> list[list[Sample]]:
    """Break one vehicle-and-trip's samples where the feed went quiet.

    A vehicle keeps its trip_id through a layover and sometimes through the
    return leg, so without this a "leg" could span the turnaround.
    """
    samples.sort()
    out: list[list[Sample]] = []
    current: list[Sample] = []
    previous = None
    for sample in samples:
        if previous is not None and sample[0] - previous > RUN_GAP_MAX_S:
            out.append(current)
            current = []
        current.append(sample)
        previous = sample[0]
    if current:
        out.append(current)
    return out


def nearest_passes(
    run: list[Sample], path_index: dict[str, int], feed: StaticFeed
) -> dict[int, int]:
    """When this run came nearest to each stop on its path: {path index: ts}.

    Nearest approach rather than first entry into the radius, so the timestamp
    is the moment at the stop rather than the moment the bus came into range of
    it, which on a 60 m radius is up to a few seconds of slack either side.
    """
    best: dict[int, tuple[float, int]] = {}
    for ts, x, y in run:
        for stop_id, d2 in stops_near(x, y, path_index, feed, STOP_PASS_RADIUS_M):
            k = path_index[stop_id]
            current = best.get(k)
            if current is None or d2 < current[0]:
                best[k] = (d2, ts)
    return {k: ts for k, (_, ts) in best.items()}


def _observed_without_holes(times: list[int], start: int, end: int) -> bool:
    """True if the run was sampled continuously between two moments.

    The feed drops vehicles for minutes at a time. A leg spanning one of those
    holes may not be the leg it looks like — the bus could have been diverted,
    or the "stop pass" at either end could be the wrong approach entirely.
    """
    lo = bisect_left(times, start)
    hi = bisect_right(times, end)
    previous = start
    for ts in times[lo:hi]:
        if ts - previous > SEG_GAP_MAX_S:
            return False
        previous = ts
    return end - previous <= SEG_GAP_MAX_S


def legs_of_run(
    run: list[Sample], path: tuple[str, ...], feed: StaticFeed, stats: SegStats
) -> list[tuple[int, str, str, int]]:
    """Adjacent-stop legs this run actually performed.

    Each is (start timestamp, from stop, to stop, seconds).
    """
    path_index = {stop_id: k for k, stop_id in enumerate(path)}
    passes = nearest_passes(run, path_index, feed)
    stats["passes"] += len(passes)
    if len(passes) < 2:
        return []

    times = [ts for ts, _, _ in run]
    out = []
    for k in sorted(passes):
        if k + 1 not in passes:
            continue
        start, end = passes[k], passes[k + 1]
        # Nearest approach can land out of order when a bus crawls between two
        # stops that are close together and the GPS wanders.
        if end <= start:
            stats["leg_backwards"] += 1
            continue
        if end - start > SEG_MAX_S:
            stats["leg_too_long"] += 1
            continue
        if not _observed_without_holes(times, start, end):
            stats["leg_gap"] += 1
            continue
        out.append((start, path[k], path[k + 1], end - start))
    stats["legs"] += len(out)
    return out


def accumulate(
    runs: dict[tuple[str, str, str], list[Sample]],
    feed: StaticFeed,
    acc: dict[tuple[str, int, str, str, str, str], list],
    hist: dict[tuple[str, int, str, str, str, str, int], int],
    stats: SegStats,
) -> None:
    paths: dict[tuple[str, str], tuple[str, ...]] = {}

    for (_, trip_id, route_id), samples in runs.items():
        route_dir = feed.route_dir_for(trip_id, route_id)
        if route_dir is None:
            stats["drop_no_path"] += len(samples)
            continue
        path = paths.get(route_dir)
        if path is None:
            path = feed.route_dir_path.get(route_dir, ())
            paths[route_dir] = path
        if len(path) < 2:
            stats["drop_no_path"] += len(samples)
            continue

        for run in split_runs(samples):
            stats["runs"] += 1
            if len(run) < 2:
                stats["runs_short"] += 1
                continue
            for start, from_stop, to_stop, seconds in legs_of_run(run, path, feed, stats):
                local = datetime.fromtimestamp(start, tz=timezone.utc).astimezone(_LOCAL_TZ)
                key = (
                    local.strftime("%Y-%m"),
                    local.hour,
                    route_dir[0],
                    route_dir[1],
                    from_stop,
                    to_stop,
                )
                bucket = acc.get(key)
                if bucket is None:
                    acc[key] = [1, seconds]
                else:
                    bucket[0] += 1
                    bucket[1] += seconds
                bin_key = (*key, int(seconds / SEG_BIN_S))
                hist[bin_key] = hist.get(bin_key, 0) + 1


KEY_COLUMNS = ["month", "hour", "route_id", "direction", "from_stop", "to_stop"]


def segments_day(
    client, date_str: str, workers: int = WORKERS
) -> tuple[pd.DataFrame, pd.DataFrame, SegStats]:
    feed = load_for_date(client, date_str)
    keys = r2.snapshot_keys(client, date_str)
    if not keys:
        return pd.DataFrame(), pd.DataFrame(), SegStats()

    stats = SegStats()
    stats["snapshots"] = len(keys)

    def fetch(key: str) -> list[VehicleRow]:
        try:
            return parse_feed(r2.get_bytes(client, key))
        except Exception:
            stats["snapshot_errors"] += 1
            return []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        runs = collect_runs(pool.map(fetch, keys), feed, stats)

    acc: dict[tuple[str, int, str, str, str, str], list] = {}
    hist: dict[tuple[str, int, str, str, str, str, int], int] = {}
    accumulate(runs, feed, acc, hist, stats)

    legs = pd.DataFrame(
        [(*key, n, total) for key, (n, total) in acc.items()],
        columns=[*KEY_COLUMNS, "n", "sum_s"],
    )
    bins = pd.DataFrame(
        [(*key, n) for key, n in hist.items()],
        columns=[*KEY_COLUMNS, "bin", "n"],
    )
    return legs, bins, stats


def write_day(client, date_str: str, force: bool = False, workers: int = WORKERS) -> bool:
    SEG_DIR.mkdir(parents=True, exist_ok=True)
    SEG_HIST_DIR.mkdir(parents=True, exist_ok=True)
    out = SEG_DIR / f"{date_str}.parquet"
    hist_out = SEG_HIST_DIR / f"{date_str}.parquet"
    if out.exists() and hist_out.exists() and not force:
        print(f"{date_str}  skip (already timed)", flush=True)
        return False

    started = time.monotonic()
    legs, bins, stats = segments_day(client, date_str, workers=workers)
    if legs.empty:
        print(f"{date_str}  no data", flush=True)
        return False

    legs.to_parquet(out, index=False)
    bins.to_parquet(hist_out, index=False)
    print(
        f"{date_str}  {stats['snapshots']} snapshots  {len(legs)} pairs  "
        f"{time.monotonic() - started:.0f}s  {stats.render()}",
        flush=True,
    )
    return True


def write_paths(client, date_str: str) -> int:
    """Snapshot the schedule geometry the leg times are laid out along.

    The legs are keyed by stop pair; without the paths they cannot be put in
    order, and stop ids mean nothing to a reader. Dumping it here keeps
    build_web offline — it reads parquet and this file, never R2.
    """
    feed = load_for_date(client, date_str)
    used: set[str] = set()
    routes = []
    for (route_id, direction), path in sorted(feed.route_dir_path.items()):
        if len(path) < 2:
            continue
        used.update(path)
        routes.append(
            {
                "route": route_id,
                "dir": direction,
                "name": feed.route_names.get(route_id, route_id),
                "path": list(path),
            }
        )
    payload = {
        "static_date": feed.static_date,
        "stops": {
            stop_id: [
                round(feed.stop_ll[stop_id][0], 5),
                round(feed.stop_ll[stop_id][1], 5),
                feed.stop_names.get(stop_id, stop_id),
            ]
            for stop_id in sorted(used)
            if stop_id in feed.stop_ll
        },
        "routes": routes,
    }
    PATHS_FILE.parent.mkdir(parents=True, exist_ok=True)
    PATHS_FILE.write_text(json.dumps(payload, ensure_ascii=False), encoding="utf-8")
    return len(routes)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("date", nargs="?", help="YYYY-MM-DD")
    ap.add_argument("--all", action="store_true", help="every day present in R2")
    ap.add_argument("--force", action="store_true", help="re-time days already on disk")
    ap.add_argument("--paths-only", action="store_true", help="just refresh data/paths.json")
    ap.add_argument("--workers", type=int, default=WORKERS)
    args = ap.parse_args(argv)

    client = r2.make_client()
    if args.paths_only:
        dates = [args.date or r2.raw_dates(client)[-1]]
        print(f"{write_paths(client, dates[0])} route-directions from {dates[0]}")
        return 0
    if args.all:
        dates = r2.raw_dates(client)
    elif args.date:
        dates = [args.date]
    else:
        ap.error("pass a date or --all")

    print(
        f"pass radius={STOP_PASS_RADIUS_M:g}m  bin={SEG_BIN_S:g}s  {len(dates)} day(s)",
        flush=True,
    )
    for date_str in dates:
        write_day(client, date_str, force=args.force, workers=args.workers)
    # The newest schedule wins: it is the one the most recent legs were timed
    # against, and stop ids are stable even when trip ids are renumbered.
    print(f"{write_paths(client, dates[-1])} route-directions written to {PATHS_FILE.name}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
