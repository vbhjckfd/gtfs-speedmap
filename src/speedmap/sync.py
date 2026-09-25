"""Keep the derived data in R2, so a fresh checkout does not re-ingest the archive.

`data/` is git-ignored, which is right — it is several hundred MB of derived
output. But it means a CI runner starts with nothing and would spend hours
re-reading every snapshot in the bucket to rebuild what it already had. The
aggregates go back to the same bucket instead:

    derived/agg/YYYY-MM-DD.parquet       per-day sums
    derived/hist/YYYY-MM-DD.parquet      per-day speed histograms
    derived/seg/YYYY-MM-DD.parquet       per-day stop-to-stop leg times
    derived/seghist/YYYY-MM-DD.parquet   per-day leg-time histograms
    derived/paths/YYYY-MM.json           each month's route geometry
    derived/depots.json                  the depot zones the days were built with
    derived/web/YYYY-MM/*.json           each month's built map files

The parquets are immutable once written: a day's snapshots never change, so a
key that exists remotely holds exactly what the same-named local file would.
That makes both directions a name comparison and nothing more.

The JSON files are not: the running month's paths move with its timetable, and
depots.json can be edited by hand. They are small, so they are pushed whole
every time and pulled only when missing locally, which never clobbers a local
edit. depots.json matters more than its size: without it every depot yard
counts as street, so a runner that lacked it would build a different map.

The built map files are kept per month so a finished month is built once: the
deploy gathers every month from R2 and only the months that changed are
rebuilt.

Run:
    python -m speedmap.sync pull [--month 2026-07]
    python -m speedmap.sync push
    python -m speedmap.sync push-web --month 2026-07
    python -m speedmap.sync pull-web
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import r2
from .build_web import month_of, month_window
from .config import (
    AGG_DIR,
    DEPOT_FILE,
    HIST_DIR,
    PATHS_DIR,
    SEG_DIR,
    SEG_HIST_DIR,
    WEB_DATA_DIR,
    WORKERS,
)

DERIVED_PREFIX = "derived/"
WEB_PREFIX = f"{DERIVED_PREFIX}web/"
# Local directory ↔ remote prefix. Keep the names in step with config.py.
KINDS = {"agg": AGG_DIR, "hist": HIST_DIR, "seg": SEG_DIR, "seghist": SEG_HIST_DIR}


def remote_names(client, prefix: str, suffix: str) -> set[str]:
    """Names directly under `prefix` ending in `suffix`."""
    names: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=r2.bucket(), Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"][len(prefix) :]
            if name.endswith(suffix) and "/" not in name:
                names.add(name)
    return names


def remote_keys(client, kind: str) -> set[str]:
    """Parquet filenames present under derived/<kind>/."""
    return remote_names(client, f"{DERIVED_PREFIX}{kind}/", ".parquet")


def local_names(directory: Path) -> set[str]:
    return {p.name for p in directory.glob("*.parquet")}


def _download(client, key: str, target: Path) -> None:
    body = r2.get_bytes(client, key)
    # Write beside the target and rename, so an interrupted run cannot leave a
    # half-file that the next one would happily skip.
    target.parent.mkdir(parents=True, exist_ok=True)
    temp = target.with_name(f".{target.name}.part")
    temp.write_bytes(body)
    temp.replace(target)


def _upload(client, key: str, source: Path) -> None:
    kind = "application/json" if source.suffix == ".json" else "application/vnd.apache.parquet"
    client.put_object(Bucket=r2.bucket(), Key=key, Body=source.read_bytes(), ContentType=kind)


def _in_window(name: str, month: str | None) -> bool:
    if month is None:
        return True
    first, last = month_window(month)
    return first <= name[:10] <= last


def pull(client, workers: int = WORKERS, month: str | None = None) -> int:
    """Download what is in R2 but not on disk; with `month`, only what it needs."""
    fetched = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for kind, directory in KINDS.items():
            missing = sorted(
                name
                for name in remote_keys(client, kind) - local_names(directory)
                if _in_window(name, month)
            )
            list(
                pool.map(
                    lambda name, kind=kind, directory=directory: _download(
                        client, f"{DERIVED_PREFIX}{kind}/{name}", directory / name
                    ),
                    missing,
                )
            )
            fetched += len(missing)
            print(f"{kind}: pulled {len(missing)} file(s)" if missing else f"{kind}: up to date")

    wanted = [(f"{DERIVED_PREFIX}{DEPOT_FILE.name}", DEPOT_FILE)] + [
        (f"{DERIVED_PREFIX}paths/{name}", PATHS_DIR / name)
        for name in sorted(remote_names(client, f"{DERIVED_PREFIX}paths/", ".json"))
        if month is None or name == f"{month}.json"
    ]
    for key, path in wanted:
        if path.exists():
            continue
        try:
            _download(client, key, path)
        except client.exceptions.NoSuchKey:
            print(f"{path.name}: not in R2")
            continue
        fetched += 1
        print(f"{key[len(DERIVED_PREFIX):]}: pulled")
    return fetched


def push(client, workers: int = WORKERS) -> int:
    """Upload the day files R2 lacks, and every small JSON file whole."""
    sent = 0
    with ThreadPoolExecutor(max_workers=workers) as pool:
        for kind, directory in KINDS.items():
            if not directory.exists():
                continue
            missing = sorted(local_names(directory) - remote_keys(client, kind))
            list(
                pool.map(
                    lambda name, kind=kind, directory=directory: _upload(
                        client, f"{DERIVED_PREFIX}{kind}/{name}", directory / name
                    ),
                    missing,
                )
            )
            sent += len(missing)
            print(f"{kind}: pushed {len(missing)} file(s)" if missing else f"{kind}: nothing new")

    small = [(f"{DERIVED_PREFIX}{DEPOT_FILE.name}", DEPOT_FILE)] + [
        (f"{DERIVED_PREFIX}paths/{p.name}", p) for p in sorted(PATHS_DIR.glob("*.json"))
    ]
    for key, path in small:
        if path.exists():
            _upload(client, key, path)
            sent += 1
    print(f"json: pushed {sum(1 for _, p in small if p.exists())} file(s)")
    return sent


def push_web(client, month: str, workers: int = WORKERS) -> int:
    """Replace a month's built files in R2 with the ones in web/data."""
    prefix = f"{WEB_PREFIX}{month}/"
    files = sorted(p for p in WEB_DATA_DIR.glob("*.json") if month_of(p.name) == month)
    if not files:
        raise SystemExit(f"nothing built for {month} in {WEB_DATA_DIR}")
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda p: _upload(client, f"{prefix}{p.name}", p), files))
    # Upload first, then drop what the new build no longer writes, so a
    # failure half-way leaves the month whole rather than missing files.
    stale = remote_names(client, prefix, ".json") - {p.name for p in files}
    for name in stale:
        client.delete_object(Bucket=r2.bucket(), Key=f"{prefix}{name}")
    print(f"web/{month}: pushed {len(files)} file(s), removed {len(stale)}")
    return len(files)


def pull_web(client, workers: int = WORKERS) -> int:
    """Download every month's built files into web/data."""
    keys: list[str] = []
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=r2.bucket(), Prefix=WEB_PREFIX):
        keys += [obj["Key"] for obj in page.get("Contents", []) if obj["Key"].endswith(".json")]
    with ThreadPoolExecutor(max_workers=workers) as pool:
        list(pool.map(lambda k: _download(client, k, WEB_DATA_DIR / k.rsplit("/", 1)[1]), keys))
    months = sorted({k[len(WEB_PREFIX) :].split("/")[0] for k in keys})
    print(f"web: pulled {len(keys)} file(s) for {', '.join(months) or 'no months'}")
    return len(keys)


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=("pull", "push", "push-web", "pull-web"))
    ap.add_argument("--month", help="YYYY-MM: pull only what that month needs; push-web needs it")
    ap.add_argument("--workers", type=int, default=WORKERS)
    args = ap.parse_args(argv)

    client = r2.make_client()
    if args.action == "pull":
        pull(client, workers=args.workers, month=args.month)
    elif args.action == "push":
        push(client, workers=args.workers)
    elif args.action == "push-web":
        if not args.month:
            ap.error("push-web needs --month")
        push_web(client, args.month, workers=args.workers)
    else:
        pull_web(client, workers=args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
