"""What the two archive passes share: writing a file safely, and running days side by side.

Every day is its own pair of output files, so days are independent and a pass
over the archive can run several at once, each in its own process so parsing
and accumulating do not queue on one GIL.
"""

from __future__ import annotations

import os
import traceback
from collections.abc import Callable
from concurrent.futures import ProcessPoolExecutor, as_completed
from pathlib import Path

from . import r2


def write_atomic(path: Path, write: Callable[[Path], object]) -> None:
    """Write through a temp file beside `path` and rename it into place.

    The passes skip any day whose output exists, so a half-written file left by
    an interrupted run would be trusted forever; and two processes building the
    same cache must never read each other's partial bytes. The pid keeps two
    writers of one path off the same temp file.
    """
    temp = path.with_name(f".{path.name}.{os.getpid()}.part")
    try:
        write(temp)
        temp.replace(path)
    finally:
        temp.unlink(missing_ok=True)


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
