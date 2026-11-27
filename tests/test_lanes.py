"""Slowest stretches: delayed corridors ranked by their slowest hour against the first and last runs."""

from __future__ import annotations

import pytest

from speedmap.lanes import rank_corridors

STOPS = {s: [49.8, 24.0 + i * 0.005, f"Stop {s}"] for i, s in enumerate("1234567")}


def route(path: str, name: str = "A1") -> dict:
    """A route over single-character stop ids, 400 m between stops."""
    return {
        "route": name,
        "dir": "0",
        "name": name,
        "path": list(path),
        "dist": [400 * i for i in range(len(path))],
    }


def hour(**routes: list[tuple[int, int]]) -> dict:
    """One hour's payload: per route, (avg seconds, rides) per leg."""
    return {
        f"{name}|0": {
            "avg": [s for s, _ in legs],
            "med": [s for s, _ in legs],
            "n": [n for _, n in legs],
        }
        for name, legs in routes.items()
    }


def test_adjacent_delayed_legs_merge_into_one_corridor_timed_against_free_flow():
    paths = [route("1234")]
    by_hour = {
        22: hour(A1=[(30, 100)] * 3),  # empty night roads: the free-flow time
        12: hour(A1=[(60, 100), (60, 100), (30, 100)]),
        8: hour(A1=[(120, 100), (120, 100), (30, 100)]),
    }
    (corridor,) = rank_corridors(paths, STOPS, by_hour, weekdays=20)

    assert corridor["from"] == "Stop 1"
    assert corridor["to"] == "Stop 3"
    assert corridor["legs"] == ["1>2", "2>3"]
    assert corridor["length_m"] == 800
    assert corridor["routes"] == ["A1"]
    # Per leg 100 rides x 30 s at midday + 100 x 90 s at rush over free flow;
    # 2 legs over 20 weekdays = 1200 s = 20 bus-minutes.
    assert corridor["lost_min"] == pytest.approx(20)
    assert corridor["lost_per_km"] == pytest.approx(25)
    assert corridor["buses"] == pytest.approx(5)
    # Slowest hour 120 s against the 30 s of the last night runs.
    assert corridor["delay"] == pytest.approx(3.0)


def test_a_street_jammed_all_day_is_a_candidate():
    paths = [route("123")]
    by_hour = {
        6: hour(A1=[(30, 100)] * 2),
        12: hour(A1=[(90, 100)] * 2),  # as slow at midday as at rush
        8: hour(A1=[(90, 100)] * 2),
    }
    (corridor,) = rank_corridors(paths, STOPS, by_hour, weekdays=20)
    assert corridor["legs"] == ["1>2", "2>3"]
    # 2 legs x 2 hours x 100 rides x 60 s over 20 weekdays = 1200 s.
    assert corridor["lost_min"] == pytest.approx(20)


def test_corridor_carries_each_leg_drawn_along_the_route_shape():
    shaped = route("123") | {
        # A dogleg, 847 m long: stop 2 sits past its first corner, stop 3 at its end.
        "shape": [[49.8, 24.0], [49.801, 24.0], [49.801, 24.005], [49.8, 24.010]],
        "dist": [0, 400, 847],
    }
    by_hour = {
        12: hour(A1=[(60, 100)] * 2),
        8: hour(A1=[(120, 100)] * 2),
    }
    (corridor,) = rank_corridors([shaped], STOPS, by_hour, weekdays=20)

    first, second = corridor["lines"]
    assert first[0] == pytest.approx([49.8, 24.0])
    assert [49.801, 24.0] in first  # the shape's corner, not a straight stop-to-stop line
    assert first[-1] == pytest.approx(second[0])
    assert second[-1] == pytest.approx([49.8, 24.010], abs=1e-4)


def test_leg_is_drawn_from_a_route_with_a_shape_even_when_a_shapeless_one_comes_first():
    shapeless = route("123", "A1")
    shaped = route("123", "B2") | {
        "shape": [[49.8, 24.0], [49.801, 24.0], [49.801, 24.005], [49.8, 24.010]],
        "dist": [0, 400, 847],
    }
    by_hour = {
        12: hour(A1=[(60, 100)] * 2, B2=[(60, 100)] * 2),
        8: hour(A1=[(120, 100)] * 2, B2=[(120, 100)] * 2),
    }
    (corridor,) = rank_corridors([shapeless, shaped], STOPS, by_hour, weekdays=20)

    assert [49.801, 24.0] in corridor["lines"][0]
    assert corridor["length_m"] == 847


def test_corridor_shorter_than_a_lane_is_worth_is_dropped():
    paths = [route("123")]
    by_hour = {
        12: hour(A1=[(60, 100), (60, 100)]),
        8: hour(A1=[(120, 100), (60, 100)]),  # one delayed 400 m leg only
    }
    assert rank_corridors(paths, STOPS, by_hour, weekdays=20, min_m=500) == []


def test_street_carries_on_through_a_stop_where_a_lighter_route_joins():
    paths = [route("1234", "A1"), route("52", "B2")]
    by_hour = {
        12: hour(A1=[(60, 100)] * 3, B2=[(60, 20)]),
        8: hour(A1=[(120, 100)] * 3, B2=[(120, 20)]),
    }
    legs = sorted(c["legs"] for c in rank_corridors(paths, STOPS, by_hour, weekdays=20))
    assert legs == [["1>2", "2>3", "3>4"], ["5>2"]]


def test_street_follows_the_busier_branch_where_routes_split():
    # Both drive 1>2, then A1 goes on to 3 and the lighter B2 turns off to 6.
    paths = [route("123", "A1"), route("126", "B2")]
    by_hour = {
        12: hour(A1=[(60, 100)] * 2, B2=[(60, 20)] * 2),
        8: hour(A1=[(120, 100)] * 2, B2=[(120, 20)] * 2),
    }
    legs = sorted(c["legs"] for c in rank_corridors(paths, STOPS, by_hour, weekdays=20))
    assert legs == [["1>2", "2>3"], ["2>6"]]


def test_a_few_rides_caught_in_a_terminal_layover_do_not_make_a_leg_hot():
    # Typical rides take 50 s at every hour; at rush a few runs swallowed a
    # 20-minute layover, which drags the mean but not the median.
    paths = [route("12")]
    by_hour = {
        12: {"A1|0": {"avg": [50], "med": [50], "n": [100]}},
        8: {"A1|0": {"avg": [110], "med": [50], "n": [100]}},
    }
    assert rank_corridors(paths, STOPS, by_hour, weekdays=20) == []


def test_leg_without_a_trusted_free_flow_hour_is_ignored():
    paths = [route("123")]
    by_hour = {
        22: hour(A1=[(30, 3)] * 2),  # too few rides to count
        12: hour(A1=[(60, 3)] * 2),
        8: hour(A1=[(120, 3)] * 2),
    }
    assert rank_corridors(paths, STOPS, by_hour, weekdays=20) == []


def test_slowest_ranking_puts_the_most_delayed_busy_stretch_first():
    paths = [route("12", "A1"), route("34", "B2"), route("56", "C3")]
    by_hour = {
        6: hour(A1=[(60, 100)], B2=[(60, 100)], C3=[(60, 10)]),
        # A1 loses more bus time, B2 is slower; C3 is slowest but nearly empty.
        8: hour(A1=[(90, 400)], B2=[(150, 100)], C3=[(300, 10)]),
    }
    ranked = rank_corridors(paths, STOPS, by_hour, weekdays=20, min_buses=5)
    assert [c["routes"] for c in ranked] == [["B2"], ["A1"]]


def test_baseline_is_the_quicker_of_the_first_and_last_service_hour_not_a_quiet_midday():
    paths = [route("123")]
    by_hour = {
        6: hour(A1=[(60, 100)] * 2),  # first buses of the morning
        10: hour(A1=[(30, 100)] * 2),  # an odd quick mid-morning: not the yardstick
        17: hour(A1=[(90, 100)] * 2),  # the slowest hour of the day
        22: hour(A1=[(45, 100)] * 2),  # last buses of the evening
    }
    (corridor,) = rank_corridors(paths, STOPS, by_hour, weekdays=20)
    # Slowest hour 90 s against the evening's 45 s.
    assert corridor["delay"] == pytest.approx(1.0)


def test_last_service_hour_runs_past_midnight():
    paths = [route("123")]
    by_hour = {
        0: hour(A1=[(40, 100)] * 2),  # the last runs, after midnight
        8: hour(A1=[(100, 100)] * 2),
        23: hour(A1=[(60, 100)] * 2),
    }
    (corridor,) = rank_corridors(paths, STOPS, by_hour, weekdays=20)
    # First runs are at 8 (100 s), last at 0 (40 s): the baseline is 40 s.
    assert corridor["delay"] == pytest.approx(1.5)


def test_corridor_delay_is_its_slowest_hour_as_a_whole_not_each_legs_own_worst():
    paths = [route("123")]
    by_hour = {
        6: hour(A1=[(60, 100)] * 2),
        8: hour(A1=[(120, 100), (60, 100)]),
        17: hour(A1=[(60, 100), (120, 100)]),
        22: hour(A1=[(60, 100)] * 2),
    }
    (corridor,) = rank_corridors(paths, STOPS, by_hour, weekdays=20)
    # 180 s at 8 or at 17 against 120 s at either end of the day.
    assert corridor["delay"] == pytest.approx(0.5)
