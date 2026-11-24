"""Route shapes redrawn from where the buses actually drove."""

from __future__ import annotations

import pandas as pd
import pytest

from speedmap.grid import project
from speedmap.segments import SegStats, accumulate
from speedmap.static_feed import _build
from speedmap.traces import TRACE_COLUMNS, dense_shapes, metres

from .test_filters import STOP_FAR, STOP_NORTH, STOP_START, make_static_zip
from .test_segments import T0, leg

# START and N are 1.8 km apart on a diagonal; the street between them bends
# through this corner, nearly 900 m off the straight line.
CORNER = (49.83, 24.03)


@pytest.fixture(scope="module")
def feed():
    return _build("2026-08-02", make_static_zip())


def stop_ll(ll):
    return [ll[0], ll[1]]


def bent_run(t0):
    """START, round the corner, N, then on to FAR."""
    return (
        leg(STOP_START, CORNER, t0, t0 + 100)
        + leg(CORNER, STOP_NORTH, t0 + 110, t0 + 200)
        + leg(STOP_NORTH, STOP_FAR, t0 + 210, t0 + 380)
    )


def traced(feed, runs: int) -> pd.DataFrame:
    acc, hist, trace = {}, {}, {}
    samples = {(f"v{i}", "T1_0", "R_BUS"): bent_run(T0 + 1000 * i) for i in range(runs)}
    accumulate(samples, feed, acc, hist, SegStats(), trace)
    return pd.DataFrame([(*k, *v) for k, v in trace.items()], columns=TRACE_COLUMNS)


def route():
    return {"route": "R_BUS", "dir": "0", "name": "1", "path": ["START", "N", "FAR"]}


STOPS = {
    "START": [*STOP_START, "Start"],
    "N": [*STOP_NORTH, "North"],
    "FAR": [*STOP_FAR, "Far"],
}


def nearest(line, point):
    return min(metres(p, point) for p in line)


def test_a_route_with_no_shape_is_drawn_round_the_corner_its_buses_take(feed, monkeypatch):
    monkeypatch.setattr("speedmap.segments.TRACE_MAX_OFF_M", 1000)
    (r,) = dense_shapes([route()], STOPS, traced(feed, runs=6))

    assert nearest(r["shape"], CORNER) < 25
    # Each stop sits where its leg starts, the distances measured along the new line.
    assert r["dist"][0] == 0
    assert r["dist"][1] == pytest.approx(
        metres(STOP_START, CORNER) + metres(CORNER, STOP_NORTH), rel=0.05
    )
    assert r["shape"][0] == pytest.approx(stop_ll(STOP_START), abs=1e-5)
    assert r["shape"][-1] == pytest.approx(stop_ll(STOP_FAR), abs=1e-5)


def test_samples_far_off_the_reference_line_are_left_out(feed):
    # At the default limit a 900 m bend reads as a diversion, not the street.
    (r,) = dense_shapes([route()], STOPS, traced(feed, runs=6))
    assert nearest(r["shape"], CORNER) > 300


def test_too_few_buses_leave_the_reference_line_in_place(feed, monkeypatch):
    monkeypatch.setattr("speedmap.segments.TRACE_MAX_OFF_M", 1000)
    (r,) = dense_shapes([route()], STOPS, traced(feed, runs=1))
    assert "shape" not in r


def test_project_is_the_projection_the_runs_use():
    # Sanity for the fixture: samples are UTM metres, as segments.py stores them.
    x, y = project(STOP_START[1], STOP_START[0])
    assert leg(STOP_START, STOP_START, T0, T0)[0][1:] == (x, y)
