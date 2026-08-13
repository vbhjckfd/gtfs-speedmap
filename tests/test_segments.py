"""Leg timing: what survives the run splitting, and what the legs come out as."""

from __future__ import annotations

import pytest

from speedmap.config import RUN_GAP_MAX_S, SEG_GAP_MAX_S, SEG_MAX_S
from speedmap.grid import project
from speedmap.segments import (
    SegStats,
    _observed_without_holes,
    accumulate,
    legs_of_run,
    split_runs,
)
from speedmap.static_feed import _build

from .test_filters import STOP_FAR, STOP_NORTH, STOP_START, make_static_zip

T0 = 1_786_000_000  # a Wednesday, 10:06 local
STEP_S = 10


@pytest.fixture(scope="module")
def feed():
    return _build("2026-08-02", make_static_zip())


def leg(start_ll, end_ll, t_start, t_end, step_s: int = STEP_S):
    """Samples interpolated between two points, both endpoints included."""
    steps = max(1, (t_end - t_start) // step_s)
    out = []
    for i in range(steps + 1):
        f = i / steps
        lat = start_ll[0] + (end_ll[0] - start_ll[0]) * f
        lon = start_ll[1] + (end_ll[1] - start_ll[1]) * f
        x, y = project(lon, lat)
        out.append((t_start + round((t_end - t_start) * f), x, y))
    return out


def a_run(dwell_at_north: int = 0):
    """START at T0, N two minutes later, FAR three minutes after that."""
    first = leg(STOP_START, STOP_NORTH, T0, T0 + 120)
    second = leg(STOP_NORTH, STOP_FAR, T0 + 120 + dwell_at_north, T0 + 300 + dwell_at_north)
    return first + second


def path_of(feed):
    return feed.route_dir_path[("R_BUS", "0")]


def test_path_is_ordered(feed):
    assert path_of(feed) == ("START", "N", "FAR")


def test_route_dir_falls_back_to_the_trip_id_suffix(feed):
    # A trip renumbered since this schedule was archived still places itself.
    assert feed.route_dir_for("999_9_0", "R_BUS") == ("R_BUS", "0")
    assert feed.route_dir_for("999_9_7", "R_BUS") is None


def test_legs_come_out_at_the_observed_times(feed):
    stats = SegStats()
    legs = legs_of_run(a_run(), path_of(feed), feed, stats)
    assert [(f, t) for _, f, t, _ in legs] == [("START", "N"), ("N", "FAR")]
    assert [seconds for *_, seconds in legs] == [120, 180]
    assert stats["legs"] == 2


def test_dwell_is_part_of_the_leg(feed):
    """A bus standing at a stop is riding time, which is the whole point.

    A leg runs arrival to arrival, so a dwell is charged to the leg that leaves
    the stop, and a sum across several legs telescopes: the total from A to B is
    the time between arriving at A and arriving at B however the dwells fall.
    """
    stats = SegStats()
    legs = legs_of_run(a_run(dwell_at_north=45), path_of(feed), feed, stats)
    assert [seconds for *_, seconds in legs] == [120, 225]
    assert sum(seconds for *_, seconds in legs) == 345  # 300 moving + 45 standing


def test_a_leg_spanning_a_feed_hole_is_dropped(feed):
    """The hole is punched inside the N → FAR leg, leaving the stop passes at
    either end of it intact, so only that leg is unobservable."""
    slow_second_leg = leg(STOP_START, STOP_NORTH, T0, T0 + 120) + leg(
        STOP_NORTH, STOP_FAR, T0 + 120, T0 + 720
    )
    hole = (T0 + 200, T0 + 200 + SEG_GAP_MAX_S + 60)
    run = [s for s in slow_second_leg if not (hole[0] < s[0] < hole[1])]
    stats = SegStats()
    legs = legs_of_run(run, path_of(feed), feed, stats)
    assert stats["leg_gap"] == 1
    assert [(f, t) for _, f, t, _ in legs] == [("START", "N")]


def test_a_backwards_leg_is_dropped(feed):
    """Nearest approach can land out of order; that is not a leg."""
    stats = SegStats()
    reversed_run = leg(STOP_NORTH, STOP_START, T0, T0 + 120)
    legs = legs_of_run(reversed_run, path_of(feed), feed, stats)
    assert legs == []
    assert stats["leg_backwards"] == 1


def test_an_implausibly_long_leg_is_dropped(feed):
    stats = SegStats()
    slow = leg(STOP_START, STOP_NORTH, T0, T0 + SEG_MAX_S + 60, step_s=30)
    legs = legs_of_run(slow, path_of(feed), feed, stats)
    assert legs == []
    assert stats["leg_too_long"] == 1


def test_a_stop_never_approached_breaks_the_chain(feed):
    """A short turn contributes the legs it ran and nothing either side."""
    stats = SegStats()
    legs = legs_of_run(leg(STOP_START, STOP_NORTH, T0, T0 + 120), path_of(feed), feed, stats)
    assert [(f, t) for _, f, t, _ in legs] == [("START", "N")]


def test_runs_split_on_a_long_silence():
    samples = [(T0, 0.0, 0.0), (T0 + 30, 1.0, 1.0), (T0 + 30 + RUN_GAP_MAX_S + 1, 2.0, 2.0)]
    assert [len(r) for r in split_runs(list(samples))] == [2, 1]


def test_runs_are_sorted_before_splitting():
    samples = [(T0 + 30, 1.0, 1.0), (T0, 0.0, 0.0)]
    assert split_runs(samples) == [[(T0, 0.0, 0.0), (T0 + 30, 1.0, 1.0)]]


@pytest.mark.parametrize(
    "times, expected",
    [
        ([0, 10, 20, 30], True),
        ([0, 30], True),
        ([0, SEG_GAP_MAX_S + 10, SEG_GAP_MAX_S + 20], False),
    ],
)
def test_holes_are_detected(times, expected):
    assert _observed_without_holes(times, times[0], times[-1]) is expected


def test_accumulate_keys_by_route_direction_and_local_half_hour(feed):
    acc, hist, stats = {}, {}, SegStats()
    accumulate({("BUS1", "100_0_0", "R_BUS"): a_run()}, feed, acc, hist, stats)

    assert len(acc) == 2
    months = {key[0] for key in acc}
    slots = {key[1] for key in acc}
    assert months == {"2026-08"}
    assert slots == {20}  # T0 is 10:06 in Europe/Kyiv, the 10:00–10:29 slot
    assert acc[("2026-08", 20, "R_BUS", "0", "START", "N")] == [1, 120]
    assert sum(hist.values()) == 2


def test_a_trip_with_no_known_path_is_skipped(feed):
    acc, hist, stats = {}, {}, SegStats()
    accumulate({("BUS1", "77_7_9", "NOT_A_ROUTE"): a_run()}, feed, acc, hist, stats)
    assert acc == {}
    assert stats["drop_no_path"] > 0
