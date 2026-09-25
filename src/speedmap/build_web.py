"""Merge the per-day aggregates into the JSON the viewer loads.

Each month is built on its own, from its own days only, with "all" rollups on
the day-type and hour axes:

    web/data/2026-07-wd-08.json       July weekdays, 08:00–08:59 local
    web/data/2026-07-all-all.json     July, every day, whole day
    web/data/profile-2026-07-wd.json  per-cell speed by hour, for the popup
    web/data/rides-2026-07-wd-08.json observed leg times per route, same selection
    web/data/paths-2026-07.json       July's route paths and stop geometry
    web/data/month-2026-07.json       what the index needs to know about July

and then, from the month-*.json summaries alone:

    web/data/index.json               menu contents, metrics, colour scales

There is no all-months view. It was the only thing that needed every month in
memory at once, and a month is the unit people compare.

The ride files are written only when segments.py has produced leg times; the
speed map predates them and still builds without them.

Run:
    python -m speedmap.build_web                       every month on disk, then the index
    python -m speedmap.build_web --month 2026-07 --no-index
    python -m speedmap.build_web --index               from months already built
"""

from __future__ import annotations

import argparse
import calendar
import json
import re
import sys
from datetime import date, timedelta

import numpy as np
import pandas as pd

from .depots import load_sites
from .utm import project_xy
from .config import (
    AGG_DIR,
    CELL_SIZE_M,
    FREE_FLOW_Q,
    HEADING_BINS,
    HIST_BIN_KMH,
    HIST_DIR,
    MIN_SAMPLES,
    SEG_BIN_S,
    SEG_DIR,
    SEG_HIST_DIR,
    SEG_MIN_OBS,
    STOP_PASS_RADIUS_M,
    PROFILE_MIN_HOURS,
    PROFILE_MIN_SAMPLES,
    REL_MIN_FF_KMH,
    REL_SCALE_HIGH,
    REL_SCALE_LOW,
    SCALE_HIGH_KMH,
    SCALE_LOW_KMH,
    SPREAD_SCALE_HIGH_KMH,
    SPREAD_SCALE_LOW_KMH,
    STOP_RADIUS_M,
    TERMINAL_RADIUS_M,
    TZ,
    WEB_DATA_DIR,
    paths_file,
)

MPS_TO_KMH = 3.6
ALL = "all"
NO_DATA = -1  # profile sentinel: an int array stays half the size of one with nulls

# Base day types. Saturday and Sunday differ by at most 1.5 km/h at every hour,
# against a 4–5 km/h weekday/weekend gap, so splitting them three ways would
# only halve the samples behind each weekend cell for no signal.
DAYTYPES = ("wd", "we")
DAYTYPE_LABELS = {"wd": "Weekdays", "we": "Weekends", ALL: "All days"}

MONTH_NAMES = (
    "January",
    "February",
    "March",
    "April",
    "May",
    "June",
    "July",
    "August",
    "September",
    "October",
    "November",
    "December",
)

# A cell is a place *and* a direction of travel: the two sides of a street share
# a 25 m square, and the queue into a junction has nothing to do with the run
# out of it. Everything downstream — percentiles, free-flow, the hourly profile
# — is keyed the same way, so a figure never mixes the two.
CELL_KEYS = ["cx", "cy", "dir"]
PLACE_KEYS = ["cx", "cy"]
CELL_SUMS = ["n", "sum_speed", "sum_lat", "sum_lon", "sum_sin", "sum_cos"]

# Each metric needs its own colour domain and formatting: km/h, a ratio and a
# spread cannot share one scale. `invert` flips the ramp for metrics where the
# high end is the bad end.
METRICS = [
    {"key": "v", "label": "Average", "unit": "km/h"},
    {"key": "med", "label": "Median", "unit": "km/h"},
    {"key": "p15", "label": "Slow day (p15)", "unit": "km/h"},
    # Not called "free-flow": that name belongs to the month-wide reference
    # behind `rel`, and this one is the p85 of the selection on screen.
    {"key": "p85", "label": "Fast day (p85)", "unit": "km/h"},
    {
        "key": "rel",
        "label": "% of free-flow",
        "unit": "%",
        "scale": {"low": REL_SCALE_LOW, "high": REL_SCALE_HIGH},
        "factor": 100,
        "decimals": 0,
    },
    {
        "key": "spread",
        "label": "Unreliability (p85−p15)",
        "unit": "km/h",
        "scale": {"low": SPREAD_SCALE_LOW_KMH, "high": SPREAD_SCALE_HIGH_KMH},
        "invert": True,
    },
]


def _metric_descriptors() -> list[dict]:
    """Fill in the defaults so the viewer never has to guess."""
    out = []
    for metric in METRICS:
        out.append(
            {
                "scale": {"low": SCALE_LOW_KMH, "high": SCALE_HIGH_KMH},
                "factor": 1,
                "decimals": 1,
                "invert": False,
                **metric,
            }
        )
    return out


def daytype_of(day: str) -> str:
    """Weekday or weekend, from the aggregate's YYYY-MM-DD filename.

    The day is the observed *service* day. A handful of rows near midnight land
    in the neighbouring local day, which is not worth a second timestamp column.
    """
    return "wd" if date.fromisoformat(day).weekday() < 5 else "we"


def _slice_key(day: str, month: str) -> tuple[str, str]:
    return month, daytype_of(day)


# How many day parts a slice collects before they are summed into one. Holding
# every day until the end made peak memory grow with the archive (6 GB at 136
# days); summing as it goes bounds it by the slices themselves.
FOLD_EVERY = 7


def _fold_into(
    store: dict, key: tuple[str, str], part: pd.DataFrame, keys: list[str], sums: list[str]
) -> None:
    """Add one day's part to a slice, summing the pending parts down when due."""
    parts = store.setdefault(key, [])
    parts.append(part)
    if len(parts) >= FOLD_EVERY:
        store[key] = [_sum_parts(parts, keys, sums)]


def _sum_parts(parts: list[pd.DataFrame], keys: list[str], sums: list[str]) -> pd.DataFrame:
    return (
        pd.concat(parts, ignore_index=True)
        .groupby(keys, as_index=False, sort=False)[sums]
        .sum()
    )


def _month_paths(directory, month: str) -> list:
    """The day files that can hold rows of `month`, the folder either side included."""
    first, last = month_window(month)
    return sorted(p for p in directory.glob("*.parquet") if first <= p.stem <= last)


def load_base_slices(month: str) -> tuple[dict, dict, list[str]]:
    """Read one month's per-day aggregates, bucketed into (month, daytype) slices.

    Only the 8-ish base slices are held; every rollup the viewer offers is a sum
    of these. Grouping each bucket down as it is finished keeps peak memory near
    what a single full load costs today rather than multiplying it by the new
    axis.
    """
    paths = _month_paths(AGG_DIR, month)
    if not paths:
        raise SystemExit(f"no aggregates for {month} in {AGG_DIR} — run `make ingest` first")

    cell_parts: dict[tuple[str, str], list[pd.DataFrame]] = {}
    hist_parts: dict[tuple[str, str], list[pd.DataFrame]] = {}
    days = [p.stem for p in paths if p.stem.startswith(month)]

    for path in paths:
        day = path.stem
        cells = pd.read_parquet(path)
        if "dir" not in cells.columns:
            raise SystemExit(
                f"{path} predates the direction split — re-run "
                '`make ingest ARGS="--force --only speed"`'
            )
        hist_path = HIST_DIR / path.name
        hist = pd.read_parquet(hist_path) if hist_path.exists() else None
        # A day file is almost entirely one month, but the local-time
        # conversion can push a few rows over a month boundary, so split on the
        # column rather than assuming.
        cells = cells[cells["month"] == month]
        if not cells.empty:
            _fold_into(cell_parts, _slice_key(day, month), cells, ["hour", *CELL_KEYS], CELL_SUMS)
        if hist is not None:
            hist = hist[hist["month"] == month]
            if not hist.empty:
                _fold_into(
                    hist_parts, _slice_key(day, month), hist, ["hour", *CELL_KEYS, "bin"], ["n"]
                )
        del cells, hist

    cell_slices = {
        key: _sum_parts(parts, ["hour", *CELL_KEYS], CELL_SUMS)
        for key, parts in cell_parts.items()
    }
    hist_slices = {
        key: _sum_parts(parts, ["hour", *CELL_KEYS, "bin"], ["n"])
        for key, parts in hist_parts.items()
    }
    return cell_slices, hist_slices, days


def _combine(slices: dict, month: str, daytype: str, keys: list[str]) -> pd.DataFrame:
    """Sum the base slices that make up one (month, daytype) selection."""
    parts = [
        frame
        for (m, d), frame in slices.items()
        if (month == ALL or m == month) and (daytype == ALL or d == daytype)
    ]
    if not parts:
        return pd.DataFrame(columns=keys + (["n"] if "bin" in keys else CELL_SUMS))
    if len(parts) == 1:
        return parts[0]
    columns = ["n"] if "bin" in keys else CELL_SUMS
    return pd.concat(parts, ignore_index=True).groupby(keys, as_index=False, sort=False)[
        columns
    ].sum()


def _percentiles(
    bins: pd.DataFrame,
    quantiles: tuple[float, ...],
    keys: list[str] | None = None,
    width: float = HIST_BIN_KMH,
) -> dict[float, pd.Series]:
    """Several percentiles per group, read off the cumulative histogram.

    Resolution is the bin width, so each answer is the centre of the bin the
    q-th sample falls in. The sort and the cumulative sum are the expensive part
    and do not depend on q, so every quantile shares one pass — at three
    percentiles across 300 payloads, doing it per quantile is three times the
    work for the same result.

    Cells and their speeds are the usual caller; leg times per stop pair are the
    other one, which is why the group keys and the bin width are arguments.
    """
    keys = keys or CELL_KEYS
    if bins.empty:
        return {q: pd.Series(dtype=float) for q in quantiles}

    groups = bins.groupby(keys, sort=False)["n"].sum().rename("total")
    ordered = bins.sort_values(keys + ["bin"])
    ordered["cumulative"] = ordered.groupby(keys, sort=False)["n"].cumsum()
    ordered = ordered.join(groups, on=keys)

    out = {}
    for q in quantiles:
        # The q-th sample is the ceil(total*q)-th, at least the first; the bin
        # whose running total first reaches it is the bin holding it. At q=0.5
        # this is (total+1)//2, the lower of the two middles on an even count.
        target = np.maximum(1, np.ceil(ordered["total"] * q))
        reached = ordered[ordered["cumulative"] >= target]
        first = reached.groupby(keys, sort=False)["bin"].first()
        out[q] = (first + 0.5) * width
    return out


def _percentile(
    bins: pd.DataFrame,
    q: float,
    keys: list[str] | None = None,
    width: float = HIST_BIN_KMH,
) -> pd.Series:
    return _percentiles(bins, (q,), keys=keys, width=width)[q]


def _medians(bins: pd.DataFrame) -> pd.Series:
    return _percentile(bins, 0.5)


def free_flow(hist_slices: dict) -> pd.Series:
    """Each cell's free-flow reference: its p85 over every hour and day type.

    Collapsing hour before summing keeps this far smaller than the slices it
    is derived from.
    """
    parts = [
        frame.groupby([*CELL_KEYS, "bin"], as_index=False, sort=False)["n"].sum()
        for frame in hist_slices.values()
    ]
    if not parts:
        return pd.Series(dtype=float)
    everything = (
        pd.concat(parts, ignore_index=True)
        .groupby([*CELL_KEYS, "bin"], as_index=False, sort=False)["n"]
        .sum()
    )
    reference = _percentile(everything, FREE_FLOW_Q)
    return reference[reference >= REL_MIN_FF_KMH]


# --- folding the heading bins back into two directions of travel ----------
#
# Eight bins are how the samples are *counted*: fine enough that a bend inside
# one square cannot mix the two sides of a street. They are not how a street
# should be *drawn*. Where the road curves, one direction of travel sweeps
# through three to five bins and emits an arrow for each, all at their own
# sample-mean position inside the same square — measured at 17:00 city-wide,
# 2.59 arrows per square and 23% of squares carrying four or more, which at
# street zoom is a thicket rather than a road.
#
# So each square's bins are folded into at most two groups: the busiest bin
# leads, everything within 90° of it travels with it, the rest travel against
# it. 2.59 arrows per square becomes 1.46, and a curve's samples stop being
# split five ways.
#
# The fold is a property of the square across the whole month, never of one
# selection. Derive it per payload and the 08:00 view would group its bins
# differently from the all-hours view, and neither would line up with the
# free-flow reference, which spans the month too.
_GROUPS: dict[tuple[int, int, int], int] | None = None


def install_groups(cell_slices: dict) -> tuple[int, int, int]:
    """Decide, once, which group each (square, heading bin) belongs to."""
    global _GROUPS
    universe = (
        pd.concat(cell_slices.values(), ignore_index=True)
        .groupby(CELL_KEYS, as_index=False, sort=False)[["n", "sum_sin", "sum_cos"]]
        .sum()
    )
    universe["bearing"] = np.degrees(np.arctan2(universe["sum_sin"], universe["sum_cos"])) % 360
    # The busiest bin of each square is the one whose heading the rest are
    # judged against — a lane with ten times the traffic of the turn beside it
    # is the direction that square is really about.
    lead = universe.sort_values("n").groupby(PLACE_KEYS)["bearing"].last()
    against = universe.join(lead.rename("lead"), on=PLACE_KEYS)
    offset = (against["bearing"] - against["lead"]).abs() % 360
    apart = np.minimum(offset, 360 - offset)
    _GROUPS = {
        (cx, cy, direction): int(flip)
        for cx, cy, direction, flip in zip(
            against["cx"], against["cy"], against["dir"], apart > 90
        )
    }
    folded = {(cx, cy, group) for (cx, cy, _bin), group in _GROUPS.items()}
    return len(_GROUPS), len(lead), len(folded)


def regroup(frame: pd.DataFrame, keys: list[str]) -> pd.DataFrame:
    """Rewrite `dir` from heading bin to group, and sum what that merges.

    Every frame the build touches goes through here — cells, speed histograms
    and the hourly profile alike — so a figure and the arrow drawn for it are
    always the same set of samples.
    """
    if _GROUPS is None or frame.empty:
        return frame
    columns = ["n"] if "bin" in frame.columns else CELL_SUMS
    frame = frame.copy()
    frame["dir"] = [
        _GROUPS.get((cx, cy, direction), 0)
        for cx, cy, direction in zip(frame["cx"], frame["cy"], frame["dir"])
    ]
    return frame.groupby(keys, as_index=False, sort=False)[columns].sum()


_ZONES: list[tuple[float, float, float]] | None = None


def mask_zones() -> list[tuple[float, float, float]]:
    """Discovered depots as (easting, northing, radius²), projected once.

    Projecting per cell per site was costing ~1.3M redundant projections per
    payload, which the new axis would have multiplied by three.
    """
    global _ZONES
    if _ZONES is None:
        _ZONES = [
            (*project_xy(site["lon"], site["lat"]), site["radius_m"] ** 2)
            for site in load_sites()
        ]
    return _ZONES


def _masked_cells(cells: pd.DataFrame) -> pd.Series:
    """True for cells sitting inside a discovered depot or layover zone.

    aggregate.py already drops these samples, so this only matters when a zone
    was added after the last ingest — it makes a widened mask show up in the
    map immediately, instead of after another pass over R2.
    """
    zones = mask_zones()
    if not zones or cells.empty:
        return pd.Series(False, index=cells.index)

    xy = np.array([project_xy(lon, lat) for lat, lon in zip(cells["lat"], cells["lon"])])
    inside = np.zeros(len(cells), dtype=bool)
    for zx, zy, limit in zones:
        inside |= (xy[:, 0] - zx) ** 2 + (xy[:, 1] - zy) ** 2 <= limit
    return pd.Series(inside, index=cells.index)


# Whether a cell is masked depends only on where it is, not on which selection
# is being written, so the projection is done once over the whole cell universe
# and every payload then answers by lookup. Left None outside a build so the
# per-frame path above still works on its own.
_MASKED_IDS: set[tuple[int, int]] | None = None


def install_mask(cell_slices: dict) -> int:
    """Precompute the masked cell ids from every cell the build will ever emit.

    Keyed by place alone: a depot swallows every heading through it, and the
    directions of one square would otherwise each be tested at their own
    slightly different mean position.
    """
    global _MASKED_IDS
    universe = (
        pd.concat(cell_slices.values(), ignore_index=True)
        .groupby(PLACE_KEYS, as_index=False, sort=False)[CELL_SUMS]
        .sum()
    )
    universe["lat"] = universe["sum_lat"] / universe["n"]
    universe["lon"] = universe["sum_lon"] / universe["n"]
    hit = universe[_masked_cells(universe)]
    _MASKED_IDS = set(zip(hit["cx"], hit["cy"]))
    return len(_MASKED_IDS)


def _cells_of(group: pd.DataFrame) -> pd.DataFrame:
    """Collapse a selection to one row per cell, ordered by sample count so the
    busiest cells are drawn last and stay on top."""
    if group.empty:
        return group
    cells = group.groupby(CELL_KEYS, as_index=False, sort=False)[CELL_SUMS].sum()
    cells = cells[cells["n"] >= MIN_SAMPLES]
    if cells.empty:
        return cells
    cells["lat"] = cells["sum_lat"] / cells["n"]
    cells["lon"] = cells["sum_lon"] / cells["n"]
    # Degrees clockwise from north, recovered from the summed unit vectors so
    # the mean survives the 359°/0° seam. Within one bin the samples span 45°
    # at most, so this is the heading of the traffic, not of the bin.
    cells["bearing"] = np.degrees(np.arctan2(cells["sum_sin"], cells["sum_cos"])) % 360
    if _MASKED_IDS is None:
        keep = ~_masked_cells(cells)
    else:
        keep = ~pd.Series(
            [pair in _MASKED_IDS for pair in zip(cells["cx"], cells["cy"])],
            index=cells.index,
        )
    return cells[keep].sort_values("n")


def _lookup(cells: pd.DataFrame, values: pd.Series) -> np.ndarray:
    """Align a per-cell Series onto the payload's cell order, NaN where absent."""
    if values.empty:
        return np.full(len(cells), np.nan)
    return cells.set_index(CELL_KEYS).index.map(values).to_numpy(dtype=float)


def _round(values: np.ndarray, decimals: int) -> list:
    """JSON-safe rounding: NaN has no JSON spelling, so it becomes null."""
    rounded = np.round(values, decimals)
    return [None if np.isnan(v) else float(v) for v in rounded]


EMPTY_PAYLOAD = {
    k: [] for k in ("lat", "lon", "dir", "v", "med", "p15", "p85", "spread", "rel", "n")
}


def _payload(group: pd.DataFrame, bins: pd.DataFrame, ff: pd.Series | None = None) -> dict:
    cells = _cells_of(group)
    if cells.empty:
        return dict(EMPTY_PAYLOAD)

    quantiles = _percentiles(bins, (0.15, 0.5, 0.85))
    median = _lookup(cells, quantiles[0.5])
    p15 = _lookup(cells, quantiles[0.15])
    p85 = _lookup(cells, quantiles[0.85])
    reference = _lookup(cells, ff if ff is not None else pd.Series(dtype=float))

    n = cells["n"].to_numpy()
    with np.errstate(invalid="ignore", divide="ignore"):
        rel = median / reference
    return {
        "lat": cells["lat"].to_numpy().round(5).tolist(),
        "lon": cells["lon"].to_numpy().round(5).tolist(),
        # Whole degrees, and 360 rounds back to 0 — a heading a hair short of
        # north is north. The arrow drawn from this is a few pixels long, so a
        # decimal place on it would cost bytes the map cannot show.
        "dir": (cells["bearing"].to_numpy().round().astype(int) % 360).tolist(),
        "v": (cells["sum_speed"].to_numpy() / n * MPS_TO_KMH).round(1).tolist(),
        "med": _round(median, 1),
        "p15": _round(p15, 1),
        "p85": _round(p85, 1),
        "spread": _round(p85 - p15, 1),
        "rel": _round(rel, 2),
        "n": n.astype(int).tolist(),
    }


def _profile(group: pd.DataFrame, hours: list[int]) -> dict:
    """Per-cell mean speed by hour, for the popup sparkline.

    Carries its own coordinates on purpose. A payload's lat/lon is the mean of
    the samples in *that* selection, so it shifts between selections and cannot
    be used as a join key — the viewer matches by nearest cell instead.
    """
    cells = _cells_of(group)
    if cells.empty:
        return {"lat": [], "lon": [], "dir": [], "hours": hours, "q": [], "no_data": NO_DATA}

    index = pd.MultiIndex.from_frame(cells[CELL_KEYS])
    by_hour = group.groupby(["hour", *CELL_KEYS], sort=False)[["n", "sum_speed"]].sum()
    speed = (by_hour["sum_speed"] / by_hour["n"] * MPS_TO_KMH).where(
        by_hour["n"] >= PROFILE_MIN_SAMPLES
    )

    columns = []
    for hour in hours:
        try:
            at_hour = speed.xs(hour, level="hour")
        except KeyError:
            columns.append(np.full(len(cells), np.nan))
            continue
        columns.append(index.map(at_hour).to_numpy(dtype=float))

    grid = np.round(np.column_stack(columns)).astype(float)
    # A cell measured in only a handful of hours has no profile worth drawing,
    # and carrying it would nearly double the file for nothing.
    keep = (~np.isnan(grid)).sum(axis=1) >= PROFILE_MIN_HOURS
    grid = grid[keep]
    cells = cells[keep]
    grid[np.isnan(grid)] = NO_DATA
    return {
        "lat": cells["lat"].to_numpy().round(5).tolist(),
        "lon": cells["lon"].to_numpy().round(5).tolist(),
        # The two directions of a street sit a lane apart at most, closer than
        # the viewer can tell by position, so the heading is what picks the
        # right one of them out of this file.
        "dir": (cells["bearing"].to_numpy().round().astype(int) % 360).tolist(),
        "hours": hours,
        "q": grid.astype(int).ravel().tolist(),
        "no_data": NO_DATA,
    }


SEG_KEYS = ["route_id", "direction", "from_stop", "to_stop"]
SEG_SUMS = ["n", "sum_s"]


def load_ride_slices(month: str) -> tuple[dict, dict, list[dict], dict]:
    """One month's observed leg times, bucketed into the same (month, daytype) slices.

    Returns empty structures when segments.py has not run — the speed map is
    the older half of this build and must not depend on the newer one.
    """
    source = paths_file(month)
    paths_payload = json.loads(source.read_text(encoding="utf-8")) if source.exists() else {}
    paths = paths_payload.get("routes", [])
    stops = paths_payload.get("stops", {})

    leg_paths = _month_paths(SEG_DIR, month) if SEG_DIR.exists() else []
    if not leg_paths or not paths:
        return {}, {}, paths, stops

    leg_parts: dict[tuple[str, str], list[pd.DataFrame]] = {}
    bin_parts: dict[tuple[str, str], list[pd.DataFrame]] = {}
    for path in leg_paths:
        day = path.stem
        legs = pd.read_parquet(path)
        hist_path = SEG_HIST_DIR / path.name
        legs = legs[legs["month"] == month]
        if not legs.empty:
            _fold_into(leg_parts, _slice_key(day, month), legs, ["hour", *SEG_KEYS], SEG_SUMS)
        if hist_path.exists():
            bins = pd.read_parquet(hist_path)
            bins = bins[bins["month"] == month]
            if not bins.empty:
                _fold_into(
                    bin_parts, _slice_key(day, month), bins, ["hour", *SEG_KEYS, "bin"], ["n"]
                )
        del legs

    leg_slices = {
        key: _sum_parts(parts, ["hour", *SEG_KEYS], SEG_SUMS) for key, parts in leg_parts.items()
    }
    bin_slices = {
        key: _sum_parts(parts, ["hour", *SEG_KEYS, "bin"], ["n"])
        for key, parts in bin_parts.items()
    }
    return leg_slices, bin_slices, paths, stops


def _combine_rides(slices: dict, month: str, daytype: str, keys: list[str]) -> pd.DataFrame:
    parts = [
        frame
        for (m, d), frame in slices.items()
        if (month == ALL or m == month) and (daytype == ALL or d == daytype)
    ]
    columns = ["n"] if "bin" in keys else SEG_SUMS
    if not parts:
        return pd.DataFrame(columns=keys + columns)
    if len(parts) == 1:
        return parts[0]
    return pd.concat(parts, ignore_index=True).groupby(keys, as_index=False, sort=False)[
        columns
    ].sum()


def _rides(legs: pd.DataFrame, bins: pd.DataFrame, paths: list[dict]) -> dict:
    """Per route-direction, one figure per leg of its path.

    Laid out along the path rather than keyed by stop pair, so the viewer can
    add up a range of legs by slicing. A leg with too few observations is null
    rather than absent: the reader still needs to see the hole.
    """
    if legs.empty:
        return {}

    totals = legs.groupby(SEG_KEYS, sort=False)[SEG_SUMS].sum()
    counts = totals["n"].to_dict()
    means = (totals["sum_s"] / totals["n"]).to_dict()
    medians = _percentile(bins, 0.5, keys=SEG_KEYS, width=SEG_BIN_S).to_dict()

    out = {}
    for route in paths:
        key = (route["route"], route["dir"])
        path = route["path"]
        med, avg, obs = [], [], []
        for a, b in zip(path, path[1:]):
            pair = (*key, a, b)
            n = int(counts.get(pair, 0))
            obs.append(n)
            if n < SEG_MIN_OBS:
                med.append(None)
                avg.append(None)
                continue
            median = medians.get(pair)
            med.append(None if median is None or np.isnan(median) else round(float(median)))
            avg.append(round(float(means[pair])))
        if not any(obs):
            continue
        out[f"{key[0]}|{key[1]}"] = {"med": med, "avg": avg, "n": obs}
    return out


def _write(name: str, obj) -> int:
    path = WEB_DATA_DIR / f"{name}.json"
    text = json.dumps(obj, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    return len(text)


def month_window(month: str) -> tuple[str, str]:
    """First and last UTC day folder that can hold rows of a local month.

    Day folders are named by UTC date and rows carry the local month, so the
    folder either side of a month can spill a few rows into it.
    """
    year, mon = int(month[:4]), int(month[5:7])
    first = date(year, mon, 1)
    last = date(year, mon, calendar.monthrange(year, mon)[1])
    return (first - timedelta(days=1)).isoformat(), (last + timedelta(days=1)).isoformat()


def _month_part(month: str, last_day: str) -> str | None:
    """How much of a still-running month the data covers, in whole weeks.

    None for a finished month. Weekly runs land on the 8th, 15th and 22nd, so
    the running month reads 1/4, 1/2 and 3/4 of the way through; "0/4" means
    less than a week, too thin to show.
    """
    year, mon = int(month[:4]), int(month[5:7])
    if last_day[:7] != month or int(last_day[8:]) >= calendar.monthrange(year, mon)[1]:
        return None
    return {0: "0/4", 1: "1/4", 2: "1/2"}.get(int(last_day[8:]) // 7, "3/4")


def month_of(name: str) -> str | None:
    """The month a web/data file belongs to, from its name."""
    match = re.search(r"\d{4}-\d{2}(?!-\d)", name)
    return match.group(0) if match else None


def build_month(month: str) -> dict:
    """Write one month's files. Every month stands on its own.

    The heading fold, the depot mask and the free-flow reference are all
    derived from the month's own data, so a finished month is built once and
    never has to be rebuilt because a later one arrived — which is what lets
    each month run on its own machine.
    """
    cell_slices, hist_slices, days = load_base_slices(month)
    leg_slices, seg_bin_slices, paths, stops = load_ride_slices(month)
    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)
    written: set[str] = set()

    hours = sorted({int(h) for frame in cell_slices.values() for h in frame["hour"].unique()})
    types: dict[str, int] = {}
    for day in days:
        types[daytype_of(day)] = types.get(daytype_of(day), 0) + 1

    # Fold the heading bins down before anything is derived from them, so the
    # arrows, the percentiles and the free-flow reference are all folded the
    # same way.
    binned, squares, folded = install_groups(cell_slices)
    cell_slices = {
        key: regroup(frame, ["hour", *CELL_KEYS]) for key, frame in cell_slices.items()
    }
    hist_slices = {
        key: regroup(frame, ["hour", *CELL_KEYS, "bin"]) for key, frame in hist_slices.items()
    }
    masked = install_mask(cell_slices)
    ff = free_flow(hist_slices)
    print(
        f"{month}: {binned:,} heading bins over {squares:,} squares folded into {folded:,}; "
        f"{masked:,} cells masked; free-flow reference for {len(ff):,} cells "
        f"(p{FREE_FLOW_Q * 100:.0f})",
        flush=True,
    )

    total_bytes = 0

    def write(name: str, obj) -> None:
        nonlocal total_bytes
        total_bytes += _write(name, obj)
        written.add(f"{name}.json")

    for daytype in [*DAYTYPES, ALL]:
        selection = _combine(cell_slices, month, daytype, ["hour", *CELL_KEYS])
        bins = _combine(hist_slices, month, daytype, ["hour", *CELL_KEYS, "bin"])
        for hour in [*hours, ALL]:
            if hour == ALL:
                part, part_bins = selection, bins
            else:
                part = selection[selection["hour"] == hour]
                part_bins = bins[bins["hour"] == hour]
            hour_key = ALL if hour == ALL else f"{hour:02d}"
            write(f"{month}-{daytype}-{hour_key}", _payload(part, part_bins, ff))
        write(f"profile-{month}-{daytype}", _profile(selection, hours))
        del selection, bins

        if leg_slices:
            legs = _combine_rides(leg_slices, month, daytype, ["hour", *SEG_KEYS])
            seg_bins = _combine_rides(seg_bin_slices, month, daytype, ["hour", *SEG_KEYS, "bin"])
            for hour in [*hours, ALL]:
                if hour == ALL:
                    part, part_bins = legs, seg_bins
                else:
                    part = legs[legs["hour"] == hour]
                    part_bins = seg_bins[seg_bins["hour"] == hour]
                hour_key = ALL if hour == ALL else f"{hour:02d}"
                write(f"rides-{month}-{daytype}-{hour_key}", _rides(part, part_bins, paths))
            del legs, seg_bins

    if leg_slices:
        # Geometry is the same whatever day type or hour is on screen, so it
        # ships once per month and the selection files carry only numbers.
        write(f"paths-{month}", {"stops": stops, "routes": paths})

    meta = {
        "key": month,
        "days": days,
        "daytypes": {ALL: len(days), **{key: types.get(key, 0) for key in DAYTYPES}},
        "hours": hours,
        "samples": int(sum(int(f["n"].sum()) for f in cell_slices.values())),
        "routes": len(paths) if leg_slices else 0,
    }
    write(f"month-{month}", meta)

    stale = [
        p for p in WEB_DATA_DIR.glob("*.json") if month_of(p.name) == month and p.name not in written
    ]
    for path in stale:
        path.unlink()
    print(
        f"{month}: {len(written)} files, {total_bytes / 1e6:.1f} MB, {len(days)} days, "
        f"{meta['samples']:,} samples"
        + (f", {len(stale)} stale file(s) removed" if stale else ""),
        flush=True,
    )
    return meta


def write_index() -> dict:
    """Assemble index.json from the months already built into web/data.

    Reads only the small month-*.json summaries, so it runs anywhere the built
    months have been gathered, without the aggregates behind them.
    """
    metas = sorted(
        (json.loads(p.read_text(encoding="utf-8")) for p in WEB_DATA_DIR.glob("month-*.json")),
        key=lambda m: m["key"],
    )
    metas = [m for m in metas if m["days"]]
    if not metas:
        raise SystemExit(f"no months built in {WEB_DATA_DIR} — run a month build first")
    days = sorted(day for m in metas for day in m["days"])
    last = days[-1]

    months = []
    for meta in metas:
        part = _month_part(meta["key"], last)
        if part == "0/4":
            print(f"{meta['key']}: under a week of data, left out of the menu")
            continue
        months.append(
            {
                "key": meta["key"],
                "label": f"{MONTH_NAMES[int(meta['key'][5:7]) - 1]} {meta['key'][:4]}",
                "days": len(meta["days"]),
                "part": part,
                "daytypes": meta["daytypes"],
            }
        )
    shown = {m["key"] for m in months}
    rides = any(m["routes"] for m in metas if m["key"] in shown)

    index = {
        "generated": date.today().isoformat(),
        "timezone": TZ,
        "cell_size_m": CELL_SIZE_M,
        # Each cell is one heading bin of one square, so a figure is per
        # direction of travel and the viewer draws it as an arrow.
        "heading_bins": HEADING_BINS,
        "stop_radius_m": STOP_RADIUS_M,
        "terminal_radius_m": TERMINAL_RADIUS_M,
        "min_samples": MIN_SAMPLES,
        "metrics": _metric_descriptors(),
        "hist_bin_kmh": HIST_BIN_KMH,
        "free_flow_q": FREE_FLOW_Q,
        # Absent until segments.py has run, which is what the viewer keys the
        # whole ride-time feature off.
        "rides": rides
        and {
            "stop_pass_radius_m": STOP_PASS_RADIUS_M,
            "min_observations": SEG_MIN_OBS,
            "bin_s": SEG_BIN_S,
            "routes": max(m["routes"] for m in metas if m["key"] in shown),
            # Average first, and the default: only the mean is additive, so a
            # journey summed from per-leg medians comes out short. Measured on
            # Рясне-2 → Ковча at 08:00 over ten legs, summed medians said 22
            # minutes against 25-26 for the vehicles themselves.
            "metrics": [
                {"key": "avg", "label": "Average"},
                {"key": "med", "label": "Median"},
            ],
        },
        # Kept for viewers built before per-metric scales existed.
        "scale": {"low_kmh": SCALE_LOW_KMH, "high_kmh": SCALE_HIGH_KMH},
        "hours": sorted({h for m in metas for h in m["hours"]}),
        "days": {"count": len(days), "first": days[0], "last": last},
        "samples": sum(m["samples"] for m in metas if m["key"] in shown),
        "months": months,
        "daytypes": [
            {
                "key": key,
                "label": DAYTYPE_LABELS[key],
                "days": sum(m["daytypes"][key] for m in metas if m["key"] in shown),
            }
            for key in (ALL, *DAYTYPES)
        ],
    }
    _write("index", index)

    # Anything not owned by a built month is left over from an older layout —
    # the all-months rollups, the single paths.json — and would otherwise ship.
    built = {m["key"] for m in metas}
    stale = [
        p
        for p in WEB_DATA_DIR.glob("*.json")
        if p.name != "index.json" and month_of(p.name) not in built
    ]
    for path in stale:
        path.unlink()
    print(
        f"index: {', '.join(m['key'] + (f' ({m['part']})' if m['part'] else '') for m in months)}; "
        f"{len(days)} days ({days[0]} … {last}), {index['samples']:,} samples"
        + (f", {len(stale)} stale file(s) removed" if stale else "")
    )
    return index


def available_months() -> list[str]:
    """Every month with at least one day of aggregates on disk."""
    return sorted({p.stem[:7] for p in AGG_DIR.glob("*.parquet")})


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument(
        "--month",
        action="append",
        help="YYYY-MM to build (repeatable); default every month on disk",
    )
    ap.add_argument("--index", action="store_true", help="only rewrite index.json")
    ap.add_argument(
        "--no-index", action="store_true", help="build the months but leave index.json alone"
    )
    args = ap.parse_args(argv)

    if not args.index:
        months = args.month or available_months()
        if not months:
            raise SystemExit(f"no aggregates in {AGG_DIR} — run `make ingest` first")
        for month in months:
            build_month(month)
    if not args.no_index:
        write_index()
    return 0


if __name__ == "__main__":
    sys.exit(main())
