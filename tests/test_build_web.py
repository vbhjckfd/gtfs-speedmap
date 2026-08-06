"""Percentiles from histograms, payload shape, day-type classification."""

from __future__ import annotations

import pandas as pd
import pytest

from speedmap.build_web import (
    _medians,
    _payload,
    _percentile,
    _percentiles,
    _profile,
    daytype_of,
    free_flow,
)
from speedmap.config import PROFILE_MIN_HOURS, PROFILE_MIN_SAMPLES


def bins(rows: list[tuple[int, int, int, int]]) -> pd.DataFrame:
    """rows of (cx, cy, bin, count)."""
    return pd.DataFrame(
        [("2026-07", 8, cx, cy, b, n) for cx, cy, b, n in rows],
        columns=["month", "hour", "cx", "cy", "bin", "n"],
    )


def test_median_is_the_bin_holding_the_middle_sample():
    # Speeds: 2 at 10 km/h, 1 at 30, 2 at 50 → middle sample sits in bin 30.
    result = _medians(bins([(0, 0, 10, 2), (0, 0, 30, 1), (0, 0, 50, 2)]))
    assert result.loc[(0, 0)] == pytest.approx(30.5)


def test_median_ignores_a_long_slow_tail_that_drags_the_mean():
    # 9 samples at 30 km/h, 21 stuck at 0: mean 9 km/h, median 0.
    result = _medians(bins([(1, 1, 30, 9), (1, 1, 0, 21)]))
    assert result.loc[(1, 1)] == pytest.approx(0.5)

    # Flip the balance and the median follows the bulk, not the outliers.
    result = _medians(bins([(1, 1, 30, 21), (1, 1, 0, 9)]))
    assert result.loc[(1, 1)] == pytest.approx(30.5)


def test_median_with_even_sample_count_picks_the_lower_middle():
    result = _medians(bins([(2, 2, 10, 1), (2, 2, 20, 1)]))
    assert result.loc[(2, 2)] == pytest.approx(10.5)


def test_medians_of_empty_histogram_is_empty():
    assert _medians(bins([])).empty


def test_payload_carries_both_statistics():
    cells = pd.DataFrame(
        [("2026-07", 8, 0, 0, 10, 25.0, 498.4, 240.3)],
        columns=["month", "hour", "cx", "cy", "n", "sum_speed", "sum_lat", "sum_lon"],
    )
    # 10 samples: 8 slow, 2 fast — mean 9 km/h, median in the slow bin.
    payload = _payload(cells, bins([(0, 0, 2, 8), (0, 0, 37, 2)]))

    assert payload["v"] == [pytest.approx(9.0)]  # 25 m/s summed over 10 → 2.5 m/s
    assert payload["med"] == [pytest.approx(2.5)]
    assert payload["n"] == [10]
    assert payload["lat"] == [pytest.approx(49.84)]


def test_payload_without_histograms_still_builds():
    """Aggregates predating the histogram files must not break the build."""
    cells = pd.DataFrame(
        [("2026-07", 8, 0, 0, 10, 25.0, 498.4, 240.3)],
        columns=["month", "hour", "cx", "cy", "n", "sum_speed", "sum_lat", "sum_lon"],
    )
    payload = _payload(cells, bins([]))
    assert payload["v"] == [pytest.approx(9.0)]
    assert payload["med"] == [None]


# --- percentiles ---------------------------------------------------------


def test_percentiles_bracket_the_distribution():
    # 100 samples: 20 at 5 km/h, 60 at 20, 20 at 40. Cumulative 20 / 80 / 100,
    # so the 15th sample is in the slow bin, the 50th in the middle one and the
    # 85th has already crossed into the fast one.
    b = bins([(0, 0, 5, 20), (0, 0, 20, 60), (0, 0, 40, 20)])
    q = _percentiles(b, (0.15, 0.5, 0.85))
    assert q[0.15].loc[(0, 0)] == pytest.approx(5.5)
    assert q[0.5].loc[(0, 0)] == pytest.approx(20.5)
    assert q[0.85].loc[(0, 0)] == pytest.approx(40.5)

    # Shrink the fast tail and the 85th falls back into the middle bin:
    # cumulative 20 / 80 of 85, and the 73rd sample is where it lands.
    b = bins([(0, 0, 5, 20), (0, 0, 20, 60), (0, 0, 40, 5)])
    assert _percentile(b, 0.85).loc[(0, 0)] == pytest.approx(20.5)


def test_percentile_never_falls_off_the_bottom():
    """A tiny q must still land on the first bin, not on nothing."""
    b = bins([(0, 0, 12, 1), (0, 0, 30, 1)])
    assert _percentile(b, 0.01).loc[(0, 0)] == pytest.approx(12.5)


def test_percentile_of_a_single_bin_is_that_bin():
    b = bins([(3, 3, 7, 40)])
    for q in (0.15, 0.5, 0.85):
        assert _percentile(b, q).loc[(3, 3)] == pytest.approx(7.5)


def test_percentiles_share_one_pass_with_the_single_shot_version():
    b = bins([(0, 0, 4, 3), (0, 0, 9, 11), (0, 0, 31, 6), (1, 0, 15, 2)])
    batched = _percentiles(b, (0.15, 0.5, 0.85))
    for q in (0.15, 0.5, 0.85):
        pd.testing.assert_series_equal(batched[q], _percentile(b, q))


# --- free-flow reference and the relative metric --------------------------


def test_free_flow_pools_every_hour_and_day_type():
    """The reference has to be global, or the colours shift as the slider moves."""
    slices = {
        ("2026-07", "wd"): bins([(0, 0, 10, 90)]).rename(columns={}),
        ("2026-07", "we"): bins([(0, 0, 40, 10)]).rename(columns={}),
    }
    reference = free_flow(slices)
    # 100 samples pooled: the 85th sits in the slow bin, which one slice alone
    # would never have said.
    assert reference.loc[(0, 0)] == pytest.approx(10.5)


def test_cells_below_the_free_flow_floor_get_no_ratio():
    """A ratio against a 4 km/h reference is noise over noise."""
    slices = {("2026-07", "wd"): bins([(0, 0, 4, 50), (1, 1, 30, 50)])}
    reference = free_flow(slices)
    assert (0, 0) not in reference.index
    assert reference.loc[(1, 1)] == pytest.approx(30.5)


def test_payload_rel_is_median_over_the_reference():
    cells = pd.DataFrame(
        [("2026-07", 8, 0, 0, 10, 25.0, 498.4, 240.3)],
        columns=["month", "hour", "cx", "cy", "n", "sum_speed", "sum_lat", "sum_lon"],
    )
    reference = pd.Series([40.5], index=pd.MultiIndex.from_tuples([(0, 0)], names=["cx", "cy"]))
    payload = _payload(cells, bins([(0, 0, 20, 10)]), reference)
    assert payload["med"] == [pytest.approx(20.5)]
    assert payload["rel"] == [pytest.approx(round(20.5 / 40.5, 2))]
    assert payload["spread"] == [pytest.approx(0.0)]


def test_payload_rel_is_null_without_a_reference():
    cells = pd.DataFrame(
        [("2026-07", 8, 0, 0, 10, 25.0, 498.4, 240.3)],
        columns=["month", "hour", "cx", "cy", "n", "sum_speed", "sum_lat", "sum_lon"],
    )
    payload = _payload(cells, bins([(0, 0, 20, 10)]), pd.Series(dtype=float))
    assert payload["rel"] == [None]


# --- day types -----------------------------------------------------------


def test_daytype_splits_the_week_at_saturday():
    assert [daytype_of(d) for d in ("2026-08-03", "2026-08-07")] == ["wd", "wd"]
    assert [daytype_of(d) for d in ("2026-08-08", "2026-08-09")] == ["we", "we"]


# --- hourly profile ------------------------------------------------------


def cells_by_hour(rows: list[tuple[int, int, int]]) -> pd.DataFrame:
    """rows of (hour, n, speed_mps), all for one cell."""
    return pd.DataFrame(
        [(hour, 0, 0, n, speed * n, 49.84 * n, 24.03 * n) for hour, n, speed in rows],
        columns=["hour", "cx", "cy", "n", "sum_speed", "sum_lat", "sum_lon"],
    )


def test_profile_reports_speed_per_hour():
    hours = [5, 6, 7, 8, 9, 10]
    rows = [(hour, 10, 5.0) for hour in hours]
    rows[2] = (7, 10, 2.5)  # a slow hour
    profile = _profile(cells_by_hour(rows), hours)
    assert profile["q"] == [18, 18, 9, 18, 18, 18]
    assert profile["hours"] == hours


def test_profile_drops_cells_measured_in_too_few_hours():
    """A two-bar sparkline reads as 'no buses at 14:00', which is not what it means."""
    hours = list(range(5, 5 + PROFILE_MIN_HOURS + 1))
    thin = cells_by_hour([(hour, 10, 5.0) for hour in hours[: PROFILE_MIN_HOURS - 1]])
    assert _profile(thin, hours)["lat"] == []

    thick = cells_by_hour([(hour, 10, 5.0) for hour in hours[:PROFILE_MIN_HOURS]])
    assert len(_profile(thick, hours)["lat"]) == 1


def test_profile_marks_unmeasured_hours_rather_than_guessing():
    hours = list(range(5, 5 + PROFILE_MIN_HOURS + 1))
    rows = [(hour, 10, 5.0) for hour in hours]
    rows[1] = (hours[1], PROFILE_MIN_SAMPLES - 1, 5.0)  # too thin to report
    profile = _profile(cells_by_hour(rows), hours)
    assert profile["q"][1] == profile["no_data"]
    assert profile["q"][0] == 18
