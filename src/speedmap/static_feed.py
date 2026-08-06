"""GTFS static: which routes are buses, and which stops belong to which trip.

Only two things are needed from the schedule feed:

1. A bus filter. `route_type` alone is not one — Lviv trolleybuses (`Тр22`…)
   are published as `route_type=3` next to the real bus routes (`А01`…), and
   trams are `route_type=0`.
2. The stop set of the trip a vehicle is running, so positions sitting at a
   stop can be dropped. Scoping the set to the trip (or, failing that, to the
   route *and direction*) is what keeps the stop across the street — a
   different `stop_id`, served only by the opposite-direction trips — from
   excluding anything. Measured on the archived feed: the two directions of a
   route share zero stop_ids.

gtfs-collector archives a dated `static/YYYY-MM-DD/static.zip` only since
2026-07-31, so older raw days are matched against the oldest archive available.
Their trip_ids have since been renumbered (≈35% miss on May days), which is
exactly what the route+direction fallback is for.
"""

from __future__ import annotations

import csv
import io
import pickle
import zipfile
from collections import defaultdict
from typing import NamedTuple

from . import r2
from .config import STATIC_CACHE_DIR, STOP_BUCKET_M
from .utm import project_xy

TROLLEYBUS_PREFIX = "Тр"  # Cyrillic Т + р
BUS_ROUTE_TYPE = "3"


class StaticFeed(NamedTuple):
    static_date: str
    bus_route_ids: frozenset[str]
    # trip_id -> index into `stop_sets` / `terminal_sets`
    trip_pattern: dict[str, int]
    # (route_id, direction) -> index into `stop_sets`; fallback for trip_ids
    # that no longer exist in this schedule
    route_dir_pattern: dict[tuple[str, str], int]
    stop_sets: list[frozenset[str]]
    # The first and last stop of each pattern. Buses lay over at these, often
    # parked well beyond the stop itself, so they get a wider exclusion.
    terminal_sets: list[frozenset[str]]
    # stop_id -> UTM 35N (easting, northing)
    stop_xy: dict[str, tuple[float, float]]
    # (bucket_x, bucket_y) -> stop_ids whose centre falls in that bucket
    stop_buckets: dict[tuple[int, int], tuple[str, ...]]

    def pattern_for(self, trip_id: str, route_id: str) -> int | None:
        """This trip's pattern; falls back to the route's pattern in this
        direction when the trip_id is not in this schedule."""
        idx = self.trip_pattern.get(trip_id)
        if idx is not None:
            return idx
        direction = trip_id.rsplit("_", 1)[-1]
        return self.route_dir_pattern.get((route_id, direction))

    def stops_for(self, trip_id: str, route_id: str) -> frozenset[str] | None:
        idx = self.pattern_for(trip_id, route_id)
        return self.stop_sets[idx] if idx is not None else None

    def terminals_for(self, trip_id: str, route_id: str) -> frozenset[str] | None:
        idx = self.pattern_for(trip_id, route_id)
        return self.terminal_sets[idx] if idx is not None else None


def _read_csv(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))


def _build(static_date: str, zip_bytes: bytes) -> StaticFeed:
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))

    bus_route_ids = frozenset(
        r["route_id"]
        for r in _read_csv(zf, "routes.txt")
        if r["route_type"] == BUS_ROUTE_TYPE
        and not r["route_short_name"].startswith(TROLLEYBUS_PREFIX)
    )

    trip_meta = {
        t["trip_id"]: (t["route_id"], t["direction_id"])
        for t in _read_csv(zf, "trips.txt")
        if t["route_id"] in bus_route_ids
    }

    # stop_times.txt is ~17 MB and not grouped in any guaranteed order.
    seq_by_trip: dict[str, list[tuple[int, str]]] = defaultdict(list)
    for st in _read_csv(zf, "stop_times.txt"):
        trip_id = st["trip_id"]
        if trip_id in trip_meta:
            seq_by_trip[trip_id].append((int(st["stop_sequence"]), st["stop_id"]))

    stop_sets: list[frozenset[str]] = []
    terminal_sets: list[frozenset[str]] = []
    interned: dict[tuple[frozenset[str], frozenset[str]], int] = {}

    def intern(stops: frozenset[str], terminals: frozenset[str]) -> int:
        idx = interned.get((stops, terminals))
        if idx is None:
            idx = len(stop_sets)
            interned[(stops, terminals)] = idx
            stop_sets.append(stops)
            terminal_sets.append(terminals)
        return idx

    trip_pattern: dict[str, int] = {}
    route_dir_stops: dict[tuple[str, str], set[str]] = defaultdict(set)
    route_dir_terminals: dict[tuple[str, str], set[str]] = defaultdict(set)
    for trip_id, seq in seq_by_trip.items():
        ordered = [stop_id for _, stop_id in sorted(seq)]
        stops = frozenset(ordered)
        terminals = frozenset({ordered[0], ordered[-1]})
        trip_pattern[trip_id] = intern(stops, terminals)
        route_dir_stops[trip_meta[trip_id]].update(stops)
        route_dir_terminals[trip_meta[trip_id]].update(terminals)

    route_dir_pattern = {
        key: intern(frozenset(stops), frozenset(route_dir_terminals[key]))
        for key, stops in route_dir_stops.items()
    }

    used = set().union(*stop_sets) if stop_sets else set()
    stop_xy: dict[str, tuple[float, float]] = {}
    for s in _read_csv(zf, "stops.txt"):
        if s["stop_id"] in used:
            stop_xy[s["stop_id"]] = project_xy(float(s["stop_lon"]), float(s["stop_lat"]))

    buckets: dict[tuple[int, int], list[str]] = defaultdict(list)
    for stop_id, (x, y) in stop_xy.items():
        buckets[(int(x // STOP_BUCKET_M), int(y // STOP_BUCKET_M))].append(stop_id)

    return StaticFeed(
        static_date=static_date,
        bus_route_ids=bus_route_ids,
        trip_pattern=trip_pattern,
        route_dir_pattern=route_dir_pattern,
        stop_sets=stop_sets,
        terminal_sets=terminal_sets,
        stop_xy=stop_xy,
        stop_buckets={k: tuple(v) for k, v in buckets.items()},
    )


def load_for_date(client, date_str: str, available: list[str] | None = None) -> StaticFeed:
    """The schedule in effect on `date_str`, cached on disk per static date."""
    if available is None:
        available = r2.static_dates(client)
    static_date = r2.static_date_for(date_str, available)
    if static_date is None:
        raise RuntimeError("no static/ archives in the bucket")

    STATIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    cache = STATIC_CACHE_DIR / f"{static_date}.pkl"
    if cache.exists():
        with cache.open("rb") as fh:
            return pickle.load(fh)

    feed = _build(static_date, r2.get_bytes(client, f"{r2.STATIC_PREFIX}{static_date}/static.zip"))
    with cache.open("wb") as fh:
        pickle.dump(feed, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return feed
