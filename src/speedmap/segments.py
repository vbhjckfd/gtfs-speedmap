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

Run through ingest.py, which feeds this pass and aggregate.py from one download.
"""

from __future__ import annotations

import json
import time
from bisect import bisect_left, bisect_right
from collections import Counter, defaultdict
from datetime import datetime, timezone
from pathlib import Path
from zoneinfo import ZoneInfo

import pandas as pd

from .config import (
    paths_file,
    RUN_GAP_MAX_S,
    SEG_BIN_S,
    SEG_DIR,
    SEG_GAP_MAX_S,
    SEG_HIST_DIR,
    SEG_MAX_S,
    STALE_MAX_S,
    STOP_PASS_RADIUS_M,
    TZ,
)
from .files import write_atomic
from .grid import in_bbox, project, stops_near
from .snapshots import VehicleRow
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


def add_rows(
    rows: list[VehicleRow],
    feed: StaticFeed,
    runs: dict[tuple[str, str, str], list[Sample]],
    seen: set[tuple[str, int]],
    stats: SegStats,
) -> None:
    """Fold one snapshot into every vehicle's day, grouped by (vehicle, trip, route).

    The stop and depot filters the speed map applies are deliberately absent:
    standing at a stop is the part of a ride this pass is here to measure.
    """
    bus_routes = feed.bus_route_ids
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


class SegmentDay:
    """One day of stop-to-stop legs. Snapshots only collect runs; the legs are
    timed once the whole day is in, because a run needs its later samples."""

    def __init__(self, feed: StaticFeed) -> None:
        self.feed = feed
        self.runs: dict[tuple[str, str, str], list[Sample]] = defaultdict(list)
        self.seen: set[tuple[str, int]] = set()
        self.stats = SegStats()

    def add(self, rows: list[VehicleRow]) -> None:
        add_rows(rows, self.feed, self.runs, self.seen, self.stats)

    def frames(self) -> tuple[pd.DataFrame, pd.DataFrame]:
        acc: dict[tuple[str, int, str, str, str, str], list] = {}
        hist: dict[tuple[str, int, str, str, str, str, int], int] = {}
        accumulate(self.runs, self.feed, acc, hist, self.stats)
        legs = pd.DataFrame(
            [(*key, n, total) for key, (n, total) in acc.items()],
            columns=[*KEY_COLUMNS, "n", "sum_s"],
        )
        bins = pd.DataFrame(
            [(*key, n) for key, n in hist.items()],
            columns=[*KEY_COLUMNS, "bin", "n"],
        )
        return legs, bins


def _outputs(date_str: str) -> tuple[Path, Path]:
    return SEG_DIR / f"{date_str}.parquet", SEG_HIST_DIR / f"{date_str}.parquet"


def is_done(date_str: str) -> bool:
    return all(path.exists() for path in _outputs(date_str))


def save(date_str: str, day: SegmentDay | None, started: float) -> bool:
    legs, bins = day.frames() if day else (pd.DataFrame(), pd.DataFrame())
    if legs.empty:
        print(f"{date_str}  no data", flush=True)
        return False

    SEG_DIR.mkdir(parents=True, exist_ok=True)
    SEG_HIST_DIR.mkdir(parents=True, exist_ok=True)
    out, hist_out = _outputs(date_str)
    write_atomic(out, lambda p: legs.to_parquet(p, index=False))
    write_atomic(hist_out, lambda p: bins.to_parquet(p, index=False))
    print(
        f"{date_str}  {day.stats['snapshots']} snapshots  {len(legs)} pairs  "
        f"{time.monotonic() - started:.0f}s  {day.stats.render()}",
        flush=True,
    )
    return True


def paths_day(month: str) -> str | None:
    """The day a month's paths were last snapshotted from, if they exist."""
    target = paths_file(month)
    if not target.exists():
        return None
    return json.loads(target.read_text(encoding="utf-8")).get("day")


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
        route = {
            "route": route_id,
            "dir": direction,
            "name": feed.route_names.get(route_id, route_id),
            "path": list(path),
        }
        # The street the route follows, and where along it each stop sits. With
        # these the viewer can answer for two arbitrary points; without them it
        # can only answer stop to stop, which is what routes lacking a usable
        # shape fall back to.
        shape = feed.route_dir_shape.get((route_id, direction))
        distances = feed.route_dir_stop_dist.get((route_id, direction))
        if shape and distances:
            route["shape"] = [[round(lat, 5), round(lon, 5)] for lat, lon in shape]
            route["dist"] = [round(d) for d in distances]
        routes.append(route)
    payload = {
        "day": date_str,
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
    target = paths_file(date_str[:7])
    target.parent.mkdir(parents=True, exist_ok=True)
    body = json.dumps(payload, ensure_ascii=False)
    write_atomic(target, lambda p: p.write_text(body, encoding="utf-8"))
    return len(routes)
