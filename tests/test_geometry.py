"""Projecting points and stops onto a route's shape."""

from __future__ import annotations

import math

import pytest

from speedmap.geometry import candidates, cumulative, match_stops, project, simplify

# A path 1000 m east, then 1000 m north. Plain metres, as if already projected.
CORNER = [(0.0, 0.0), (500.0, 0.0), (1000.0, 0.0), (1000.0, 1000.0)]

# Out along a street and back down it, 40 m apart — the shape of a route that
# serves the same road in both directions on one trip.
THERE_AND_BACK = [(0.0, 0.0), (1000.0, 0.0), (1000.0, 40.0), (0.0, 40.0)]


def test_cumulative_measures_along_the_line():
    assert cumulative(CORNER) == [0.0, 500.0, 1000.0, 2000.0]


def test_simplify_keeps_the_corner_and_drops_the_collinear_point():
    assert simplify(CORNER, 1.0) == [0, 2, 3]  # the midpoint of the straight run goes


def test_simplify_keeps_a_point_that_is_off_the_line_at_all():
    """A point exactly on the line carries nothing and goes at any tolerance;
    one that bends the line is kept once the tolerance is tight enough."""
    kinked = [(0.0, 0.0), (500.0, 2.0), (1000.0, 0.0)]
    assert simplify(kinked, 5.0) == [0, 2]
    assert simplify(kinked, 0.0) == [0, 1, 2]


def test_project_finds_the_distance_along_and_the_distance_off():
    along, off = project(CORNER, cumulative(CORNER), 300.0, 20.0)
    assert along == pytest.approx(300.0)
    assert off == pytest.approx(20.0)


def test_project_past_the_end_lands_on_the_end():
    along, off = project(CORNER, cumulative(CORNER), 1200.0, 1200.0)
    assert along == pytest.approx(2000.0)
    assert off == pytest.approx(math.hypot(200.0, 200.0))


def test_a_doubled_back_route_offers_both_passes():
    cum = cumulative(THERE_AND_BACK)
    places = candidates(THERE_AND_BACK, cum, 500.0, 20.0, max_off=30.0)
    assert len(places) == 2
    assert places[0][0] == pytest.approx(500.0)  # outbound
    assert places[1][0] == pytest.approx(1540.0)  # and again on the way back


def test_stops_are_matched_in_order():
    stops = [(0.0, 0.0), (500.0, 5.0), (1000.0, 0.0), (1000.0, 700.0)]
    out = match_stops(CORNER, cumulative(CORNER), stops, max_off=50.0)
    assert out == pytest.approx([0.0, 500.0, 1000.0, 1700.0])


def test_a_stop_on_the_return_leg_is_placed_on_the_return_leg():
    """Nearest-point matching would put the last stop back at 500 m."""
    stops = [(100.0, 0.0), (900.0, 0.0), (500.0, 40.0)]
    out = match_stops(THERE_AND_BACK, cumulative(THERE_AND_BACK), stops, max_off=30.0)
    assert out == pytest.approx([100.0, 900.0, 1540.0])


def test_a_small_backwards_blip_is_tolerated_and_flattened():
    # The middle stop projects 10 m behind the one before it.
    stops = [(500.0, 0.0), (490.0, 0.0), (900.0, 0.0)]
    out = match_stops(CORNER, cumulative(CORNER), stops, max_off=50.0)
    assert out == pytest.approx([500.0, 500.0, 900.0])


def test_a_stop_nowhere_near_the_shape_fails_the_match():
    stops = [(0.0, 0.0), (500.0, 900.0)]
    assert match_stops(CORNER, cumulative(CORNER), stops, max_off=50.0) is None


def test_no_path_is_no_match():
    assert match_stops([(0.0, 0.0)], [0.0], [(0.0, 0.0)], max_off=50.0) is None
