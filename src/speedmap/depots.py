"""Find bus depots and off-street layover yards, which GTFS does not publish.

They show up in the aggregates as tight knots of cells where buses sit
motionless for hours, far from any stop. Congested junctions look similar for
an hour or two at a time, so the discriminator is *persistence*: a depot is
still full of stationary buses at 05:00 and at 23:00, while a jam is not.

Discovery reads the per-day parquet aggregates already on disk — no second
pass over R2 — and writes data/depots.json, which `aggregate.py` then uses as
an exclusion mask.

Run: python -m speedmap.depots            (discover and write)
     python -m speedmap.depots --list     (show what is on file)
"""

from __future__ import annotations

import argparse
import json
import math
import sys
from collections import defaultdict

import pandas as pd

from . import r2
from .config import (
    AGG_DIR,
    CELL_SIZE_M,
    DEPOT_FILE,
    DEPOT_MAX_KMH,
    DEPOT_MIN_DIST_TO_STOP_M,
    DEPOT_MIN_HOURS,
    DEPOT_MIN_SAMPLES,
    DEPOT_PAD_M,
)
from .static_feed import load_for_date
from .utm import project_xy

MPS_TO_KMH = 3.6
# Cells this far apart (in cell units) still belong to the same yard.
_LINK_RADIUS_CELLS = 2


def _stationary_cells(df: pd.DataFrame) -> pd.DataFrame:
    """Cells that are slow, busy, and slow across many separate hours."""
    per_hour = (
        df.groupby(["cx", "cy", "hour"])[["n", "sum_speed", "sum_lat", "sum_lon"]]
        .sum()
        .reset_index()
    )
    per_hour["kmh"] = per_hour["sum_speed"] / per_hour["n"] * MPS_TO_KMH
    slow_hours = per_hour[per_hour["kmh"] <= DEPOT_MAX_KMH]

    hours_slow = slow_hours.groupby(["cx", "cy"])["hour"].nunique()
    cells = (
        df.groupby(["cx", "cy"])[["n", "sum_speed", "sum_lat", "sum_lon"]].sum().reset_index()
    )
    cells["kmh"] = cells["sum_speed"] / cells["n"] * MPS_TO_KMH
    cells["lat"] = cells["sum_lat"] / cells["n"]
    cells["lon"] = cells["sum_lon"] / cells["n"]
    cells["hours_slow"] = cells.set_index(["cx", "cy"]).index.map(hours_slow).fillna(0).astype(int)

    return cells[
        (cells["kmh"] <= DEPOT_MAX_KMH)
        & (cells["n"] >= DEPOT_MIN_SAMPLES)
        & (cells["hours_slow"] >= DEPOT_MIN_HOURS)
    ]


def _away_from_stops(cells: pd.DataFrame, feed) -> pd.DataFrame:
    """Drop candidates near any bus stop — those are dwell, not a depot."""
    bucket = max(DEPOT_MIN_DIST_TO_STOP_M, 100.0)
    reach = math.ceil(DEPOT_MIN_DIST_TO_STOP_M / bucket)
    index: dict[tuple[int, int], list[tuple[float, float]]] = defaultdict(list)
    for sx, sy in feed.stop_xy.values():
        index[(int(sx // bucket), int(sy // bucket))].append((sx, sy))

    limit = DEPOT_MIN_DIST_TO_STOP_M**2
    keep = []
    for lat, lon in zip(cells["lat"], cells["lon"]):
        x, y = project_xy(lon, lat)
        bx, by = int(x // bucket), int(y // bucket)
        near = False
        for dx in range(-reach, reach + 1):
            for dy in range(-reach, reach + 1):
                for sx, sy in index.get((bx + dx, by + dy), ()):
                    if (sx - x) ** 2 + (sy - y) ** 2 <= limit:
                        near = True
                        break
                if near:
                    break
            if near:
                break
        keep.append(not near)
    return cells[pd.Series(keep, index=cells.index)]


def _cluster(cells: pd.DataFrame) -> list[dict]:
    """Group neighbouring candidate cells into one site each."""
    coords = {(int(cx), int(cy)): i for i, (cx, cy) in enumerate(zip(cells["cx"], cells["cy"]))}
    rows = cells.reset_index(drop=True)
    unvisited = set(coords)
    sites = []

    while unvisited:
        seed = unvisited.pop()
        members = [seed]
        queue = [seed]
        while queue:
            cx, cy = queue.pop()
            for dx in range(-_LINK_RADIUS_CELLS, _LINK_RADIUS_CELLS + 1):
                for dy in range(-_LINK_RADIUS_CELLS, _LINK_RADIUS_CELLS + 1):
                    nb = (cx + dx, cy + dy)
                    if nb in unvisited:
                        unvisited.discard(nb)
                        members.append(nb)
                        queue.append(nb)

        idx = [coords[m] for m in members]
        member_rows = rows.iloc[idx]
        weight = member_rows["n"].sum()
        lat = float((member_rows["lat"] * member_rows["n"]).sum() / weight)
        lon = float((member_rows["lon"] * member_rows["n"]).sum() / weight)
        cx0, cy0 = project_xy(lon, lat)
        spread = max(
            math.hypot(*(v - o for v, o in zip(project_xy(mlon, mlat), (cx0, cy0))))
            for mlat, mlon in zip(member_rows["lat"], member_rows["lon"])
        )
        sites.append(
            {
                "lat": round(lat, 6),
                "lon": round(lon, 6),
                "radius_m": round(spread + DEPOT_PAD_M, 1),
                "cells": len(members),
                "samples": int(weight),
                "kmh": round(
                    float(member_rows["sum_speed"].sum() / weight * MPS_TO_KMH), 2
                ),
            }
        )

    sites.sort(key=lambda s: -s["samples"])
    return sites


def discover(force: bool = False) -> list[dict]:
    # Discovery must see *un-masked* aggregates: once aggregate.py starts
    # excluding these sites, the evidence for them is gone from the parquets,
    # and re-running discovery on masked data would quietly empty the file.
    if DEPOT_FILE.exists() and not force:
        raise SystemExit(
            f"{DEPOT_FILE} already exists. The aggregates on disk were probably built with it "
            "as a mask, so re-discovering from them would lose sites. Pass --force only after "
            "`make ingest-all --force` was run with the depot file moved aside."
        )

    paths = sorted(AGG_DIR.glob("*.parquet"))
    if not paths:
        raise SystemExit(f"no aggregates in {AGG_DIR} — run `make ingest-all` first")
    df = pd.concat([pd.read_parquet(p) for p in paths], ignore_index=True)

    feed = load_for_date(r2.make_client(), paths[-1].stem)
    candidates = _away_from_stops(_stationary_cells(df), feed)
    sites = _cluster(candidates)

    DEPOT_FILE.parent.mkdir(parents=True, exist_ok=True)
    DEPOT_FILE.write_text(
        json.dumps(
            {
                "criteria": {
                    "max_kmh": DEPOT_MAX_KMH,
                    "min_samples": DEPOT_MIN_SAMPLES,
                    "min_hours_slow": DEPOT_MIN_HOURS,
                    "min_dist_to_stop_m": DEPOT_MIN_DIST_TO_STOP_M,
                    "pad_m": DEPOT_PAD_M,
                    "cell_size_m": CELL_SIZE_M,
                },
                "sites": sites,
            },
            indent=2,
        ),
        encoding="utf-8",
    )
    return sites


def load_sites() -> list[dict]:
    """Depot sites as (x, y, radius²) in UTM, ready for the filter chain."""
    if not DEPOT_FILE.exists():
        return []
    return json.loads(DEPOT_FILE.read_text(encoding="utf-8"))["sites"]


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("--list", action="store_true", help="print the sites already on file")
    args = ap.parse_args(argv)

    sites = load_sites() if args.list else discover()
    total = sum(s["samples"] for s in sites)
    print(f"{len(sites)} site(s), {total:,} samples inside them")
    for site in sites[:20]:
        print(
            f"  {site['lat']:.5f},{site['lon']:.5f}  r={site['radius_m']:>5.0f} m  "
            f"{site['cells']:>3} cells  {site['samples']:>7,} samples  {site['kmh']:.1f} km/h"
        )
    if len(sites) > 20:
        print(f"  … {len(sites) - 20} more")
    return 0


if __name__ == "__main__":
    sys.exit(main())
