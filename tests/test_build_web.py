"""Median-from-histogram and payload shape."""

from __future__ import annotations

import pandas as pd
import pytest

from speedmap.build_web import _medians, _payload


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
