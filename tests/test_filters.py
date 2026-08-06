"""Filter-chain tests driven through synthetic feeds and a synthetic schedule."""

from __future__ import annotations

import io
import math
import zipfile

import pandas as pd
import pytest
from google.transit import gtfs_realtime_pb2

from speedmap.aggregate import DayStats, accumulate, in_depot
from speedmap.grid import cell_of, near_trip_stop
from speedmap.snapshots import parse_feed
from speedmap.static_feed import _build
from speedmap.utm import project_xy

FEED_TS = 1_786_000_000

# A pair of opposite-direction stops on the same street, offset along it by
# 70 m — the measured median offset between the two directions of a Lviv route.
# Both are mid-route, so the wider terminal exclusion does not reach them.
STOP_NORTH = (49.840000, 24.030000)  # served by direction 0
STOP_SOUTH = (49.839371, 24.030000)  # served by direction 1 — the opposite side
STOP_START = (49.830000, 24.010000)  # first stop of both directions
STOP_FAR = (49.850000, 24.050000)  # last stop of both directions


def _csv(rows: list[dict], header: list[str]) -> str:
    out = [",".join(header)]
    out += [",".join(str(r[h]) for h in header) for r in rows]
    return "\n".join(out) + "\n"


def make_static_zip() -> bytes:
    routes = [
        {"route_id": "R_BUS", "route_short_name": "А25", "route_type": "3"},
        {"route_id": "R_TROLLEY", "route_short_name": "Тр30", "route_type": "3"},
        {"route_id": "R_TRAM", "route_short_name": "Т07", "route_type": "0"},
    ]
    trips = [
        {"route_id": "R_BUS", "trip_id": "100_0_0", "direction_id": "0"},
        {"route_id": "R_BUS", "trip_id": "100_1_1", "direction_id": "1"},
        {"route_id": "R_TROLLEY", "trip_id": "200_0_0", "direction_id": "0"},
        {"route_id": "R_TRAM", "trip_id": "300_0_0", "direction_id": "0"},
    ]
    stop_times = [
        {"trip_id": "100_0_0", "stop_id": "START", "stop_sequence": "1"},
        {"trip_id": "100_0_0", "stop_id": "N", "stop_sequence": "2"},
        {"trip_id": "100_0_0", "stop_id": "FAR", "stop_sequence": "3"},
        {"trip_id": "100_1_1", "stop_id": "START", "stop_sequence": "1"},
        {"trip_id": "100_1_1", "stop_id": "S", "stop_sequence": "2"},
        {"trip_id": "100_1_1", "stop_id": "FAR", "stop_sequence": "3"},
        {"trip_id": "200_0_0", "stop_id": "N", "stop_sequence": "1"},
        {"trip_id": "300_0_0", "stop_id": "S", "stop_sequence": "1"},
    ]
    stops = [
        {"stop_id": "N", "stop_lat": STOP_NORTH[0], "stop_lon": STOP_NORTH[1]},
        {"stop_id": "S", "stop_lat": STOP_SOUTH[0], "stop_lon": STOP_SOUTH[1]},
        {"stop_id": "START", "stop_lat": STOP_START[0], "stop_lon": STOP_START[1]},
        {"stop_id": "FAR", "stop_lat": STOP_FAR[0], "stop_lon": STOP_FAR[1]},
    ]

    buf = io.BytesIO()
    with zipfile.ZipFile(buf, "w") as zf:
        zf.writestr("routes.txt", _csv(routes, ["route_id", "route_short_name", "route_type"]))
        zf.writestr("trips.txt", _csv(trips, ["route_id", "trip_id", "direction_id"]))
        zf.writestr("stop_times.txt", _csv(stop_times, ["trip_id", "stop_id", "stop_sequence"]))
        zf.writestr("stops.txt", _csv(stops, ["stop_id", "stop_lat", "stop_lon"]))
    return buf.getvalue()


@pytest.fixture(scope="module")
def feed():
    return _build("2026-08-02", make_static_zip())


def make_feed_bytes(entities: list[dict], feed_ts: int = FEED_TS) -> bytes:
    msg = gtfs_realtime_pb2.FeedMessage()
    msg.header.gtfs_realtime_version = "2.0"
    msg.header.timestamp = feed_ts
    for i, e in enumerate(entities):
        ent = msg.entity.add()
        ent.id = str(i)
        v = ent.vehicle
        v.trip.trip_id = e["trip_id"]
        v.trip.route_id = e["route_id"]
        v.position.latitude = e["lat"]
        v.position.longitude = e["lon"]
        v.position.speed = e.get("speed", 8.0)
        v.timestamp = e.get("veh_ts", feed_ts)
        v.vehicle.id = e.get("vehicle_id", f"v{i}")
    return msg.SerializeToString()


def run(entities, feed, seen=None, zones=None):
    rows = parse_feed(make_feed_bytes(entities))
    acc: dict = {}
    stats = DayStats()
    accumulate(rows, feed, acc, seen if seen is not None else set(), stats, zones)
    return acc, stats


# A point on the running lane, ~90 m north of both stops.
MOVING = {"lat": 49.840800, "lon": 24.030000}
# A point 10 m north of STOP_NORTH.
AT_NORTH_STOP = {"lat": 49.840090, "lon": 24.030000}


def test_bus_kept_trolleybus_and_tram_dropped(feed):
    _, stats = run(
        [
            {"trip_id": "100_0_0", "route_id": "R_BUS", **MOVING},
            {"trip_id": "200_0_0", "route_id": "R_TROLLEY", **MOVING},
            {"trip_id": "300_0_0", "route_id": "R_TRAM", **MOVING},
        ],
        feed,
    )
    assert stats["kept"] == 1
    assert stats["drop_not_bus"] == 2


def test_trolleybus_route_is_route_type_3(feed):
    """Guard the reason route_type alone can't be the bus filter."""
    assert "R_BUS" in feed.bus_route_ids
    assert "R_TROLLEY" not in feed.bus_route_ids
    assert "R_TRAM" not in feed.bus_route_ids


def test_stale_vehicle_dropped(feed):
    _, stats = run(
        [
            {"trip_id": "100_0_0", "route_id": "R_BUS", "veh_ts": FEED_TS - 200, **MOVING},
            {"trip_id": "100_0_0", "route_id": "R_BUS", "veh_ts": FEED_TS - 30, **MOVING},
        ],
        feed,
    )
    assert stats["drop_stale"] == 1
    assert stats["kept"] == 1


def test_duplicate_vehicle_timestamp_counted_once(feed):
    entity = {"trip_id": "100_0_0", "route_id": "R_BUS", "vehicle_id": "bus1", **MOVING}
    seen: set = set()
    _, first = run([entity], feed, seen)
    acc, second = run([entity], feed, seen)
    assert first["kept"] == 1
    assert second["drop_duplicate"] == 1
    assert second["kept"] == 0


def test_position_at_own_trip_stop_dropped(feed):
    _, stats = run(
        [{"trip_id": "100_0_0", "route_id": "R_BUS", **AT_NORTH_STOP}],
        feed,
    )
    assert stats["drop_at_stop"] == 1
    assert stats["kept"] == 0


def test_position_at_opposite_side_stop_kept(feed):
    """The same spot, for the direction-1 trip whose stop is across the street.

    Stop N belongs to direction 0 only, so a direction-1 bus rolling past it is
    an observation of running speed, not a dwell.
    """
    _, stats = run(
        [{"trip_id": "100_1_1", "route_id": "R_BUS", **AT_NORTH_STOP}],
        feed,
    )
    assert stats["drop_at_stop"] == 0
    assert stats["kept"] == 1


def test_unknown_trip_falls_back_to_route_and_direction(feed):
    """May-era trip_ids are gone from the July schedule; direction still holds."""
    assert feed.stops_for("999_7_0", "R_BUS") == frozenset({"START", "N", "FAR"})
    assert feed.stops_for("999_7_1", "R_BUS") == frozenset({"START", "S", "FAR"})

    _, stats = run([{"trip_id": "999_7_0", "route_id": "R_BUS", **AT_NORTH_STOP}], feed)
    assert stats["drop_at_stop"] == 1

    _, stats = run([{"trip_id": "999_7_1", "route_id": "R_BUS", **AT_NORTH_STOP}], feed)
    assert stats["kept"] == 1


def test_terminal_exclusion_reaches_further_than_a_normal_stop(feed):
    """Buses lay over at the end of a run, often standing past the stop."""
    tx, ty = project_xy(STOP_FAR[1], STOP_FAR[0])
    own = feed.terminals_for("100_0_0", "R_BUS")
    assert own == frozenset({"START", "FAR"})

    # 80 m out: past the 40 m stop radius, inside the 120 m terminal radius.
    assert near_trip_stop(tx + 80.0, ty, own, feed, radius_m=120.0) is True
    assert near_trip_stop(tx + 80.0, ty, own, feed) is False
    # 200 m out is running road again.
    assert near_trip_stop(tx + 200.0, ty, own, feed, radius_m=120.0) is False


def test_terminal_dropped_through_the_filter_chain(feed):
    """A point 80 m from the terminal is dropped, and reported as a terminal."""
    metres_per_deg_lon = 111_320 * math.cos(math.radians(STOP_FAR[0]))
    near_terminal = {
        "lat": STOP_FAR[0],
        "lon": STOP_FAR[1] + 80.0 / metres_per_deg_lon,
    }
    _, stats = run([{"trip_id": "100_0_0", "route_id": "R_BUS", **near_terminal}], feed)
    assert stats["drop_at_terminal"] == 1
    assert stats["drop_at_stop"] == 0
    assert stats["kept"] == 0


def test_depot_zone_dropped(feed):
    """Discovered depots mask out parked buses that no stop rule would catch."""
    zx, zy = project_xy(MOVING["lon"], MOVING["lat"])
    zones = [(zx, zy, 100.0**2)]

    assert in_depot(zx + 50.0, zy, zones) is True
    assert in_depot(zx + 150.0, zy, zones) is False

    _, stats = run([{"trip_id": "100_0_0", "route_id": "R_BUS", **MOVING}], feed, zones=zones)
    assert stats["drop_depot"] == 1
    assert stats["kept"] == 0

    # Same point, no depot on file: kept.
    _, stats = run([{"trip_id": "100_0_0", "route_id": "R_BUS", **MOVING}], feed)
    assert stats["kept"] == 1


def test_depot_discovery_is_shielded_by_mid_route_stops_only(feed):
    """A yard at a terminus must stay discoverable; dwell at a stop must not.

    Layovers sprawl past the terminal stop, so terminals cannot be allowed to
    hide a candidate — that is what kept the bus station forecourt and the end
    of Horodotska out of the mask.
    """
    from speedmap.depots import _away_from_stops

    candidates = pd.DataFrame(
        {
            "cx": [0, 1],
            "cy": [0, 1],
            # 60 m from a mid-route stop, and 60 m from a terminal.
            "lat": [STOP_NORTH[0] + 60 / 111_320, STOP_FAR[0] + 60 / 111_320],
            "lon": [STOP_NORTH[1], STOP_FAR[1]],
            "n": [1000, 1000],
        }
    )
    kept = _away_from_stops(candidates, feed)

    assert list(kept["cx"]) == [1], "only the terminal-side candidate survives"


def test_speed_and_bbox_gates(feed):
    _, stats = run(
        [
            {"trip_id": "100_0_0", "route_id": "R_BUS", "speed": 40.0, **MOVING},
            {"trip_id": "100_0_0", "route_id": "R_BUS", "lat": 52.0, "lon": 21.0},
        ],
        feed,
    )
    assert stats["drop_speed"] == 1
    assert stats["drop_bbox"] == 1


def test_accumulator_averages_and_bins(feed):
    acc, stats = run(
        [
            {"trip_id": "100_0_0", "route_id": "R_BUS", "speed": 6.0, "vehicle_id": "a", **MOVING},
            {"trip_id": "100_0_0", "route_id": "R_BUS", "speed": 10.0, "vehicle_id": "b", **MOVING},
        ],
        feed,
    )
    assert stats["kept"] == 2
    assert len(acc) == 1
    (month, hour, _cx, _cy), (n, sum_speed, sum_lat, _sum_lon) = next(iter(acc.items()))
    assert n == 2
    assert sum_speed == pytest.approx(16.0)
    # float32 in the wire format, hence the loose tolerance.
    assert sum_lat / n == pytest.approx(MOVING["lat"], abs=1e-5)
    # FEED_TS is 2026-08-06T07:06:40Z → 10:06 in Kyiv (UTC+3), not 07:00.
    assert month == "2026-08"
    assert hour == 10


def test_cells_are_true_25_m_squares():
    x, y = project_xy(24.030000, 49.840000)
    # Points 1 m apart share a cell; 25 m apart never do, on either axis.
    assert cell_of(x, y) == cell_of(x + 1.0, y + 1.0) or cell_of(x, y) == cell_of(x - 1.0, y - 1.0)
    assert cell_of(x, y)[0] != cell_of(x + 25.0, y)[0]
    assert cell_of(x, y)[1] != cell_of(x, y + 25.0)[1]

    # And the projection really is metric: 25 m of longitude at this latitude
    # moves the easting by 25 m.
    east = project_xy(24.030000 + 25.0 / (111_320 * math.cos(math.radians(49.84))), 49.840000)
    assert east[0] - x == pytest.approx(25.0, abs=0.5)


def test_stop_radius_boundary(feed):
    x, y = project_xy(STOP_NORTH[1], STOP_NORTH[0])
    own = frozenset({"N"})
    assert near_trip_stop(x + 30.0, y, own, feed) is True
    assert near_trip_stop(x + 60.0, y, own, feed) is False
    # A stop 200 m away in the same bucket neighbourhood must not count.
    assert near_trip_stop(x + 30.0, y, frozenset({"FAR"}), feed) is False
