"""Merge the per-day leg times into the JSON the viewer loads.

Each month is built on its own, from its own days only, with "all" rollups on
the day-type and hour axes:

    web/data/rides-2026-07-wd-08.json observed leg times per route, July weekdays 08:00
    web/data/paths-2026-07.json       July's route paths and stop geometry
    web/data/month-2026-07.json       what the index needs to know about July

and then, from the month-*.json summaries alone:

    web/data/index.json               menu contents

The viewer pools the routes over each stop pair and compares every hour with
that leg's quickest one, so the map shows delay, not distance.

There is no all-months view. A month is the unit people compare.

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

from .lanes import rank_corridors
from .config import (
    SEG_BIN_S,
    SEG_DIR,
    SEG_HIST_DIR,
    SEG_MIN_OBS,
    STOP_PASS_RADIUS_M,
    TZ,
    WEB_DATA_DIR,
    paths_file,
)

ALL = "all"

# Weekdays against weekends: the rush hours are a weekday thing, and splitting
# Saturday from Sunday would only thin the rides behind each leg.
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


def _percentiles(
    bins: pd.DataFrame,
    quantiles: tuple[float, ...],
    keys: list[str],
    width: float = SEG_BIN_S,
) -> dict[float, pd.Series]:
    """Several percentiles per group, read off the cumulative histogram.

    Resolution is the bin width, so each answer is the centre of the bin the
    q-th sample falls in. The sort and the cumulative sum are the expensive part
    and do not depend on q, so every quantile shares one pass — at three
    percentiles across 300 payloads, doing it per quantile is three times the
    work for the same result.

    """
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
    keys: list[str],
    width: float = SEG_BIN_S,
) -> pd.Series:
    return _percentiles(bins, (q,), keys=keys, width=width)[q]


SEG_KEYS = ["route_id", "direction", "from_stop", "to_stop"]
SEG_SUMS = ["n", "sum_s"]


def load_ride_slices(month: str) -> tuple[dict, dict, list[dict], dict]:
    """One month's observed leg times, bucketed into the same (month, daytype) slices.

    Returns empty structures when segments.py has not run.
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
    """Write one month's files. Every month stands on its own, so a finished
    month is built once and each month can run on its own machine."""
    leg_slices, seg_bin_slices, paths, stops = load_ride_slices(month)
    if not leg_slices:
        raise SystemExit(f"no leg times for {month} in {SEG_DIR} — run `make ingest` first")
    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)
    written: set[str] = set()

    days = sorted({p.stem for p in _month_paths(SEG_DIR, month) if p.stem.startswith(month)})
    hours = sorted({int(h) for frame in leg_slices.values() for h in frame["hour"].unique()})
    types: dict[str, int] = {}
    for day in days:
        types[daytype_of(day)] = types.get(daytype_of(day), 0) + 1

    total_bytes = 0

    def write(name: str, obj) -> None:
        nonlocal total_bytes
        total_bytes += _write(name, obj)
        written.add(f"{name}.json")

    weekday_rides: dict[int, dict] = {}
    for daytype in [*DAYTYPES, ALL]:
        legs = _combine_rides(leg_slices, month, daytype, ["hour", *SEG_KEYS])
        seg_bins = _combine_rides(seg_bin_slices, month, daytype, ["hour", *SEG_KEYS, "bin"])
        for hour in [*hours, ALL]:
            if hour == ALL:
                part, part_bins = legs, seg_bins
            else:
                part = legs[legs["hour"] == hour]
                part_bins = seg_bins[seg_bins["hour"] == hour]
            hour_key = ALL if hour == ALL else f"{hour:02d}"
            rides = _rides(part, part_bins, paths)
            write(f"rides-{month}-{daytype}-{hour_key}", rides)
            if daytype == "wd" and hour != ALL:
                weekday_rides[hour] = rides
        del legs, seg_bins

    # Geometry is the same whatever day type or hour is on screen, so it ships
    # once per month and the selection files carry only numbers.
    write(f"paths-{month}", {"stops": stops, "routes": paths})
    write(
        f"lanes-{month}",
        rank_corridors(paths, stops, weekday_rides, types.get("wd", 0))
        if types.get("wd")
        else [],
    )

    meta = {
        "key": month,
        "days": days,
        "daytypes": {ALL: len(days), **{key: types.get(key, 0) for key in DAYTYPES}},
        "hours": hours,
        # Observed stop-to-stop legs: one per vehicle per pair of stops.
        "samples": int(sum(int(f["n"].sum()) for f in leg_slices.values())),
        "routes": len(paths),
    }
    write(f"month-{month}", meta)

    stale = [
        p for p in WEB_DATA_DIR.glob("*.json") if month_of(p.name) == month and p.name not in written
    ]
    for path in stale:
        path.unlink()
    print(
        f"{month}: {len(written)} files, {total_bytes / 1e6:.1f} MB, {len(days)} days, "
        f"{meta['samples']:,} legs"
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

    index = {
        "generated": date.today().isoformat(),
        "timezone": TZ,
        "rides": {
            "stop_pass_radius_m": STOP_PASS_RADIUS_M,
            "min_observations": SEG_MIN_OBS,
            "bin_s": SEG_BIN_S,
            "routes": max(m["routes"] for m in metas if m["key"] in shown),
            # Median first, for the map: a leg's typical time, not dragged up by
            # the odd bus stuck at a light. Only the mean is additive, though, so
            # a journey summed from per-leg medians comes out short. Measured on
            # Рясне-2 → Ковча at 08:00 over ten legs, summed medians said 22
            # minutes against 25-26 for the vehicles themselves.
            "metrics": [
                {"key": "med", "label": "Median"},
                {"key": "avg", "label": "Average"},
            ],
        },
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
        f"{len(days)} days ({days[0]} … {last}), {index['samples']:,} legs"
        + (f", {len(stale)} stale file(s) removed" if stale else "")
    )
    return index


def available_months() -> list[str]:
    """Every month with at least one day of leg times on disk."""
    return sorted({p.stem[:7] for p in SEG_DIR.glob("*.parquet")})


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
            raise SystemExit(f"no leg times in {SEG_DIR} — run `make ingest` first")
        for month in months:
            build_month(month)
    if not args.no_index:
        write_index()
    return 0


if __name__ == "__main__":
    sys.exit(main())
