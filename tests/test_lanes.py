"""Bus-lane candidates: delayed stretches ranked by the bus time they cost."""

from __future__ import annotations

import pytest

from speedmap.lanes import rank_corridors

STOPS = {s: [49.8, 24.0 + i * 0.005, f"Stop {s}"] for i, s in enumerate("123456")}


def route(path: list[str], name: str = "A1", direction: str = "0") -> dict:
    return {
        "route": name,
        "dir": direction,
        "name": name,
        "path": path,
        "dist": [400 * i for i in range(len(path))],
    }


def payload(*legs: tuple[int, int]) -> dict:
    """One route's hour: (avg seconds, rides) per leg."""
    return {
        "A1|0": {
            "avg": [s for s, _ in legs],
            "med": [s for s, _ in legs],
            "n": [n for _, n in legs],
        }
    }


def test_adjacent_delayed_legs_merge_into_one_ranked_corridor():
    paths = [route(["1", "2", "3", "4"])]
    by_hour = {
        12: payload((60, 100), (60, 100), (60, 100)),  # off-peak
        8: payload((120, 100), (120, 100), (60, 100)),  # legs 1-2 and 2-3 double
    }
    (corridor,) = rank_corridors(paths, STOPS, by_hour, weekdays=20)

    assert corridor["from"] == "Stop 1"
    assert corridor["to"] == "Stop 3"
    assert corridor["legs"] == ["1>2", "2>3"]
    assert corridor["length_m"] == 800
    assert corridor["routes"] == ["A1"]
    # 2 legs x 100 rides x 60 s lost, over 20 weekdays = 600 s = 10 bus-minutes.
    assert corridor["lost_min"] == pytest.approx(10)
    assert corridor["buses"] == pytest.approx(5)
    assert corridor["delay"] == pytest.approx(1.0)


def test_corridor_shorter_than_a_lane_is_worth_is_dropped():
    paths = [route(["1", "2", "3"])]
    by_hour = {
        12: payload((60, 100), (60, 100)),
        8: payload((120, 100), (60, 100)),  # one delayed 400 m leg only
    }
    assert rank_corridors(paths, STOPS, by_hour, weekdays=20, min_m=500) == []


def test_worst_corridor_comes_first_and_slow_legs_without_traffic_do_not_count():
    paths = [route(["1", "2", "3"], "A1"), route(["4", "5", "6"], "B2")]
    by_hour = {
        12: {
            "A1|0": {"avg": [60, 60], "med": [60, 60], "n": [100, 100]},
            "B2|0": {"avg": [60, 60], "med": [60, 60], "n": [100, 100]},
        },
        8: {
            "A1|0": {"avg": [90, 90], "med": [90, 90], "n": [100, 100]},
            "B2|0": {"avg": [120, 120], "med": [120, 120], "n": [100, 100]},
        },
    }
    ranked = rank_corridors(paths, STOPS, by_hour, weekdays=20)
    assert [c["routes"] for c in ranked] == [["B2"], ["A1"]]


def test_leg_without_a_trusted_off_peak_is_ignored():
    paths = [route(["1", "2", "3"])]
    by_hour = {
        12: payload((60, 3), (60, 3)),  # too few rides to be a baseline
        8: payload((120, 100), (120, 100)),
    }
    assert rank_corridors(paths, STOPS, by_hour, weekdays=20) == []
