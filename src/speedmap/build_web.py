"""Merge the per-day aggregates into the JSON the viewer loads.

Emits one file per (month, hour) selection, plus "all" rollups on both axes:

    web/data/2026-07-08.json     July, 08:00–08:59 local
    web/data/2026-07-all.json    July, whole day
    web/data/all-08.json         every month, 08:00–08:59
    web/data/all-all.json        everything
    web/data/index.json          menu contents + colour scale

Run: python -m speedmap.build_web
"""

from __future__ import annotations

import argparse
import json
import sys
from datetime import date

import pandas as pd

from .config import (
    AGG_DIR,
    CELL_SIZE_M,
    MIN_SAMPLES,
    SCALE_HIGH_KMH,
    SCALE_LOW_KMH,
    STOP_RADIUS_M,
    TZ,
    WEB_DATA_DIR,
)

MPS_TO_KMH = 3.6
ALL = "all"
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


def load_aggregates() -> tuple[pd.DataFrame, list[str]]:
    paths = sorted(AGG_DIR.glob("*.parquet"))
    if not paths:
        raise SystemExit(f"no aggregates in {AGG_DIR} — run `make ingest-all` first")
    frames = []
    for path in paths:
        frame = pd.read_parquet(path)
        frame["day"] = path.stem
        frames.append(frame)
    return pd.concat(frames, ignore_index=True), [p.stem for p in paths]


def _payload(group: pd.DataFrame) -> dict:
    """Collapse one (month, hour) selection to parallel arrays, ordered by
    sample count so the busiest cells are drawn last and stay on top."""
    cells = (
        group.groupby(["cx", "cy"], sort=False)[["n", "sum_speed", "sum_lat", "sum_lon"]]
        .sum()
        .reset_index()
    )
    cells = cells[cells["n"] >= MIN_SAMPLES].sort_values("n")
    if cells.empty:
        return {"lat": [], "lon": [], "v": [], "n": []}

    n = cells["n"].to_numpy()
    return {
        "lat": (cells["sum_lat"].to_numpy() / n).round(5).tolist(),
        "lon": (cells["sum_lon"].to_numpy() / n).round(5).tolist(),
        "v": (cells["sum_speed"].to_numpy() / n * MPS_TO_KMH).round(1).tolist(),
        "n": n.astype(int).tolist(),
    }


def _write(name: str, obj) -> int:
    path = WEB_DATA_DIR / f"{name}.json"
    text = json.dumps(obj, separators=(",", ":"))
    path.write_text(text, encoding="utf-8")
    return len(text)


def build() -> None:
    df, days = load_aggregates()
    WEB_DATA_DIR.mkdir(parents=True, exist_ok=True)

    months = sorted(df["month"].unique())
    hours = sorted(int(h) for h in df["hour"].unique())
    days_per_month = df.groupby("month")["day"].nunique().to_dict()

    total_bytes = 0
    file_count = 0
    for month in [*months, ALL]:
        month_df = df if month == ALL else df[df["month"] == month]
        for hour in [*hours, ALL]:
            hour_df = month_df if hour == ALL else month_df[month_df["hour"] == hour]
            hour_key = ALL if hour == ALL else f"{hour:02d}"
            total_bytes += _write(f"{month}-{hour_key}", _payload(hour_df))
            file_count += 1

    index = {
        "generated": date.today().isoformat(),
        "timezone": TZ,
        "cell_size_m": CELL_SIZE_M,
        "stop_radius_m": STOP_RADIUS_M,
        "min_samples": MIN_SAMPLES,
        "scale": {"low_kmh": SCALE_LOW_KMH, "high_kmh": SCALE_HIGH_KMH},
        "hours": hours,
        "days": {"count": len(days), "first": days[0], "last": days[-1]},
        "samples": int(df["n"].sum()),
        "months": [
            {
                "key": month,
                "label": f"{MONTH_NAMES[int(month[5:7]) - 1]} {month[:4]}",
                "days": int(days_per_month.get(month, 0)),
            }
            for month in months
        ],
    }
    _write("index", index)

    print(
        f"{file_count} data files, {total_bytes / 1e6:.1f} MB total\n"
        f"{len(days)} days ({days[0]} … {days[-1]}), "
        f"{int(df['n'].sum()):,} samples, months: {', '.join(months)}"
    )


def main(argv: list[str] | None = None) -> int:
    argparse.ArgumentParser(description=__doc__).parse_args(argv)
    build()
    return 0


if __name__ == "__main__":
    sys.exit(main())
