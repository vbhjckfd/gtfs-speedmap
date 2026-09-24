"""What the archive passes share: one download of a day, and days side by side.

Every day is its own pair of output files per pass, so days are independent and
a run over the archive can take several at once, each in its own process so
parsing and accumulating do not queue on one GIL.

A day's snapshots are ~245 MB and the passes are bound by download, not CPU, so
`fold_day` streams each snapshot once into every pass that still needs the day.
"""

from __future__ import annotations

import time
import traceback
from collections.abc import Callable, Sequence
from concurrent.futures import FIRST_COMPLETED, ProcessPoolExecutor, ThreadPoolExecutor, wait
from typing import Protocol

from . import r2
from .config import DAY_RETRIES, FAIL_STREAK_MAX, RETRY_BASE_S
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


def _attempt(write_day: Callable, client, date_str: str, kwargs: dict) -> None:
    """Run one day, retrying with backoff.

    The failures worth retrying are the network's: a Wi-Fi blip, or the minute
    after a wake from sleep before the link is back. The backoff (30 s, 60 s,
    120 s by default) outlasts both; a day that still fails is a real problem.
    """
    for attempt in range(DAY_RETRIES + 1):
        try:
            write_day(client, date_str, **kwargs)
            return
        except Exception as error:
            if attempt == DAY_RETRIES:
                raise
            wait = RETRY_BASE_S * 2**attempt
            print(
                f"{date_str}  {type(error).__name__}, retry {attempt + 1}/{DAY_RETRIES} "
                f"in {wait:g}s",
                flush=True,
            )
            time.sleep(wait)


def _run_day(write_day: Callable, date_str: str, kwargs: dict) -> None:
    _attempt(write_day, _client, date_str, kwargs)


class _Tally:
    """Failures overall and in a row; a success breaks the streak."""

    def __init__(self) -> None:
        self.failed = 0
        self.streak = 0

    def ok(self) -> None:
        self.streak = 0

    def fail(self, date_str: str) -> bool:
        """Report a failed day; True when the streak says to stop."""
        self.failed += 1
        self.streak += 1
        print(f"{date_str}  FAILED", flush=True)
        traceback.print_exc()
        return self.streak >= FAIL_STREAK_MAX


def _stopped(tally: _Tally, not_started: int, in_flight: int = 0) -> None:
    waiting = f", waiting for {in_flight} in flight" if in_flight else ""
    print(
        f"stopping: {tally.streak} days in a row failed after retries — the network or R2 is "
        f"down, not one bad day. {not_started} not started{waiting}",
        flush=True,
    )


def run_days(write_day: Callable, client, dates: list[str], jobs: int, **kwargs) -> int:
    """Call `write_day(client, date, **kwargs)` for every date; return the failure count.

    A day that still raises after its retries is reported and the rest carry on
    — with several days in flight, stopping at the first failure would throw
    away the others' work. But FAIL_STREAK_MAX failures in a row mean the cause
    is not the day, and running on would only fail every remaining one.
    """
    tally = _Tally()
    if jobs <= 1 or len(dates) <= 1:
        for k, date_str in enumerate(dates):
            try:
                _attempt(write_day, client, date_str, kwargs)
                tally.ok()
            except Exception:
                if tally.fail(date_str):
                    _stopped(tally, len(dates) - k - 1)
                    break
        return tally.failed

    # Fed `jobs` at a time rather than all submitted up front: the pool hands
    # queued work to its workers early, so once the streak trips, already-queued
    # days would still run — each through its full retries — and go unreported.
    queue = iter(dates)
    running: dict = {}
    stopping = False
    with ProcessPoolExecutor(max_workers=jobs, initializer=_init_worker) as pool:

        def feed() -> None:
            while not stopping and len(running) < jobs:
                date_str = next(queue, None)
                if date_str is None:
                    return
                running[pool.submit(_run_day, write_day, date_str, kwargs)] = date_str

        feed()
        while running:
            done, _ = wait(running, return_when=FIRST_COMPLETED)
            for future in done:
                date_str = running.pop(future)
                try:
                    future.result()
                    tally.ok()
                except Exception:
                    if tally.fail(date_str) and not stopping:
                        stopping = True
                        _stopped(tally, sum(1 for _ in queue), len(running))
            feed()
    return tally.failed
