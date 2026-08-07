"""The depot mask ships in the repo, so it gets checked like any other input.

It is the one piece of curated data here — regenerating it needs a full
un-masked re-ingest, so a bad edit is expensive to notice late.
"""

from __future__ import annotations

import json
import math

import pytest

from speedmap.config import BBOX, DEPOT_FILE
from speedmap.depots import load_sites
from speedmap.utm import project_xy

# A yard is tens of metres across; a kilometre-wide circle would be blanking a
# neighbourhood, which is what an unbounded growth pass produces.
MAX_PLAUSIBLE_RADIUS_M = 300.0


@pytest.fixture(scope="module")
def sites():
    if not DEPOT_FILE.exists():
        pytest.skip(f"{DEPOT_FILE} not present")
    return load_sites()


def test_the_mask_file_is_valid_json_with_criteria(sites):
    doc = json.loads(DEPOT_FILE.read_text(encoding="utf-8"))
    assert doc["criteria"]["cell_size_m"] > 0
    assert doc["sites"] is not None


def test_every_site_has_a_usable_shape(sites):
    for site in sites:
        assert 0 < site["radius_m"] <= MAX_PLAUSIBLE_RADIUS_M, site
        lat_min, lat_max, lon_min, lon_max = BBOX
        assert lat_min <= site["lat"] <= lat_max, site
        assert lon_min <= site["lon"] <= lon_max, site


def test_sites_do_not_swallow_each_other(sites):
    """Two zones may abut, but one wholly inside another means a bad radius."""
    projected = [(*project_xy(s["lon"], s["lat"]), s["radius_m"], s) for s in sites]
    for i, (x, y, r, site) in enumerate(projected):
        for j, (ox, oy, orad, other) in enumerate(projected):
            if i == j:
                continue
            if math.hypot(ox - x, oy - y) + orad < r:
                pytest.fail(f"{other} is entirely inside {site}")


def test_hand_edited_sites_say_why(sites):
    """A radius that is not what discovery would produce needs its reasoning
    written down, or the next re-discovery silently reverts it."""
    for site in sites:
        if "note" in site:
            assert len(site["note"]) > 40, site
