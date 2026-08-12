"""GTFS static: which routes are buses, and which stops belong to which trip.

Three things are needed from the schedule feed:

1. A bus filter. `route_type` alone is not one — Lviv trolleybuses (`Тр22`…)
   are published as `route_type=3` next to the real bus routes (`А01`…), and
   trams are `route_type=0`.
2. The stop set of the trip a vehicle is running, so positions sitting at a
   stop can be dropped. Scoping the set to the trip (or, failing that, to the
   route *and direction*) is what keeps the stop across the street — a
   different `stop_id`, served only by the opposite-direction trips — from
   excluding anything. Measured on the archived feed: the two directions of a
   route share zero stop_ids.
3. The stop **order** along a trip, which is what turns a stream of positions
   into "this bus left stop 4 at 08:12 and reached stop 5 at 08:15". The speed
   map needs only the set; segments.py needs the sequence.

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
CACHE_VERSION = 2  # bump when StaticFeed gains or loses a field


class StaticFeed(NamedTuple):
    static_date: str
    bus_route_ids: frozenset[str]
    # trip_id -> index into `stop_sets` / `terminal_sets` / `stop_seqs`
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
    # stop_id -> (lat, lon), for drawing rather than measuring
    stop_ll: dict[str, tuple[float, float]]
    # (bucket_x, bucket_y) -> stop_ids whose centre falls in that bucket
    stop_buckets: dict[tuple[int, int], tuple[str, ...]]
    # The stops of each pattern in order. Empty for the route+direction
    # fallback patterns, whose stop set is a union across trips and therefore
    # has no order to speak of — `path_for` answers those from `route_dir_path`.
    stop_seqs: list[tuple[str, ...]]
    # (route_id, direction) per pattern, for attributing an observed run
    pattern_route: list[tuple[str, str]]
    # (route_id, direction) -> the longest ordered stop list seen for it, which
    # is the path segment times are laid out along
    route_dir_path: dict[tuple[str, str], tuple[str, ...]]
    route_names: dict[str, str]
    stop_names: dict[str, str]

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

    def route_dir_for(self, trip_id: str, route_id: str) -> tuple[str, str] | None:
        """Which route and direction a running trip belongs to.

        Trip ids are renumbered between schedule archives, so a trip absent from
        this one is placed by the direction suffix its id carries.
        """
        idx = self.trip_pattern.get(trip_id)
        if idx is not None:
            return self.pattern_route[idx]
        direction = trip_id.rsplit("_", 1)[-1]
        key = (route_id, direction)
        return key if key in self.route_dir_path else None

    def path_for(self, trip_id: str, route_id: str) -> tuple[str, ...]:
        """The ordered stops this run is expected to serve.

        A trip this schedule knows gets its own sequence; anything else gets the
        longest path recorded for its route and direction, which is the same
        line of stops for all but the short-turn variants.
        """
        idx = self.trip_pattern.get(trip_id)
        if idx is not None and self.stop_seqs[idx]:
            return self.stop_seqs[idx]
        key = self.route_dir_for(trip_id, route_id)
        return self.route_dir_path.get(key, ()) if key else ()


def _read_csv(zf: zipfile.ZipFile, name: str):
    with zf.open(name) as fh:
        yield from csv.DictReader(io.TextIOWrapper(fh, encoding="utf-8-sig"))


def _build(static_date: str, zip_bytes: bytes) -> StaticFeed:
    zf = zipfile.ZipFile(io.BytesIO(zip_bytes))

    route_names = {
        r["route_id"]: r["route_short_name"]
        for r in _read_csv(zf, "routes.txt")
        if r["route_type"] == BUS_ROUTE_TYPE
        and not r["route_short_name"].startswith(TROLLEYBUS_PREFIX)
    }
    bus_route_ids = frozenset(route_names)

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
    stop_seqs: list[tuple[str, ...]] = []
    pattern_route: list[tuple[str, str]] = []
    # Keyed on the sequence, not the set: two variants of a route can serve the
    # same stops in a different order, and sharing one entry between them would
    # lay their segment times out back to front.
    interned: dict[tuple[tuple[str, ...], frozenset[str], tuple[str, str]], int] = {}

    def intern(
        ordered: tuple[str, ...],
        stops: frozenset[str],
        terminals: frozenset[str],
        route_dir: tuple[str, str],
    ) -> int:
        key = (ordered, terminals, route_dir)
        idx = interned.get(key)
        if idx is None:
            idx = len(stop_sets)
            interned[key] = idx
            stop_sets.append(stops)
            terminal_sets.append(terminals)
            stop_seqs.append(ordered)
            pattern_route.append(route_dir)
        return idx

    trip_pattern: dict[str, int] = {}
    route_dir_stops: dict[tuple[str, str], set[str]] = defaultdict(set)
    route_dir_terminals: dict[tuple[str, str], set[str]] = defaultdict(set)
    route_dir_path: dict[tuple[str, str], tuple[str, ...]] = {}
    for trip_id, seq in seq_by_trip.items():
        ordered = tuple(stop_id for _, stop_id in sorted(seq))
        stops = frozenset(ordered)
        terminals = frozenset({ordered[0], ordered[-1]})
        route_dir = trip_meta[trip_id]
        trip_pattern[trip_id] = intern(ordered, stops, terminals, route_dir)
        route_dir_stops[route_dir].update(stops)
        route_dir_terminals[route_dir].update(terminals)
        # The longest variant is the one that reaches both ends of the line;
        # short turns are a prefix or a suffix of it.
        if len(ordered) > len(route_dir_path.get(route_dir, ())):
            route_dir_path[route_dir] = ordered

    # The fallback pattern for a trip this schedule has never heard of. Its stop
    # set is a union across every variant, so it carries no sequence — anything
    # needing order asks `route_dir_path` instead.
    route_dir_pattern = {
        key: intern((), frozenset(stops), frozenset(route_dir_terminals[key]), key)
        for key, stops in route_dir_stops.items()
    }

    used = set().union(*stop_sets) if stop_sets else set()
    stop_xy: dict[str, tuple[float, float]] = {}
    stop_ll: dict[str, tuple[float, float]] = {}
    stop_names: dict[str, str] = {}
    for s in _read_csv(zf, "stops.txt"):
        if s["stop_id"] in used:
            lat, lon = float(s["stop_lat"]), float(s["stop_lon"])
            stop_xy[s["stop_id"]] = project_xy(lon, lat)
            stop_ll[s["stop_id"]] = (lat, lon)
            # Absent from the synthetic feeds the tests build, and not worth
            # failing a whole day's ingest over.
            stop_names[s["stop_id"]] = s.get("stop_name") or s["stop_id"]

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
        stop_ll=stop_ll,
        stop_buckets={k: tuple(v) for k, v in buckets.items()},
        stop_seqs=stop_seqs,
        pattern_route=pattern_route,
        route_dir_path=route_dir_path,
        route_names=route_names,
        stop_names=stop_names,
    )


def load_for_date(client, date_str: str, available: list[str] | None = None) -> StaticFeed:
    """The schedule in effect on `date_str`, cached on disk per static date."""
    if available is None:
        available = r2.static_dates(client)
    static_date = r2.static_date_for(date_str, available)
    if static_date is None:
        raise RuntimeError("no static/ archives in the bucket")

    STATIC_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    # Versioned: a cached pickle carries the field layout it was written with,
    # and unpickling it into a wider StaticFeed fails at load.
    cache = STATIC_CACHE_DIR / f"{static_date}-v{CACHE_VERSION}.pkl"
    if cache.exists():
        with cache.open("rb") as fh:
            return pickle.load(fh)

    feed = _build(static_date, r2.get_bytes(client, f"{r2.STATIC_PREFIX}{static_date}/static.zip"))
    with cache.open("wb") as fh:
        pickle.dump(feed, fh, protocol=pickle.HIGHEST_PROTOCOL)
    return feed
