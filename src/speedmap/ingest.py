"""Read the archive once for both products: the speed map and the leg times.

A day's snapshots are ~245 MB and reading them is bound by download, not CPU,
so each snapshot is fetched once and fed to both passes — aggregate.py's speed
cells and segments.py's stop-to-stop legs — instead of each pass fetching the
whole archive on its own.

Resumable per pass: a day is fetched only if at least one pass still lacks its
output, and only that pass is fed.

Run:
    python -m speedmap.ingest --all
    python -m speedmap.ingest 2026-07-15
    python -m speedmap.ingest --all --force --only segments   # re-time legs only
"""

from __future__ import annotations

import argparse
import sys
import time

from . import aggregate, r2, segments
from .config import CELL_SIZE_M, JOBS, PATHS_FILE, SEG_BIN_S, STOP_PASS_RADIUS_M, WORKERS
from .days import fold_day, run_days

# name -> (module with is_done/save, per-day accumulator)
PASSES = {
    "speed": (aggregate, aggregate.SpeedDay),
    "segments": (segments, segments.SegmentDay),
}


def write_day(
    client,
    date_str: str,
    only: list[str] | None = None,
    force: bool = False,
    workers: int = WORKERS,
) -> bool:
    todo = [name for name in only or PASSES if force or not PASSES[name][0].is_done(date_str)]
    if not todo:
        print(f"{date_str}  skip (already ingested)", flush=True)
        return False

    started = time.monotonic()
    days = fold_day(client, date_str, [PASSES[name][1] for name in todo], workers)
    wrote = False
    for k, name in enumerate(todo):
        wrote |= PASSES[name][0].save(date_str, days[k] if days else None, started)
    return wrote


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("date", nargs="?", help="YYYY-MM-DD")
    ap.add_argument("--all", action="store_true", help="every day present in R2")
    ap.add_argument("--force", action="store_true", help="redo days already on disk")
    ap.add_argument("--only", choices=list(PASSES), help="run one pass instead of both")
    ap.add_argument("--workers", type=int, default=WORKERS, help="R2 fetch threads per day")
    ap.add_argument("--jobs", type=int, default=JOBS, help="days processed side by side")
    args = ap.parse_args(argv)

    client = r2.make_client()
    if args.all:
        dates = r2.raw_dates(client)
    elif args.date:
        dates = [args.date]
    else:
        ap.error("pass a date or --all")

    only = [args.only] if args.only else None
    print(
        f"cell={CELL_SIZE_M:g}m  {len(aggregate.depot_zones())} depot zone(s)  "
        f"pass radius={STOP_PASS_RADIUS_M:g}m  bin={SEG_BIN_S:g}s  {len(dates)} day(s)",
        flush=True,
    )
    failed = run_days(
        write_day, client, dates, args.jobs, only=only, force=args.force, workers=args.workers
    )
    if only is None or "segments" in only:
        # The newest schedule wins: it is the one the most recent legs were
        # timed against, and stop ids are stable even when trip ids are renumbered.
        routes = segments.write_paths(client, dates[-1])
        print(f"{routes} route-directions written to {PATHS_FILE.name}")
    return 1 if failed else 0


if __name__ == "__main__":
    sys.exit(main())
