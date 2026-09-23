"""Writing outputs so an interrupted run never leaves a file that looks finished."""

from __future__ import annotations

import os
from collections.abc import Callable
from pathlib import Path


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
