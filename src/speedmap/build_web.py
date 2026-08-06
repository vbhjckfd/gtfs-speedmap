"""Merge the per-day aggregates into the JSON the viewer loads.

Emits one file per (month, daytype, hour) selection, with "all" rollups on
every axis:

    web/data/2026-07-wd-08.json       July weekdays, 08:00–08:59 local
    web/data/2026-07-all-all.json     July, every day, whole day
    web/data/all-we-17.json           every month, weekends, 17:00–17:59
    web/data/all-all-all.json         everything
    web/data/profile-all-wd.json      per-cell speed by hour, for the popup
    web/data/index.json               menu contents, metrics, colour scales

Run: python -m speedmap.build_web
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

import numpy as np
import pandas as pd

from .depots import load_sites
from .utm import project_xy
from .config import (
    AGG_DIR,
    CELL_SIZE_M,
    FREE_FLOW_Q,
    HIST_BIN_KMH,
    HIST_DIR,
    MIN_SAMPLES,
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

CELL_KEYS = ["cx", "cy"]
CELL_SUMS = ["n", "sum_speed", "sum_lat", "sum_lon"]

# Each metric needs its own colour domain and formatting: km/h, a ratio and a
# spread cannot share one scale. `invert` flips the ramp for metrics where the
# high end is the bad end.
METRICS = [
    {"key": "v", "label": "Average", "unit": "km/h"},
    {"key": "med", "label": "Median", "unit": "km/h"},
    {"key": "p15", "label": "Slow day (p15)", "unit": "km/h"},
    # Not called "free-flow": that name belongs to the global reference behind
    # `rel`, and this one is the p85 of the selection on screen.
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


def load_base_slices() -> tuple[dict, dict, list[str]]:
    """Read every per-day aggregate once, bucketed into (month, daytype) slices.

    Only the 8-ish base slices are held; every rollup the viewer offers is a sum
    of these. Grouping each bucket down as it is finished keeps peak memory near
    what a single full load costs today rather than multiplying it by the new
    axis.
    """
    paths = sorted(AGG_DIR.glob("*.parquet"))
    if not paths:
        raise SystemExit(f"no aggregates in {AGG_DIR} — run `make ingest-all` first")

    cell_parts: dict[tuple[str, str], list[pd.DataFrame]] = {}
    hist_parts: dict[tuple[str, str], list[pd.DataFrame]] = {}
    days = [p.stem for p in paths]

    for path in paths:
        day = path.stem
        cells = pd.read_parquet(path)
        hist_path = HIST_DIR / path.name
        hist = pd.read_parquet(hist_path) if hist_path.exists() else None
        # A day file is almost entirely one month, but the local-time
        # conversion can push a few rows over a month boundary, so split on the
        # column rather than assuming.
        for month, part in cells.groupby("month", sort=False):
            cell_parts.setdefault(_slice_key(day, month), []).append(part)
        if hist is not None:
            for month, part in hist.groupby("month", sort=False):
                hist_parts.setdefault(_slice_key(day, month), []).append(part)

    cell_slices = {
        key: pd.concat(parts, ignore_index=True)
        .groupby(["hour", *CELL_KEYS], as_index=False, sort=False)[CELL_SUMS]
        .sum()
        for key, parts in cell_parts.items()
    }
    hist_slices = {
        key: pd.concat(parts, ignore_index=True)
        .groupby(["hour", *CELL_KEYS, "bin"], as_index=False, sort=False)["n"]
        .sum()
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


def _percentiles(bins: pd.DataFrame, quantiles: tuple[float, ...]) -> dict[float, pd.Series]:
    """Several percentile speeds per (cx, cy), read off the cumulative histogram.

    Resolution is the bin width, so each answer is the centre of the bin the
    q-th sample falls in. The sort and the cumulative sum are the expensive part
    and do not depend on q, so every quantile shares one pass — at three
    percentiles across 300 payloads, doing it per quantile is three times the
    work for the same result.
    """
    if bins.empty:
        return {q: pd.Series(dtype=float) for q in quantiles}

    cells = bins.groupby(CELL_KEYS, sort=False)["n"].sum().rename("total")
    ordered = bins.sort_values(CELL_KEYS + ["bin"])
    ordered["cumulative"] = ordered.groupby(CELL_KEYS, sort=False)["n"].cumsum()
    ordered = ordered.join(cells, on=CELL_KEYS)

    out = {}
    for q in quantiles:
        # The q-th sample is the ceil(total*q)-th, at least the first; the bin
        # whose running total first reaches it is the bin holding it. At q=0.5
        # this is (total+1)//2, the lower of the two middles on an even count.
        target = np.maximum(1, np.ceil(ordered["total"] * q))
        reached = ordered[ordered["cumulative"] >= target]
        first = reached.groupby(CELL_KEYS, sort=False)["bin"].first()
        out[q] = (first + 0.5) * HIST_BIN_KMH
    return out


def _percentile(bins: pd.DataFrame, q: float) -> pd.Series:
    return _percentiles(bins, (q,))[q]


def _medians(bins: pd.DataFrame) -> pd.Series:
    return _percentile(bins, 0.5)


def free_flow(hist_slices: dict) -> pd.Series:
    """Each cell's free-flow reference: its p85 over every hour and day type.

    Collapsing hour and bin before summing keeps this far smaller than the
    all-months slice it is derived from.
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
    """Precompute the masked cell ids from every cell the build will ever emit."""
    global _MASKED_IDS
    universe = (
        pd.concat(cell_slices.values(), ignore_index=True)
        .groupby(CELL_KEYS, as_index=False, sort=False)[CELL_SUMS]
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


EMPTY_PAYLOAD = {k: [] for k in ("lat", "lon", "v", "med", "p15", "p85", "spread", "rel", "n")}


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
        return {"lat": [], "lon": [], "hours": hours, "q": [], "no_data": NO_DATA}

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
        "hours": hours,
        "q": grid.astype(int).ravel().tolist(),
        "no_data": NO_DATA,
    }


_WRITTEN: set[str] = set()


def _write(name: str, obj) -> int:
    path = WEB_DATA_DIR / f"{name}.json"
    text = json.dumps(obj, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    _WRITTEN.add(path.name)
    return len(text)


def _sweep() -> int:
    """Delete payloads left by an earlier build under a different naming scheme.

    The deploy uploads whatever is in web/data, so stale files would ship —
    and a viewer that still had an old URL cached would silently get old data.
    """
    stale = [p for p in WEB_DATA_DIR.glob("*.json") if p.name not in _WRITTEN]
    for path in stale:
        path.unlink()
    return len(stale)


def build() -> None:
    cell_slices, hist_slices, days = load_base_slices()
    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)

    months = sorted({month for month, _ in cell_slices})
    hours = sorted({int(h) for frame in cell_slices.values() for h in frame["hour"].unique()})
    days_per_month: dict[str, int] = {}
    days_per_type: dict[str, int] = {ALL: len(days)}
    for day in days:
        days_per_month[day[:7]] = days_per_month.get(day[:7], 0) + 1
        kind = daytype_of(day)
        days_per_type[kind] = days_per_type.get(kind, 0) + 1

    masked = install_mask(cell_slices)
    ff = free_flow(hist_slices)
    print(
        f"{masked:,} cells masked by {len(mask_zones())} depot zone(s); "
        f"free-flow reference for {len(ff):,} cells (p{FREE_FLOW_Q * 100:.0f})"
    )

    total_bytes = 0
    file_count = 0
    for month in [*months, ALL]:
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
                total_bytes += _write(
                    f"{month}-{daytype}-{hour_key}", _payload(part, part_bins, ff)
                )
                file_count += 1
            total_bytes += _write(f"profile-{month}-{daytype}", _profile(selection, hours))
            file_count += 1
            del selection, bins

    index = {
        "generated": date.today().isoformat(),
        "timezone": TZ,
        "cell_size_m": CELL_SIZE_M,
        "stop_radius_m": STOP_RADIUS_M,
        "terminal_radius_m": TERMINAL_RADIUS_M,
        "min_samples": MIN_SAMPLES,
        "metrics": _metric_descriptors(),
        "hist_bin_kmh": HIST_BIN_KMH,
        "free_flow_q": FREE_FLOW_Q,
        # Kept for viewers built before per-metric scales existed.
        "scale": {"low_kmh": SCALE_LOW_KMH, "high_kmh": SCALE_HIGH_KMH},
        "hours": hours,
        "days": {"count": len(days), "first": days[0], "last": days[-1]},
        "samples": int(sum(int(f["n"].sum()) for f in cell_slices.values())),
        "months": [
            {
                "key": month,
                "label": f"{MONTH_NAMES[int(month[5:7]) - 1]} {month[:4]}",
                "days": days_per_month.get(month, 0),
            }
            for month in months
        ],
        "daytypes": [
            {"key": key, "label": DAYTYPE_LABELS[key], "days": days_per_type.get(key, 0)}
            for key in (ALL, *DAYTYPES)
        ],
    }
    _write("index", index)
    swept = _sweep()

    print(
        f"{file_count} data files, {total_bytes / 1e6:.1f} MB total"
        + (f", {swept} stale file(s) removed" if swept else "")
        + "\n"
        f"{len(days)} days ({days[0]} … {days[-1]}), "
        f"{index['samples']:,} samples, months: {', '.join(months)}, "
        f"{days_per_type.get('wd', 0)} weekdays / {days_per_type.get('we', 0)} weekend days"
    )


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
