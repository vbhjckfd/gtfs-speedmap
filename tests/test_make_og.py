from speedmap.make_og import leg_delays

ROUTES = [
    {"route": "1", "dir": "0", "path": ["a", "b"]},
    {"route": "2", "dir": "0", "path": ["a", "b"]},
]


def row(t, n):
    return {"med": [t], "avg": [t], "n": [n]}


def test_pools_routes_by_rides_and_compares_with_quickest_busy_hour():
    hourly = {
        8: {"1|0": row(200, 10), "2|0": row(100, 10)},  # pooled 150
        22: {"1|0": row(100, 5), "2|0": row(50, 5)},  # pooled 75, 10 rides
        23: {"1|0": row(10, 2)},  # quicker but too few rides to be the off-peak
    }
    assert leg_delays(ROUTES, hourly, 8) == {("a", "b"): 1.0}


def test_leg_without_a_busy_hour_has_no_delay():
    assert leg_delays(ROUTES, {8: {"1|0": row(100, 3)}}, 8) == {}


def test_stamps_month_on_every_og_png_url():
    from speedmap.make_og import stamp_version

    html = '<meta content="https://x/og.png" /><meta content="https://x/og.png?v=2026-07" />'
    assert stamp_version(html, "2026-10") == (
        '<meta content="https://x/og.png?v=2026-10" /><meta content="https://x/og.png?v=2026-10" />'
    )
