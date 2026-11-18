"""Percentiles from histograms, ride payload shape, day-type classification."""

from __future__ import annotations

import pandas as pd
import pytest

from speedmap.build_web import (
    _month_part,
    _percentile,
    _percentiles,
    _rides,
    daytype_of,
    month_of,
    month_window,
)
from speedmap.config import SEG_MIN_OBS

KEYS = ["route_id", "from_stop"]


def bins(rows: list[tuple[int, int, int, int]]) -> pd.DataFrame:
    """rows of (route, stop, bin, count), at one bin per unit for readable figures."""
    return pd.DataFrame(rows, columns=[*KEYS, "bin", "n"])


def test_median_is_the_bin_holding_the_middle_sample():
    # 2 in bin 10, 1 in bin 30, 2 in bin 50: the middle sample sits in bin 30.
    result = _percentile(bins([(0, 0, 10, 2), (0, 0, 30, 1), (0, 0, 50, 2)]), 0.5, keys=KEYS, width=1)
    assert result.loc[(0, 0)] == pytest.approx(30.5)


def test_median_with_even_sample_count_picks_the_lower_middle():
    result = _percentile(bins([(2, 2, 10, 1), (2, 2, 20, 1)]), 0.5, keys=KEYS, width=1)
    assert result.loc[(2, 2)] == pytest.approx(10.5)


def test_medians_of_empty_histogram_is_empty():
    assert _percentile(bins([]), 0.5, keys=KEYS).empty


# --- percentiles ---------------------------------------------------------


def test_percentiles_bracket_the_distribution():
    # 100 samples: 20 at 5 km/h, 60 at 20, 20 at 40. Cumulative 20 / 80 / 100,
    # so the 15th sample is in the slow bin, the 50th in the middle one and the
    # 85th has already crossed into the fast one.
    b = bins([(0, 0, 5, 20), (0, 0, 20, 60), (0, 0, 40, 20)])
    q = _percentiles(b, (0.15, 0.5, 0.85), keys=KEYS, width=1)
    assert q[0.15].loc[(0, 0)] == pytest.approx(5.5)
    assert q[0.5].loc[(0, 0)] == pytest.approx(20.5)
    assert q[0.85].loc[(0, 0)] == pytest.approx(40.5)

    # Shrink the fast tail and the 85th falls back into the middle bin:
    # cumulative 20 / 80 of 85, and the 73rd sample is where it lands.
    b = bins([(0, 0, 5, 20), (0, 0, 20, 60), (0, 0, 40, 5)])
    assert _percentile(b, 0.85, keys=KEYS, width=1).loc[(0, 0)] == pytest.approx(20.5)


def test_percentile_never_falls_off_the_bottom():
    """A tiny q must still land on the first bin, not on nothing."""
    b = bins([(0, 0, 12, 1), (0, 0, 30, 1)])
    assert _percentile(b, 0.01, keys=KEYS, width=1).loc[(0, 0)] == pytest.approx(12.5)


def test_percentile_of_a_single_bin_is_that_bin():
    b = bins([(3, 3, 7, 40)])
    for q in (0.15, 0.5, 0.85):
        assert _percentile(b, q, keys=KEYS, width=1).loc[(3, 3)] == pytest.approx(7.5)


def test_percentiles_share_one_pass_with_the_single_shot_version():
    b = bins([(0, 0, 4, 3), (0, 0, 9, 11), (0, 0, 31, 6), (1, 0, 15, 2)])
    batched = _percentiles(b, (0.15, 0.5, 0.85), keys=KEYS, width=1)
    for q in (0.15, 0.5, 0.85):
        pd.testing.assert_series_equal(batched[q], _percentile(b, q, keys=KEYS, width=1))


def test_daytype_splits_the_week_at_saturday():
    assert [daytype_of(d) for d in ("2026-08-03", "2026-08-07")] == ["wd", "wd"]
    assert [daytype_of(d) for d in ("2026-08-08", "2026-08-09")] == ["we", "we"]


# --- hourly profile ------------------------------------------------------


# --- Real ride times ------------------------------------------------------

RIDE_PATHS = [{"route": "R1", "dir": "0", "name": "А25", "path": ["A", "B", "C"]}]


def legs(rows: list[tuple[str, str, int, int]]) -> pd.DataFrame:
    """rows of (from_stop, to_stop, n, sum_s)."""
    return pd.DataFrame(
        [("2026-07", 8, "R1", "0", a, b, n, s) for a, b, n, s in rows],
        columns=["month", "hour", "route_id", "direction", "from_stop", "to_stop", "n", "sum_s"],
    )


def leg_bins(rows: list[tuple[str, str, int, int]]) -> pd.DataFrame:
    """rows of (from_stop, to_stop, bin, count)."""
    return pd.DataFrame(
        [("2026-07", 8, "R1", "0", a, b, k, n) for a, b, k, n in rows],
        columns=[
            "month", "hour", "route_id", "direction", "from_stop", "to_stop", "bin", "n",
        ],
    )


def test_rides_lay_out_along_the_path():
    frame = legs([("A", "B", 10, 1200), ("B", "C", 10, 600)])
    bins_frame = leg_bins([("A", "B", 24, 10), ("B", "C", 12, 10)])
    out = _rides(frame, bins_frame, RIDE_PATHS)

    assert list(out) == ["R1|0"]
    assert out["R1|0"]["avg"] == [120, 60]  # sum_s / n
    # Bin 24 at 5 s per bin holds 120–125 s and is reported as its centre,
    # 122.5 s, rounded to whole seconds on the way into the JSON.
    assert out["R1|0"]["med"] == [122, 62]
    assert out["R1|0"]["n"] == [10, 10]


def test_a_leg_with_too_few_observations_is_a_hole_not_a_guess():
    frame = legs([("A", "B", SEG_MIN_OBS - 1, 100), ("B", "C", 10, 600)])
    bins_frame = leg_bins([("A", "B", 4, SEG_MIN_OBS - 1), ("B", "C", 12, 10)])
    out = _rides(frame, bins_frame, RIDE_PATHS)

    assert out["R1|0"]["med"][0] is None
    assert out["R1|0"]["avg"][0] is None
    # The observation count is still reported, so the viewer can say why.
    assert out["R1|0"]["n"] == [SEG_MIN_OBS - 1, 10]


def test_a_route_nobody_was_seen_on_is_left_out():
    frame = legs([("A", "B", 10, 1200)])
    other = [{"route": "R2", "dir": "0", "name": "А99", "path": ["A", "B"]}]
    assert _rides(frame, leg_bins([("A", "B", 24, 10)]), other) == {}


def test_rides_of_nothing_is_nothing():
    assert _rides(legs([]), leg_bins([]), RIDE_PATHS) == {}


def test_running_month_reads_in_whole_weeks():
    # Weekly runs land on the 8th, 15th and 22nd, so the data ends a day before.
    assert _month_part("2026-09", "2026-09-07") == "1/4"
    assert _month_part("2026-09", "2026-09-14") == "1/2"
    assert _month_part("2026-09", "2026-09-21") == "3/4"
    assert _month_part("2026-09", "2026-09-29") == "3/4"
    assert _month_part("2026-09", "2026-09-03") == "0/4"


def test_finished_month_has_no_part():
    assert _month_part("2026-09", "2026-09-30") is None
    assert _month_part("2026-08", "2026-09-14") is None
    assert _month_part("2026-02", "2026-02-28") is None


def test_month_window_takes_the_folder_either_side():
    assert month_window("2026-03") == ("2026-02-28", "2026-04-01")
    assert month_window("2026-12") == ("2026-11-30", "2027-01-01")


def test_files_belong_to_their_month():
    assert month_of("rides-2026-07-we-all.json") == "2026-07"
    assert month_of("paths-2026-07.json") == "2026-07"
    assert month_of("month-2026-07.json") == "2026-07"
    assert month_of("all-wd-08.json") is None
    assert month_of("paths.json") is None
