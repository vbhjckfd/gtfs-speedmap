"""What the archive passes share: one download of a day, and days side by side.

Every day is its own pair of output files per pass, so days are independent and
a run over the archive can take several at once, each in its own process so
parsing and accumulating do not queue on one GIL.

A day's snapshots are ~245 MB and the passes are bound by download, not CPU, so
`fold_day` streams each snapshot once into every pass that still needs the day.
"""

from __future__ import annotations

import traceback
from collections.abc import Callable, Sequence
from concurrent.futures import ProcessPoolExecutor, ThreadPoolExecutor, as_completed
from typing import Protocol

from . import r2
from .snapshots import VehicleRow, parse_feed
from .static_feed import StaticFeed, load_for_date


class DayPass(Protocol):
    """One pass's accumulators for one day, fed a snapshot at a time."""

    stats: dict

    def add(self, rows: list[VehicleRow]) -> None: ...


def fold_day(
    client,
    date_str: str,
    passes: Sequence[Callable[[StaticFeed], DayPass]],
    workers: int,
) -> list[DayPass] | None:
    """Download a day's snapshots once and feed every row to each pass.

    Returns the filled passes, or None when R2 holds no snapshots for the day.
    """
    feed = load_for_date(client, date_str)
    keys = r2.snapshot_keys(client, date_str)
    if not keys:
        return None
    days = [make(feed) for make in passes]
    errors = 0

    def fetch(key: str) -> list[VehicleRow]:
        nonlocal errors
        try:
            return parse_feed(r2.get_bytes(client, key))
        except Exception:
            # A single unreadable object must not sink a whole day.
            errors += 1
            return []

    with ThreadPoolExecutor(max_workers=workers) as pool:
        for rows in pool.map(fetch, keys):
            for day in days:
                day.add(rows)

    for day in days:
        day.stats["snapshots"] = len(keys)
        day.stats["snapshot_errors"] = errors
    return days


# One R2 client per worker process, made once: boto3 clients do not cross
# process boundaries, and making one per day would redo the handshake each time.
_client = None


def _init_worker() -> None:
    global _client
    _client = r2.make_client()


def _run_day(write_day: Callable, date_str: str, kwargs: dict) -> None:
    write_day(_client, date_str, **kwargs)


def run_days(write_day: Callable, client, dates: list[str], jobs: int, **kwargs) -> int:
    """Call `write_day(client, date, **kwargs)` for every date; return the failure count.

    A day that raises is reported and the rest carry on — with several days in
    flight, stopping at the first failure would throw away the others' work.
    """
    if jobs <= 1 or len(dates) <= 1:
        for date_str in dates:
            write_day(client, date_str, **kwargs)
        return 0

    failed = 0
    with ProcessPoolExecutor(max_workers=jobs, initializer=_init_worker) as pool:
        futures = {pool.submit(_run_day, write_day, d, kwargs): d for d in dates}
        for future in as_completed(futures):
            try:
                future.result()
            except Exception:
                failed += 1
                print(f"{futures[future]}  FAILED", flush=True)
                traceback.print_exc()
    return failed
