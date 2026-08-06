"""Keep the derived parquets in R2, so a fresh checkout does not re-ingest 88 days.

`data/` is git-ignored, which is right — it is 300-odd MB of derived output. But
it means a CI runner starts with nothing and would spend two hours re-reading
every snapshot in the bucket to rebuild what it already had. The aggregates go
back to the same bucket instead:

    derived/agg/YYYY-MM-DD.parquet     per-day sums
    derived/hist/YYYY-MM-DD.parquet    per-day speed histograms

Both are immutable once written: a day's snapshots never change, so a key that
exists remotely holds exactly what the same-named local file would. That makes
both directions a name comparison and nothing more.

Run:
    python -m speedmap.sync pull
    python -m speedmap.sync push
"""

from __future__ import annotations

import argparse
import sys
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from . import r2
from .config import AGG_DIR, HIST_DIR, WORKERS

DERIVED_PREFIX = "derived/"
# Local directory ↔ remote prefix. Keep the names in step with config.py.
KINDS = {"agg": AGG_DIR, "hist": HIST_DIR}


def remote_keys(client, kind: str) -> set[str]:
    """Parquet filenames present under derived/<kind>/."""
    prefix = f"{DERIVED_PREFIX}{kind}/"
    names: set[str] = set()
    paginator = client.get_paginator("list_objects_v2")
    for page in paginator.paginate(Bucket=r2.bucket(), Prefix=prefix):
        for obj in page.get("Contents", []):
            name = obj["Key"][len(prefix) :]
            if name.endswith(".parquet"):
                names.add(name)
    return names


def local_names(directory: Path) -> set[str]:
    return {p.name for p in directory.glob("*.parquet")}


def pull(client, workers: int = WORKERS) -> int:
    """Download aggregates that are in R2 but not on disk."""
    fetched = 0
    for kind, directory in KINDS.items():
        directory.mkdir(parents=True, exist_ok=True)
        missing = sorted(remote_keys(client, kind) - local_names(directory))
        if not missing:
            print(f"{kind}: up to date")
            continue

        def download(name: str, kind=kind, directory=directory) -> None:
            body = r2.get_bytes(client, f"{DERIVED_PREFIX}{kind}/{name}")
            # Write beside the target and rename, so an interrupted run cannot
            # leave a half-file that the next one would happily skip.
            temp = directory / f".{name}.part"
            temp.write_bytes(body)
            temp.replace(directory / name)

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(download, missing))
        fetched += len(missing)
        print(f"{kind}: pulled {len(missing)} file(s)")
    return fetched


def push(client, workers: int = WORKERS) -> int:
    """Upload aggregates that are on disk but not in R2."""
    sent = 0
    for kind, directory in KINDS.items():
        if not directory.exists():
            continue
        missing = sorted(local_names(directory) - remote_keys(client, kind))
        if not missing:
            print(f"{kind}: nothing new")
            continue

        def upload(name: str, kind=kind, directory=directory) -> None:
            client.put_object(
                Bucket=r2.bucket(),
                Key=f"{DERIVED_PREFIX}{kind}/{name}",
                Body=(directory / name).read_bytes(),
                ContentType="application/vnd.apache.parquet",
            )

        with ThreadPoolExecutor(max_workers=workers) as pool:
            list(pool.map(upload, missing))
        sent += len(missing)
        print(f"{kind}: pushed {len(missing)} file(s)")
    return sent


def main(argv: list[str] | None = None) -> int:
    ap = argparse.ArgumentParser(description=__doc__)
    ap.add_argument("action", choices=("pull", "push"))
    ap.add_argument("--workers", type=int, default=WORKERS)
    args = ap.parse_args(argv)

    client = r2.make_client()
    (pull if args.action == "pull" else push)(client, workers=args.workers)
    return 0


if __name__ == "__main__":
    sys.exit(main())
